#!/usr/bin/env python3
"""Find code a run never reached by taking the branches the game did not take.

    python tools/branchforce.py game.a78 --frames 240
    python tools/branchforce.py game.a78 --frames 240 --truth 1800     # and grade it

A simulated run only follows the sides of each conditional branch the game's own state
chose. This tool watches that run, and at every branch whose other side has not run,
keeps a copy of the machine -- registers, RAM, the bank mapped. After the run each copy
is restarted on the OTHER side of its branch and run for a while on its own, in a
sandbox, without asking what would have made the branch go that way.

A forced path is then judged by where it goes:

  joined    it reached an instruction the real run executed. Kept: the two sides of an
            if/else, or an error path that returns to the main loop.
  ran on    it ran its whole budget, or settled into a wait loop, without trouble. Kept,
            but reported apart, as less sure.
  dead      it executed something that is not code: an undefined or illegal opcode, BRK,
            JAM, an address that is neither ROM nor RAM, or a byte the real run reads as
            data. The whole path is thrown away -- everything it ran since the fork, not
            just the last instruction, because it was never real code that got there.

What this adds over `disasm.py`'s tracer, which follows both sides of every branch anyway,
is concrete values: the forced path runs with real registers and RAM, so a computed jump,
a jump table or a pushed return address on that side is followed to where it goes, and
bank switches in it are taken. What it cannot do is know the forced side is possible --
the registers do not agree with the branch, so a path can run to a plausible place that
the game never reaches. `--truth` measures how often that happens against a longer run.

Slow in Python; a few hundred frames and a few hundred forks is the practical size.
"""
import argparse
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cart as cart_module  # noqa: E402
import disasm  # noqa: E402
import m6502  # noqa: E402
import sim  # noqa: E402
import simprobe  # noqa: E402

BRANCHES = {0x10: ("N", 0), 0x30: ("N", 1), 0x50: ("V", 0), 0x70: ("V", 1),
            0x90: ("C", 0), 0xB0: ("C", 1), 0xD0: ("Z", 0), 0xF0: ("Z", 1)}
FLAG = {"N": sim.N, "V": sim.V, "C": sim.C, "Z": sim.Z}
# hand-written code in RAM runs from main RAM, not the zero page or I/O
RAM_CODE = (0x1800, 0x27FF)


class Dead(Exception):
    def __init__(self, why):
        Exception.__init__(self, why)
        self.why = why


class Watcher(sim.Observer):
    """Passes every hook to `inner` (the collector the caller already uses) and keeps
    copies of the machine at branches whose other side has not run."""

    HOOKS = ("frame_start", "fetch", "data_read", "jump_indirect", "call", "ret",
             "bank_switch", "nmi", "maria_write", "data_write", "pointer")

    def __init__(self, inner, max_keys=3000, at=(1, 16, 256)):
        self.inner = inner
        self.max_keys = max_keys
        self.at = set(at)
        self.count = {}              # (branch loc, taken) -> times seen
        self.snaps = {}              # (branch loc, taken) -> [snapshot]
        self.illegal_seen = False

    def attach(self, bus, cpu):
        sim.Observer.attach(self, bus, cpu)
        self.inner.attach(bus, cpu)

    def frame_start(self, frame):
        self.inner.frame_start(frame)

    def data_read(self, addr):
        self.inner.data_read(addr)

    def jump_indirect(self, pc, target):
        self.inner.jump_indirect(pc, target)

    def call(self, pc, target, ret, sp):
        self.inner.call(pc, target, ret, sp)

    def ret(self, pc, target, sp):
        self.inner.ret(pc, target, sp)

    def bank_switch(self, addr, value, bank):
        self.inner.bank_switch(addr, value, bank)

    def nmi(self):
        self.inner.nmi()

    def maria_write(self, reg, value):
        self.inner.maria_write(reg, value)

    def data_write(self, addr, value):
        self.inner.data_write(addr, value)

    def pointer(self, zp):
        self.inner.pointer(zp)

    def fetch(self, pc, opcode):
        self.inner.fetch(pc, opcode)
        info = m6502.OPCODES.get(opcode)
        if info is not None and info[2] and info[0] not in ("NOP", "JAM"):
            self.illegal_seen = True
        br = BRANCHES.get(opcode)
        if br is None:
            return
        cpu, bus = self.cpu, self.bus
        loc = self.inner.loc(pc)
        if loc is None:
            return
        flag, want = br
        taken = bool(cpu.p & FLAG[flag]) == bool(want)
        key = (loc, taken)
        n = self.count.get(key, 0) + 1
        self.count[key] = n
        if n not in self.at or (key not in self.snaps and len(self.snaps) >= self.max_keys):
            return
        off = bus.read(pc + 1)
        target = (pc + 2 + (off - 256 if off & 0x80 else off)) & 0xFFFF
        alt = (pc + 2) & 0xFFFF if taken else target
        self.snaps.setdefault(key, []).append(snapshot(bus, cpu, alt))


