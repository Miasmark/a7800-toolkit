#!/usr/bin/env python3
"""Measure the toolkit against a whole library of cartridges.

    python tools/corpus.py /path/to/roms --frames 300 --jobs 4 --cache corpus-cache
    python tools/corpus.py /path/to/roms --filter "(ntsc)" --sample 40
    python tools/corpus.py --report corpus-cache          # summarise a finished run

For every cartridge it does two things and compares them:

  * the STATIC pass: `disasm.py`'s tracer from the vectors, no annotations -- what the
    toolkit finds with nothing to go on;
  * the DYNAMIC pass: the simulator runs the cartridge for `--frames` frames
    (`simprobe.py`) and records the instructions that executed and the ROM bytes read
    as data.

What actually ran is ground truth for code, and what was read as data is ground truth for
data, so the two passes grade each other:

  recall        executed instructions the static pass also reached. The rest are code
                the tracer missed (an indirect jump, a computed bank switch, an `RTS`
                trick, code in a bank nothing switches to).
  assisted      the same, after adding what the run observed -- every indirect-jump
                target as an entry, every bank switch as a pin (what `dyn.py` does).
  data in code  ROM bytes the CPU read as data that the static pass printed as
                *instructions*. Tables mis-read as code, or code read as a table.
  data in gaps  bytes read as data that the static pass left unexplained -- tables the
                listing shows only as `.byte` runs, now known to be tables.

It also says how far the simulator got: how many cartridges reached a display list, and
which ones stalled. A stall is a cartridge waiting for hardware the simulator does not
provide, so this is the measure of how far the simulator can stand in for MAME.

Results are cached per cartridge (by content) so a change to the tools can be re-measured
without re-running what has not changed; `--force` ignores the cache. A full pack at 300
frames takes the better part of an hour on four cores; `--sample N` gives a stable subset.

This reads the library but never copies, edits or republishes anything in it. The cache
holds counts and addresses -- no cartridge bytes.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VERSION = 5          # bump when the measure changes, so old cache entries are not reused


def find_roms(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            if f.lower().endswith(".a78"):
                out.append(os.path.join(d, f))
    return out


def _bytes_of(an, cart, spaces):
    """{space: set(addr)} covered by instructions the static pass found."""
    out = {}
    for sp in spaces:
        out[sp] = set()
    for (sp, a) in an.code:
        n = an.insn[(sp, a)][3]
        for i in range(n):
            out[sp].add(a + i)
    return out


FILL_OPCODES = (0x00, 0xFF, 0xEA)       # BRK, an erased ROM, NOP: what fill is made of


def _sled(cart, executed, minrun=16):
    """Executed locations that lie in a run of `minrun` or more of the same FILL
    instruction following one another (BRK over zeros, $FF, NOP). Not any repeated opcode:
    an unrolled run of `STA abs` is code someone wrote."""
    import m6502
    by = {}
    for sp, a in executed:
        by.setdefault(sp, []).append(a)
    out = set()
    for sp, addrs in by.items():
        addrs.sort()
        run = []
        prev = None
        for a in addrs:
            try:
                op = cart.byte(sp, a)
                n = 2 if op == 0 else m6502.LENGTH[op]     # BRK skips a signature byte
            except Exception:                                  # noqa: BLE001
                continue
            if op not in FILL_OPCODES:
                if len(run) >= minrun:
                    out.update((sp, x) for x in run)
                run, prev = [], None
                continue
            if prev is not None and prev[0] + prev[2] == a and prev[1] == op:
                run.append(a)
            else:
                if len(run) >= minrun:
                    out.update((sp, x) for x in run)
                run = [a]
            prev = (a, op, n)
        if len(run) >= minrun:
            out.update((sp, x) for x in run)
    return out


def measure(path, frames=300, drive=True):
    """One cartridge's record (a dict of plain numbers and strings)."""
    import m6502
    import disasm
    import dyn
    import simprobe
    import sim
    rec = {"name": os.path.basename(path), "ok": False}
    try:
        cart = disasm.Cart(path)
    except Exception as e:                                    # noqa: BLE001
        rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:100])
        return rec
    spaces = cart.spaces()
    rec["size"] = len(cart.rom)
    rec["mapper"] = cart.map.name
    rec["rom_bytes"] = sum(cart.size_of(s) for s in spaces)
    t = time.time()
    try:
        an, _g, _w, _v = disasm.analyse(cart, disasm.Config())
    except Exception as e:                                    # noqa: BLE001
        rec["error"] = "static: %s: %s" % (type(e).__name__, str(e)[:100])
        return rec
    static = _bytes_of(an, cart, spaces)
    rec["static"] = {"instructions": len(an.code),
                     "bytes": sum(len(v) for v in static.values()),
                     "unresolved_switches": sum(1 for b in an.bankswitch.values() if b is None),
                     "illegal_stops": len(an.illegal_stops)}
    # the run
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    col = simprobe.Collector(dump_frame=frames)
    try:
        bus = sim.run(cart, frames, region, drive=drive, observer=col)
    except Exception as e:                                    # noqa: BLE001
        rec["error"] = "run: %s: %s" % (type(e).__name__, str(e)[:100])
        return rec
    rec["run"] = {"frames": frames, "frames_with_display_list": col.frames_with_list,
                  "instructions": len(col.x), "data_bytes": len(col.d),
                  "indirect_jumps": len(col.j), "bank_switches": len(col.s),
                  "stuck_fetches": col.stuck, "audio_writes": len(bus.writes),
                  "seconds": round(time.time() - t, 1)}
    # recall: executed instructions the static pass reached
    missed = sorted(loc for loc in col.x if loc not in an.code)
    # A sled is a long run of the same instruction executed back to back: the CPU walking
    # zero fill (BRK, whose handler is an RTI) or $EA fill. It is real execution but not
    # code anyone wrote, and a tracer is right not to follow it, so it is left out of the
    # score and reported on its own.
    sled = _sled(cart, col.x)
    missed = [l for l in missed if l not in sled]
    rec["sled"] = len(sled)
    rec["recall"] = {"executed": len(col.x) - len(sled),
                     "reached": len(col.x) - len(sled) - len(missed)}
    rec["missed_examples"] = ["%s:%04X" % l for l in missed[:6]]
    # why: for every missed location execution did not reach from another missed one,
    # how did it arrive? (the instruction that ran before it, or RAM, or an interrupt)
    missed_set = set(missed)
    why, ex = {}, {}
    for loc in missed:
        how = col.arrival.get(loc, "?")
        if col.pred.get(loc) in missed_set:
            continue                  # still inside a run that started elsewhere
        why[how] = why.get(how, 0) + 1
        ex.setdefault(how, []).append("%s:%04X" % loc)
    rec["missed_how"] = why
    rec["missed_how_examples"] = {k: v[:3] for k, v in ex.items()}
    # assisted: add what the run observed, as dyn.py does. Two scores. "In sample" uses
    # everything the run saw to explain everything the run executed, which is circular --
    # a ceiling. "Held out" takes the jumps and bank switches seen in the FIRST HALF of the
    # run and asks how much of the WHOLE run's code they explain, so the code the evidence
    # could not have come from is in the score.
    def assist(limit):
        log = {"x": {}, "j": [("%s:%04X" % a, "%s:%04X" % b) for (a, b) in col.j
                              if limit is None or col.jfirst.get((a, b), 0) <= limit],
               "s": {}}
        for (loc, bank) in col.s:
            if limit is None or col.sfirst.get((loc, bank), 0) <= limit:
                log["s"].setdefault("%s:%04X" % loc, set()).add(bank)
        doc = {}
        dyn.merge(doc, log)
        an2, _g, _w, _v = disasm.analyse(cart, disasm.Config(data=doc))
        still = sum(1 for loc in col.x if loc not in an2.code and loc not in sled)
        return rec["recall"]["executed"] - still
    try:
        rec["recall"]["assisted_reached"] = assist(None)
        rec["recall"]["heldout_reached"] = assist(frames // 2)
    except Exception as e:                                    # noqa: BLE001
        rec["recall"]["assisted_error"] = str(e)[:80]
    # data reads against the static classification
    # A byte both read and executed is code the program also reads (a checksum, a copy): not
    # evidence of false code. Read and NEVER executed, yet printed as an instruction, is.
    ran = set()
    for (sp, a) in col.x:
        try:
            n = m6502.LENGTH[cart.byte(sp, a)]
        except Exception:                                     # noqa: BLE001
            n = 1
        ran.update((sp, a + k) for k in range(n))
    in_code = in_gap = false_code = 0
    for (sp, a) in col.d:
        if a in static.get(sp, ()):
            in_code += 1
            if (sp, a) not in ran:
                false_code += 1
        else:
            in_gap += 1
    rec["data"] = {"read": len(col.d), "in_static_code": in_code, "in_gap": in_gap,
                   "false_code_bytes": false_code}
    rec["ok"] = True
    return rec


def _work(item):
    path, frames, drive, cache, force = item
    with open(path, "rb") as f:
        digest = hashlib.sha1(f.read()).hexdigest()[:16]
    cpath = None
    if cache:
        cpath = os.path.join(cache, "%s-%d-%d-v%d.json" % (digest, frames, int(drive), VERSION))
        if not force and os.path.isfile(cpath):
            with open(cpath) as f:
                return json.load(f)
    rec = measure(path, frames, drive)
    rec["sha1"] = digest
    if cpath:
        os.makedirs(cache, exist_ok=True)
        with open(cpath, "w") as f:
            json.dump(rec, f)
    return rec


def load_cache(cache, frames=None):
    out = []
    for f in sorted(os.listdir(cache)):
        if f.endswith("-v%d.json" % VERSION):
            with open(os.path.join(cache, f)) as fh:
                r = json.load(fh)
            if frames is None or r.get("run", {}).get("frames") == frames or not r.get("ok"):
                out.append(r)
    return out


def pct(a, b):
    return "%5.1f%%" % (100.0 * a / b) if b else "   -  "


def report(recs, worst=15):
    n = len(recs)
    ok = [r for r in recs if r["ok"]]
    bad = [r for r in recs if not r["ok"]]
    print("%d cartridges: %d measured, %d not (could not be laid out or the run failed)"
          % (n, len(ok), len(bad)))
    errs = {}
    for r in bad:
        k = r.get("error", "?").split(":")[0:2]
        errs[": ".join(k)] = errs.get(": ".join(k), 0) + 1
    for k, v in sorted(errs.items(), key=lambda kv: -kv[1])[:6]:
        print("    %3d  %s" % (v, k))
    if not ok:
        return
    reached = [r for r in ok if r["run"]["frames_with_display_list"] >= 0.5 * r["run"]["frames"]]
    print("\nTHE SIMULATOR (how far it can stand in for MAME)")
    print("  reached a display list on at least half its frames: %d of %d (%s)"
          % (len(reached), len(ok), pct(len(reached), len(ok)).strip()))
    stalled = [r for r in ok if r not in reached]
    for r in sorted(stalled, key=lambda r: r["run"]["frames_with_display_list"])[:8]:
        print("    never got going: %-52s %3d of %d frames, %d instructions"
              % (r["name"][:52], r["run"]["frames_with_display_list"], r["run"]["frames"],
                 r["run"]["instructions"]))
    print("  median run: %d distinct instructions, %.1f s"
          % (sorted(r["run"]["instructions"] for r in ok)[len(ok) // 2],
             sorted(r["run"]["seconds"] for r in ok)[len(ok) // 2]))
    print("\nCODE: executed instructions the static tracer reached (over cartridges that got going)")
    use = reached
    tot = sum(r["recall"]["executed"] for r in use)
    got = sum(r["recall"]["reached"] for r in use)
    ass = sum(r["recall"].get("assisted_reached", r["recall"]["reached"]) for r in use)
    held = sum(r["recall"].get("heldout_reached", r["recall"].get("assisted_reached",
                                                                 r["recall"]["reached"]))
               for r in use)
    print("  all instructions pooled   static %s   with what the run observed %s "
          "(in sample: a ceiling)   held out %s   (%d executed)"
          % (pct(got, tot), pct(ass, tot), pct(held, tot), tot))
    medians = sorted(100.0 * r["recall"]["reached"] / r["recall"]["executed"]
                     for r in use if r["recall"]["executed"])
    if medians:
        print("  per cartridge, the median static recall is %.1f%% (pooled figures are "
              "dominated by the largest runs)" % medians[len(medians) // 2])
    sl = [r.get("sled", 0) for r in use]
    if any(sl):
        print("  left out as sleds (the CPU walking fill, not code): %d instructions in %d cartridges"
              % (sum(sl), sum(1 for x in sl if x)))
    per = [(r["recall"]["reached"] / float(r["recall"]["executed"]) if r["recall"]["executed"] else 1.0, r)
           for r in use]
    for lo, hi in ((0.999, 1.01), (0.99, 0.999), (0.95, 0.99), (0.8, 0.95), (0, 0.8)):
        k = sum(1 for p, _r in per if lo <= p < hi)
        print("    %5.1f%%-%5.1f%% reached: %4d cartridges" % (100 * lo, min(100, 100 * hi), k))
    print("  worst, with the first addresses missed:")
    for p, r in sorted(per, key=lambda t: t[0])[:worst]:
        print("    %5.1f%%  %-50s missed %5d  e.g. %s"
              % (100 * p, r["name"][:50], r["recall"]["executed"] - r["recall"]["reached"],
                 " ".join(r["missed_examples"][:3])))
    how = {}
    for r in use:
        for k, v in r.get("missed_how", {}).items():
            how[k] = how.get(k, 0) + v
    if how:
        print("  where the missed code is entered from (one count per run of missed code):")
        for k, v in sorted(how.items(), key=lambda kv: -kv[1]):
            carts = sum(1 for r in use if r.get("missed_how", {}).get(k))
            print("    %-8s %5d  in %3d cartridges" % (k, v, carts))
    print("\nDATA: ROM bytes the CPU read as data")
    dr = sum(r["data"]["read"] for r in use)
    inc = sum(r["data"]["in_static_code"] for r in use)
    ing = sum(r["data"]["in_gap"] for r in use)
    print("  %d bytes read; %d (%s) lie inside what the static pass printed as instructions, "
          "%d (%s) in gaps" % (dr, inc, pct(inc, dr).strip(), ing, pct(ing, dr).strip()))
    fc = sum(r["data"].get("false_code_bytes", 0) for r in use)
    print("  of those, %d were never executed: printed as instructions yet read as data, which "
          "is the measured lower bound on false code (the rest are code the program also "
          "reads: checksums, copies)" % fc)
    worstd = sorted(use, key=lambda r: -r["data"].get("false_code_bytes", 0))[:8]
    print("  most false code (read as data, never executed, listed as instructions):")
    for r in worstd:
        print("    %5d bytes  %s" % (r["data"].get("false_code_bytes", 0), r["name"][:60]))
    sw = sum(r["static"]["unresolved_switches"] for r in ok)
    print("\nstatic bank-switch sites it could not resolve: %d across %d cartridges"
          % (sw, sum(1 for r in ok if r["static"]["unresolved_switches"])))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", help="a folder of .a78 files (searched recursively)")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--cache", default="corpus-cache")
    ap.add_argument("--filter", action="append", default=[],
                    help="keep cartridges whose path contains this text (repeatable, all must match); "
                         "prefix with ! to exclude")
    ap.add_argument("--sample", type=int, help="a random subset of this size (seeded)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-drive", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--report", metavar="CACHE", help="summarise a cache and exit")
    args = ap.parse_args(argv)
    if args.report:
        report(load_cache(args.report))
        return 0
    if not args.root or not os.path.isdir(args.root):
        ap.error("give a folder of cartridges (or --report CACHE)")
    roms = find_roms(args.root)
    for f in args.filter:
        if f.startswith("!"):
            roms = [r for r in roms if f[1:].lower() not in r.lower()]
        else:
            roms = [r for r in roms if f.lower() in r.lower()]
    if args.sample and args.sample < len(roms):
        random.Random(args.seed).shuffle(roms)
        roms = sorted(roms[:args.sample])
    if not roms:
        sys.exit("corpus: no cartridges found")
    print("%d cartridges, %d frames each, %d workers" % (len(roms), args.frames, args.jobs))
    items = [(r, args.frames, not args.no_drive, args.cache, args.force) for r in roms]
    recs, start = [], time.time()
    with multiprocessing.Pool(args.jobs) as pool:
        for i, rec in enumerate(pool.imap_unordered(_work, items), 1):
            recs.append(rec)
            if i % 10 == 0 or i == len(items):
                el = time.time() - start
                sys.stderr.write("  %d/%d  %.0f s elapsed, ~%.0f s to go\n"
                                 % (i, len(items), el, el / i * (len(items) - i)))
    report(recs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
