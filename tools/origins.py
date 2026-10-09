#!/usr/bin/env python3
"""Which bytes of a cartridge are addresses, from a run of probes/addrorigin.lua.

    python tools/origins.py addrorigin.log game.a78
    python tools/origins.py addrorigin.log game.a78 -c annotations.json

addrorigin.lua follows every address the CPU uses back to the bytes it was built
from. This groups what it found:

  tables     address tables in the ROM: two bytes side by side (a table of words),
             or a table of low bytes and another of high bytes a fixed distance
             apart, with every entry the run used and the range of targets it
             reached. With -c, each becomes a block in the annotations file
             (`words` for the first kind, a note on two `bytes` blocks for the
             second) so the listing shows them as addresses.
  immediates addresses written into the code itself, `LDA #<routine`: the operand
             bytes, which is what has to change if the routine moves
  computed   addresses built in RAM from nothing the run could trace

WHAT THIS IS NOT. Observed, not proven: only entries the run used are found, so a
table is reported as far as it was read and may be longer; a pointer built by
arithmetic from two sources is credited to one. Nothing already in the annotations
file is replaced, and every block added is listed under `_dynamic`.
"""
import argparse
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LINE = re.compile(r"^P (\w+) (\S+) (\S+) (\S+) x(\d+) ([0-9A-F]{4}) ([0-9A-F]{4})$")


def split_loc(text):
    sp, _, a = text.partition(":")
    return sp, int(a, 16)


def parse_log(text):
    """({(kind, pc, hi, lo): (n, tmin, tmax)}, set of immediate-operand origins)."""
    uses, imms = {}, set()
    for ln in text.splitlines():
        m = LINE.match(ln.strip())
        if m:
            kind, pc, hi, lo, n, t0, t1 = m.groups()
            uses[(kind, pc, hi, lo)] = (int(n), int(t0, 16), int(t1, 16))
        elif ln.startswith("I "):
            imms.add(ln[2:].strip())
    return uses, imms


def is_rom(cart, loc):
    sp, a = split_loc(loc)
    if sp == "r":
        return False
    try:
        return bool(cart.in_space(sp, a))
    except KeyError:
        return False


def tables(pairs):
    """Group (lo origin, hi origin) pairs into address tables.

    Returns ([{"shape": "words"|"split", "lo": (space, addr), "hi": (space, addr),
    "n": entries, "step": stride, "targets": (min, max)}], isolated) where
    `isolated` counts pairs that make no run of two or more. A pair whose halves
    are adjacent (hi = lo + 1) is a word; runs of those at a stride of two form one
    table. A pair whose halves are in the same space a fixed distance apart is a
    split table; runs of those at a constant stride of one or two (two when each
    entry is followed by another field) form one. A byte shared by many pairs on
    one side is a constant, not a table, and is left out.
    """
    lo_use, hi_use = {}, {}
    for lo, hi in pairs:
        lo_use.setdefault(lo, set()).add(hi)
        hi_use.setdefault(hi, set()).add(lo)
    words, split = {}, {}
    for (lo, hi), tgt in pairs.items():
        (sl, al), (sh, ah) = split_loc(lo), split_loc(hi)
        if sl != sh:
            continue
        if ah == al + 1:
            words[(sl, al)] = tgt
        elif ah != al and len(lo_use[lo]) == 1 and len(hi_use[hi]) == 1:
            split.setdefault((sl, ah - al), {})[al] = tgt
    out, isolated = [], 0

    def runs(addrs, steps):
        addrs = sorted(addrs)
        i = 0
        while i < len(addrs):
            best = [addrs[i]]
            for st in steps:
                run = [addrs[i]]
                for a in addrs[i + 1:]:
                    if a - run[-1] == st:
                        run.append(a)
                    elif a - run[-1] > st:
                        break
                if len(run) > len(best):
                    best = run
            yield best
            i += len(best)

    def add(shape, sp, dist, d, steps):
        nonlocal isolated
        for run in runs(d, steps):
            if len(run) < 2:
                isolated += 1
                continue
            ts = [d[a] for a in run]
            out.append({"shape": shape, "lo": (sp, run[0]),
                        "hi": (sp, run[0] + dist), "n": len(run),
                        "step": run[1] - run[0],
                        "targets": (min(t[0] for t in ts), max(t[1] for t in ts))})

    by_space = {}
    for (sp, a), tgt in words.items():
        by_space.setdefault(sp, {})[a] = tgt
    for sp, d in sorted(by_space.items()):
        add("words", sp, 1, d, (2,))
    for (sp, dist), d in sorted(split.items()):
        add("split", sp, dist, d, (1, 2))
    return out, isolated


