#!/usr/bin/env python3
"""
Measure what MARIA's DMA costs on an emulator, and compare emulators.

    python tools/dmameasure.py --emu new=/path/mame:a7800pr --emu fork=/path/mame64
    python tools/dmameasure.py --emu mame=/usr/games/mame:a7800pr --fit

`probes/dma-costcart.py` builds a cartridge that spins a fixed-cost loop for the visible
period and publishes the iteration count; `probes/dma-count.lua` reads it. Everything in the
ROM is the same from one display shape to the next, so the count lost IS the DMA cost. This
runs a fixed set of shapes on each emulator you name (`label=executable` or
`label=executable:bios`, the BIOS being a `-bios` name for MAME and nothing for the a7800
fork), prints the cost of each in CPU cycles, and the difference between emulators.

With `--fit` it also fits `tools/dmabudget.py`'s constants (cycles per line, per zone, per
object, per graphics byte) to each emulator's numbers by least squares and prints them beside
the model's, which is how the model was re-derived when MARIA's timing changed (the a7800
fork's, now in MAME: a 430-clock DMA limit, a first-hole penalty, an 8-clock last-line
shutdown). The BIOS folder comes from --rompath or A7800_ROMPATH.

The ceiling (DMA off) is 1,960 iterations of a 14.0156-cycle loop on every emulator tried.
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOOP = 14.0156          # cycles per iteration, calibrated with the `nops` parameter
CEILING = 1960          # iterations per frame with DMA off (12 zones of 16 lines)

# (name, build kwargs, zones, lines) -- the shapes the model was fitted to, plus the ones
# the fork's timing changes: holey fetches, display interrupts, and a line heavy enough to
# hit the DMA limit
SHAPES = [
    ("2 x 8b", dict(nobj=2, width=8), 12, 16),
    ("2 x 1b", dict(nobj=2, width=1), 12, 16),
    ("2 x 16b", dict(nobj=2, width=16), 12, 16),
    ("2 x 8b, 5-byte", dict(nobj=2, width=8, five_byte=True), 12, 16),
    ("2 x 4 chars, 1 b/char", dict(nobj=2, width=4, indirect=True), 12, 16),
    ("2 x 4 chars, 2 b/char", dict(nobj=2, width=4, indirect=True, charwidth1=True), 12, 16),
    ("2 x 20b, 8-line zones", dict(nobj=2, width=20, zlines=8, nzones=24), 24, 8),
    ("2 x 8b, 8-line zones", dict(nobj=2, width=8, zlines=8, nzones=24), 24, 8),
    ("2 x 20b holey", dict(nobj=2, width=20, zlines=8, nzones=24, holey=True), 24, 8),
    ("2 x 8b holey", dict(nobj=2, width=8, zlines=8, nzones=24, holey=True), 24, 8),
    ("2 x 20b + DLI", dict(nobj=2, width=20, zlines=8, nzones=24, dli=True), 24, 8),
    ("2 x 8b + DLI", dict(nobj=2, width=8, zlines=8, nzones=24, dli=True), 24, 8),
    ("6 x 16b (hits the limit)", dict(nobj=6, width=16), 12, 16),
    ("8 x 16b (over the limit)", dict(nobj=8, width=16), 12, 16),
]


def costcart():
    spec = importlib.util.spec_from_file_location(
        "dma_costcart", os.path.join(ROOT, "probes", "dma-costcart.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iterations(exe, bios, rom, rompath, timeout=120):
    """The median iteration count the cartridge reports, or None."""
    cmd = [exe, "a7800", "-cart", rom, "-video", "none", "-sound", "none", "-nothrottle",
           "-str", "12", "-autoboot_script", os.path.join(ROOT, "probes", "dma-count.lua")]
    if rompath:
        cmd += ["-rompath", rompath]
    if bios:
        cmd += ["-bios", bios]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, cwd=tempfile.gettempdir()).stdout
    except subprocess.TimeoutExpired:
        return None
    for ln in out.decode("utf-8", "replace").splitlines():
        if ln.startswith("RESULT"):
            return int(ln.split("median=")[1].split()[0])
    return None


def measure(emus, rompath, shapes=SHAPES):
    cc = costcart()
    work = tempfile.mkdtemp(prefix="dmameasure-")
    rows = []
    for name, kw, nz, zl in shapes:
        rom = os.path.join(work, "t.a78")
        kw = dict(kw)
        nobj, width = kw.pop("nobj"), kw.pop("width")
        kw.setdefault("zones_used", nz)          # every zone carries the objects
        cc.build(rom, nobj, width, **kw)
        got = {}
        for label, (exe, bios) in emus.items():
            it = iterations(exe, bios, rom, rompath)
            got[label] = None if it is None else (CEILING - it) * LOOP
        rows.append((name, nz, zl, nobj, width, kw, got))
    return rows


def fit_shapes():
    """(nz, zline, used zones, objects, width, extras): every shape varies one thing, so the
    constants separate. 192 lines are always drawn; empty zones still cost a line and a zone."""
    out = []
    for nz, zl in ((12, 16), (16, 12), (24, 8)):
        for used in (0, nz // 2, nz):
            for nobj, width in ((1, 4), (2, 4), (2, 12), (4, 8), (3, 16)):
                if used == 0 and (nobj, width) != (1, 4):
                    continue
                out.append((nz, zl, used, nobj, width, {}))
    for nz, zl in ((12, 16), (24, 8)):
        for nobj, width in ((2, 8), (2, 20), (4, 8)):
            out.append((nz, zl, nz, nobj, width, {"holey": True}))
            out.append((nz, zl, nz, nobj, width, {"dli": True}))
    return out


def run_fit(emu, rompath):
    """Fit PER_LINE, PER_ZONE, PER_OBJ, PER_BYTE, HOLE (per object-line, holey) and DLI to one
    emulator by least squares. Needs numpy. Returns (constants, worst relative residual)."""
    import numpy as np
    cc = costcart()
    work = tempfile.mkdtemp(prefix="dmafit-")
    rom = os.path.join(work, "t.a78")
    exe, bios = emu
    A, b = [], []
    for nz, zl, used, nobj, width, ex in fit_shapes():
        cc.build(rom, nobj, width, zones_used=used, zlines=zl, nzones=nz, **ex)
        it = iterations(exe, bios, rom, rompath)
        if it is None:
            continue
        cost = (CEILING - it) * LOOP
        ol = used * zl * nobj                    # object-lines
        holey = ex.get("holey", False)
        A.append([192, nz, ol, 0 if holey else ol * width, ol if holey else 0,
                  nz if ex.get("dli") else 0])
        b.append(cost)
    A, b = np.array(A, float), np.array(b, float)
    sol = np.linalg.lstsq(A, b, rcond=None)[0]
    res = np.abs(A @ sol - b) / b
    names = ("PER_LINE", "PER_ZONE", "PER_OBJ", "PER_BYTE", "HOLE", "DLI_COST")
    sol = dict(zip(names, sol))
    sol["PER_LINE"] = sol["PER_LINE"]            # (192 lines were drawn in every shape)
    return sol, float(res.max()), len(b)


def fit(rows, label):                            # kept for the simple table fit
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emu", action="append", required=True, metavar="LABEL=EXE[:BIOS]")
    ap.add_argument("--rompath", default=os.environ.get("A7800_ROMPATH"))
    ap.add_argument("--fit", action="store_true", help="fit the model's constants per emulator")
    ap.add_argument("--only", metavar="TEXT", help="only shapes whose name contains TEXT")
    args = ap.parse_args(argv)
    emus = {}
    for e in args.emu:
        label, _, rest = e.partition("=")
        exe, _, bios = rest.partition(":")
        if not os.path.isfile(exe):
            sys.exit("dmameasure: no such executable: %s" % exe)
        emus[label] = (exe, bios or None)
    shapes = [s for s in SHAPES if not args.only or args.only in s[0]]
    rows = measure(emus, args.rompath, shapes)

    import dmabudget as d
    labels = list(emus)
    print("%-28s %9s" % ("cycles lost to DMA / frame", "model") + "".join("%10s" % l for l in labels))
    for name, nz, zl, nobj, width, kw, got in rows:
        z = d.Zone(zl, nobj, width, five=kw.get("five_byte", False),
                   chars=(2 if kw.get("charwidth1") else 1) if kw.get("indirect") else 0,
                   holey=kw.get("holey", False), dli=kw.get("dli", False))
        print("%-28s %9.0f" % (name, nz * z.cycles()) + "".join(
            "%10s" % ("-" if got[l] is None else "%.0f" % got[l]) for l in labels))
    if len(labels) > 1:
        same = all(len({r[6][l] for l in labels}) == 1 for r in rows if None not in r[6].values())
        print("\nevery emulator agrees on every shape" if same else
              "\nthe emulators differ on: " + ", ".join(
                  r[0] for r in rows if len({r[6][l] for l in labels}) > 1))
    if args.fit:
        import dmabudget as d
        print("\nfitted constants, CPU cycles")
        print("  %-8s %8s %8s %8s %8s %8s %8s   worst  runs" % (
            "", "PER_LINE", "PER_ZONE", "PER_OBJ", "PER_BYTE", "HOLE", "DLI"))
        for l in labels:
            sol, worst, n = run_fit(emus[l], args.rompath)
            print("  %-8s %8.3f %8.3f %8.3f %8.3f %8.3f %8.2f  %5.2f%%  %d" % (
                l, sol["PER_LINE"], sol["PER_ZONE"], sol["PER_OBJ"], sol["PER_BYTE"],
                sol["HOLE"], sol["DLI_COST"], 100 * worst, n))
        print("  %-8s %8.3f %8.3f %8.3f %8.3f %8s %8.2f" % (
            "model", d.PER_LINE, d.PER_ZONE, d.PER_OBJ, d.PER_BYTE, "-", d.DLI_COST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
