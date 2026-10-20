#!/usr/bin/env python3
"""Before you publish a port, a mod or its notes: is any of the cartridge in there?

    python tools/romscan.py Karateka.car                   # the git repository here
    python tools/romscan.py game.a78 src/ notes/ --min 12  # these folders
    python tools/romscan.py Karateka.car --trace -- port/build7800.py --car Karateka.car

A repository that says "this contains no game code or data" should be able to show it. This
looks for any run of MIN (default 16) consecutive bytes of the cartridge, in order, in:

  - binary files, byte for byte (an image, a stray .bin, a patch's literal blocks);
  - text files, as the byte literals a person or a tool would write them: `$A9`, `0xA9`,
    `A9h`, bare two-digit hex (`a9 00 8d 02 d0`, as a hex dump or a disassembly's byte
    column has them) and long hex strings (`a9008d02d0`). A run may span lines, so a data
    block written eight bytes to a line is found whole. Anything else on a line (labels,
    mnemonics, comments) is skipped over; a line with no byte literal ends the run.

Each find gives the file, the line (or the offset), the length and where in the cartridge
the bytes come from. The exit status is 1 if anything was found.

Sixteen bytes in the cartridge's own order do not happen by accident, but short runs of
common values do: a table of 00s or FFs, a run of opcodes your own code also uses. Runs of
one repeated value are skipped. Lower --min to look harder, and read what it reports.

What this does not see: decimal byte lists (`.byte 169, 0`), data compressed or encoded
some other way, graphics converted to another format, and four-digit words (`$D002`: they
are almost always addresses). It answers "did a dump get pasted in", not "could the game be
rebuilt from this".

`--trace -- SCRIPT [ARGS]` runs a Python build script under an audit hook and lists every
file it opened outside the scanned tree, read or written: the check that a build takes
nothing but the cartridge you give it (and no copy of the game left beside the repository).
"""
import argparse
import io
import os
import re
import subprocess
import sys
import tempfile

TEXT_EXT = {".asm", ".s", ".inc", ".a", ".py", ".lua", ".txt", ".md", ".json", ".cfg", ".ini",
            ".c", ".h", ".cpp", ".hpp", ".sh", ".bat", ".cmd", ".xml", ".html", ".htm", ".csv",
            ".yml", ".yaml", ".toml", ".patch", ".diff", ".log", ".ex", ".sym", ".map", ".lst"}
SKIP_DIRS = {".git", "__pycache__", "node_modules"}

# one byte literal: $A9, 0xA9, A9h, or a bare pair of hex digits standing alone
_BYTE = re.compile(r"(?<![0-9A-Za-z_$])(?:\$([0-9A-Fa-f]{2})|0[xX]([0-9A-Fa-f]{2})|([0-9A-Fa-f]{2})[hH]?)"
                   r"(?![0-9A-Za-z_])")
_HEXSTR = re.compile(r"(?<![0-9A-Za-z_])((?:[0-9A-Fa-f]{2}){8,})(?![0-9A-Za-z_])")


def rom_bytes(path):
    """The cartridge's data: an A78 header (128 bytes) or a CAR header (16) taken off."""
    raw = io.open(path, "rb").read()
    if raw[1:10] == b"ATARI7800":
        return raw[128:]
    if raw[:4] == b"CART":
        return raw[16:]
    return raw


class Index(object):
    """Every MIN-byte window of the cartridge, for lookups."""

    def __init__(self, rom, n):
        self.rom, self.n = rom, n
        self.at = {}
        for i in range(len(rom) - n + 1):
            w = rom[i:i + n]
            if w.count(w[0]) == n:
                continue                    # one value repeated: not evidence
            self.at.setdefault(w, i)

    def runs(self, data):
        """[(start in data, length, rom offset)]: maximal runs of the cartridge's bytes."""
        n, out, i = self.n, [], 0
        while i <= len(data) - n:
            r = self.at.get(bytes(data[i:i + n]))
            if r is None:
                i += 1
                continue
            k = n
            while i + k < len(data) and r + k < len(self.rom) and data[i + k] == self.rom[r + k]:
                k += 1
            out.append((i, k, r))
            i += k
        return out


def text_stream(lines):
    """The byte literals of a text file as one stream per stretch of lines that hold them:
    [(bytes, [line number of each byte])]."""
    streams, cur, where = [], bytearray(), []
    for no, line in enumerate(lines, 1):
        got = []
        for m in _HEXSTR.finditer(line):
            s = m.group(1)
            got.append((m.start(), bytes.fromhex(s)))
        spans = [(m.start(), m.end()) for m in _HEXSTR.finditer(line)]
        for m in _BYTE.finditer(line):
            if any(a <= m.start() < b for a, b in spans):
                continue
            v = m.group(1) or m.group(2) or m.group(3)
            got.append((m.start(), bytes([int(v, 16)])))
        if not got:
            if cur:
                streams.append((bytes(cur), where))
            cur, where = bytearray(), []
            continue
        for _, b in sorted(got, key=lambda g: g[0]):
            cur += b
            where += [no] * len(b)
    if cur:
        streams.append((bytes(cur), where))
    return streams


