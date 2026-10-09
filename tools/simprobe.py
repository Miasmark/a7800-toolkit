#!/usr/bin/env python3
"""Watch a cartridge run in the simulator and write what the MAME probes would have.

    python tools/simprobe.py game.a78 -o out --frames 900
    python tools/dyn.py game.a78 out/exectrace.log -c annotations.json

No emulator, no BIOS: `sim.py` runs the cartridge's own code (RAM, the mapper, MARIA
far enough to keep a game's timing loop and interrupts honest, the sound chips) and
this records what happened, in the files the tools already read:

  exectrace.log   which instructions ran, where indirect jumps went (`JMP (vec)`
                  and an `RTS` that did not come from a `JSR`), which banks were
                  switched in. The format of probes/exectrace.lua, so `dyn.py`,
                  `init.py --dynamic` and the workbench read it unchanged.
  dataread.log    `D space:addr xN`: ROM bytes the CPU read as data -- operands of
                  `LDA table,X`, `CMP`, `(zp),Y` reads of a table -- and how often.
                  What is table and what is code, from the machine instead of a guess.
  regs.txt, ram.bin
                  MARIA's registers (and every write to them in the last frame) and
                  the 7800's RAM at the end of the run: the format of
                  probes/dumpgfx.lua, so `firstlook.py`'s graphics step reads it.
  audio.log       the sound-chip writes, the format of probes/audio.lua.

HOW FAR TO TRUST IT. It is the same simulator `sim.py` scores against MAME: five TIA
games reproduce the reference music, and on the synthetic cartridge the instructions
it reports are the ones MAME's probe reports. What it does not model is whatever a game
waits on that the simulator does not provide (input beyond the fire button, lightgun,
paddles, a second POKEY's timing); a game that stalls there shows as a short run, and
the summary says how many frames reached a display list and how often the program
counter stood still. The log is observed, not proven, like the MAME probe's: a run
reaches the code that run reaches.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cart as cart_module  # noqa: E402
import m6502  # noqa: E402
import sim  # noqa: E402

# How execution first arrived at an instruction, from the one that ran before it.
SEQ, JSR, JMP, JMPIND, BRANCH, RTS, RTI, BRK, RAM, NMI = (
    "fall", "jsr", "jmp", "jmp-ind", "branch", "rts", "rti", "brk", "ram", "nmi")


class Collector(sim.Observer):
    """Records what the CPU did, keyed by the toolkit's own space names."""

    def __init__(self, dump_frame=None, snap_frames=(), profile=False):
        self.x = set()               # (space, addr) fetched
        self.profile = {} if profile else None    # (space, addr) -> cycles spent there
        self.d = {}                  # (space, addr) -> times read as data
        self.j = {}                  # (from, to) -> times
        self.s = {}                  # (from, bank) -> times
        self.shadow = {}             # stack pointer after a JSR -> its return address
        self.nmis = 0
        self.dump_frame = dump_frame
        self.snap_frames = set(snap_frames) | ({dump_frame} if dump_frame else set())
        self.snaps = {}              # frame -> {"ram": bytes, "regs": text, "bank": n}
        self._cur = None             # the snapshot frame in progress
        self.last = None             # the last instruction fetched, as a location
        self.stuck = 0               # fetches at the address of the previous one
        self._prev = None
        self._prev_op = None
        self._irq = False
        self.frames_with_list = 0
        self.arrival = {}            # location -> how it was first reached
        self.pred = {}               # location -> the location fetched just before it, first time

    def attach(self, bus, cpu):
        sim.Observer.attach(self, bus, cpu)
        cart = bus.cart
        self.kind = {}
        for start, end, kind, arg in cart._region:
            for page in range(start >> 8, (end + 255) >> 8):
                if kind == "fixed":
                    self.kind[page] = cart._fixed_name(arg)
                elif kind == "window":
                    self.kind[page] = None       # the bank in force names it

    def loc(self, addr):
        """(space, addr) for a ROM address under the bank mapped now, else None."""
        page = (addr & 0xFFFF) >> 8
        if page not in self.kind:
            return None
        sp = self.kind[page]
        if sp is None:
            sp = "b%d" % self.bus.bank
        return (sp, addr & 0xFFFF)

    def _finish(self):
        """Close the snapshot frame in progress: RAM and registers as it ended."""
        if self._cur is None:
            return
        frame, start, writes, bank0 = self._cur
        self.snaps[frame] = {"ram": ram_bytes(self.bus), "bank": self.bus.bank,
                             "regs": regs_text(frame, self.bus, start, writes)}
        self._cur = None

    def finish(self):
        """Call once after the run, to close the last snapshot."""
        self._finish()

    def frame_start(self, frame):
        self._finish()
        if frame in self.snap_frames:
            self._cur = (frame, dict(self.bus.maria), [], self.bus.bank)
            self.nmis = 0
        if self.bus.dpph is not None and (self.bus.ctrl & 0x60) == 0x40:
            self.frames_with_list += 1

    def fetch(self, pc, opcode):
        loc = self.loc(pc)
        if loc is not None:
            if loc not in self.x:
                self.arrival[loc] = self._how(pc)
                self.pred[loc] = self.last         # the instruction that ran just before
            self.x.add(loc)
            if self.profile is not None:
                self.profile[loc] = self.profile.get(loc, 0) + m6502.CYCLES[opcode]
        self.last = loc
        if pc == self._prev:
            self.stuck += 1
        self._prev, self._prev_op = pc, opcode

    def _how(self, pc):
        """How control reached `pc`, judged from the instruction that ran before."""
        if self._irq:
            self._irq = False
            return NMI
        prev, op = self._prev, self._prev_op
        if prev is None:
            return "reset"
        if (prev >> 8) < 0x40:
            return RAM                       # the previous instruction ran from RAM
        mn, mode, _il = m6502.OPCODES[op]
        if mn == "JSR":
            return JSR
        if mn == "JMP":
            return JMPIND if mode == "ind" else JMP
        if mn == "RTS":
            return RTS
        if mn == "RTI":
            return RTI
        if mn == "BRK":
            return BRK
        if pc != ((prev + 1 + m6502.MODES[mode]) & 0xFFFF):
            return BRANCH                    # a taken branch
        return SEQ

    def data_read(self, addr):
        loc = self.loc(addr)
        if loc is not None:
            self.d[loc] = self.d.get(loc, 0) + 1

    def jump_indirect(self, pc, target):
        a, b = self.loc(pc), self.loc(target)
        if a and b:
            self.j[(a, b)] = self.j.get((a, b), 0) + 1

    def call(self, pc, target, ret, sp):
        self.shadow[sp] = ret

    def ret(self, pc, target, sp):
        want = self.shadow.pop(sp, None)
        if want == target:
            return
        # a return that did not come from a JSR: the return address was pushed by
        # hand (PHA / PHA / RTS), which is a computed jump no static tracer follows
        a, b = self.loc(pc), self.loc(target)
        if a and b:
            self.j[(a, b)] = self.j.get((a, b), 0) + 1

    def bank_switch(self, addr, value, bank):
        if self.last is not None:
            k = (self.last, bank)
            self.s[k] = self.s.get(k, 0) + 1

    def nmi(self):
        self.nmis += 1
        self._irq = True

    def maria_write(self, reg, value):
        if self._cur is not None:
            self._cur[2].append((self.nmis, reg, value))


