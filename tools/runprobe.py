#!/usr/bin/env python3
"""Run one of the Lua probes on a cartridge, headless, and say what it wrote.

    python tools/runprobe.py game.a78 exectrace --seconds 30 -o out
    python tools/runprobe.py game.a78 cyclebudget -e A7800_CB_END=400 -e A7800_CB_RANGES=main=8000-BFFF
    python tools/runprobe.py game.a78 audio -e A7800_POKEY=auto -o out
    python tools/runprobe.py --list

Every probe in `probes/` is run the same way -- MAME with no window or sound, the
BIOS found for you, the probe loaded with `-autoboot_script`, the working folder
set to the output folder so the log lands there -- and every probe takes its
settings from `A7800_*` environment variables. That is the whole job of this
command: the part that was typed out, slightly differently, in every recipe.

  -e KEY=VALUE   an environment variable for the probe (repeat it). The one value
                 with meaning here is `A7800_POKEY=auto`, which becomes the
                 cartridge's POKEY address(es) from its header.
  --seconds N    how long MAME runs (default 30). A probe that stops itself at a
                 frame (`A7800_*_END`, `A7800_AUDIO_FRAMES`) stops sooner.
  --playback F   replay a recording made with `session.py record` instead of
                 running with no input.

MAME and the BIOS are found as `capture.py` finds them (`--mame`, `A7800_MAME`,
`--rompath`, `A7800_ROMPATH`, `A7800_BIOS`; docs/emulation.md). A probe that did
not write anything is reported, because that usually means the cartridge never got
past the BIOS, or the probe's tap did not see what it was watching.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture  # noqa: E402

PROBES = os.path.join(os.path.dirname(HERE), "probes")


def probe_path(name):
    """The probe file for `name` (a name in probes/, with or without .lua, or a path)."""
    if os.path.isfile(name):
        return os.path.abspath(name)
    stem = name[:-4] if name.endswith(".lua") else name
    p = os.path.join(PROBES, stem + ".lua")
    if os.path.isfile(p):
        return p
    return None


def list_probes():
    """[(name, summary)] for every probe, from its header's first line."""
    out = []
    for f in sorted(os.listdir(PROBES)):
        if not f.endswith(".lua"):
            continue
        summary = ""
        with open(os.path.join(PROBES, f), encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                m = re.match(r"--\s*\S+\.lua\s*--\s*(.*)$", ln.strip()) or \
                    re.match(r"--\s*(.+)$", ln.strip())
                if m and not ln.startswith("--   "):
                    summary = m.group(1).strip()
                    break
        out.append((f[:-4], summary))
    return out


def parse_env(items, rom):
    """{KEY: VALUE} from `-e KEY=VALUE` items, resolving A7800_POKEY=auto."""
    env = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("-e wants KEY=VALUE, not %r" % item)
        k, v = item.split("=", 1)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise ValueError("%r is not an environment variable name" % k)
        if k == "A7800_POKEY" and v == "auto":
            bases = capture.inspect(rom)["pokeys"]
            if not bases:
                continue                  # no POKEY: leave the probe on the TIA
            v = ",".join("0x%04X" % b for b in bases)
        env[k] = v
    return env


def run(rom, probe, out, seconds=30, env=None, playback=None, mame=None,
        rompath=None, timeout=1800):
    """Run `probe` on `rom`. Returns (ok, text, written) where `written` lists the
    files the run created or changed in `out`, as (name, size)."""
    rom = os.path.abspath(rom)
    path = probe_path(probe)
    if not path:
        names = ", ".join(n for n, _s in list_probes())
        return False, "no probe called %r. They are: %s" % (probe, names), []
    exe = capture.find_mame(mame)
    if not exe:
        return False, ("MAME was not found (set A7800_MAME or pass --mame; "
                       "docs/emulation.md)"), []
    roms = capture.find_rompath(rom, rompath)
    if not roms:
        return False, ("the 7800 BIOS was not found (set A7800_ROMPATH, and "
                       "A7800_BIOS=a7800pr for the open BIOS; docs/emulation.md)"), []
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    before = {f: os.path.getmtime(os.path.join(out, f)) for f in os.listdir(out)}
    machine = capture.inspect(rom)["machine"]
    bios, roms = capture.machine_setup(machine, roms)
    cmd = [exe, machine] + bios + [
        "-rompath", roms, "-cart", rom, "-video", "none", "-sound", "none",
        "-skip_gameinfo", "-nothrottle", "-seconds_to_run", str(int(seconds))]
    if playback:
        pb = os.path.abspath(playback)
        cmd += ["-input_directory", os.path.dirname(pb), "-playback",
                os.path.basename(pb), "-exit_after_playback"]
    cmd += ["-autoboot_script", path]
    e = dict(os.environ)
    runtime = tempfile.mkdtemp(prefix="runprobe-")
    os.chmod(runtime, 0o700)
    e.setdefault("XDG_RUNTIME_DIR", runtime)
    e.setdefault("SDL_AUDIODRIVER", "dummy")
    e.update(env or {})
    try:
        p = subprocess.run(cmd, cwd=out, env=e, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        return False, "MAME did not finish in %d seconds" % timeout, []
    finally:
        try:
            os.rmdir(runtime)
        except OSError:
            pass
    text = p.stdout.decode("utf-8", "replace")
    written = []
    for f in sorted(os.listdir(out)):
        full = os.path.join(out, f)
        if os.path.isfile(full) and (f not in before or
                                     os.path.getmtime(full) > before[f]):
            written.append((f, os.path.getsize(full)))
    return True, text, written


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", nargs="?")
    ap.add_argument("probe", nargs="?", help="a name in probes/, or a path")
    ap.add_argument("-o", "--out", default="probe-out")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("-e", "--env", action="append", metavar="KEY=VALUE")
    ap.add_argument("--playback")
    ap.add_argument("--mame")
    ap.add_argument("--rompath")
    ap.add_argument("--list", action="store_true", help="list the probes and exit")
    args = ap.parse_args(argv)
    if args.list:
        for name, summary in list_probes():
            print("%-16s %s" % (name, summary))
        return 0
    if not (args.rom and args.probe):
        ap.error("give a cartridge and a probe (or --list)")
    if not os.path.isfile(args.rom):
        sys.exit("runprobe: no such file: %s" % args.rom)
    try:
        env = parse_env(args.env, args.rom)
    except ValueError as e:
        sys.exit("runprobe: %s" % e)
    ok, text, written = run(args.rom, args.probe, args.out, args.seconds, env,
                            args.playback, args.mame, args.rompath)
    if not ok:
        sys.stderr.write("runprobe: %s\n" % text)
        return 2
    keep = [ln for ln in text.splitlines()
            if re.search(r"probe|error|lua|%s" % re.escape(args.probe), ln, re.I)]
    for ln in keep[:12]:
        print(ln)
    if not written:
        print("runprobe: the probe wrote nothing to %s. Either the cartridge never got "
              "past the BIOS, or the probe saw nothing it watches for (read its "
              "header)." % args.out)
        return 1
    print("wrote, in %s:" % args.out)
    for name, size in written:
        print("  %-28s %9d bytes" % (name, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
