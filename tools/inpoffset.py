#!/usr/bin/env python3
"""How far to shift a recording so it plays on a game that starts on another frame.

    python tools/inpoffset.py old.a78 new.a78                       # same BIOS, two builds
    python tools/inpoffset.py game.a78 game.a78 --old-bios a7800pr --new-bios skip
    python tools/inpoffset.py old.a78 new.a78 --inp run-01.inp      # and play it shifted

An `.inp` holds button states against frame numbers, so it only replays a game
that reaches its first instruction on the same frame. That moves with the BIOS
(Atari's NTSC BIOS about frame 204, 7800OpenBIOS 322, `-bios skip` 0), with
whether the BIOS's signature check passes, and with the signature bytes
themselves (docs/pitfalls.md). This runs each side in MAME with
probes/handover.lua, finds the frame the cartridge's reset code first runs, and
prints the `-playback_offset` that lines the recording up:

    offset = hand-over frame where it was recorded - hand-over frame where it plays

`-playback_offset` and `-playback_force` need MAME with the a7800 patch. The
tools here pass them from A7800_PLAYBACK_OFFSET and A7800_PLAYBACK_FORCE.

Lining up the first frame is not always enough. The game also has to start at
the same point *within* the frame and from the same state. `-bios skip` starts
where OpenBIOS hands over (exactly on PAL, within a third of a cycle on NTSC),
but RAM a BIOS leaves behind is not reproduced. A game whose interrupts are
timed to the cycle can still drift (docs/pitfalls.md has the measurements).
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capture  # noqa: E402
import cart as cartlib  # noqa: E402

PROBE = os.path.join(os.path.dirname(HERE), "probes", "handover.lua")


def handover_frame(mame, rom, bios=None, rompath=None, end=900):
    """The frame on which the cartridge's reset code first runs, in this MAME with this BIOS."""
    rom = os.path.abspath(rom)
    reset = cartlib.Cart(rom).vectors().get("RESET")
    if reset is None:
        raise RuntimeError("%s: no reset vector" % rom)
    machine = capture.inspect(rom)["machine"]
    roms = capture.find_rompath(rom, rompath, bios_name=bios)
    if not roms:
        raise RuntimeError("cannot find the 7800 BIOS images: pass --rompath or set A7800_ROMPATH")
    bios_args, roms = capture.machine_setup(machine, roms, bios)
    with tempfile.TemporaryDirectory(prefix="inpoffset-") as d:
        log = os.path.join(d, "handover.log")
        env = dict(os.environ, A7800_HO_RESET="%04X" % reset, A7800_HO_LOG=log,
                   A7800_HO_END=str(end))
        env.setdefault("XDG_RUNTIME_DIR", d)
        cmd = [mame, machine] + bios_args + [
            "-rompath", roms, "-cart", rom, "-video", "none", "-sound", "none",
            "-skip_gameinfo", "-nothrottle", "-autoboot_script", PROBE]
        r = subprocess.run(cmd, env=env, cwd=d, capture_output=True, text=True, timeout=600)
        text = open(log).read() if os.path.exists(log) else ""
    m = re.search(r"handover at frame (\d+)", text)
    if not m:
        raise RuntimeError("%s (%s): %s" % (os.path.basename(rom), " ".join(bios_args) or "default BIOS",
                                            text.strip() or r.stdout[-300:] + r.stderr[-300:]))
    return int(m.group(1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", help="the cartridge the recording was made on")
    ap.add_argument("new", help="the cartridge to play it on (may be the same file)")
    ap.add_argument("--old-bios", help="-bios it was recorded with (default A7800_BIOS)")
    ap.add_argument("--new-bios", help="-bios to play it with (default A7800_BIOS)")
    ap.add_argument("--mame")
    ap.add_argument("--rompath")
    ap.add_argument("--inp", help="then play this recording, shifted, in a MAME window")
    a = ap.parse_args(argv)
    mame = capture.find_mame(a.mame)
    if not mame:
        sys.exit("MAME was not found: set A7800_MAME")
    try:
        old = handover_frame(mame, a.old, a.old_bios, a.rompath)
        new = handover_frame(mame, a.new, a.new_bios, a.rompath)
    except RuntimeError as e:
        sys.exit("inpoffset: %s" % e)
    off = old - new
    print("recorded: %s, hand-over at frame %d" % (os.path.basename(a.old), old))
    print("played:   %s, hand-over at frame %d" % (os.path.basename(a.new), new))
    print("offset:   -playback_offset %d   (A7800_PLAYBACK_OFFSET=%d)" % (off, off))
    if a.inp:
        rom = os.path.abspath(a.new)
        machine = capture.inspect(rom)["machine"]
        bios_args, roms = capture.machine_setup(
            machine, capture.find_rompath(rom, a.rompath, bios_name=a.new_bios), a.new_bios)
        inp = os.path.abspath(a.inp)
        cmd = [mame, machine] + bios_args + ["-rompath", roms, "-cart", rom,
                                             "-input_directory", os.path.dirname(inp),
                                             "-playback", os.path.basename(inp)] + capture.playback_args(off, True)
        return subprocess.call(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