def fmt(loc):
    return "%s:%04X" % loc


def write_exectrace(col, path):
    lines = ["X " + fmt(l) for l in col.x]
    lines += ["J %s %s" % (fmt(a), fmt(b)) for (a, b) in col.j]
    lines += ["S %s %d x%d" % (fmt(a), b, n) for (a, b), n in col.s.items()]
    with open(path, "w") as f:
        for ln in sorted(lines):
            f.write(ln + "\n")


def write_profile(col, path):
    """probes/pcprof.lua's format -- `space:pc count` -- but the count is CPU cycles
    spent at that address: exact, not sampled, and without the cycles MARIA's DMA took."""
    with open(path, "w") as f:
        for loc in sorted(col.profile):
            f.write("%s %d\n" % (fmt(loc), col.profile[loc]))


def write_dataread(col, path):
    with open(path, "w") as f:
        for loc in sorted(col.d):
            f.write("D %s x%d\n" % (fmt(loc), col.d[loc]))


def ram_bytes(bus):
    """The 7800's whole RAM, $1800-$27FF."""
    return bytes(bus.ram[sim.fold(a)] for a in range(0x1800, 0x2800))


def regs_text(frame, bus, start, writes):
    """probes/dumpgfx.lua's register file: the registers now, at the frame's start
    (`s`), and every MARIA write in the frame with the interrupts taken before it (`w`)."""
    m = bus.maria
    r = lambda k: m.get(k, 0)                                   # noqa: E731
    out = ["frame %d" % frame, "bank %d" % bus.bank,
           "DPPH=$%02X DPPL=$%02X" % (r(0x2C), r(0x30)),
           "CHARBASE=$%02X OFFSET=$%02X CTRL=$%02X BACKGRND=$%02X"
           % (r(0x34), r(0x38), r(0x3C), r(0x20))]
    for reg in sorted(start):
        out.append("s $%02X = $%02X" % (reg, start[reg]))
    for reg in sorted(m):
        out.append("$%02X = $%02X" % (reg, m[reg]))
    for n, reg, val in writes:
        out.append("w %d $%02X = $%02X" % (n, reg, val))
    return "\n".join(out) + "\n"


