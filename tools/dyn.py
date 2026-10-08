#!/usr/bin/env python3
"""Turn what a run observed into annotations the disassembler can use.

    python tools/dyn.py game.a78 exectrace.log -c annotations.json
    python tools/init.py game.a78 --dynamic exectrace.log

A disassembler that follows code from the vectors stops where the next step is
decided at run time: a `JMP (ptr)` through RAM, a bank switch whose number is
computed. probes/exectrace.lua watches the real machine take those steps. This
reads its log and writes down what it saw, in the annotations file's own terms:

  entries   where an indirect jump went, and any executed code still not
            reached after that (the first address of each run of it);
  banksw    which banks a computed bank-switch store selected -- a list, which
            is exactly what makes the tracer explore every one of them.

It then re-runs the disassembler and says how much more was reached.

WHAT THIS IS NOT. Everything it adds is OBSERVED, not proven: the run took this
path, and a run takes few. Nothing it adds contradicts what is already in the
file -- a pin or entry you wrote is never replaced -- and each addition is
listed under `_dynamic` with the log it came from, so it can be reviewed and
pruned. The log is only as good as the play behind it: a title screen is not a
game. Record a session and run exectrace.lua over its playback to reach more.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_log(text):
    """{'x': {space: set(addr)}, 'j': [(from, to)], 's': {loc: set(bank)}}."""
    x, j, s = {}, [], {}
    for ln in text.splitlines():
        p = ln.split()
        if not p:
            continue
        if p[0] == "X" and len(p) == 2:
            sp, _, a = p[1].partition(":")
            x.setdefault(sp, set()).add(int(a, 16))
        elif p[0] == "J" and len(p) == 3:
            j.append((p[1], p[2]))
        elif p[0] == "S" and len(p) >= 3:
            s.setdefault(p[1], set()).add(int(p[2]))
    return {"x": x, "j": j, "s": s}


def reached(srcdir):
    """{space: set(addr)} of instructions a disassembly's listings contain."""
    out = {}
    for name in os.listdir(srcdir):
        if not name.endswith(".asm"):
            continue
        sp = name[:-4]
        got = out.setdefault(sp, set())
        for ln in io.open(os.path.join(srcdir, name), encoding="utf-8"):
            m = re.search(r";\s+([0-9A-F]{4}): [0-9A-F]{2}", ln)
            if m and not ln.lstrip().startswith(".byte"):
                got.add(int(m.group(1), 16))
    return out


def disassemble(rom, config, low=None, mapper=None):
    """Run the disassembler; return the listing directory, or None."""
    out = tempfile.mkdtemp(prefix="dyn-")
    cmd = [sys.executable, os.path.join(HERE, "disasm.py"), rom, "-c", config,
           "-o", out]
    if low:
        cmd += ["--low", low]
    if mapper:
        cmd += ["--mapper", mapper]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return out if p.returncode == 0 else None


def runs(addrs, gap=3):
    """First address of each cluster of addresses lying within `gap` bytes."""
    firsts, prev = [], None
    for a in sorted(addrs):
        if prev is None or a - prev > gap:
            firsts.append(a)
        prev = a
    return firsts


def merge(doc, log, missed=None):
    """Add what the log observed to annotations `doc`; return what was added.

    Never replaces anything already there. `missed` is {space: set(addr)} of
    executed instructions the current listing does not reach.
    """
    added = {"entries": [], "banksw": {}}
    have = set(doc.get("entries", []))
    doc.setdefault("entries", [])
    for _src, dst in log["j"]:
        if dst not in have:
            doc["entries"].append(dst)
            have.add(dst)
            added["entries"].append(dst)
    for sp, addrs in sorted((missed or {}).items()):
        for a in runs(addrs):
            loc = "%s:%04X" % (sp, a)
            if loc not in have:
                doc["entries"].append(loc)
                have.add(loc)
                added["entries"].append(loc)
    sw = doc.setdefault("banksw", {})
    pinned = doc.get("bankat", {})
    for loc, banks in sorted(log["s"].items()):
        if loc in sw or loc in pinned:
            continue
        sw[loc] = sorted(banks)
        added["banksw"][loc] = sorted(banks)
    return added


def apply(rom, doc, log, logname, low=None, mapper=None, rounds=4, quiet=False):
    """Merge, re-disassemble, seed what is still missed, until nothing is.

    Returns (doc, summary lines). `doc` is modified in place.
    """
    work = tempfile.mkdtemp(prefix="dyn-cfg-")
    cfg = os.path.join(work, "a.json")
    total = {"entries": [], "banksw": {}}
    lines = []

    def unreached():
        with io.open(cfg, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        out = disassemble(rom, cfg, low, mapper)
        if out is None:
            return None
        got = reached(out)
        miss = {sp: {a for a in addrs if a not in got.get(sp, ())}
                for sp, addrs in log["x"].items()}
        return {sp: a for sp, a in miss.items() if a}

    missed = unreached()                      # where the file stands before us
    if missed is None:
        return None, ["the disassembler does not run on this file as it is"]
    before = sum(len(v) for v in missed.values())
    for i in range(rounds):
        # first what the log says outright (jump targets, bank pins); only if
        # code is still unreached after that, seed the leftovers as entries
        added = merge(doc, log, missed if i else None)
        total["entries"] += added["entries"]
        total["banksw"].update(added["banksw"])
        if not (added["entries"] or added["banksw"]) and i:
            break
        missed = unreached()
        if missed is None:
            return None, ["the disassembler failed after merging; nothing kept"]
        if not missed:
            break
    n = sum(len(v) for v in log["x"].values())
    left = sum(len(v) for v in (missed or {}).values())
    note = doc.setdefault("_dynamic", {})
    note[logname] = {
        "observed_not_proven": True,
        "entries": total["entries"], "banksw": total["banksw"],
        "executed": n, "still_unreached": left,
    }
    lines.append("%d distinct instructions executed; %d reached by the "
                 "disassembler (%d unreached before, %d after)"
                 % (n, n - left, before, left))
    if total["entries"]:
        lines.append("  %d entry point%s added: %s" % (
            len(total["entries"]), "" if len(total["entries"]) == 1 else "s",
            ", ".join(total["entries"][:8]) + (" ..." if len(total["entries"]) > 8 else "")))
    for loc, banks in sorted(total["banksw"].items()):
        lines.append("  bank switch %s -> banks %s" % (loc, ", ".join(map(str, banks))))
    if left:
        lines.append("  %d executed instructions are still not reached; look at "
                     "them with disasm.py --gaps" % left)
    return doc, lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("log", help="a log from probes/exectrace.lua")
    ap.add_argument("-c", "--config", required=True,
                    help="the annotations file to add to (written in place)")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be added; write nothing")
    args = ap.parse_args(argv)
    for p in (args.rom, args.log, args.config):
        if not os.path.isfile(p):
            sys.exit("dyn: no such file: %s" % p)
    with io.open(args.config, encoding="utf-8") as f:
        doc = json.load(f)
    log = parse_log(io.open(args.log, encoding="utf-8").read())
    doc, lines = apply(args.rom, doc, log, os.path.basename(args.log),
                       args.low, args.mapper)
    print("\n".join(lines))
    if doc is None:
        return 1
    if not args.dry_run:
        with io.open(args.config, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print("wrote %s" % args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