def snapshot(bus, cpu, pc):
    return {"pc": pc, "regs": (cpu.a, cpu.x, cpu.y, cpu.s, cpu.p),
            "ram": zlib.compress(bytes(bus.ram), 1), "bank": bus.bank,
            "maria": dict(bus.maria), "ctrl": bus.ctrl, "dpph": bus.dpph,
            "dppl": bus.dppl, "frame": bus.frame, "vblank": bus.vblank,
            "cycles": cpu.cycles}


def restore(cart, snap, drive, observer):
    bus = sim.Bus(cart, drive=drive)
    cpu = sim.CPU(bus)
    bus.ram[:] = zlib.decompress(snap["ram"])
    bus.bank = snap["bank"]
    bus.maria = dict(snap["maria"])
    bus.ctrl, bus.dpph, bus.dppl = snap["ctrl"], snap["dpph"], snap["dppl"]
    bus.frame, bus.vblank = snap["frame"], snap["vblank"]
    cpu.a, cpu.x, cpu.y, cpu.s, cpu.p = snap["regs"]
    cpu.pc = snap["pc"]
    cpu.cycles = snap["cycles"]
    cpu.obs = bus.obs = observer
    observer.attach(bus, cpu)
    return bus, cpu


class Trace(simprobe.Collector):
    """What one forced path runs, and the checks that declare it dead."""

    def __init__(self, real, allow_illegal, budget):
        simprobe.Collector.__init__(self)
        self.real = real
        self.allow_illegal = allow_illegal
        self.budget = budget
        self.n = 0
        self.order = []
        self.verdict = None

    def fetch(self, pc, opcode):
        info = m6502.OPCODES.get(opcode)
        if info is None or info[0] in ("JAM", "BRK"):
            raise Dead("opcode $%02X" % opcode)
        if info[2] and not self.allow_illegal and info[0] != "NOP":
            raise Dead("illegal opcode %s" % info[0])
        loc = self.loc(pc)
        if loc is None:
            f = sim.fold(pc)
            if not (RAM_CODE[0] <= f <= RAM_CODE[1]):
                raise Dead("fetch from $%04X" % pc)
        else:
            if loc in self.real.d and loc not in self.real.x:
                raise Dead("%s:%04X is read as data" % loc)
            if self.n and loc in self.real.x:
                self.verdict = "joined"
                raise StopIteration
            if loc not in self.x:
                self.order.append(loc)
        simprobe.Collector.fetch(self, pc, opcode)
        self.n += 1
        if self.stuck > 8:
            self.verdict = "ran on"
            raise StopIteration
        if self.n > self.budget:
            self.verdict = "ran on"
            raise StopIteration


def force(cart, real, watcher, budget=2000, drive=True, log=None):
    """Run every saved copy down its other side. Returns (kept, dead, trimmed) where
    `kept` maps location -> "joined" / "ran on", `dead` maps branch location -> why."""
    kept, dead = {}, {}
    nfork = 0
    for (loc, taken), snaps in sorted(watcher.snaps.items()):
        alt_known = False
        for snap in snaps:
            tr = Trace(real, watcher.illegal_seen, budget)
            bus, cpu = restore(cart, snap, drive, tr)
            if not alt_known:
                t0 = tr.loc(snap["pc"])
                if t0 is not None and t0 in real.x:
                    alt_known = True
            if alt_known:
                break
            nfork += 1
            try:
                for _ in range(budget + 2):
                    cpu.step()
            except Dead as d:
                dead.setdefault(loc, d.why)
                continue
            except StopIteration:
                pass
            except Exception as e:                         # noqa: BLE001
                dead.setdefault(loc, "simulator: %s" % e)
                continue
            if tr.verdict is None:
                tr.verdict = "ran on"
            dead.pop(loc, None)
            for l in tr.order:
                if l not in kept or tr.verdict == "joined":
                    kept[l] = tr.verdict
            break
    return kept, dead, nfork


def null_rate(cart, real, watcher, n=300, budget=2000, drive=True, seed=1):
    """How often a path started at bytes the real run READ AS DATA is kept anyway. This is
    the test's false-positive rate: the same snapshots, the same budget, but the start is
    something known not to be code."""
    import random
    import re
    rng = random.Random(seed)
    starts = sorted(l for l in real.d if l not in real.x)
    snaps = [sn for v in watcher.snaps.values() for sn in v]
    out = {"joined": 0, "ran on": 0, "dead": 0}
    if not starts or not snaps:
        return out
    for _ in range(n):
        loc = rng.choice(starts)
        snap = dict(rng.choice(snaps))
        snap["pc"] = loc[1]
        m = re.match(r"b(\d+)$", loc[0])
        if m:
            snap["bank"] = int(m.group(1))
        tr = Trace(real, watcher.illegal_seen, budget)
        bus, cpu = restore(cart, snap, drive, tr)
        try:
            for _i in range(budget + 2):
                cpu.step()
        except Dead:
            out["dead"] += 1
            continue
        except StopIteration:
            pass
        except Exception:                                  # noqa: BLE001
            out["dead"] += 1
            continue
        out[tr.verdict or "ran on"] += 1
    return out


