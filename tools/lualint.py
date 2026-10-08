#!/usr/bin/env python3
"""Lint MAME Lua probes for the mistakes that fail silently.

    python tools/lualint.py                    # every probe in probes/
    python tools/lualint.py path/to/probe.lua  # files, or folders of them

A probe that cannot be run here -- or anywhere but the machine with MAME on it
-- still deserves a review that does not depend on remembering the traps. This
checks three things, all cheap and none of them a parse of Lua:

  tap-held-in-local   a tap (install_read_tap / install_write_tap) kept only in
                      a chunk-level `local`. docs/pitfalls.md, "MAME write taps
                      are garbage-collected": nothing in the running machine
                      refers to the tap, so Lua collects it a few hundred
                      frames in and the probe goes on printing plausible
                      numbers from a dead tap. Keep taps in a global (or a
                      table that is one). Bare, dropped calls are caught too.
  no-header           the file must open with a comment saying what it is for;
                      a probe nobody can recognise gets copied and drifts.
  undocumented-env    every os.getenv("NAME") must be named in the comment
                      block at the top of the file, or in the usage text there.

It is deliberately conservative: it is a regex pass, so a report means "look
here", and silence means only that these three traps are absent -- not that the
probe works. It has never seen MAME run; neither has this lint.

Exit status 1 if anything is reported, so it can sit in a test.
"""
import glob
import os
import re
import sys

TAP = r"install_(?:read|write)_tap"


def header_block(lines):
    """The leading run of comment lines (blank lines inside it are allowed)."""
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("--"):
            out.append(s)
        elif not s and out:
            out.append("")
        else:
            break
    return "\n".join(out)


def lint_text(text, name="<probe>"):
    """[(line number, rule, message)] for one probe's source."""
    lines = text.splitlines()
    found = []

    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is None or not lines[first].strip().startswith("--"):
        found.append((first + 1 if first is not None else 1, "no-header",
                      "the file does not open with a comment saying what the "
                      "probe is for"))

    head = header_block(lines)
    seen = set()
    for i, l in enumerate(lines, 1):
        code = l.split("--", 1)[0]
        for m in re.finditer(r'os\.getenv\(\s*["\']([A-Za-z0-9_]+)["\']', code):
            var = m.group(1)
            if var not in seen and var not in head:
                seen.add(var)
                found.append((i, "undocumented-env",
                              "%s is read here but not named in the header "
                              "comment" % var))

    # a tap held only in a chunk-level `local` -- or dropped on the floor
    holders = set()
    depth = 0                       # open { ... } across lines (naive)
    for i, l in enumerate(lines, 1):
        code = re.sub(r'"[^"]*"|\'[^\']*\'', '""', l.split("--", 1)[0])
        m = re.match(r"^local\s+(\w+)\s*=\s*(.*)$", code)
        if m and re.search(TAP, m.group(2)):
            found.append((i, "tap-held-in-local",
                          "`local %s` holds a tap at chunk scope; keep it in a "
                          "global so it is not collected" % m.group(1)))
        if m and m.group(2).strip() == "{}":
            holders.add(m.group(1))
        m = re.match(r"^\s*(\w+)\s*\[[^\]]*\]\s*=\s*.*" + TAP, code)
        if m and m.group(1) in holders:
            found.append((i, "tap-held-in-local",
                          "%s is a chunk-level `local` table; a tap stored in "
                          "it is still collected -- make it a global"
                          % m.group(1)))
        elif depth == 0 and re.match(r"^\s*[\w.]+\s*:\s*" + TAP + r"\s*\(", code):
            found.append((i, "tap-held-in-local",
                          "the tap this call returns is dropped; assign it "
                          "to a global"))
        depth += code.count("{") - code.count("}")
    found.sort()
    return [(n, r, m) for n, r, m in found]


def lint_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return lint_text(f.read(), path)


def collect(args):
    paths = []
    for a in args or [os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "probes")]:
        if os.path.isdir(a):
            paths += sorted(glob.glob(os.path.join(a, "*.lua")))
        else:
            paths.append(a)
    return paths


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    paths = collect(argv)
    bad = 0
    for p in paths:
        if not os.path.isfile(p):
            print("lualint: no such file: %s" % p)
            return 2
        for n, rule, msg in lint_file(p):
            print("%s:%d: %s: %s" % (p, n, rule, msg))
            bad += 1
    print("lualint: %d file(s), %d problem(s)" % (len(paths), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
