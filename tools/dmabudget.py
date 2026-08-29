#!/usr/bin/env python3
"""
What MARIA leaves you: the 7800's cycle budget, for a given screen.

    python tools/dmabudget.py --uniform 12,16,4,8
    python tools/dmabudget.py --zone 16:2@8 --zone 16:6@16 --zone 16:0@0
    python tools/dmabudget.py --uniform 12,16,4,8 --afford

MARIA draws by DMA and halts the 6502 while it does, so on the 7800 "how much
can my game compute" is a question about how much it is drawing. Every other
machine of the era lets you answer that from a manual; here the honest answer
was a shrug, so these numbers were measured.

HOW THEY WERE MEASURED
    A cartridge that spins a fixed-cost loop for the whole visible period and
    publishes the iteration count each frame. Build it once per display-list
    shape, change nothing but the display lists, and the difference in the
    count is the DMA cost. The loop was calibrated by running it again with
    extra NOPs: the fitted cost came out at 14.020 cycles per iteration
    against 14.016 counted by hand, and the measured window at exactly 241.0
    scanlines of 114.00 cycles.

ACCURACY
    Typically well under 1%. The worst residual is about 2.5%, on
    single-byte-wide objects, where the linear model slightly UNDER-states
    the cost -- so a screen full of very narrow objects is the one case
    where this reads optimistic. Budget accordingly.

    Seventeen configurations were fitted (objects per zone, object width, zone
    count, zone height, 4- versus 5-byte entries), then the model was checked
    by PREDICTING four shapes it had never seen. All four landed within 0.3%.
    Worst residual anywhere: 56 cycles, which is 0.2% of a frame.

    These are MAME's timings. MAME's 7800 DMA model is good enough that the
    constants fall out as round numbers in MARIA colour clocks -- a graphics
    byte costs 2.98, which is the documented 3 -- but that is corroboration,
    not silicon.
"""
import argparse
import sys

# CPU cycles, NTSC, measured as described above. In MARIA colour clocks
# (4 per CPU cycle) these are 22.5, 6.7, 8.3, 3.0 and 1.9 -- the graphics byte
# landing on the documented 3 is the main reason to trust the rest.
PER_LINE  = 5.633    # a scanline inside any zone, even an empty one
PER_ZONE  = 1.678    # the DLL fetch at a zone boundary
PER_OBJ   = 2.081    # reading one 4-byte display-list entry, per scanline
PER_BYTE  = 0.744    # one graphics byte, per scanline
FIVE_XTRA = 0.483    # a 5-byte entry costs this much more than a 4-byte one

REGIONS = {                      # lines/frame, CPU Hz, frames/sec
    "ntsc": (262, 1789772.5, 59.9224),
    "pal":  (312, 1773447.0, 49.8607),
}
MAX_ZONE_LINES = 16              # the DLL offset field is 4 bits: lines-1 <= 15


class Zone(object):
    def __init__(self, lines, count, width, five=False, chars=0):
        # chars: 0 = direct mode; 1 or 2 = character mode, that many bytes per
        # character. In character mode `width` counts CHARACTERS, and each one
        # costs a fetch from the character list plus its own data bytes.
        self.lines, self.count, self.width = lines, count, width
        self.five, self.chars = five, chars

    def cycles(self):
        if self.chars:
            per_obj = (PER_OBJ + FIVE_XTRA
                       + self.width * (1 + self.chars) * PER_BYTE)
        else:
            per_obj = (PER_OBJ + self.width * PER_BYTE
                       + (FIVE_XTRA if self.five else 0))
        return self.lines * (PER_LINE + per_obj * self.count) + PER_ZONE

    def label(self):
        if self.chars:
            return "%2d lines x %2d obj @ %2d chars (%d b/char)" % (
                self.lines, self.count, self.width, self.chars)
        return "%2d lines x %2d obj @ %2d bytes%s" % (
            self.lines, self.count, self.width, "  (5-byte)" if self.five else "")


