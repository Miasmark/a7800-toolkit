#!/usr/bin/env python3
"""Record and play back a MAME input session, for any cartridge.

    python tools/session.py record game.a78            # next free run-NN.inp
    python tools/session.py record game.a78 title      # title.inp
    python tools/session.py play   game.a78 run-01
    python tools/session.py list   game.a78

A recording is the thing every measuring probe rests on. MAME's a7800 driver
reports savestates as unsupported, so a .sta cannot be restored; an .inp
replays your exact session from power-on, frame for frame. Two replays of one
recording produce identical logs, so a before-and-after number means something
-- which a scripted run cannot give you, because scripted input reaches a
title screen and stops.

Recordings live in a `recordings` folder beside the cartridge, and `record`
never overwrites one: with no name it takes the next unused run-NN.

A recording is button states against frame numbers, not intentions. Replay it
on a build that runs at a different speed and the same presses land at
different moments in the game. To compare an original with a patch, record a
session on each.

Play to the point you want captured, then close MAME. To measure a recording,
run a probe over it with `-playback` (probes/reclength.lua first, with
`-exit_after_playback`, to learn how long it really is) or hand it to tools/regress.py.

MAME and the BIOS are found the way tools/capture.py finds them:
--mame / A7800_MAME and --rompath / A7800_ROMPATH. Set A7800_BIOS=a7800pr to
use the open BIOS in that slot (docs/bios.md).
"""
import argparse
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import capture  # noqa: E402
import cart as cartlib  # noqa: E402


def rec_dir(rom):
    return os.path.join(os.path.dirname(os.path.abspath(rom)), "recordings")


def next_name(folder):
    taken = set()
    for p in glob.glob(os.path.join(folder, "run-*.inp")):
        m = re.fullmatch(r"run-(\d+)\.inp", os.path.basename(p))
        if m:
            taken.add(int(m.group(1)))
    n = 1
    while n in taken:
        n += 1
    return "run-%02d" % n


def machine_for(rom):
    """a7800 for NTSC, a7800p for PAL -- from the header, like capture.py."""
    try:
        region = (cartlib.Cart(rom).info or {}).get("region", "NTSC")
    except Exception:
        region = "NTSC"
    return "a7800p" if str(region).lower() == "pal" else "a7800"


def launch(args, extra):
    mame = capture.find_mame(args.mame)
    if not mame:
        sys.exit("cannot find MAME. Pass --mame or set A7800_MAME.")
    rompath = capture.find_rompath(args.rom, args.rompath)
    if not rompath:
        sys.exit("cannot find the 7800 BIOS. Pass --rompath or set "
                 "A7800_ROMPATH to the folder holding it.")
    folder = rec_dir(args.rom)
    os.makedirs(folder, exist_ok=True)
    cmd = [mame, machine_for(args.rom)] + capture.bios_args() + [
           "-rompath", rompath,
           "-cart", os.path.abspath(args.rom), "-skip_gameinfo", "-window",
           "-input_directory", folder] + extra
    print(" ".join('"%s"' % c if " " in c else c for c in cmd))
    subprocess.call(cmd)


def cmd_record(args):
    folder = rec_dir(args.rom)
    name = args.name or next_name(folder)
    name = re.sub(r"\.inp$", "", name)
    path = os.path.join(folder, name + ".inp")
    if os.path.exists(path):
        sys.exit("%s exists; pick another name (recordings are never "
                 "overwritten)." % path)
    print("Recording to %s\nPlay to the point you want captured, then close "
          "MAME." % path)
    launch(args, ["-record", name + ".inp"])
    if os.path.exists(path):
        print("Saved: %s (%d bytes)" % (path, os.path.getsize(path)))
    else:
        print("No recording was written. If MAME printed an error above, "
              "that is why.")
        return 1


def cmd_play(args):
    name = re.sub(r"\.inp$", "", args.name)
    path = os.path.join(rec_dir(args.rom), name + ".inp")
    if not os.path.exists(path):
        sys.exit("no such recording: %s (try `list`)" % path)
    print("P pauses, Esc quits. When the recording runs out the game carries "
          "on under your control.")
    launch(args, ["-playback", name + ".inp"])


def cmd_list(args):
    found = sorted(glob.glob(os.path.join(rec_dir(args.rom), "*.inp")))
    if not found:
        print("no recordings in %s" % rec_dir(args.rom))
    for p in found:
        print("%-24s %8d bytes" % (os.path.basename(p), os.path.getsize(p)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Record and play back a MAME input session.",
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mame", help="MAME executable (or A7800_MAME)")
    ap.add_argument("--rompath", help="folder with the BIOS (or A7800_ROMPATH)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a session")
    r.add_argument("rom")
    r.add_argument("name", nargs="?")
    r.set_defaults(fn=cmd_record)
    p = sub.add_parser("play", help="watch a recording play back")
    p.add_argument("rom")
    p.add_argument("name")
    p.set_defaults(fn=cmd_play)
    l = sub.add_parser("list", help="list recordings beside a cartridge")
    l.add_argument("rom")
    l.set_defaults(fn=cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
