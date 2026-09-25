#!/usr/bin/env python3
"""
What a mod is made of, byte by byte: how much of the original is still there
and still used, what was overwritten, and what is new.

    python tools/modmap.py original.a78 modded.a78 [coverage.txt ...]
        [--label "new code=4000-63FF" --label "tables=7C00-7FFF" ...]
        [--png map.png]

Both cartridges may be headered or bare; the bodies are compared aligned at
their ends, as a linear 7800 cartridge ends at $FFFF, so a mod that grew the
cartridge (32K to 48K, say) lines up. Coverage files are
probes/romcoverage.lua's output from runs of the MODDED cartridge, one
character per byte of the range it mapped; several are combined by OR. Read
is a lower bound: code for situations no run reached shows as unread.

Every byte of the modded body falls in one class:

    original, read            unchanged from the original and read in play
    original, not read        unchanged, and no run read it
    overwritten               the original had something else here
    new                       beyond the original's size, and not empty
    new, empty                beyond the original, still the fill ($FF), and
                              no run read it (a read $FF is data -- solid
                              pixels, a table entry -- so it counts as new)

--label NAME=RANGE[,RANGE] names part of the mod (hex ranges): overwritten
or new bytes inside it are counted under that name instead. The labels are
the mod author's knowledge -- which new bytes are code and which are
graphics -- and the tool does not guess them.

Prints a markdown table, and with --png draws the map: one pixel column per
byte, one row per 256-byte page.
"""
import argparse
import collections
import os
import sys

A78 = 128


def body(path):
    blob = open(path, "rb").read()
    if len(blob) % 1024 == A78 and blob[1:10] == b"ATARI7800":
        blob = blob[A78:]
    return blob


def parse_ranges(text):
    out = []
    for part in text.split(","):
        lo, _, hi = part.strip().partition("-")
        out.append((int(lo, 16), int(hi or lo, 16)))
    return out


def classify(orig, mod, read, labels, fill=0xFF):
    """One class name per byte of `mod` (a body ending at $FFFF)."""
    base = 0x10000 - len(mod)
    obase = 0x10000 - len(orig)
    out = []
    for i, v in enumerate(mod):
        addr = base + i
        name = next((n for n, rs in labels for lo, hi in rs if lo <= addr <= hi), None)
        if addr >= obase:
            if orig[addr - obase] == v:
                out.append("original, read" if read[i] else "original, not read")
            else:
                out.append(name or "overwritten")
        elif v == fill and name is None and not read[i]:
            out.append("new, empty")
        else:
            out.append(name or "new")
    return out


PALETTE = [(120, 170, 120), (60, 90, 60), (230, 60, 60), (60, 130, 240),
           (40, 40, 48), (250, 160, 40), (160, 110, 240), (80, 210, 230),
           (240, 140, 200), (250, 210, 60), (200, 200, 90), (140, 90, 60)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("original")
    ap.add_argument("modded")
    ap.add_argument("coverage", nargs="*", help="probes/romcoverage.lua maps")
    ap.add_argument("--label", action="append", default=[],
                    help="NAME=RANGE[,RANGE]: count those bytes under NAME")
    ap.add_argument("--from", dest="cov_from", default="4000",
                    help="first address of the coverage maps (default 4000)")
    ap.add_argument("--png", help="draw the map here (needs Pillow)")
    args = ap.parse_args()

    orig, mod = body(args.original), body(args.modded)
    if len(orig) > len(mod):
        raise SystemExit("the original is larger than the mod; give them the "
                         "other way round")
    base = 0x10000 - len(mod)
    read = [False] * len(mod)
    cfrom = int(args.cov_from, 16)
    for fn in args.coverage:
        s = open(fn).read().strip()
        for j, ch in enumerate(s):
            i = cfrom + j - base
            if ch == "1" and 0 <= i < len(mod):
                read[i] = True
    labels = []
    for spec in args.label:
        name, _, rng = spec.partition("=")
        if not rng:
            raise SystemExit("--label wants NAME=RANGE, got %r" % spec)
        labels.append((name.strip(), parse_ranges(rng)))
    cat = classify(orig, mod, read, labels)

    order = ["original, read", "original, not read", "overwritten"]
    order += [n for n, _ in labels if n not in order]
    order += ["new", "new, empty"]
    cnt = collections.Counter(cat)
    readc = collections.Counter(c for c, r in zip(cat, read) if r)
    tot = len(mod)
    print("| bytes | share | read in play | what |")
    print("|---:|---:|---:|---|")
    for c in order:
        if cnt[c]:
            print("| %d | %.1f%% | %d | %s |" % (cnt[c], 100.0 * cnt[c] / tot, readc[c], c))
    kept = cnt["original, read"] + cnt["original, not read"]
    print("")
    print("original bytes kept unchanged: %d of %d (%.1f%%); read in play: %d%s"
          % (kept, len(orig), 100.0 * kept / len(orig), cnt["original, read"],
             "" if args.coverage else " (no coverage given)"))
    own = tot - kept - cnt["new, empty"]
    print("the mod's own bytes (overwritten and new, not empty): %d (%.1f%%)"
          % (own, 100.0 * own / tot))

    if args.png:
        from PIL import Image, ImageDraw
        col = dict((c, PALETTE[i % len(PALETTE)]) for i, c in enumerate(order))
        sx, sy = 3, 3
        pages = len(mod) // 256
        w, h = 256 * sx, pages * sy
        im = Image.new("RGB", (w + 470, max(h + 20, 40 + 22 * len(order))), (24, 24, 28))
        for i, c in enumerate(cat):
            x, y = (i & 0xFF) * sx, (i >> 8) * sy + 10
            im.paste(col[c], (x, y, x + sx, y + sy))
        d = ImageDraw.Draw(im)
        for pg in range(0, pages, 16):
            d.text((w + 6, pg * sy + 6), "$%02X00" % ((base >> 8) + pg), fill=(200, 200, 200))
        ly = 20
        for c in order:
            if cnt[c]:
                d.rectangle((w + 60, ly, w + 72, ly + 12), fill=col[c])
                d.text((w + 80, ly), "%s  %d" % (c, cnt[c]), fill=(230, 230, 230))
                ly += 22
        d.text((w + 60, ly + 10), "one pixel column per byte, one row per page",
               fill=(180, 180, 180))
        im.save(args.png)
        print("\nwrote %s" % args.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
