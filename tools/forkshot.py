#!/usr/bin/env python3
"""
Photograph what the `a7800` fork draws, and set it beside the simulator's picture.

    python tools/forkshot.py game.a78 -o shots --frame 600
    python tools/forkshot.py game.a78 -o shots --sim          # fork | simulator

The fork is the 7800-devtools build of MAME's driver, and the only machine here that runs
BANKSET cartridges (mainline MAME 0.264 answers "Unsupported mapper" and leaves the BIOS's
own game running). This runs it headless with `probes/a7800-snap.lua` and writes the frame
it reached to `<out>/fork.png`. With `--sim` it also runs the simulator's first look and
writes `<out>/side-by-side.png`, the fork on the left and the simulator on the right,
which is how the bankset layout and the simulator's bank RAM were checked against the real
cartridge code (docs/cartridges.md).

The fork has to be found: `--exe` or A7800_FORK. It is a MAME build -- see docs/emulation.md
for what a modern toolchain needs patched -- and takes `A7800_ROMPATH` like MAME does (its
BIOS is optional). A game that waits at a title is held at fire with `--drive`; the
simulator side always drives, so a title screen can differ between the two.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def find_fork(exe=None):
    for c in (exe, os.environ.get("A7800_FORK")):
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def shoot(rom, out, frame=600, drive=False, exe=None, rompath=None, timeout=240):
    """Run the fork to `frame` and return the path of the picture, or raise RuntimeError."""
    exe = find_fork(exe)
    if not exe:
        raise RuntimeError("cannot find the a7800 fork: pass --exe or set A7800_FORK")
    os.makedirs(out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="forkshot-")
    env = dict(os.environ, A7800_SNAP_FRAME=str(frame))
    if drive:
        env["A7800_DRIVE"] = "1"
    cmd = [exe, "a7800", "-cart", os.path.abspath(rom), "-video", "none", "-sound", "none",
           "-nothrottle", "-str", str(frame // 30 + 10), "-snapshot_directory", work,
           "-autoboot_script", os.path.join(ROOT, "probes", "a7800-snap.lua"), "-log"]
    rompath = rompath or os.environ.get("A7800_ROMPATH")
    if rompath:
        cmd += ["-rompath", rompath]
    try:
        # the fork writes error.log into its working directory: keep that out of the way
        subprocess.run(cmd, cwd=work, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
        shot = os.path.join(work, "a7800", "0000.png")
        if not os.path.isfile(shot):
            raise RuntimeError("the fork produced no picture (did it reach frame %d?)" % frame)
        dst = os.path.join(out, "fork.png")
        shutil.copyfile(shot, dst)
        return dst
    except subprocess.TimeoutExpired:
        raise RuntimeError("the fork did not finish in %d s" % timeout)
    finally:
        shutil.rmtree(work, True)


def side_by_side(fork_png, sim_png, dst):
    from PIL import Image
    a = Image.open(fork_png).convert("RGB")
    b = Image.open(sim_png).convert("RGB")
    b = b.resize((b.width * a.width // b.width, b.height * a.width // b.width), Image.NEAREST)
    m = Image.new("RGB", (a.width + b.width + 10, max(a.height, b.height)), (255, 255, 255))
    m.paste(a, (0, 0))
    m.paste(b, (a.width + 10, 0))
    m.save(dst)
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-o", "--out", default="forkshot")
    ap.add_argument("--frame", type=int, default=600, help="the frame to photograph")
    ap.add_argument("--drive", action="store_true", help="hold fire on a loop (the fork)")
    ap.add_argument("--exe", help="the a7800 fork (or A7800_FORK)")
    ap.add_argument("--rompath", help="where the BIOS is, if there is one (or A7800_ROMPATH)")
    ap.add_argument("--sim", action="store_true",
                    help="also run the simulator and write side-by-side.png")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.rom):
        sys.exit("forkshot: no such file: %s" % args.rom)
    try:
        png = shoot(args.rom, args.out, args.frame, args.drive, args.exe, args.rompath)
    except RuntimeError as e:
        sys.exit("forkshot: %s" % e)
    print("wrote %s" % png)
    if args.sim:
        sim_dir = os.path.join(args.out, "sim")
        r = subprocess.run([sys.executable, os.path.join(HERE, "firstlook.py"), args.rom,
                            "-o", sim_dir, "--engine", "sim", "--seconds", "12"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        scr = os.path.join(sim_dir, "graphics", "screen.png")
        if r.returncode != 0 or not os.path.isfile(scr):
            sys.exit("forkshot: the simulator drew no screen")
        print("wrote %s" % side_by_side(png, scr, os.path.join(args.out, "side-by-side.png")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