def is_text(path, head):
    if os.path.splitext(path)[1].lower() in TEXT_EXT:
        return True
    return b"\0" not in head and all(c in b"\t\r\n" or 32 <= c < 127 or c >= 128 for c in head[:512])


def scan_file(index, path):
    """[(where, length, rom offset)] for one file; where is "line N" or "offset $N"."""
    try:
        raw = io.open(path, "rb").read()
    except OSError:
        return []
    out = []
    if is_text(path, raw[:2048]):
        lines = raw.decode("utf-8", "replace").splitlines()
        for data, where in text_stream(lines):
            for i, k, r in index.runs(data):
                out.append(("line %d" % where[i], k, r))
    for i, k, r in index.runs(raw):
        out.append(("offset $%X" % i, k, r))
    return out


def files_under(paths, git):
    if git:
        try:
            names = subprocess.check_output(["git", "ls-files", "-z"], cwd=paths[0],
                                            stderr=subprocess.DEVNULL).decode("utf-8").split("\0")
            return [os.path.join(paths[0], n) for n in names if n and os.path.isfile(os.path.join(paths[0], n))]
        except (OSError, subprocess.CalledProcessError):
            pass
    out = []
    for p in paths:
        if os.path.isfile(p):
            out.append(p)
            continue
        for dp, dns, fns in os.walk(p):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            out += [os.path.join(dp, f) for f in fns]
    return out


HOOK = r'''
import os, sys, runpy
_log = open(os.environ["ROMSCAN_LOG"], "a", encoding="utf-8")
_seen = set()
def _hook(ev, args):
    if ev == "open" and not isinstance(args[0], int):
        k = (os.path.abspath(str(args[0])), str(args[1]))
        if k not in _seen:
            _seen.add(k)
            _log.write("%s\t%s\n" % (k[1], k[0])); _log.flush()
sys.addaudithook(_hook)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def trace(cmd, root):
    """Run a Python script under an audit hook: (files read, files written) outside root and
    outside Python's own installation."""
    fd, log = tempfile.mkstemp(prefix="romscan-", suffix=".log")
    os.close(fd)
    env = dict(os.environ, ROMSCAN_LOG=log)
    rc = subprocess.call([sys.executable, "-c", HOOK] + cmd, env=env)
    reads, writes = set(), set()
    root = os.path.normcase(os.path.abspath(root))
    pyroots = {os.path.normcase(os.path.abspath(p)) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    for line in io.open(log, encoding="utf-8"):
        mode, path = line.rstrip("\n").split("\t", 1)
        n = os.path.normcase(path)
        if n.startswith(root + os.sep) or any(n.startswith(p + os.sep) for p in pyroots):
            continue
        if "__pycache__" in n:
            continue
        (writes if any(c in mode for c in "wax+") else reads).add(path)
    os.remove(log)
    return rc, sorted(reads - writes), sorted(writes)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", help="the cartridge (.a78, .car, .bin, .a26)")
    ap.add_argument("paths", nargs="*", help="files or folders (default: the git repository here)")
    ap.add_argument("--min", type=int, default=16, help="shortest run reported (default 16)")
    ap.add_argument("--trace", action="store_true",
                    help="-- SCRIPT [ARGS] (last): run a Python build and list what it opens "
                         "outside the tree")
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--trace" in argv:                   # everything after it is the build's command line
        k = argv.index("--trace")
        argv, cmd = argv[:k + 1], argv[k + 1:]
        if cmd[:1] == ["--"]:
            cmd = cmd[1:]
    args = ap.parse_args(argv)
    if args.trace and not cmd:
        ap.error("--trace needs a script: --trace -- build.py [ARGS]")
    rom = rom_bytes(args.rom)
    paths = args.paths or ["."]
    git = not args.paths
    found = 0
    index = Index(rom, args.min)
    rom_abs = os.path.normcase(os.path.abspath(args.rom))
    files = [f for f in files_under(paths, git) if os.path.normcase(os.path.abspath(f)) != rom_abs]
    for f in files:
        for where, k, r in scan_file(index, f):
            found += 1
            print("%s: %s: %d bytes of the cartridge (its offset $%X)" % (f, where, k, r))
    print("%d files scanned, %s" % (len(files), "%d runs of %d+ cartridge bytes found" % (found, args.min)
                                    if found else "no run of %d cartridge bytes" % args.min))
    if args.trace:
        rc, reads, writes = trace(cmd, paths[0])
        print("the build (exit %d) read outside %s:" % (rc, os.path.abspath(paths[0])))
        for p in reads:
            print("   ", p, "(the cartridge)" if os.path.normcase(os.path.abspath(p)) == rom_abs else "")
        if not reads:
            print("    nothing")
        if writes:
            print("and wrote outside it:")
            for p in writes:
                print("   ", p)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
