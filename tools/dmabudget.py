#!/usr/bin/env python3
"""
What MARIA leaves you: the 7800's cycle budget, for a given screen.

    python tools/dmabudget.py --uniform 12,16,4,8
    python tools/dmabudget.py --zone 16:2@8 --zone 16:6@16 --zone 16:0@0
    python tools/dmabudget.py --uniform 12,16,4,8 --afford
    python tools/dmabudget.py --uniform 12,16,4,8 --timing mame0264

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
    scanlines of 114.00 cycles. (A later direct count found the frame is 263 lines of
    113.5 -- 29,850.5 cycles -- which that calibration could not tell from 241 of 114
    over its window; the costs below are differences and are not affected.)

HOLEY DMA, and what it is worth
    A zone can tell MARIA to suppress graphics fetches from part of memory, so
    one display-list entry can span a region that is mostly empty. The saving
    is large and now modelled: `h` on a zone. Measured, it is exactly the byte
    cost and nothing else -- the objects still pay for their display-list
    entries, and their pixels become free. Two 20-byte objects over 192
    scanlines went from 1,412 iterations a frame to 1,825, which is 0.753
    cycles a byte against the 0.744 charged here.

    Which addresses are suppressed was measured rather than looked up: with
    the 16K bit set, a fetch is dropped when **address bit 12 is set**
    ($D000 and $F000 free; $C000 and $E000 pay in full). The 8K bit made no
    difference anywhere in $C000-$FFFF, so this tool does not model it, and
    does not pretend to know what it does.

WHAT DOES NOT COST WHAT YOU MIGHT THINK
    Zone height does not matter: the same objects in 8-scanline and
    16-scanline zones cost within 0.2% of each other. Nor does where the
    graphics live -- fetching them from RAM measures identically to fetching
    them from ROM, to the iteration.

ACCURACY
    Typically well under 1%, and 1.5% at worst across the holey and
    display-interrupt cases added later. The one systematic error is on
    single-byte-wide objects, about 2.5%, where the linear model UNDER-states
    the cost -- so a screen full of very narrow objects reads optimistic.

    A caution about one number that is NOT in this model. Measuring
    Ballblazer's own screen off an emulator trace suggested MARIA was taking
    70.3 cycles a scanline where this predicts 39.6. That measurement is not
    trusted and no correction was made for it: the cycle total behind it
    under-counts taken branches, and it compared two runs that had already
    diverged. Every controlled test of the difference it blamed -- zone
    height, graphics in RAM, holey DMA, display interrupts -- came back
    showing the model right. If you find a real screen this under-states,
    that would be worth knowing; nothing here demonstrates one.

    Seventeen configurations were fitted (objects per zone, object width, zone
    count, zone height, 4- versus 5-byte entries), then the model was checked
    by PREDICTING four shapes it had never seen. All four landed within 0.3%.
    Worst residual anywhere: 56 cycles, which is 0.2% of a frame.

    TWO TIMINGS. The numbers above were measured on MAME 0.264. The a7800 fork (and now MAME
    built with its MARIA work, see docs/emulation.md) changed MARIA's DMA: a 430-clock limit
    (was 426), a 3-clock penalty for the first hole in each object's graphics, 8 clocks of
    last-line shutdown (was 6), and DMA that is billed per scanline in whole CPU cycles.
    `--timing fork` (the default) is that machine, measured with tools/dmameasure.py over 45
    display shapes: worst residual 0.85%. `--timing mame0264` is the model as first fitted.
    The two agree to within 1% on plain screens; they differ where it matters:
        holey zones   the first fetch of each object still costs a hole penalty, so a holey
                      zone is no longer "exactly the byte cost": +0.99 cycles a line and
                      +0.51 a line per object
        interrupts    a display interrupt costs 17.4, not 16.6
        saturation    a scanline cannot cost MARIA more than 108.62 cycles on either machine:
                      past that MARIA has hit its DMA limit and the 6502 gets what is left of
                      the line (about 4.9 cycles) however much more you draw. The linear
                      model alone over-states a screen of heavy lines by 8% (22,583 against
                      the 20,855 measured for eight 16-byte objects per zone), so the cap
                      is applied per scanline and a zone that reaches it is reported.

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
# The constants come in two sets, one per machine's timing (see TWO TIMINGS above). They
# are module globals so the rest of the file reads them as before; `set_timing` switches.
TIMINGS = {
    # MAME 0.264: the model as first fitted
    "mame0264": dict(PER_LINE=5.633,    # a scanline inside any zone, even an empty one
                     PER_ZONE=1.678,    # the DLL fetch at a zone boundary
                     PER_OBJ=2.081,     # reading one 4-byte display-list entry, per scanline
                     PER_BYTE=0.744,    # one graphics byte, per scanline
                     FIVE_XTRA=0.483,   # a 5-byte entry costs this much more than a 4-byte one
                     DLI_COST=16.6,     # one display interrupt: MARIA's signal plus the 6502's
                                        # own entry and exit (24 zones and 12 agreed, 16.9, 16.3)
                     HOLE_LINE=0.0,     # holey zones: free pixels and nothing else
                     HOLE_OBJ=0.0),
    # the a7800 fork's MARIA (and MAME built with it): tools/dmameasure.py --fit, 45 shapes
    "fork": dict(PER_LINE=5.624, PER_ZONE=1.888, PER_OBJ=1.990, PER_BYTE=0.755,
                 FIVE_XTRA=0.483, DLI_COST=17.4,
                 HOLE_LINE=0.988,       # a holey zone pays this each scanline ...
                 HOLE_OBJ=0.510),       # ... and this per object: the first-hole penalty
}
LINE_CAP = 108.62    # the most one scanline can cost MARIA: its DMA limit (measured, the same
                     # on both machines; the 6502 keeps the other ~4.9 cycles of the line)
TIMING = None


def set_timing(name):
    """Make `name` ("fork" or "mame0264") the constants the model uses."""
    global TIMING, PER_LINE, PER_ZONE, PER_OBJ, PER_BYTE, FIVE_XTRA, DLI_COST
    global HOLE_LINE, HOLE_OBJ
    k = TIMINGS[name]
    TIMING = name
    PER_LINE, PER_ZONE, PER_OBJ, PER_BYTE = k["PER_LINE"], k["PER_ZONE"], k["PER_OBJ"], k["PER_BYTE"]
    FIVE_XTRA, DLI_COST = k["FIVE_XTRA"], k["DLI_COST"]
    HOLE_LINE, HOLE_OBJ = k["HOLE_LINE"], k["HOLE_OBJ"]


set_timing("fork")

REGIONS = {                      # lines/frame, CPU Hz, frames/sec
    # 263 x 113.5 = 29,850.5 cycles: measured on MAME (a cartridge that never turns
    # MARIA on executes 29,850 cycles a frame) and the 7800 Software Guide's figure.
    # PAL (313 lines, from the Guide) has not been measured.
    "ntsc": (263, 1789772.5, 59.9579),
    "pal":  (313, 1773447.0, 49.9204),
}
MAX_ZONE_LINES = 16              # the DLL offset field is 4 bits: lines-1 <= 15


class Zone(object):
    def __init__(self, lines, count, width, five=False, chars=0,
                 holey=False, dli=False):
        # chars: 0 = direct mode; 1 or 2 = character mode, that many bytes per
        # character. In character mode `width` counts CHARACTERS, and each one
        # costs a fetch from the character list plus its own data bytes.
        # holey: this zone's graphics sit in a region holey DMA suppresses,
        # so MARIA pays for the display-list entry and fetches no pixels.
        # Measured: the saving is exactly the byte cost, 0.753 per byte
        # against the 0.744 charged here.
        self.lines, self.count, self.width = lines, count, width
        self.five, self.chars = five, chars
        self.holey, self.dli = holey, dli

    def line_cycles(self):
        """What one scanline of this zone costs MARIA, before the DMA limit."""
        bytes_per_obj = 0 if self.holey else self.width
        if self.chars:
            per_obj = (PER_OBJ + FIVE_XTRA
                       + bytes_per_obj * (1 + self.chars) * PER_BYTE)
        else:
            per_obj = (PER_OBJ + bytes_per_obj * PER_BYTE
                       + (FIVE_XTRA if self.five else 0))
        hole = (HOLE_LINE + HOLE_OBJ * self.count) if self.holey and self.count else 0.0
        return PER_LINE + per_obj * self.count + hole

    def saturated(self):
        """True if a scanline of this zone reaches MARIA's DMA limit."""
        return self.line_cycles() >= LINE_CAP

    def cycles(self):
        return (self.lines * min(self.line_cycles(), LINE_CAP) + PER_ZONE
                + (DLI_COST if self.dli else 0))

    def label(self):
        tag = ("".join(x for x, on in (("holey", self.holey), ("dli", self.dli))
                       if on))
        tag = "  " + tag if tag else ""
        if self.chars:
            return "%2d lines x %2d obj @ %2d chars (%d b/char)%s" % (
                self.lines, self.count, self.width, self.chars, tag)
        return "%2d lines x %2d obj @ %2d bytes%s%s" % (
            self.lines, self.count, self.width,
            "  (5-byte)" if self.five else "", tag)


