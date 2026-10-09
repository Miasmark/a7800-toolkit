#!/usr/bin/env python3
"""Which ROM bytes are addresses, found by running the cartridge in the simulator.

    python tools/simorigins.py game.a78 -o out --frames 600
    python tools/origins.py out/addrorigin.log game.a78 -c annotations.json

This is `probes/addrorigin.lua` without MAME: it follows every value the CPU uses as an
address back to the byte it was loaded from (through register transfers, stores to RAM,
pushes and pulls), and writes the same `addrorigin.log`, so `origins.py` turns it into
address tables and immediates exactly as it does for the MAME probe. Read that probe's
header for what the log means and for the one heuristic in it: arithmetic leaves the
accumulator's origin as it was, so a pointer plus an offset still traces to the
pointer's byte.

It sees what the simulator runs, so it is as good as the simulator's play: a game that
stalls before its tables are used shows few of them. Slow -- every instruction is decoded
in Python -- so a few hundred frames is the practical length.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cart as cart_module  # noqa: E402
import m6502  # noqa: E402
import sim  # noqa: E402
import simprobe  # noqa: E402

JSR_MARK, INT_MARK = "jsr", "int"


class OriginTracker(simprobe.Collector):
    """Follows where each byte the CPU uses as an address came from."""

    def __init__(self, dump_frame=None):
        simprobe.Collector.__init__(self, dump_frame=dump_frame)
        self.memsrc = {}              # folded RAM address -> origin of the byte last stored
        self.src = {"A": None, "X": None, "Y": None}
        self.cur = None               # the instruction fetched last, resolved but not run
        self.uses = {}                # (kind, pc loc, hi origin, lo origin) -> [n, tmin, tmax]
        self.imms = set()

    # -- origins are ("r", addr) for RAM nothing tracked wrote, or (space, addr) in ROM
    def origin(self, addr):
        addr &= 0xFFFF
        key = sim.fold(addr)
        if key in self.memsrc:
            return self.memsrc[key]
        loc = self.loc(addr)
        return loc if loc is not None else ("r", addr)

    def _use(self, kind, pc, hi, lo, target):
        if hi is None or lo is None or target is None:
            return
        if hi in (JSR_MARK, INT_MARK) or lo in (JSR_MARK, INT_MARK):
            return
        where = self.loc(pc)
        if where is None:
            return
        k = (kind, where, hi, lo)
        u = self.uses.get(k)
        if u is None:
            self.uses[k] = [1, target, target]
        else:
            u[0] += 1
            u[1], u[2] = min(u[1], target), max(u[2], target)

    # -- the hooks
    def fetch(self, pc, opcode):
        if self.cur is not None:
            self._effects(self.cur, pc)
        simprobe.Collector.fetch(self, pc, opcode)
        self.cur = self._start(pc, opcode)

    def nmi(self):
        simprobe.Collector.nmi(self)
        # an interrupt pushes three bytes of return address and flags
        s = self.cpu.s
        for k in range(3):
            self.memsrc[sim.fold(0x100 + ((s - k) & 0xFF))] = INT_MARK

    def _start(self, pc, op):
        mn, mode, _il = m6502.OPCODES[op]
        c = {"pc": pc, "mn": mn, "mode": mode, "s": self.cpu.s}
        rd = self.bus.read
        cpu = self.cpu
        x, y = cpu.x & 0xFF, cpu.y & 0xFF
        if mode == "imm":
            c["ea"] = pc + 1
            c["imm"] = self.loc(pc + 1)
        elif mode == "zp":
            c["ea"] = rd(pc + 1)
        elif mode == "zpx":
            c["ea"] = (rd(pc + 1) + x) & 0xFF
        elif mode == "zpy":
            c["ea"] = (rd(pc + 1) + y) & 0xFF
        elif mode == "abs":
            c["ea"] = rd(pc + 1) | (rd(pc + 2) << 8)
        elif mode == "abx":
            c["ea"] = ((rd(pc + 1) | (rd(pc + 2) << 8)) + x) & 0xFFFF
        elif mode == "aby":
            c["ea"] = ((rd(pc + 1) | (rd(pc + 2) << 8)) + y) & 0xFFFF
        elif mode == "izy":
            z = rd(pc + 1)
            c["zp"] = z
            c["ea"] = ((rd(z) | (rd((z + 1) & 0xFF) << 8)) + y) & 0xFFFF
        elif mode == "izx":
            z = (rd(pc + 1) + x) & 0xFF
            c["zp"] = z
            c["ea"] = rd(z) | (rd((z + 1) & 0xFF) << 8)
        elif mode == "ind":
            c["vec"] = rd(pc + 1) | (rd(pc + 2) << 8)
        return c

    def _effects(self, c, nextpc):
        mn, mode = c["mn"], c["mode"]
        s = self.src
        if mn in ("LDA", "LAX"):
            s["A"] = self._loaded(c)
            if mn == "LAX":
                s["X"] = s["A"]
        elif mn == "LDX":
            s["X"] = self._loaded(c)
        elif mn == "LDY":
            s["Y"] = self._loaded(c)
        elif mn == "PLA":
            s["A"] = self.memsrc.get(sim.fold(0x100 + ((c["s"] + 1) & 0xFF)))
        elif mn == "TAX":
            s["X"] = s["A"]
        elif mn == "TAY":
            s["Y"] = s["A"]
        elif mn == "TXA":
            s["A"] = s["X"]
        elif mn == "TYA":
            s["A"] = s["Y"]
        # stores and pushes record what they stored
        if mn == "STA":
            self.memsrc[sim.fold(c["ea"])] = s["A"]
        elif mn == "STX":
            self.memsrc[sim.fold(c["ea"])] = s["X"]
        elif mn == "STY":
            self.memsrc[sim.fold(c["ea"])] = s["Y"]
        elif mn == "PHA":
            self.memsrc[sim.fold(0x100 + c["s"])] = s["A"]
        elif mn == "JSR":
            self.memsrc[sim.fold(0x100 + c["s"])] = JSR_MARK
            self.memsrc[sim.fold(0x100 + ((c["s"] - 1) & 0xFF))] = JSR_MARK
        # uses of an address
        if mode in ("izy", "izx"):
            z = c["zp"]
            self._use("ptr", c["pc"], self.origin((z + 1) & 0xFF), self.origin(z), c["ea"])
        elif mode == "ind":
            v = c["vec"]
            self._use("jmpind", c["pc"], self.origin(v + 1), self.origin(v), nextpc)
        elif mn == "RTS":
            sp = c["s"]
            self._use("rts", c["pc"], self.origin(0x100 + ((sp + 2) & 0xFF)),
                      self.origin(0x100 + ((sp + 1) & 0xFF)), nextpc)
        elif mode in ("abs", "abx", "aby"):
            pc = c["pc"]
            if sim.fold(pc + 1) in self.memsrc or sim.fold(pc + 2) in self.memsrc:
                target = nextpc if mn in ("JMP", "JSR") else c["ea"]
                self._use("selfmod", pc, self.origin(pc + 2), self.origin(pc + 1), target)

    def _loaded(self, c):
        if c["mode"] == "imm":
            if c["imm"] is not None:
                self.imms.add(c["imm"])
                return c["imm"]
            return None
        return self.origin(c["ea"])


def fmt(o):
    return "%s:%04X" % o


def write_log(col, path):
    """probes/addrorigin.lua's output: P lines (uses) and I lines (immediate origins)."""
    lines, seen = [], set()
    for (kind, where, hi, lo), (n, t0, t1) in col.uses.items():
        lines.append("P %s %s %s %s x%d %04X %04X" % (kind, fmt(where), fmt(hi), fmt(lo),
                                                      n, t0, t1))
        seen.add(hi)
        seen.add(lo)
    for o in col.imms:
        if o in seen:
            lines.append("I " + fmt(o))
    with open(path, "w") as f:
        for ln in sorted(lines):
            f.write(ln + "\n")
    return len(lines)


def trace(rom, out, frames=300, drive=True, mapper=None, low=None):
    cart = cart_module.Cart(rom, mapper=mapper, low=low)
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    col = OriginTracker()
    sim.run(cart, frames, region, drive=drive, observer=col)
    os.makedirs(out, exist_ok=True)
    n = write_log(col, os.path.join(out, "addrorigin.log"))
    return {"uses": len(col.uses), "lines": n, "instructions": len(col.x)}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-o", "--out", default="simorigins-out")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--no-drive", action="store_true")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    args = ap.parse_args(argv)
    if not os.path.isfile(args.rom):
        sys.exit("simorigins: no such file: %s" % args.rom)
    try:
        r = trace(args.rom, args.out, args.frames, not args.no_drive, args.mapper, args.low)
    except (cart_module.UnknownMapper, cart_module.UnknownSpace, IOError) as e:
        sys.exit("simorigins: %s" % e)
    print("%d frames: %d distinct uses of an address across %d instructions; wrote "
          "addrorigin.log in %s" % (args.frames, r["uses"], r["instructions"], args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