def explore(rom, frames=240, budget=2000, drive="explore", mapper=None, low=None):
    cart = cart_module.Cart(rom, mapper=mapper, low=low)
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    real = simprobe.Collector()
    w = Watcher(real)
    sim.run(cart, frames, region, drive=drive, observer=w)
    kept, dead, nfork = force(cart, real, w, budget, drive)
    return {"cart": cart, "real": real, "kept": kept, "dead": dead, "forks": nfork,
            "sites": len(w.snaps), "watcher": w}


def truth_run(cart, frames, drive="explore"):
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    col = simprobe.Collector()
    sim.run(cart, frames, region, drive=drive, observer=col)
    return col.x


def grade(r, truth):
    """Against a longer run: how much of what was forced is real, and how much of what the
    short run missed was found. Joined and ran-on paths are scored apart."""
    real = r["real"].x
    out = {}
    for why in ("joined", "ran on"):
        locs = {l for l, v in r["kept"].items() if v == why and l not in real}
        out[why] = (len(locs), len(locs & truth))
    missed = truth - real
    found = {l for l in r["kept"] if l not in real} & truth
    out["missed"] = len(missed)
    out["found"] = len(found)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("--frames", type=int, default=240)
    ap.add_argument("--budget", type=int, default=2000,
                    help="instructions a forced path may run (default 2000)")
    ap.add_argument("--truth", type=int, metavar="FRAMES",
                    help="also run this many frames and grade the forced paths against it")
    ap.add_argument("--no-explore", action="store_true",
                    help="drive with fire taps only, not the joystick and switches")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    args = ap.parse_args(argv)
    if not os.path.isfile(args.rom):
        sys.exit("branchforce: no such file: %s" % args.rom)
    drive = True if args.no_explore else "explore"
    try:
        r = explore(args.rom, args.frames, args.budget, drive, args.mapper, args.low)
    except (cart_module.UnknownMapper, cart_module.UnknownSpace, IOError) as e:
        sys.exit("branchforce: %s" % e)
    real = r["real"].x
    new = {l: v for l, v in r["kept"].items() if l not in real}
    print("%d frames: %d instructions ran; %d branches with an untaken side; "
          "%d forced paths run" % (args.frames, len(real), r["sites"], r["forks"]))
    for why in ("joined", "ran on"):
        print("  %-7s %6d new instructions" % (why, sum(1 for v in new.values() if v == why)))
    reasons = {}
    for why in r["dead"].values():
        key = why.split(" ")[0] if not why.startswith("illegal") else "illegal"
        reasons[key] = reasons.get(key, 0) + 1
    print("  dead    %6d paths trimmed (%s)" % (
        len(r["dead"]), ", ".join("%s %d" % kv for kv in sorted(reasons.items())) or "-"))
    an, _g, _w, _v = disasm.analyse(r["cart"], disasm.Config())
    beyond = {l: v for l, v in new.items() if l not in an.code}
    print("  of the new instructions, %d are not reached by the static tracer either "
          "(%d joined, %d ran on)" % (
              len(beyond), sum(1 for v in beyond.values() if v == "joined"),
              sum(1 for v in beyond.values() if v == "ran on")))
    nl = null_rate(r["cart"], r["real"], r["watcher"], 300, args.budget, drive)
    tot = max(sum(nl.values()), 1)
    print("  control: paths started at bytes the run read as data -> %.0f%% joined, "
          "%.0f%% ran on, %.0f%% dead" % (100.0 * nl["joined"] / tot,
                                          100.0 * nl["ran on"] / tot,
                                          100.0 * nl["dead"] / tot))
    if args.truth:
        truth = truth_run(r["cart"], args.truth, drive)
        t_beyond = {l for l in beyond if l in truth}
        print("against the longer run: %d of those %d beyond-static instructions really "
              "ran; the static tracer misses %d of the %d the longer run executed"
              % (len(t_beyond), len(beyond), sum(1 for l in truth if l not in an.code),
                 len(truth)))
        g = grade(r, truth)
        print("against a %d-frame run (which executed %d instructions, %d the short run "
              "missed):" % (args.truth, len(truth), g["missed"]))
        for why in ("joined", "ran on"):
            n, ok = g[why]
            print("  %-7s %6d new, %6d real (%.1f%%)" % (why, n, ok, 100.0 * ok / max(n, 1)))
        print("  found %d of the %d instructions the short run missed (%.1f%%)"
              % (g["found"], g["missed"], 100.0 * g["found"] / max(g["missed"], 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