def parse_zone(spec):
    """LINES:COUNT@WIDTH, with an optional suffix.

        (none)  direct mode, 4-byte entry
        5       direct mode, 5-byte entry
        c       character mode, 1 byte per character  (CTRL bit 4 clear)
        c2      character mode, 2 bytes per character (CTRL bit 4 SET)
        h       the zone's graphics are in a region holey DMA suppresses,
                so its objects cost their headers and no pixels
        d       the zone raises a display interrupt

    In character mode WIDTH counts characters, not bytes.
    """
    holey = dli = False
    while spec and spec[-1] in "hd":
        if spec[-1] == "h":
            holey = True
        else:
            dli = True
        spec = spec[:-1]
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
        return Zone(int(lines), int(count), int(width), five, chars,
                    holey, dli)
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
    print("timing            %s" % TIMING)
    print("")
    print("  %-34s %10s" % ("zone", "cycles"))
    for i, z in enumerate(zones):
        print("  %2d  %-30s %10.0f%s" % (i, z.label(), z.cycles(),
                                       "   at MARIA's DMA limit" if z.saturated() else ""))
    print("  %-34s %10.0f" % ("total DMA", dma))
    print("")
    print("drawn scanlines   %8d  of %d" % (drawn, lines_total))
    print("MARIA takes       %8.0f cycles  (%.1f%% of the frame)"
          % (dma, 100.0 * dma / frame))
    print("you get           %8.0f cycles  (%.1f%%)" % (left, 100.0 * left / frame))
    print("                  %8.0f cycles per scanline of game logic, averaged"
          % (left / lines_total))
    full = [z for z in zones if z.saturated()]
    if full:
        print("")
        print("  ** %d zone(s) reach MARIA's DMA limit (%.1f cycles a scanline): the 6502 gets"
              % (len(full), LINE_CAP))
        print("     about %.1f cycles on each of those scanlines, and anything more you"
              % (113.5 - LINE_CAP))
        print("     draw there is lost rather than slowing you further.")
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
        # Two different questions, and reporting only the first invites a
        # reader to take it for the second. A band across the whole screen and
        # a sprite in one zone differ by the number of zones they touch, which
        # here is a factor of twenty-five.
        drawn = sum(z.lines for z in zones)
        tall = max((z.lines for z in zones), default=16)
        print("")
        print("what the leftover buys, by object width in bytes:")
        print("   %-8s %14s %16s" % ("width", "full-height", "one %d-line zone" % tall))
        for w in (1, 2, 4, 8, 16):
            band = drawn * (PER_OBJ + w * PER_BYTE)
            one = tall * (PER_OBJ + w * PER_BYTE)
            print("   %-8d %14d %16d"
                  % (w, max(0, int(left / band)), max(0, int(left / one))))
        print("   full-height counts objects present in EVERY zone, spanning")
        print("   all %d drawn scanlines. A sprite is the other column." % drawn)


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("--zone", action="append", default=[], metavar="L:C@W",
                    help="one zone: LINES:COUNT@WIDTH, repeatable. Suffixes: "
                         "5 a 5-byte entry; c/c2 character mode with 1 or 2 "
                         "bytes per character (WIDTH then counts characters); "
                         "h graphics in a holey-suppressed region; d the zone "
                         "raises a display interrupt")
    ap.add_argument("--uniform", metavar="ZONES,LINES,COUNT,WIDTH",
                    help="shorthand for identical zones, e.g. 12,16,4,8")
    ap.add_argument("--region", choices=sorted(REGIONS), default="ntsc")
    ap.add_argument("--timing", choices=sorted(TIMINGS), default="fork",
                    help="whose MARIA timing: the a7800 fork's (also current MAME with its work "
                         "ported), or MAME 0.264's")
    ap.add_argument("--afford", action="store_true",
                    help="also report how many more objects the leftover fits")
    args = ap.parse_args()

    set_timing(args.timing)
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
