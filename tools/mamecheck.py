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

VERSION = 2
HANDOVER = 330            # MAME frame after which the cartridge is running (OpenBIOS)
WINDOW = 360              # frames judged, in each engine


def live_frames(rows):
    """(frames, live) from (dpph, dppl, ctrl) per frame: DMA on and the list pointer in RAM
    or ROM. The simulator's collector applies the same rule (simprobe.Collector.frame_start),
    so the two sides answer one question."""
    live = 0
    for dpph, _dppl, ctrl in rows:
        if (ctrl & 0x60) == 0x40 and (0x18 <= dpph <= 0x27 or dpph >= 0x40):
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


MAME_COMPLAINTS = ("Unsupported mapper", "invalid BIOS", "NOT FOUND", "Fatal error",
                   "Required files are missing", "bankswitch detected")


def mame_complaint(text):
    """The line MAME printed that says why it cannot run an image, or ''."""
    for ln in text.splitlines():
        if any(k in ln for k in MAME_COMPLAINTS):
            return ln.strip()[:140]
    return ""


def vectors_of(cart):
    """The NMI/RESET/IRQ vectors of each half of a cartridge, as the six bytes at $FFFA."""
    out = []
    for c in ([cart, cart.for_maria()] if cart.bankset else [cart]):
        try:
            v = c.vectors()
            out.append(bytes([v["NMI"] & 0xFF, v["NMI"] >> 8, v["RESET"] & 0xFF,
                              v["RESET"] >> 8, v["IRQ"] & 0xFF, v["IRQ"] >> 8]))
        except Exception:                                  # noqa: BLE001
            continue
    return out


def run_mame(rom, work, seconds=40):
    """MAME's side: {"ran", "frames", "live", "dli", "cart", "why"}.

    "cart" says whether the CARTRIDGE's code is what was running at the end: the six bytes
    at $FFFA must be the cartridge's own vectors. A cartridge the BIOS rejects leaves the BIOS
    running its built-in game, which keeps a live display list, so liveness alone proves
    nothing. "which" says which half of a bankset image supplied them ("cpu"/"maria")."""
    import cart as cart_module
    import runprobe
    frames = HANDOVER + WINDOW + 10
    env = {"A7800_RS_OUT": os.path.join(work, "survey"), "A7800_RS_FRAMES": str(frames)}
    ok, text, written = runprobe.run(rom, "rendersurvey", work, seconds=seconds, env=env)
    if not ok:
        return {"ran": False, "why": text[:140]}
    why = mame_complaint(text)
    if "Unsupported mapper" in why:
        # MAME does not implement this cartridge type: whatever it shows is the BIOS's own
        # game, not a reference for the image
        return {"ran": False, "why": why}
    csv = os.path.join(work, "survey-frames.csv")
    if not os.path.isfile(csv):
        return {"ran": False, "why": why or "MAME wrote no frame log"}
    rows = [r for r in parse_survey(csv) if r[0] > HANDOVER][:WINDOW]
    n, live = live_frames([(r[2], r[3], r[4]) for r in rows])
    res = {"ran": True, "frames": n, "live": live, "dli": sum(1 for r in rows if r[1]),
           "why": why, "cart": None}
    vec = os.path.join(work, "survey-vectors.txt")
    if os.path.isfile(vec):
        got = b""
        for ln in open(vec):
            if ln.startswith("vectors"):
                got = bytes(int(x, 16) for x in ln.split()[1:7])
        want = vectors_of(cart_module.Cart(rom))
        res["cart"] = got in want
        if res["cart"] and len(want) > 1:
            res["which"] = "cpu" if got == want[0] else "maria"
        elif not res["cart"]:
            res["why"] = res["why"] or "the BIOS is still running at the end: the vectors " \
                                       "are not the cartridge's"
    return res


