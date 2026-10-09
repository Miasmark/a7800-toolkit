#!/usr/bin/env python3
"""An automatic ROM and RAM census, from a run in the simulator.

    python tools/census.py game.a78 -o census --frames 1800
    python tools/census.py game.a78 -o census --explore -c annotations.json
    python tools/census.py game.a78 -o census --merge earlier/census.json

Every byte of the cartridge is sorted by what the machine did with it:

  executed   an instruction ran there
  read       the CPU read it as data (a table, a constant, a string)
  MARIA      the display lists pointed MARIA's DMA at it (artwork, character sets)
  code, not  the static tracer reaches it as code but this run never ran it:
  run        a branch not taken, a state not entered -- real code, not exercised
  block      an annotations.json data block nothing touched
  fill       a run of one repeated byte: padding
  copy       identical, at the same address, to bytes another bank DID use: a shared
             library or table duplicated into every bank so each can reach it. Not
             unexplored: explained by its twin.
  DARK       none of the above. Not executed, not read, not drawn, and not reached by
             the tracer: the areas to look at. Each is described by what its bytes look
             like (text, an address table, plausible code, graphics-like, ...).

and RAM the same way: which bytes were written and read, which were read before the game
wrote them (so they hold whatever the BIOS left), which are pointers, counters, flags,
which stretches nothing and MARIA never touched (free RAM), and how deep the stack went.

"UNREACHABLE" HAS TO BE READ CAREFULLY. This reports what one simulated run and the static
trace did not reach. A level the run never entered is dark; so is code only a cheat code
calls. Dark means "nothing found it yet", not "nothing can". Two things make it more
honest: `--explore` sweeps the joystick as well as tapping fire, and adds a second run that also steps the difficulty switches and presses Select, Reset, Pause and the second buttons, and `--merge` unions this
run with earlier ones, so coverage can only grow. The guesses at what a dark area is are
heuristics and labelled as such.

HOW MARIA'S READS ARE FOUND. The display list list is walked every few frames (as
firstlook.py does for its screen) and every byte the objects' bitmaps and character sets
would be fetched from is marked. Registers are taken as they stand at the end of the
frame, so a character base or palette changed mid-frame by an interrupt is approximated.

The simulator's limits (simprobe.py) apply: a cartridge that stalls on hardware it does not
model gives a census of a short run, and the report says how far it got.
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cart as cart_module  # noqa: E402
import m6502  # noqa: E402
import sim  # noqa: E402
import simprobe  # noqa: E402
import branchforce  # noqa: E402

EXEC, READ, GFX, UNRUN, BLOCK, FILL, COPY, DARK, FORCED = range(1, 10)
NAMES = {EXEC: "executed", READ: "read as data", GFX: "fetched by MARIA",
         UNRUN: "code, not run", BLOCK: "block, untouched", FILL: "fill",
         COPY: "copy of used bytes", DARK: "DARK",
         FORCED: "code, found by forcing branches"}
COLOURS = {EXEC: (70, 150, 90), READ: (70, 110, 200), GFX: (160, 90, 200),
           UNRUN: (200, 180, 60), BLOCK: (60, 160, 170), FILL: (90, 90, 90),
           COPY: (120, 140, 150), DARK: (210, 60, 60), FORCED: (230, 140, 60)}
RAM_LO, RAM_HI = 0x1800, 0x27FF
MIN_FILL = 16


# --------------------------------------------------------------------- the run
class CensusCollector(simprobe.Collector):
    """simprobe's collector, plus RAM usage and what MARIA's DMA reads."""

    SETTLE = 40

    def __init__(self, cart, gfx_every=4, **kw):
        simprobe.Collector.__init__(self, **kw)
        self.cart = cart
        self.gfx_every = gfx_every
        self.rd, self.wr, self.first = {}, {}, {}
        self.vals, self.writers, self.ptr, self.incdec = {}, {}, set(), {}
        self.minsp = 0xFF
        self.gfx = set()                 # ROM locations MARIA read
        self.ram_dma = set()             # RAM addresses MARIA read
        self._mn = None
        self.sampled = 0

    @staticmethod
    def canon(addr):
        a = sim.fold(addr & 0xFFFF)
        return a if RAM_LO <= a <= RAM_HI else None

    def fetch(self, pc, opcode):
        simprobe.Collector.fetch(self, pc, opcode)
        self._mn = m6502.OPCODES[opcode][0]
        s = self.cpu.s
        if s < self.minsp:
            self.minsp = s

    def data_read(self, addr):
        simprobe.Collector.data_read(self, addr)
        a = self.canon(addr)
        if a is not None:
            self.rd[a] = self.rd.get(a, 0) + 1
            self.first.setdefault(a, "r")

    def data_write(self, addr, value):
        a = self.canon(addr)
        if a is None:
            return
        self.wr[a] = self.wr.get(a, 0) + 1
        self.first.setdefault(a, "w")
        v = self.vals.setdefault(a, set())
        if len(v) < 8:
            v.add(value)
        w = self.writers.setdefault(a, set())
        if len(w) < 4 and self._prev is not None:
            w.add(self._prev)
        if self._mn in ("INC", "DEC"):
            self.incdec[a] = self.incdec.get(a, 0) + 1

    def pointer(self, zp):
        for z in (zp, (zp + 1) & 0xFF):
            a = self.canon(z)
            if a is not None:
                self.ptr.add(a)

    def frame_start(self, frame):
        simprobe.Collector.frame_start(self, frame)
        if frame >= self.SETTLE and frame % self.gfx_every == 0:
            self.sample_display()

    # -- MARIA
    def _mark(self, addr, bank):
        addr &= 0xFFFF
        if addr >= 0x4000:
            sp = self.cart.space_of(addr, bank)
            if sp is not None:
                self.gfx.add((sp, addr))
        else:
            a = self.canon(addr)
            if a is not None:
                self.ram_dma.add(a)

    def sample_display(self):
        bus = self.bus
        if bus.dpph is None or (bus.ctrl & 0x60) != 0x40:
            return
        import firstlook
        d = firstlook.parse_regs_text(simprobe.regs_text(bus.frame, bus, {}, []))
        scr = firstlook.Screen(self.cart, simprobe.ram_bytes(bus), d)
        bank = bus.bank
        try:
            zones = list(scr.zones())
        except Exception:                                    # noqa: BLE001
            return
        self.sampled += 1
        for i in range(0, 3 * max(1, len(zones))):           # the list list itself
            self._mark(d["dll"] + i, bank)
        two = bool(d["ctrl"] & 0x10)
        for _y, z in zones:
            try:
                entries = scr.dlwalk.walk_dl(scr.src, z["dl"])
            except IndexError:
                continue
            for k in range(z["dl"], z["dl"] + 4 * len(entries) + 8):
                self._mark(k, bank)
            for e in entries:
                n = z["lines"]
                if not e["indirect"]:
                    for page in range(n):
                        for k in range(e["width"]):
                            self._mark(e["gfx"] + page * 256 + k, bank)
                else:
                    for k in range(e["width"]):
                        self._mark(e["gfx"] + k, bank)
                        code = scr.src.byte(e["gfx"] + k)
                        for page in range(n):
                            a = (((d["charbase"] + page) & 0xFF) << 8) | code
                            self._mark(a, bank)
                            if two:
                                self._mark(a + 1, bank)


def run(rom, frames=1800, explore=False, mapper=None, low=None, handover=None,
        drive=None, force=False):
    """One simulated run. `drive` is sim's (True taps fire, "explore" sweeps the stick,
    "switches" adds the console switches); by default `explore` picks "explore"."""
    cart = cart_module.Cart(rom, mapper=mapper, low=low)
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    start = sim.load_handover(handover) if handover else None
    col = CensusCollector(cart)
    mode = drive or ("explore" if explore else True)
    watcher = branchforce.Watcher(col) if force else None
    bus = sim.run(cart, frames, region,
                  drive=mode,
                  start_state=start, observer=watcher or col)
    col.forced, col.force_stats = set(), None
    if force:
        kept, dead, nfork = branchforce.force(cart, col, watcher, drive=mode)
        # only paths that rejoined code the run executed are counted as code; a path that
        # merely ran its budget is not evidence enough to print as an instruction
        col.forced = {l for l in kept if l not in col.x and kept[l] == "joined"}
        col.force_stats = {"forks": nfork, "dead": len(dead),
                           "joined": len(col.forced),
                           "ran on": sum(1 for l in kept
                                         if l not in col.x and kept[l] == "ran on")}
    return cart, col, bus


# ------------------------------------------------------------ the classification
def ranges(locs):
    """{space: [(lo, hi)]} from an iterable of (space, addr)."""
    by = {}
    for sp, a in locs:
        by.setdefault(sp, []).append(a)
    out = {}
    for sp, addrs in by.items():
        addrs.sort()
        cur = [addrs[0], addrs[0]]
        res = []
        for a in addrs[1:]:
            if a == cur[1] + 1:
                cur[1] = a
            else:
                res.append(tuple(cur))
                cur = [a, a]
        res.append(tuple(cur))
        out[sp] = res
    return out


def expand(rngs):
    for sp, rs in rngs.items():
        for lo, hi in rs:
            for a in range(lo, hi + 1):
                yield (sp, a)


def executed_bytes(cart, x):
    out = set()
    for sp, a in x:
        try:
            n = 1 + m6502.MODES[m6502.OPCODES[cart.byte(sp, a)][1]]
        except Exception:                                    # noqa: BLE001
            n = 1
        for i in range(n):
            out.add((sp, a + i))
    return out


def static_view(cart, config):
    """(bytes of static code, bytes of declared blocks) by (space, addr)."""
    import disasm
    cfg = disasm.Config(config) if config else disasm.Config()
    an, _g, _w, _v = disasm.analyse(cart, cfg)
    code = set()
    for (sp, a) in an.code:
        for i in range(an.insn[(sp, a)][3]):
            code.add((sp, a + i))
    return code, set(an.forced_data)


def canon_spaces(cart):
    """The spaces to report: each stretch of the file once. A SuperGame's last bank is
    both the fixed half at $C000 and a window at $8000; the fixed name wins."""
    names = cart.spaces()
    order = [s for s in names if not s.startswith("b")] + [s for s in names if s.startswith("b")]
    seen, out = set(), []
    for sp in order:
        key = (cart._file_base(sp), cart.size_of(sp))
        if key not in seen:
            seen.add(key)
            out.append(sp)
    return sorted(out, key=names.index)


def classify(cart, sets, static_code, blocks):
    """{space: bytearray of class codes, one per byte of the space}.

    Everything is keyed by file offset first, so a bank that is reachable at two
    addresses is one set of bytes, not two."""
    marks = bytearray(len(cart.rom))
    for kind, locs in ((EXEC, sets["exec"]), (READ, sets["read"]), (GFX, sets["gfx"]),
                       (UNRUN, static_code), (FORCED, sets.get("forced", ())),
                       (BLOCK, blocks)):
        for (sp, a) in locs:
            try:
                o = cart._offset(sp, a)
            except Exception:                                # noqa: BLE001
                continue
            if 0 <= o < len(marks) and marks[o] == 0:
                marks[o] = kind
    out = {}
    for sp in canon_spaces(cart):
        o0, size = cart._file_base(sp), cart.size_of(sp)
        cls = bytearray(marks[o0:o0 + size])
        data = cart.rom[o0:o0 + size]
        # fill: long runs of one value among what is still unexplained
        i = 0
        while i < size:
            if cls[i]:
                i += 1
                continue
            j = i
            while j + 1 < size and not cls[j + 1] and data[j + 1] == data[i]:
                j += 1
            if j - i + 1 >= MIN_FILL:
                for k in range(i, j + 1):
                    cls[k] = FILL
            i = j + 1
        out[sp] = cls
    # copies: unexplained bytes identical, at the same CPU address, to bytes another
    # bank used (so the copy is there for the day that bank is the one mapped in)
    used = (EXEC, READ, GFX, UNRUN, FORCED)
    names = list(out)
    for sp in names:
        base, size = cart.base_of(sp), cart.size_of(sp)
        o1 = cart._file_base(sp)
        for sp2 in names:
            if sp2 == sp or cart.base_of(sp2) != base or cart.size_of(sp2) != size:
                continue
            o2 = cart._file_base(sp2)
            c1, c2 = out[sp], out[sp2]
            for k in range(size):
                if c1[k] == 0 and c2[k] in used and cart.rom[o1 + k] == cart.rom[o2 + k]:
                    c1[k] = COPY
    for sp in names:
        c = out[sp]
        for k in range(len(c)):
            if not c[k]:
                c[k] = DARK
    return out


def runs_of(cls, kind, base):
    out, i, n = [], 0, len(cls)
    while i < n:
        if cls[i] == kind:
            j = i
            while j + 1 < n and cls[j + 1] == kind:
                j += 1
            out.append((base + i, base + j))
            i = j + 1
        else:
            i += 1
    return out


# ------------------------------------------------------- what an area looks like
def decodable(data):
    """Fraction of `data` covered by a straight run of documented instructions that
    contains control flow, from offset 0; (fraction, has_flow)."""
    i, flow = 0, False
    n = len(data)
    while i < n:
        mn, mode, illegal = m6502.OPCODES[data[i]]
        ln = 1 + m6502.MODES[mode]
        if illegal or i + ln > n:
            break
        if mn in ("RTS", "RTI", "JMP", "JSR", "BNE", "BEQ", "BCC", "BCS", "BPL", "BMI"):
            flow = True
        i += ln
    return i / float(n), flow


def guess(data):
    """(label, evidence) for a block of dark bytes. A heuristic, said as such."""
    n = len(data)
    if n == 0:
        return "empty", ""
    counts = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    top, topn = max(counts.items(), key=lambda kv: kv[1])
    if topn >= 0.9 * n:
        return "fill", "%d%% are $%02X" % (100 * topn // n, top)
    pr = sum(1 for b in data if 0x20 <= b < 0x7F)
    if n >= 8 and pr >= 0.85 * n:
        return "text", "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data[:24])
    frac, flow = decodable(data)
    if n >= 12 and frac >= 0.9 and flow:
        return "code-like", "%d%% decodes as documented instructions with branches or returns" % (100 * frac)
    if n >= 8:
        words = [data[i] | (data[i + 1] << 8) for i in range(0, n - 1, 2)]
        near = sum(1 for w in words if w >= 0x4000)
        if near >= 0.8 * len(words) and len(set(words)) >= 0.5 * len(words):
            return "address table?", "%d of %d words fall in $4000-$FFFF" % (near, len(words))
        pairs = sum(1 for i in range(0, n - 1, 2) if data[i + 1] >= 0x40)
        if pairs >= 0.8 * (n // 2) and len(set(data[1::2])) <= 6:
            return "address table? (high bytes cluster)", "high bytes %s" % sorted(set(data[1::2]))[:6]
    zeros = counts.get(0, 0)
    if len(counts) <= 32 or zeros >= 0.25 * n:
        return "graphics-like", "%d distinct values, %d%% zero, in %d bytes" % (
            len(counts), 100 * zeros // n, n)
    return "data, no clear shape", "%d distinct values in %d bytes" % (len(counts), n)


# --------------------------------------------------------------------- the RAM
def ram_report(col):
    """(rows, uninit, free, ranges) from what the run did to RAM."""
    rows, uninit = [], []
    touched = set(col.rd) | set(col.wr)
    for a in sorted(touched):
        r, w = col.rd.get(a, 0), col.wr.get(a, 0)
        vals = col.vals.get(a, set())
        if a in col.ptr:
            role = "pointer"
        elif w and col.incdec.get(a, 0) * 2 >= w:
            role = "counter"
        elif w and len(vals) <= 2 and len(vals) > 0:
            role = "flag"
        elif w == 1 or (w and len(vals) == 1):
            role = "set once, then read" if r else "set once"
        elif w and not r:
            role = "written, never read"
        elif r and not w:
            role = "read, never written"
        else:
            role = "variable"
        if col.first.get(a) == "r":
            uninit.append(a)
        rows.append({"addr": a, "reads": r, "writes": w, "role": role,
                     "first": col.first.get(a, ""), "values": sorted(vals)[:8],
                     "writers": sorted(col.writers.get(a, ()))})
    used = touched | col.ram_dma
    free, cur = [], None
    for a in range(RAM_LO, RAM_HI + 1):
        if a in used:
            if cur:
                free.append(tuple(cur))
                cur = None
        elif cur:
            cur[1] = a
        else:
            cur = [a, a]
    if cur:
        free.append(tuple(cur))
    free = [r for r in free if r[1] - r[0] + 1 >= 8]
    return rows, uninit, free


def short(addr):
    """RAM addresses the way the 6502 sees the first two pages."""
    if 0x2040 <= addr <= 0x20FF:
        return "$%02X" % (addr - 0x2000)
    if 0x2140 <= addr <= 0x21FF:
        return "$%04X" % (addr - 0x2000)
    return "$%04X" % addr


def spans(addrs):
    """'$40-$4F, $52' from a sorted list of RAM addresses."""
    out, start, prev = [], None, None
    for a in addrs:
        if start is None:
            start = prev = a
        elif a == prev + 1:
            prev = a
        else:
            out.append((start, prev))
            start = prev = a
    if start is not None:
        out.append((start, prev))
    return ", ".join(short(lo) if lo == hi else "%s-%s" % (short(lo), short(hi))
                     for lo, hi in out)


# ------------------------------------------------------------------- the output
def build(rom, frames, explore, config=None, merge=(), mapper=None, low=None,
          handover=None, force=False):
    # Exploring is two runs, unioned: the stick alone, and the stick with the console
    # switches. Pressing Select or Reset changes where a game goes -- it opens code
    # behind the switches but can also keep a game from reaching what plain play does
    # (measured: Mat Mania 244 -> 2133 instructions, Galaga 1893 -> 1491) -- so neither
    # run contains the other.
    cart, col, bus = run(rom, frames, explore, mapper, low, handover, None, force)
    sets = {"exec": executed_bytes(cart, col.x), "read": set(col.d), "gfx": set(col.gfx),
            "forced": executed_bytes(cart, col.forced)}
    notes = []
    stats = [col.force_stats]
    if explore:
        cart2, col2, bus2 = run(rom, frames, explore, mapper, low, handover, "switches",
                                force)
        stats.append(col2.force_stats)
        sets["forced"] |= executed_bytes(cart2, col2.forced)
        sets["exec"] |= executed_bytes(cart2, col2.x)
        sets["read"] |= set(col2.d)
        sets["gfx"] |= set(col2.gfx)
        notes.append("unioned with a second run that also worked the console switches: "
                     "%d and %d instructions ran" % (len(col.x), len(col2.x)))
        if len(col2.x) > len(col.x):
            col, bus = col2, bus2
    # the CPU reads the interrupt and reset vectors itself, not through an instruction
    vsp = cart.space_of(0xFFFA, None)
    if vsp:
        sets["read"] |= {(vsp, 0xFFFA + i) for i in range(6)}
    for path in merge:
        old = json.load(io.open(path, encoding="utf-8"))
        for key in ("exec", "read", "gfx", "forced"):
            sets[key] |= set(expand({sp: [tuple(r) for r in rs]
                                     for sp, rs in old["sets"].get(key, {}).items()}))
        notes.append("merged %s (%d frames)" % (os.path.basename(path), old["frames"]))
    stats = [s for s in stats if s]
    if stats:
        notes.append("forcing the untaken side of branches (branchforce.py) ran %d paths "
                     "and kept %d instructions the runs never executed, all on paths that "
                     "rejoined code that ran, marked apart from what executed; %d more "
                     "ran on without rejoining and are not counted, and %d dead paths "
                     "were trimmed"
                     % (sum(s["forks"] for s in stats), len(sets["forced"]),
                        sum(s["ran on"] for s in stats), sum(s["dead"] for s in stats)))
    static_code, blocks = static_view(cart, config)
    cls = classify(cart, sets, static_code, blocks)
    return {"cart": cart, "col": col, "bus": bus, "sets": sets, "cls": cls,
            "notes": notes, "frames": frames, "explore": explore,
            "static_code": static_code, "blocks": blocks}


def summary(cart, cls):
    per, tot = {}, {k: 0 for k in NAMES}
    for sp, c in cls.items():
        d = {k: 0 for k in NAMES}
        for v in c:
            d[v] += 1
        per[sp] = d
        for k in d:
            tot[k] += d[k]
    return per, tot


def dark_areas(cart, cls, min_size=4):
    out = []
    for sp, c in cls.items():
        base = cart.base_of(sp)
        for lo, hi in runs_of(c, DARK, base):
            if hi - lo + 1 < min_size:
                continue
            data = cart.slice(sp, lo, hi - lo + 1)
            label, why = guess(data)
            out.append({"space": sp, "lo": lo, "hi": hi, "size": hi - lo + 1,
                        "guess": label, "evidence": why})
    out.sort(key=lambda r: -r["size"])
    return out


def suggestions(dark):
    """Annotation fragments the dark areas suggest. GUESSES: each is a proposal to read
    in the listing, not a finding; nothing here is applied for you."""
    out = []
    for a in dark:
        loc = "%s:%04X" % (a["space"], a["lo"])
        g = a["guess"]
        if g == "code-like":
            out.append({"kind": "entry", "loc": loc, "size": a["size"],
                        "why": "dark and decodes as code: try it as an entry point"})
        elif g == "text":
            out.append({"kind": "block", "loc": loc, "len": a["size"], "type": "text",
                        "why": "printable text nothing referenced"})
        elif g.startswith("address table"):
            out.append({"kind": "block", "loc": loc, "len": a["size"] & ~1, "type": "words",
                        "why": "pairs of bytes that look like addresses"})
    return out


def forced_suggestions(cart, cls):
    """An entry for each run of code only branch forcing found. Still a proposal: the
    path rejoined real code, but no run has been seen to take it."""
    out = []
    for sp in cls:
        for lo, hi in runs_of(cls[sp], FORCED, cart.base_of(sp)):
            out.append({"kind": "entry", "loc": "%s:%04X" % (sp, lo), "size": hi - lo + 1,
                        "why": "reached by forcing an untaken branch; the path rejoined "
                               "code that ran"})
    return out


def pct(a, b):
    return "%.1f%%" % (100.0 * a / b) if b else "-"


def markdown(name, r):
    cart, col, cls = r["cart"], r["col"], r["cls"]
    per, tot = summary(cart, cls)
    total = sum(tot.values())
    dark = dark_areas(cart, cls)
    L = ["# Census: %s" % name, ""]
    L.append("%d frames simulated%s; the display list was live on %d of them; %d "
             "distinct instructions ran; MARIA's reads were sampled on %d frames."
             % (r["frames"],
                " with the joystick swept, and a second run on the console switches"
                if r["explore"] else "",
                col.frames_with_list, len(col.x), col.sampled))
    for n in r["notes"]:
        L.append("(%s.)" % n)
    if col.frames_with_list < r["frames"] * 0.5:
        L.append("")
        L.append("**The run stalled** -- a display list on fewer than half the frames. "
                 "Whatever is called dark below may only be what the simulator never got "
                 "to; see simprobe.py for what it does not model.")
    L += ["", "## ROM", "", "| what the machine did with it | bytes | share |", "|---|---:|---:|"]
    for k in (EXEC, READ, GFX, UNRUN, FORCED, BLOCK, FILL, COPY, DARK):
        if k == FORCED and not tot[k]:
            continue
        L.append("| %s | %d | %s |" % (NAMES[k], tot[k], pct(tot[k], total)))
    L += ["", "Per space:", "", "| space | executed | read | MARIA | code, not run | forced | block | fill | copy | DARK |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for sp in canon_spaces(cart):
        d = per[sp]
        L.append("| %s | %d | %d | %d | %d | %d | %d | %d | %d | %d |" % (
            sp, d[EXEC], d[READ], d[GFX], d[UNRUN], d[FORCED], d[BLOCK], d[FILL], d[COPY],
            d[DARK]))
    L += ["", "## Dark areas: nothing ran, read or drew them, and the tracer does not reach them", ""]
    if not dark:
        L.append("None of 4 bytes or more.")
    else:
        L += ["| where | bytes | looks like | evidence |", "|---|---:|---|---|"]
        for a in dark[:60]:
            L.append("| `%s:%04X-%04X` | %d | %s | %s |" % (
                a["space"], a["lo"], a["hi"], a["size"], a["guess"],
                a["evidence"].replace("|", "/")))
        if len(dark) > 60:
            L.append("")
            L.append("... and %d smaller or later areas (`census.json`)." % (len(dark) - 60))
        kinds = {}
        for a in dark:
            kinds[a["guess"]] = kinds.get(a["guess"], 0) + a["size"]
        L += ["", "By what they look like: " + ", ".join(
            "%s %d bytes" % kv for kv in sorted(kinds.items(), key=lambda kv: -kv[1])) + "."]
    sug = suggestions(dark) + forced_suggestions(cart, cls)
    if sug:
        L += ["", "## What the dark areas suggest (guesses)", "",
              "%d code-like, %d text, %d address-table-like. In `census.json` under "
              "`suggestions`, as `entries` and `blocks` for annotations.json; read each in "
              "the listing before keeping it." % (
                  sum(1 for x in sug if x["kind"] == "entry"),
                  sum(1 for x in sug if x.get("type") == "text"),
                  sum(1 for x in sug if x.get("type") == "words"))]
    unrun = [(sp, lo, hi) for sp in cls for lo, hi in runs_of(cls[sp], UNRUN, cart.base_of(sp))]
    if unrun:
        unrun.sort(key=lambda t: t[1] - t[2])
        L += ["", "## Code the tracer reaches that this run did not execute", "",
              "%d ranges, %d bytes; the largest:" % (len(unrun), tot[UNRUN]), ""]
        for sp, lo, hi in unrun[:12]:
            L.append("- `%s:%04X-%04X` (%d bytes)" % (sp, lo, hi, hi - lo + 1))
    rows, uninit, free = ram_report(col)
    L += ["", "## RAM", ""]
    used = len(rows)
    L.append("%d of %d bytes were touched by the CPU; MARIA read %d RAM bytes (display "
             "lists, graphics in RAM); stack high-water mark %d bytes."
             % (used, RAM_HI - RAM_LO + 1, len(col.ram_dma), 0xFF - col.minsp))
    byrole = {}
    for x in rows:
        byrole.setdefault(x["role"], []).append(x["addr"])
    L += ["", "| role | bytes | where |", "|---|---:|---|"]
    for role, addrs in sorted(byrole.items(), key=lambda kv: -len(kv[1])):
        L.append("| %s | %d | %s |" % (role, len(addrs), spans(addrs)[:110]))
    if uninit:
        L += ["", "**Read before the game wrote them** (%d bytes, so they held whatever the BIOS "
              "or power-up left): %s" % (len(uninit), spans(uninit)[:300]),
              "A game that depends on those is the kind `sim.py --handover` exists for."]
    ptrs = sorted(col.ptr)
    if ptrs:
        L += ["", "Pointer pairs (used through `(zp),Y` or `(zp,X)`): %s" % spans(ptrs)[:300]]
    L += ["", "### Free RAM: touched by neither the CPU nor MARIA in this run", ""]
    if free:
        tot_free = sum(h - lo + 1 for lo, h in free)
        L.append("%d bytes in %d ranges of 8 or more: %s%s" % (
            tot_free, len(free), ", ".join("%s-%s (%d)" % (short(lo), short(hi), hi - lo + 1)
                                          for lo, hi in sorted(free, key=lambda r: r[0] - r[1])[:12]),
            "" if len(free) <= 12 else " ..."))
        L.append("Free in this run is not free for good: a state the run never reached may use it.")
    else:
        L.append("None.")
    return "\n".join(L) + "\n"


def write_png(cart, cls, outdir):
    try:
        from PIL import Image
    except ImportError:
        return []
    names = []
    for sp in canon_spaces(cart):
        c = cls[sp]
        size = len(c)
        cols = 256 if size >= 32768 else 128 if size >= 8192 else 64
        rows = (size + cols - 1) // cols
        img = Image.new("RGB", (cols, rows), (20, 20, 24))
        px = img.load()
        for i, v in enumerate(c):
            px[i % cols, i // cols] = COLOURS[v]
        scale = 4 if cols <= 128 else 3
        img = img.resize((cols * scale, rows * scale), Image.NEAREST)
        path = os.path.join(outdir, "census-%s.png" % sp)
        img.save(path)
        names.append(os.path.basename(path))
    return names


def to_json(name, r):
    cart, col = r["cart"], r["col"]
    per, tot = summary(cart, r["cls"])
    rows, uninit, free = ram_report(col)
    return {"rom": name, "frames": r["frames"], "explore": r["explore"],
            "frames_with_display_list": col.frames_with_list,
            "totals": {NAMES[k]: v for k, v in tot.items()},
            "per_space": {sp: {NAMES[k]: v for k, v in d.items()} for sp, d in per.items()},
            "dark": dark_areas(cart, r["cls"]),
            "suggestions": suggestions(dark_areas(cart, r["cls"]))
            + forced_suggestions(cart, r["cls"]),
            "sets": {k: ranges(v) for k, v in r["sets"].items()},
            "ram": {"touched": len(rows), "uninitialised_reads": uninit,
                    "pointers": sorted(col.ptr), "free": free,
                    "stack_high_water": 0xFF - col.minsp, "bytes": rows}}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-o", "--out", default="census")
    ap.add_argument("--frames", type=int, default=1800)
    ap.add_argument("--explore", action="store_true",
                    help="sweep the joystick as well as tapping fire, and add a second run that works the console switches and second buttons; the two are unioned")
    ap.add_argument("--force", action="store_true",
                    help="also take the untaken side of each branch (branchforce.py) and "
                         "mark the code that finds apart from what executed; slow")
    ap.add_argument("-c", "--config", help="annotations.json: its data blocks and entries count")
    ap.add_argument("--merge", action="append", default=[], metavar="CENSUS.JSON",
                    help="union an earlier census (repeatable); coverage only grows")
    ap.add_argument("--handover", metavar="LOG")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    args = ap.parse_args(argv)
    if not os.path.isfile(args.rom):
        sys.exit("census: no such file: %s" % args.rom)
    try:
        r = build(args.rom, args.frames, args.explore, args.config, args.merge,
                  args.mapper, args.low, args.handover, args.force)
    except (cart_module.UnknownMapper, cart_module.UnknownSpace, IOError) as e:
        sys.exit("census: %s" % e)
    os.makedirs(args.out, exist_ok=True)
    name = os.path.basename(args.rom)
    io.open(os.path.join(args.out, "census.md"), "w", encoding="utf-8").write(
        markdown(name, r))
    io.open(os.path.join(args.out, "census.json"), "w", encoding="utf-8").write(
        json.dumps(to_json(name, r), indent=1))
    pngs = write_png(r["cart"], r["cls"], args.out)
    per, tot = summary(r["cart"], r["cls"])
    total = sum(tot.values())
    print("ROM: %s executed, %s read, %s drawn by MARIA, %s code not run, %s copies of "
          "used bytes, %s DARK"
          % (pct(tot[EXEC], total), pct(tot[READ], total), pct(tot[GFX], total),
             pct(tot[UNRUN], total), pct(tot[COPY], total), pct(tot[DARK], total)))
    dk = dark_areas(r["cart"], r["cls"])
    print("%d dark areas of 4 bytes or more; the largest: %s" % (
        len(dk), ", ".join("%s:%04X (%d, %s)" % (a["space"], a["lo"], a["size"], a["guess"])
                           for a in dk[:3]) or "none"))
    print("wrote census.md census.json%s in %s" % (
        " and %d coverage maps" % len(pngs) if pngs else "", args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
