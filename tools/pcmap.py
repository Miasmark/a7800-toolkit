#!/usr/bin/env python3
"""Name the routines a profile spent its time in.

    python tools/pcmap.py pcprof.log -c annotations.json
    python tools/pcmap.py pcprof.log --listing src          # labels from a disassembly
    python tools/pcmap.py pcprof.log --symbols names.json   # {"name": "f7:C000", ...}

probes/pcprof.lua writes `space:pc count` lines: where the CPU was, sampled once
per display zone. This groups them by the routine each address belongs to --
the nearest label at or below it in the same space -- and ranks them.

Labels come from, in order of preference: your annotations file (the names you
chose), the listing a disassembly wrote (its generated `sub_C074` / `L_C027`
names), or a symbols file. Give any of them; later ones fill what earlier ones
lack. An address below every label in its space is reported as `space:pc?`.

"Nearest label below" is a guess about where a routine ends: a routine with no
label of its own after it absorbs whatever follows. Label the entry points you
care about and the ranking sharpens; the percentages are only as good as the
labels. A sample is where the CPU was, not what it cost (see pcprof.lua's limits).
"""
import argparse
import bisect
import io
import json
import os
import re
import sys


def parse_samples(text):
    """{(space, pc): count} from pcprof.lua's output."""
    out = {}
    for ln in text.splitlines():
        p = ln.split()
        if len(p) != 2 or ":" not in p[0]:
            continue
        sp, a = p[0].split(":")
        out[(sp, int(a, 16))] = out.get((sp, int(a, 16)), 0) + int(p[1])
    return out


def labels_from_annotations(doc):
    out = {}
    for loc, name in (doc.get("labels") or {}).items():
        try:
            sp, a = loc.split(":")
            a = a.strip().lstrip("$")
            a = a[2:] if a.lower().startswith("0x") else a
            out[(sp, int(a, 16))] = name
        except ValueError:
            continue
    return out


def labels_from_listing(path):
    """Labels in the .asm files a disassembly wrote: name: on a line by itself,
    its address taken from the next instruction or data line."""
    out = {}
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".asm"):
            continue
        sp, pending = fn[:-4], []
        for ln in io.open(os.path.join(path, fn), encoding="utf-8"):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*$", ln)
            if m:
                pending.append(m.group(1))
                continue
            m = re.search(r";\s+([0-9A-F]{4})[: ]", ln)
            if m and pending:
                for name in pending:
                    out.setdefault((sp, int(m.group(1), 16)), name)
                pending = []
    return out


def labels_from_symbols(doc):
    return labels_from_annotations({"labels": {v: k for k, v in doc.items()}})


def profile(samples, labels):
    """[(routine name, count, {pc: count})] biggest first."""
    by_space = {}
    for (sp, a), name in labels.items():
        by_space.setdefault(sp, []).append((a, name))
    for v in by_space.values():
        v.sort()
    starts = {sp: [a for a, _n in v] for sp, v in by_space.items()}
    groups = {}
    for (sp, pc), n in samples.items():
        i = bisect.bisect_right(starts.get(sp, []), pc) - 1
        if i < 0:
            key = ("%s:%04X?" % (sp, pc), sp, pc)
        else:
            a, name = by_space[sp][i]
            key = (name, sp, a)
        g = groups.setdefault(key, [0, {}])
        g[0] += n
        g[1][pc] = g[1].get(pc, 0) + n
    return sorted(((k[0], k[1], k[2], v[0], v[1]) for k, v in groups.items()),
                  key=lambda r: -r[3])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="a log from probes/pcprof.lua")
    ap.add_argument("-c", "--config", help="annotations.json (its labels)")
    ap.add_argument("--listing", help="a directory of disassembly listings")
    ap.add_argument("--symbols", help="JSON {name: 'space:addr'}")
    ap.add_argument("-n", "--top", type=int, default=20, help="rows to show")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.log):
        sys.exit("pcmap: no such log: %s" % args.log)
    samples = parse_samples(io.open(args.log, encoding="utf-8").read())
    if not samples:
        sys.exit("pcmap: no samples in %s" % args.log)
    labels = {}
    for src in ("config", "listing", "symbols"):
        path = getattr(args, src)
        if not path:
            continue
        if not os.path.exists(path):
            sys.exit("pcmap: no such %s: %s" % (src, path))
        if src == "listing":
            got = labels_from_listing(path)
        else:
            doc = json.load(io.open(path, encoding="utf-8"))
            got = labels_from_annotations(doc) if src == "config" else labels_from_symbols(doc)
        for k, v in got.items():
            labels.setdefault(k, v)
    if not labels:
        sys.stderr.write("pcmap: no labels given (-c, --listing or --symbols); "
                         "showing raw addresses.\n")
    total = float(sum(samples.values()))
    print("%d samples, %d routines" % (total, len(profile(samples, labels))))
    print("%-34s %-8s %8s %7s  %s" % ("routine", "at", "samples", "share", "hottest addresses"))
    for name, sp, a, n, pcs in profile(samples, labels)[:args.top]:
        hot = sorted(pcs.items(), key=lambda kv: -kv[1])[:3]
        print("%-34s %-8s %8d %6.1f%%  %s" % (
            name[:34], "%s:%04X" % (sp, a), n, 100.0 * n / total,
            " ".join("%04X(%d%%)" % (pc, 100.0 * c / n) for pc, c in hot)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