def run_sim(rom):
    """The simulator's side, over the same number of frames."""
    import cart as cart_module
    import sim
    import simprobe
    cart = cart_module.Cart(rom)
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    col = simprobe.Collector()
    t = time.time()
    sim_bus = sim.run(cart, WINDOW, region, drive=True, observer=col)
    bus_jam = getattr(sim_bus, "jammed", None)
    return {"ran": True, "frames": WINDOW, "live": col.frames_with_list,
            "dli": len(col.nmi_frames), "seconds": round(time.time() - t, 1),
            "why": ("the program ran a KIL at $%04X and stopped itself" % bus_jam)
                   if bus_jam is not None else ""}


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
    """Running the cartridge: a live display list on half the frames, and (for MAME) the
    cartridge's own vectors in place rather than the BIOS's."""
    return bool(side.get("ran")) and side.get("frames", 0) > 0 and \
        side["live"] >= 0.5 * side["frames"] and side.get("cart") is not False


def verdict(rec):
    m, s = is_live(rec["mame"]), is_live(rec["sim"])
    return ("both" if m and s else "sim only" if s else "mame only" if m else "neither")


def _code_stamp():
    """A short hash of the code whose behaviour the result depends on, so a cached result is
    not reused after the simulator or the layout code changed."""
    h = hashlib.sha1()
    for name in ("sim.py", "cart.py", "simprobe.py", "mamecheck.py"):
        with open(os.path.join(HERE, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:8]


INFRASTRUCTURE = ("BIOS", "NOT FOUND", "Fatal error", "Required files", "was not found",
                  "wrote no frame log")


def _work(item):
    path, cache, force = item
    with open(path, "rb") as f:
        digest = hashlib.sha1(f.read()).hexdigest()[:16]
    cpath = os.path.join(cache, "%s-%s-v%d.json" % (digest, _code_stamp(), VERSION)) \
        if cache else None
    if cpath and not force and os.path.isfile(cpath):
        with open(cpath) as f:
            return json.load(f)
    rec = measure(path)
    rec["sha1"] = digest
    # a failure of the setup (no BIOS, no MAME) says nothing about the cartridge: not kept
    why = rec.get("mame", {}).get("why", "") if rec.get("ok") else ""
    if cpath and rec.get("ok") and not rec["mame"].get("ran") and \
            any(k in why for k in INFRASTRUCTURE):
        cpath = None
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
        if f.endswith("-v%d.json" % VERSION) and ("-%s-" % _code_stamp()) in f:
            with open(os.path.join(cache, f)) as fh:
                out.append(json.load(fh))
    return out


def report(recs, show=12):
    ok = [r for r in recs if r.get("ok")]
    print("%d cartridges: %d compared, %d not (could not be laid out)"
          % (len(recs), len(ok), len(recs) - len(ok)))
    for r in recs:
        if not r.get("ok"):
            print("    skipped: %-50s %s" % (r.get("name", "?")[:50], r.get("error", "")[:70]))
    if not ok:
        return
    groups = {}
    for r in ok:
        groups.setdefault(verdict(r), []).append(r)
    for k in ("both", "sim only", "mame only", "neither"):
        print("  %-10s %5d  (%.1f%%)" % (k, len(groups.get(k, [])),
                                          100.0 * len(groups.get(k, [])) / len(ok)))
    # display interrupts: one side taking them and the other not is a disagreement even
    # when both draw
    for r in ok:
        m, s = r["mame"], r["sim"]
        if m.get("ran") and s.get("ran") and ((m.get("dli", 0) >= 10) != (s.get("dli", 0) >= 10)):
            print("    DLI disagrees: %-44s mame %d frames, sim %d" % (
                r["name"][:44], m.get("dli", 0), s.get("dli", 0)))
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
                       ("MAME ONLY -- the simulator is missing something", "mame only"),
                       ("NEITHER -- needs input, does not draw, or the image is wrong",
                        "neither")):
        rows = groups.get(key, [])
        if rows:
            print("\n%s (%d)" % (title, len(rows)))
            for r in rows[:show]:
                m, s = r["mame"], r["sim"]
                print("    %-48s %-9s mame %s  sim %s" % (
                    r["name"][:48], ("bankset" if r.get("bankset") else r.get("mapper", "?")),
                    ("%d/%d%s" % (m.get("live", 0), m.get("frames", 0),
                                  "" if m.get("cart") is not False else " (BIOS, not the cart)")
                     if m.get("ran") else "did not run"),
                    ("%d/%d" % (s.get("live", 0), s.get("frames", 0))
                     if s.get("ran") else "did not run")))
                for side, label in ((m, "mame"), (s, "sim")):
                    if side.get("why"):
                        print("        %s says: %s" % (label, side["why"]))


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