def parse_zone(spec):
    """LINES:COUNT@WIDTH, with an optional suffix.

        (none)  direct mode, 4-byte entry
        5       direct mode, 5-byte entry
        c       character mode, 1 byte per character  (CTRL bit 4 clear)
        c2      character mode, 2 bytes per character (CTRL bit 4 SET)

    In character mode WIDTH counts characters, not bytes.
    """
    chars = 0
    if spec.endswith("c2"):
        chars, spec = 2, spec[:-2]
    elif spec.endswith("c"):
        chars, spec = 1, spec[:-1]
    five = spec.endswith("5")
    if five:
        spec = spec[:-1]
    try:
        lines, rest = spec.split(":")
        count, width = rest.split("@")
        return Zone(int(lines), int(count), int(width), five, chars)
    except ValueError:
        raise SystemExit("bad --zone %r: want LINES:COUNT@WIDTH, e.g. 16:4@8"
                         % spec)


def report(zones, region, afford):
    lines_total, hz, fps = REGIONS[region]
    per_line_cycles = hz / fps / lines_total
    frame = hz / fps

    drawn = sum(z.lines for z in zones)
    dma = sum(z.cycles() for z in zones)
    left = frame - dma

    print("region            %s, %d scanlines/frame, %.2f cycles/scanline"
          % (region.upper(), lines_total, per_line_cycles))
    print("frame budget      %8.0f cycles" % frame)
    print("")
    bad = [z for z in zones if z.lines > MAX_ZONE_LINES]
    if bad:
        print("  ** %d zone(s) taller than %d lines. The DLL offset field is"
              % (len(bad), MAX_ZONE_LINES))
        print("     four bits, so lines-1 must fit in 0-15. MARIA will draw")
        print("     something, but not what you asked for.")
        print("")
    print("  %-34s %10s" % ("zone", "cycles"))
    for i, z in enumerate(zones):
        print("  %2d  %-30s %10.0f" % (i, z.label(), z.cycles()))
    print("  %-34s %10.0f" % ("total DMA", dma))
    print("")
    print("drawn scanlines   %8d  of %d" % (drawn, lines_total))
    print("MARIA takes       %8.0f cycles  (%.1f%% of the frame)"
          % (dma, 100.0 * dma / frame))
    print("you get           %8.0f cycles  (%.1f%%)" % (left, 100.0 * left / frame))
    print("                  %8.0f cycles per scanline of game logic, averaged"
          % (left / lines_total))
    if left < 0:
        print("")
        print("OVER BUDGET. MARIA does not skip work to let the 6502 finish --")
        print("the frame simply arrives with your logic unfinished.")
    elif left < frame * 0.25:
        print("")
        print("Under a quarter of the frame left. Shipping games at this point")
        print("move work off the main loop: fewer objects per zone, narrower")
        print("ones, or a zone that is empty for part of the screen.")

    if afford:
        print("")
        print("how many more objects fit, at each width (uniform across %d zones):"
              % len(zones))
        for w in (1, 2, 4, 8, 16):
            each = sum(z.lines for z in zones) * (PER_OBJ + w * PER_BYTE)
            print("   width %2d bytes: %4d more" % (w, max(0, int(left / each))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--zone", action="append", default=[], metavar="L:C@W",
                    help="one zone: LINES:COUNT@WIDTH, repeatable. Suffix 5 "
                         "for a 5-byte entry, c or c2 for character mode with "
                         "1 or 2 bytes per character (then WIDTH is in "
                         "characters)")
    ap.add_argument("--uniform", metavar="ZONES,LINES,COUNT,WIDTH",
                    help="shorthand for identical zones, e.g. 12,16,4,8")
    ap.add_argument("--region", choices=sorted(REGIONS), default="ntsc")
    ap.add_argument("--afford", action="store_true",
                    help="also report how many more objects the leftover fits")
    args = ap.parse_args()

    zones = [parse_zone(z) for z in args.zone]
    if args.uniform:
        try:
            n, lines, count, width = [int(x) for x in args.uniform.split(",")]
        except ValueError:
            raise SystemExit("bad --uniform: want ZONES,LINES,COUNT,WIDTH")
        zones += [Zone(lines, count, width) for _ in range(n)]
    if not zones:
        ap.error("give it a screen: --uniform or one or more --zone")
    report(zones, args.region, args.afford)
    return 0


if __name__ == "__main__":
    sys.exit(main())
