#!/usr/bin/env python3
"""DMA measuring cartridges that show their count on the screen, for consoles and FPGA cores.

    python tools/dmacart.py build [-o dmacarts]
    python tools/dmacart.py mame [-o dmacarts] [--mame PATH] [--rompath DIR] [--bios NAME]
    python tools/dmacart.py compare readings.txt [-o dmacarts]

`build` writes one cartridge per display shape of tools/dmameasure.py, and a baseline:

    dma-00-empty-zones.a78     DMA on, the 12 zones there but empty: the baseline
    dma-01-2-x-8b.a78          ... one per shape: 2 x 8b, 5-byte entries, character
    ...                        mode, holey, display interrupts, the DMA limit

Each is probes/dma-costcart.py's counting cartridge (a 14.0156-cycle loop spun through the
visible period, counted) with show=True: the count is drawn below the measured area, in
three numbers --

    1615  1615  1616
    now   lowest highest     (the lowest and highest are tracked from 2 s on)

-- so it can be read off a console, a capture or an FPGA core, where there is no Lua to read
it. The numbers are drawn from a fixed font: the cartridge only changes which glyph each digit
object points at, so that zone costs the same DMA whatever it shows, and the same in every
build. The NTSC cartridges are signed (sign7800.py), so a real console starts them in 7800 mode
through Atari's BIOS. `.bin` copies have no header, for flash carts that want one.

What to read: a shape's cost is the baseline's count minus its own, in loop passes; times
14.0156 it is CPU cycles a frame. On a stable machine the three numbers agree, or the lowest
and highest differ by one pass (where the frame's DMA lands across one).

`mame` runs every cartridge in MAME for 10 s and writes mame.txt: what MAME's screen shows for
each (now, lowest, highest) and its cost against the baseline. `compare` reads what you saw:

    00 1836          one line per cartridge: its number and the count it showed,
    01 1615 1616     or its lowest and highest
    07 1565

and prints, beside MAME's, both costs and the difference in passes and cycles.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dmameasure

LOOP = dmameasure.LOOP

READ_LUA = """
local M = manager.machine
local mem = M.devices[":maincpu"].spaces["program"]
local F = 0
local function u16(a) return mem:read_u8(a) + mem:read_u8(a + 1) * 256 end
emu.register_frame_done(function()
  F = F + 1
  if F == 600 then
    print(string.format("SHOWN %d %d %d %d", u16(0x90), u16(0x94), u16(0x96), mem:read_u8(0x92)))
    M:exit()
  end
end)
"""


def shapes():
    """(number, name, build kwargs): the baseline, then dmameasure's shapes."""
    out = [(0, "empty zones", dict(nobj=1, width=8, zones_used=0))]
    for i, (name, kw, nz, zl) in enumerate(dmameasure.SHAPES, 1):
        kw = dict(kw)
        kw.setdefault("zones_used", nz)
        out.append((i, name, kw))
    return out


def filename(num, name):
    return "dma-%02d-%s.a78" % (num, re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))


def build(outdir):
    cc = dmameasure.costcart()
    os.makedirs(outdir, exist_ok=True)
    made = []
    lines = ["#   shape (zones x lines: see tools/dmameasure.py)", ""]
    for num, name, kw in shapes():
        kw = dict(kw)
        nobj, width = kw.pop("nobj"), kw.pop("width")
        path = os.path.join(outdir, filename(num, name))
        cc.build(path, nobj, width, show=True, title="DMA %02d %s" % (num, name), **kw)
        data = open(path, "rb").read()
        open(path[:-4] + ".bin", "wb").write(data[128:])
        made.append(path)
        lines.append("%02d  %s" % (num, name))
    open(os.path.join(outdir, "shapes.txt"), "w").write("\n".join(lines) + "\n")
    return made


