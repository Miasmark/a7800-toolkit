#!/usr/bin/env python3
"""
What a frame's display costs MARIA, zone by zone, from a RAM dump.

    python tools/zonebill.py survey-ram-01200.bin [...]

Takes the dumps probes/rendersurvey.lua writes (RAM $1800-$27FF, with DPPH,
DPPL and CTRL in the .txt beside each), walks the display list list and every
display list in RAM with dlwalk.py, and bills each zone with dmabudget.py's
measured constants: per line, per object, per byte, the indirect (character)
extra, and a DLI. So any game's frame is costed by the same instrument --
three racing games were compared this way, for Pole Position II's
split-screen budget.

Prints, per dump: zones, lines, headers (indirect ones), DLI zones, and the
DMA cycles as a share of the frame with what is left for the CPU; then the
zone heights. --zones lists every zone. A display list outside RAM (a list
kept in ROM) is walked as empty and says so, so the figure is then a lower
bound.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dlwalk  # noqa: E402
import dmabudget as B  # noqa: E402

FRAME = 262 * 114          # NTSC CPU cycles per frame
VISIBLE = 243              # stop the walk once the zones cover this many lines
RAM_BASE = 0x1800


def bill(path):
    """[(zone, objects, cycles, in_ram)] for one dump, and CTRL."""
    meta = open(os.path.splitext(path)[0] + ".txt").read()
    vals = dict((k, int(v, 16)) for k, v in re.findall(r"(\w+)=([0-9A-Fa-f]{2})", meta))
    dpph, dppl, ctrl = vals["dpph"], vals["dppl"], vals["ctrl"]
    chars = 2 if ctrl & 0x10 else 1
    src = dlwalk.Source(open(path, "rb").read(), RAM_BASE)
    at, lines, zones = (dpph << 8) | dppl, 0, []
    while lines < VISIBLE and len(zones) < 80:
        z = dlwalk.decode_dll_entry(src, at)
        at += 3
        try:
            objs, in_ram = dlwalk.walk_dl(src, z["dl"]), True
        except IndexError:
            objs, in_ram = [], False
        cyc = 0.0
        for o in objs:
            if o["indirect"]:
                cyc += B.PER_OBJ + B.FIVE_XTRA + o["width"] * (1 + chars) * B.PER_BYTE
            else:
                cyc += B.PER_OBJ + o["width"] * B.PER_BYTE + (B.FIVE_XTRA if o["bytes"] == 5 else 0)
        cost = z["lines"] * (B.PER_LINE + cyc) + B.PER_ZONE + (B.DLI_COST if z["dli"] else 0)
        zones.append((z, objs, cost, in_ram))
        lines += z["lines"]
    return zones, ctrl


def summary(name, path, per_zone=False):
    zones, _ctrl = bill(path)
    total = sum(c for _, _, c, _ in zones)
    lines = sum(z["lines"] for z, _, _, _ in zones)
    hdrs = sum(len(o) for _, o, _, _ in zones)
    ind = sum(1 for _, o, _, _ in zones for e in o if e["indirect"])
    dlis = sum(1 for z, _, _, _ in zones if z["dli"])
    rom = sum(1 for _, _, _, r in zones if not r)
    print("%-26s zones=%3d lines=%3d  headers=%3d (indirect %2d)  DLI zones=%d  "
          "DMA=%6.0f cyc = %4.1f%% of frame  -> CPU left %5.0f" % (
              name, len(zones), lines, hdrs, ind, dlis, total,
              100 * total / FRAME, FRAME - total))
    hist = {}
    for z, _, _, _ in zones:
        hist[z["lines"]] = hist.get(z["lines"], 0) + 1
    print("%26s zone heights: %s" % ("", "  ".join("%dx%d" % (n, h) for h, n in sorted(hist.items()))))
    if rom:
        print("%26s %d zone(s) list objects outside RAM, walked as empty: "
              "a lower bound" % ("", rom))
    if per_zone:
        for i, (z, objs, cost, in_ram) in enumerate(zones):
            print("%26s zone %2d  %2d lines  %2d objects  %6.0f cyc%s%s" % (
                "", i, z["lines"], len(objs), cost, "  DLI" if z["dli"] else "",
                "" if in_ram else "  (list outside RAM)"))
    return zones


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("dumps", nargs="+", help="rendersurvey RAM dumps (.bin)")
    ap.add_argument("--zones", action="store_true", help="list every zone")
    args = ap.parse_args()
    for p in args.dumps:
        summary(os.path.splitext(os.path.basename(p))[0], p, args.zones)
    return 0


if __name__ == "__main__":
    sys.exit(main())
