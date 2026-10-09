#!/usr/bin/env python3
"""
Rebuild a complete .a78 cartridge image from the generated listings.

Bank N comes from whichever listing covers it:
    a fixed region -> src/f<bank>.asm, or src/rom.asm for an unbanked cart
    a window bank  -> src/bN.asm

The 128-byte a78 header is copied from the reference ROM.

Usage: python build.py <reference.a78> [-d src] [-o build/rebuilt.a78]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disasm import Cart
from asm import Assembler, AsmError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rom")
    ap.add_argument("-d", "--dir", default="src")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"],
                    help="what sits at $4000-$7FFF, when the header is wrong")
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"],
                    help="override the mapper the header declares")
    ap.add_argument("-o", "--out", default="build/rebuilt.a78")
    args = ap.parse_args()

    if not os.path.isfile(args.rom):
        sys.exit("build: no such reference cartridge: %s" % args.rom)
    if not os.path.isdir(args.dir):
        sys.exit("build: no such listing directory: %s" % args.dir)
    cart = Cart(args.rom, mapper=args.mapper, low=args.low)
    # a bankset cartridge is two halves; MARIA's listings are m<space>.asm
    mcart = cart.for_maria() if cart.bankset else None
    banks, mbanks = {}, {}
    for name in sorted(os.listdir(args.dir)):
        if not name.endswith(".asm"):
            continue
        space = name[:-4]
        target, store = cart, banks
        if mcart is not None and space.startswith("m"):
            target, store, space = mcart, mbanks, space[1:]
        b = target.bank_of(space)
        # prefer the listing whose .org matches where the bank really lives
        if b in store and space.startswith("b"):
            continue
        path = os.path.join(args.dir, name)
        try:
            data = Assembler().assemble(
                open(path, encoding="utf-8").read().splitlines())
        except AsmError as e:
            sys.exit("build: %s: %s" % (path, e))
        want = target.size_of(space)
        if len(data) != want:
            print("  %s: %d bytes, expected %d" % (name[:-4], len(data), want))
            return 1
        store[b] = data

    missing = [b for b in range(cart.nbanks) if b not in banks]
    if missing:
        print("missing listings for banks: %s" % missing)
        return 1
    if mcart is not None:
        missing = [b for b in range(mcart.nbanks) if b not in mbanks]
        if missing:
            print("missing listings for MARIA's banks: %s (run disasm.py again)" % missing)
            return 1

    image = b"".join(banks[b] for b in range(cart.nbanks))
    if mcart is not None:
        image += b"".join(mbanks[b] for b in range(mcart.nbanks))
    out = (cart.header_bytes or b"") + image
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    open(args.out, "wb").write(out)

    ref = open(args.rom, "rb").read()
    same = out == ref
    print("wrote %s (%d bytes)" % (args.out, len(out)))
    print("identical to reference ROM:", "YES" if same else "NO")
    if not same:
        n = min(len(out), len(ref))
        i = next((k for k in range(n) if out[k] != ref[k]), n)
        print("  first difference at file offset $%05X" % i)
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
