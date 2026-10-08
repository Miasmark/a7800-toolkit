#!/usr/bin/env python3
"""
Round-trip check: reassemble every generated listing and compare it against the
original ROM bank, byte for byte.

Usage: python verify.py <rom.a78> [-d src]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disasm import Cart
from asm import Assembler, AsmError, item_at
import m6502


def decode(data, i):
    """One instruction at data[i:], as text -- enough to read a difference."""
    if i >= len(data):
        return "(end of data)"
    op = m6502.OPCODES.get(data[i])
    if not op:
        return ".byte $%02X" % data[i]
    mn, mode, _ill = op
    n = m6502.MODES[mode]
    ob = data[i + 1:i + 1 + n]
    if len(ob) < n:
        return "%s (truncated)" % mn
    v = "$%0*X" % (2 * n, int.from_bytes(bytes(ob), "little")) if n else ""
    return (mn + " " + m6502.FMT[mode].format(v=v)).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("-d", "--dir", default="src")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"],
                    help="what sits at $4000-$7FFF, when the header is wrong")
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"],
                    help="override the mapper the header declares")
    args = ap.parse_args()

    if not os.path.isfile(args.rom):
        sys.exit("verify: no such cartridge: %s" % args.rom)
    if not os.path.isdir(args.dir):
        sys.exit("verify: no such listing directory: %s (generate one with "
                 "disasm.py -o %s)" % (args.dir, args.dir))
    cart = Cart(args.rom, mapper=args.mapper, low=args.low)
    ok = True
    for name in sorted(os.listdir(args.dir)):
        if not name.endswith(".asm"):
            continue
        space = name[:-4]
        path = os.path.join(args.dir, name)
        want = cart.slice(space, cart.base_of(space), cart.size_of(space))
        asm = Assembler()
        src = open(path, encoding="utf-8").read().splitlines()
        try:
            got = asm.assemble(src)
        except AsmError as e:
            print("  %-4s FAIL  %s: %s" % (space, path, e))
            ok = False
            continue
        if got == want:
            print("  %-4s OK    %d bytes reassemble identically" % (space, len(got)))
        else:
            ok = False
            n = min(len(got), len(want))
            first = next((i for i in range(n) if got[i] != want[i]), n)
            print("  %-4s FAIL  size %d vs %d; first difference at +$%04X "
                  "(CPU $%04X): got %s want %s"
                  % (space, len(got), len(want), first,
                     cart.base_of(space) + first,
                     got[first:first + 6].hex(" ") if first < len(got) else "-",
                     want[first:first + 6].hex(" ")))
            hit = item_at(asm.linemap, first)
            if hit:
                start, ln = hit
                print("         %s:%d: %s" % (path, ln, src[ln - 1].strip()))
                print("         got  %s   (at +$%04X)" % (decode(got, start), start))
                print("         want %s" % decode(want, start))
    print("\nROUND-TRIP", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