def mame_readings(outdir, mame=None, rompath=None, bios=None):
    """{number: (now, lowest, highest, overrun)} from MAME, for every built cartridge."""
    import capture
    exe = capture.find_mame(mame)
    if not exe:
        sys.exit("MAME was not found: pass --mame or set A7800_MAME")
    rompath = rompath or os.environ.get("A7800_ROMPATH")
    work = tempfile.mkdtemp(prefix="dmacart-")
    lua = os.path.join(work, "read.lua")
    open(lua, "w").write(READ_LUA)
    got = {}
    for num, name, kw in shapes():
        rom = os.path.abspath(os.path.join(outdir, filename(num, name)))
        if not os.path.isfile(rom):
            sys.exit("%s is missing: run `build` first" % rom)
        cmd = [exe, "a7800", "-cart", rom, "-video", "none", "-sound", "none", "-nothrottle",
               "-skip_gameinfo", "-autoboot_script", lua] + capture.bios_args(bios)
        if rompath:
            cmd += ["-rompath", rompath]
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=work,
                             timeout=300).stdout.decode("utf-8", "replace")
        m = re.search(r"^SHOWN (\d+) (\d+) (\d+) (\d+)", out, re.M)
        if not m:
            sys.exit("MAME gave no reading for %s:\n%s" % (rom, out[-2000:]))
        got[num] = tuple(int(x) for x in m.groups())
    return got


def write_mame(outdir, got):
    names = {n: s for n, s, _ in shapes()}
    base = got[0][0]
    rows = ["#   now lowest highest   cost vs 00: passes, cycles   shape", ""]
    for num in sorted(got):
        now, lo, hi, over = got[num]
        rows.append("%02d %5d %5d %5d   %6d %9.0f   %s%s" % (
            num, now, lo, hi, base - now, (base - now) * LOOP, names[num],
            "   (VBLANK OVERRUN %d)" % over if over else ""))
    path = os.path.join(outdir, "mame.txt")
    open(path, "w").write("\n".join(rows) + "\n")
    return path


def read_mame(outdir):
    got = {}
    for ln in open(os.path.join(outdir, "mame.txt")):
        m = re.match(r"^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s", ln)
        if m:
            got[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
    return got


def compare(readings, outdir):
    mame = read_mame(outdir)
    names = {n: s for n, s, _ in shapes()}
    seen = {}
    for ln in open(readings):
        f = ln.split("#")[0].split()
        if len(f) >= 2:
            vals = [int(x) for x in f[1:3]]
            seen[int(f[0])] = (min(vals), max(vals))
    if 0 not in seen or 0 not in mame:
        sys.exit("compare needs cartridge 00 (the baseline) in both")
    b_seen = sum(seen[0]) / 2
    b_mame = (mame[0][1] + mame[0][2]) / 2
    print("%-3s %-28s %11s %11s %13s %13s %8s" % (
        "#", "shape", "seen", "MAME", "cost seen", "cost MAME", "diff"))
    for num in sorted(seen):
        lo, hi = seen[num]
        mid = (lo + hi) / 2
        m = mame.get(num)
        mmid = (m[1] + m[2]) / 2 if m else None
        c_seen = (b_seen - mid) * LOOP
        c_mame = (b_mame - mmid) * LOOP if m else None
        print("%02d  %-28s %11s %11s %13.0f %13s %8s" % (
            num, names.get(num, "?")[:28], "%d-%d" % (lo, hi) if lo != hi else str(lo),
            ("%d-%d" % (m[1], m[2]) if m[1] != m[2] else str(m[1])) if m else "-",
            c_seen, "-" if m is None else "%.0f" % c_mame,
            "-" if m is None else "%+.0f" % (c_seen - c_mame)))
    print("\ncosts in CPU cycles a frame (loop passes x %.4f); diff is what you saw minus MAME"
          % LOOP)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write the cartridges")
    b.add_argument("-o", "--outdir", default="dmacarts")
    m = sub.add_parser("mame", help="MAME's reading of each cartridge (mame.txt)")
    m.add_argument("-o", "--outdir", default="dmacarts")
    m.add_argument("--mame")
    m.add_argument("--rompath")
    m.add_argument("--bios", help="-bios for the 7800 (default: A7800_BIOS, else MAME's default)")
    c = sub.add_parser("compare", help="what you saw against MAME's mame.txt")
    c.add_argument("readings")
    c.add_argument("-o", "--outdir", default="dmacarts")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        for p in build(args.outdir):
            print("wrote", p)
    elif args.cmd == "mame":
        print("wrote", write_mame(args.outdir, mame_readings(args.outdir, args.mame, args.rompath,
                                                              args.bios)))
    else:
        compare(args.readings, args.outdir)


if __name__ == "__main__":
    main()