def write_dump(col, bus, ram_path, regs_path):
    """The dumpgfx.lua files for the dump frame."""
    snap = col.snaps[col.dump_frame]
    with open(ram_path, "wb") as f:
        f.write(snap["ram"])
    with open(regs_path, "w") as f:
        f.write(snap["regs"])


def probe(rom, out, frames=600, drive=False, handover=None, steal=True, mapper=None,
          low=None, profile=False):
    """Run `rom` for `frames` frames and write the files; returns a summary dict."""
    cart = cart_module.Cart(rom, mapper=mapper, low=low)
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    start = sim.load_handover(handover) if handover else None
    col = Collector(dump_frame=frames, profile=profile)
    bus = sim.run(cart, frames, region, drive=drive, steal=steal,
                  start_state=start, observer=col)
    col.finish()
    os.makedirs(out, exist_ok=True)
    write_exectrace(col, os.path.join(out, "exectrace.log"))
    write_dataread(col, os.path.join(out, "dataread.log"))
    write_dump(col, bus, os.path.join(out, "ram.bin"), os.path.join(out, "regs.txt"))
    sim.write_log(bus, cart, os.path.join(out, "audio.log"), region)
    if profile:
        write_profile(col, os.path.join(out, "pcprof.log"))
    return {"frames": frames, "instructions": len(col.x), "data_bytes": len(col.d),
            "indirect_jumps": len(col.j), "bank_switches": len(col.s),
            "frames_with_display_list": col.frames_with_list,
            "stuck_fetches": col.stuck, "audio_writes": len(bus.writes),
            "dll": ("%02X%02X" % (bus.dpph, bus.dppl)
                    if bus.dpph is not None and bus.dppl is not None else None),
            "ctrl": bus.ctrl}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-o", "--out", default="simprobe-out")
    ap.add_argument("--frames", type=int, default=600)
    ap.add_argument("--drive", action="store_true",
                    help="tap fire now and then, to get past a title screen")
    ap.add_argument("--handover", metavar="LOG",
                    help="start from a probes/handover.lua capture, not zeroed RAM")
    ap.add_argument("--profile", action="store_true",
                    help="also write pcprof.log: cycles spent at each address, for pcmap.py")
    ap.add_argument("--no-dma-steal", dest="steal", action="store_false")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    args = ap.parse_args(argv)
    if not os.path.isfile(args.rom):
        sys.exit("simprobe: no such file: %s" % args.rom)
    try:
        r = probe(args.rom, args.out, args.frames, args.drive, args.handover,
                  args.steal, args.mapper, args.low, args.profile)
    except (cart_module.UnknownMapper, cart_module.UnknownSpace, IOError) as e:
        sys.exit("simprobe: %s" % e)
    print("%d frames: %d distinct instructions, %d ROM bytes read as data, %d indirect "
          "jumps, %d bank switches, %d audio writes"
          % (r["frames"], r["instructions"], r["data_bytes"], r["indirect_jumps"],
             r["bank_switches"], r["audio_writes"]))
    print("display list on %d of %d frames%s" % (
        r["frames_with_display_list"], r["frames"],
        "" if r["dll"] else " -- it never pointed MARIA at one"))
    if r["frames_with_display_list"] < r["frames"] * 0.5:
        print("note: it reached a display list late or never; it may be waiting on "
              "something the simulator does not provide (try --drive).")
    print("wrote exectrace.log dataread.log regs.txt ram.bin audio.log%s in %s"
          % (" pcprof.log" if args.profile else "", args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
