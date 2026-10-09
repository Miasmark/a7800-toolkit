#!/usr/bin/env python3
"""Which cartridges MAME runs, which the simulator runs, and where they disagree.

    python tools/mamecheck.py /path/to/roms --sample 60 --jobs 3 --cache mamecheck-cache
    python tools/mamecheck.py game.a78
    python tools/mamecheck.py --report mamecheck-cache

Every comparison this toolkit makes between the simulator and MAME assumes MAME can run
the image at all. This checks it, over a folder or a sample of one: each cartridge is run
in MAME with `probes/rendersurvey.lua` (headless) and in the simulator, and both are asked
the same question -- after the BIOS hands over, is MARIA given a display list and kept
drawing from it? -- plus how many display-list interrupts each raised.

  both        both reached a live display list: the image runs in each.
  sim only    the simulator did and MAME did not. Either MAME cannot run this image
              (a mapper or flag it does not implement) or it is stuck early; the
              comparison against MAME for this image means nothing.
  mame only   MAME did and the simulator did not: a gap in the simulator, and the list
              of what to fix next.
  neither     neither did: a cartridge that needs input, a demo that does not draw, or
              an image that is wrong.

"Live" is at least half the frames in the window with MARIA's DMA on and the list pointer
in RAM. The BIOS leaves a list of its own in place until it hands over, so MAME's window
starts after the hand-over (frame 330); the simulator starts at the cartridge's reset.
Results are cached by file hash, so a run over the whole library can be stopped and resumed.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VERSION = 1
HANDOVER = 330            # MAME frame after which the cartridge is running (OpenBIOS)
WINDOW = 360              # frames judged, in each engine


def live_frames(rows):
    """(frames, live) from (dpph, dppl, ctrl) per frame: DMA on and a list pointer in RAM."""
    live = 0
    for dpph, _dppl, ctrl in rows:
        if (ctrl & 0x60) == 0x40 and 0x18 <= dpph <= 0x27:
            live += 1
    return len(rows), live


def parse_survey(path):
    """[(frame, dli, dpph, dppl, ctrl)] from rendersurvey's csv."""
    rows = []
    with open(path) as f:
        next(f, None)
        for ln in f:
            p = ln.strip().split(",")
            if len(p) >= 6:
                rows.append((int(p[0]), int(p[1]), int(p[3]), int(p[4]), int(p[5])))
    return rows


def run_mame(rom, work, seconds=40):
    """MAME's side: {"ran", "frames", "live", "dli", "why"}."""
    import runprobe
    frames = HANDOVER + WINDOW + 10
    env = {"A7800_RS_OUT": os.path.join(work, "survey"), "A7800_RS_FRAMES": str(frames)}
    ok, text, written = runprobe.run(rom, "rendersurvey", work, seconds=seconds, env=env)
    if not ok:
        return {"ran": False, "why": text[:120]}
    csv = os.path.join(work, "survey-frames.csv")
    if not os.path.isfile(csv):
        return {"ran": False, "why": "MAME wrote no frame log"}
    rows = [r for r in parse_survey(csv) if r[0] > HANDOVER][:WINDOW]
    n, live = live_frames([(r[2], r[3], r[4]) for r in rows])
    return {"ran": True, "frames": n, "live": live, "dli": sum(1 for r in rows if r[1]),
            "why": ""}


def run_sim(rom):
    """The simulator's side, over the same number of frames."""
    import cart as cart_module
    import sim
    import simprobe
    cart = cart_module.Cart(rom)
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    col = simprobe.Collector()
    t = time.time()
    sim.run(cart, WINDOW, region, drive=True, observer=col)
    return {"ran": True, "frames": WINDOW, "live": col.frames_with_list,
            "dli": col.nmis, "seconds": round(time.time() - t, 1), "why": ""}


def measure(rom):
    rec = {"name": os.path.basename(rom), "ok": False}
    work = tempfile.mkdtemp(prefix="mamecheck-")
    try:
        try:
            import cart as cart_module
            cart = cart_module.Cart(rom)
            rec["mapper"] = cart.map.name
            rec["bankset"] = cart.bankset
            rec["region"] = (cart.info or {}).get("region", "NTSC")
            rec["flags"] = (cart.info or {}).get("flags", [])
        except Exception as e:                                # noqa: BLE001
            rec["error"] = "layout: %s: %s" % (type(e).__name__, str(e)[:100])
            return rec
        try:
            rec["mame"] = run_mame(rom, work)
        except Exception as e:                                # noqa: BLE001
            rec["mame"] = {"ran": False, "why": "%s: %s" % (type(e).__name__, str(e)[:100])}
        try:
            rec["sim"] = run_sim(rom)
        except Exception as e:                                # noqa: BLE001
            rec["sim"] = {"ran": False, "why": "%s: %s" % (type(e).__name__, str(e)[:100])}
        rec["ok"] = True
        return rec
    finally:
        import shutil
        shutil.rmtree(work, True)