def analyse(uses, imms, cart):
    """{"tables": [...], "immediates": [(loc, kind)], "computed": [(kind, pc, hi, lo)]}."""
    pairs, immediates, computed = {}, {}, []
    for (kind, pc, hi, lo), (n, t0, t1) in sorted(uses.items()):
        if hi in imms and lo in imms:
            immediates[lo] = immediates[hi] = kind
            continue
        if is_rom(cart, hi) and is_rom(cart, lo):
            old = pairs.get((lo, hi))
            pairs[(lo, hi)] = (min(t0, old[0]) if old else t0,
                               max(t1, old[1]) if old else t1)
        else:
            computed.append((kind, pc, hi, lo))
    found, isolated = tables(pairs)
    return {"tables": found, "isolated": isolated,
            "immediates": sorted(immediates.items()), "computed": computed}


def describe(t):
    sp, a = t["lo"]
    if t["shape"] == "words":
        return "%s:%04X  %d word%s (%d bytes), targets $%04X-$%04X" % (
            sp, a, t["n"], "" if t["n"] == 1 else "s", 2 * t["n"], *t["targets"])
    return ("%s:%04X low bytes and %s:%04X high bytes, %d entr%s%s, targets "
            "$%04X-$%04X" % (sp, a, t["hi"][0], t["hi"][1], t["n"],
                             "y" if t["n"] == 1 else "ies",
                             ", every other byte" if t["step"] == 2 else "",
                             *t["targets"]))


def merge(doc, found, logname):
    """Add a block for each table the file does not already cover; return them."""
    added = []
    have = doc.setdefault("blocks", [])

    def covered(loc, n):
        sp, a = split_loc(loc)
        for b in have:
            try:
                bs, ba = split_loc(b["loc"])
                blen = b.get("len")
                if blen is None and "end" in b:
                    blen = split_loc(b["end"])[1] - ba
            except (KeyError, ValueError):
                continue
            if bs == sp and blen and ba < a + n and a < ba + blen:
                return True
        return False

    for t in found["tables"]:
        sp, a = t["lo"]
        if t["shape"] == "words":
            loc, n = "%s:%04X" % (sp, a), 2 * t["n"]
            if covered(loc, n):
                continue
            have.append({"loc": loc, "len": n, "type": "words",
                         "note": "address table, as far as the run read it"})
            added.append(describe(t))
        else:
            fresh = False
            for who, (s2, a2) in (("low", t["lo"]), ("high", t["hi"])):
                loc = "%s:%04X" % (s2, a2)
                span = t["n"] * t["step"] - (t["step"] - 1)
                if covered(loc, span):
                    continue
                have.append({"loc": loc, "len": span, "type": "bytes",
                             "note": "%s bytes of an address table, as far as the "
                                     "run read it" % who})
                fresh = True
            if fresh:
                added.append(describe(t))
    if added:
        note = doc.setdefault("_dynamic", {})
        note[logname] = {"observed_not_proven": True, "tables": added}
    return added


def report(found):
    out = []
    t = found["tables"]
    out.append("Address tables in the ROM: %d%s" % (len(t), (
        " (and %d address%s read from the ROM that form no table of two or more "
        "entries)" % (found["isolated"], "" if found["isolated"] == 1 else "es"))
        if found["isolated"] else ""))
    out += ["  " + describe(x) for x in t]
    im = found["immediates"]
    out.append("Addresses written into the code (immediates): %d bytes" % len(im))
    out += ["  %s  (%s)" % (loc, kind) for loc, kind in im[:20]]
    if len(im) > 20:
        out.append("  ... %d more" % (len(im) - 20))
    c = found["computed"]
    out.append("Addresses built in RAM from nothing traced: %d" % len(c))
    out += ["  %s at %s: halves from %s and %s" % x for x in
            ((k, pc, hi, lo) for k, pc, hi, lo in c[:20])]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="a log from probes/addrorigin.lua")
    ap.add_argument("rom")
    ap.add_argument("-c", "--config", help="annotations file to add blocks to "
                                           "(written in place)")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    for p in (args.log, args.rom) + ((args.config,) if args.config else ()):
        if not os.path.isfile(p):
            sys.exit("origins: no such file: %s" % p)
    import cart as C
    cart = C.Cart(args.rom, mapper=args.mapper, low=args.low)
    uses, imms = parse_log(io.open(args.log, encoding="utf-8").read())
    if not uses:
        sys.exit("origins: %s holds no address uses. Was the run long enough, and "
                 "did the game get past the BIOS?" % args.log)
    found = analyse(uses, imms, cart)
    print("\n".join(report(found)))
    if args.config:
        import annotations
        doc, nl = annotations.read_json_keep(args.config)
        added = merge(doc, found, os.path.basename(args.log))
        print("%d table%s added to the annotations" % (len(added), "" if len(added) == 1 else "s"))
        if added and not args.dry_run:
            annotations.write_json_keep(args.config, doc, nl)
            print("wrote %s" % args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