def is_live(side):
    return bool(side.get("ran")) and side.get("frames", 0) > 0 and \
        side["live"] >= 0.5 * side["frames"]


def verdict(rec):
    m, s = is_live(rec["mame"]), is_live(rec["sim"])
    return ("both" if m and s else "sim only" if s else "mame only" if m else "neither")


def _work(item):
    path, cache, force = item
    with open(path, "rb") as f:
        digest = hashlib.sha1(f.read()).hexdigest()[:16]
    cpath = os.path.join(cache, "%s-v%d.json" % (digest, VERSION)) if cache else None
    if cpath and not force and os.path.isfile(cpath):
        with open(cpath) as f:
            return json.load(f)
    rec = measure(path)
    rec["sha1"] = digest
    if cpath:
        os.makedirs(cache, exist_ok=True)
        with open(cpath, "w") as f:
            json.dump(rec, f)
    return rec


def find_roms(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for d, _dirs, files in os.walk(p):
                out += [os.path.join(d, f) for f in sorted(files) if f.lower().endswith(".a78")]
        elif os.path.isfile(p):
            out.append(p)
    return out


def load_cache(cache):
    out = []
    for f in sorted(os.listdir(cache)):
        if f.endswith("-v%d.json" % VERSION):
            with open(os.path.join(cache, f)) as fh:
                out.append(json.load(fh))
    return out


def report(recs, show=12):
    ok = [r for r in recs if r.get("ok")]
    print("%d cartridges: %d compared, %d not (could not be laid out)"
          % (len(recs), len(ok), len(recs) - len(ok)))
    if not ok:
        return
    groups = {}
    for r in ok:
        groups.setdefault(verdict(r), []).append(r)
    for k in ("both", "sim only", "mame only", "neither"):
        print("  %-10s %5d  (%.1f%%)" % (k, len(groups.get(k, [])),
                                          100.0 * len(groups.get(k, [])) / len(ok)))
    mame_dead = [r for r in ok if not is_live(r["mame"])]
    print("\nMAME did not get a live display list on %d of %d" % (len(mame_dead), len(ok)))
    by = {}
    for r in mame_dead:
        key = ("bankset" if r.get("bankset") else r.get("mapper", "?"))
        by.setdefault(key, []).append(r)
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        tot = sum(1 for r in ok if ("bankset" if r.get("bankset") else r.get("mapper", "?")) == k)
        print("    %-12s %3d of %3d" % (k, len(v), tot))
    for title, key in (("SIM ONLY -- MAME cannot be the reference for these", "sim only"),
                       ("MAME ONLY -- the simulator is missing something", "mame only")):
        rows = groups.get(key, [])
        if rows:
            print("\n%s (%d)" % (title, len(rows)))
            for r in rows[:show]:
                print("    %-52s %s  mame %s  sim %s" % (
                    r["name"][:52], r.get("mapper", "?"),
                    "%d/%d" % (r["mame"].get("live", 0), r["mame"].get("frames", 0))
                    if r["mame"].get("ran") else "did not run: " + r["mame"].get("why", ""),
                    "%d/%d" % (r["sim"].get("live", 0), r["sim"].get("frames", 0))
                    if r["sim"].get("ran") else "did not run: " + r["sim"].get("why", "")))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roms", nargs="*", help="cartridges, or folders searched for .a78")
    ap.add_argument("--sample", type=int, metavar="N", help="a random N of them")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--cache", help="folder of results by file hash; resumes a stopped run")
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    ap.add_argument("--report", metavar="CACHE", help="report on a cache and exit")
    args = ap.parse_args(argv)
    if args.report:
        report(load_cache(args.report))
        return 0
    roms = find_roms(args.roms)
    if not roms:
        ap.error("give cartridges or a folder holding some")
    if args.sample and args.sample < len(roms):
        random.Random(args.seed).shuffle(roms)
        roms = roms[:args.sample]
    items = [(p, args.cache, args.force) for p in roms]
    if args.jobs > 1 and len(items) > 1:
        with multiprocessing.Pool(args.jobs) as pool:
            recs = pool.map(_work, items, chunksize=1)
    else:
        recs = [_work(i) for i in items]
    report(recs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
