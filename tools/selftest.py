#!/usr/bin/env python3
"""
Run the toolkit against itself.

    python tools/selftest.py [--rom game.a78] [--format f.json] [--log capture.log]

Most of this runs with no cartridge at all, because the toolkit ships without
one: the example songs, the display-list decoder, the note tables, the cycle
table, the docs and the refusals are all self-contained. Point `--rom` at an
image and the round trips run too.

This exists because the regression was six commands typed from memory, and the
thing that slipped through was not a crash -- it was a number in the docs that
had quietly stopped being true. So the doc checks sit here alongside the code
checks, and they are not softer.
"""
import argparse
import atexit
import collections
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

PASS, FAIL, SKIP = "ok", "FAILED", "skipped"


class Results(object):
    def __init__(self, verbose=False):
        self.rows = []
        self.verbose = verbose

    def add(self, name, status, detail=""):
        self.rows.append((name, status, detail))
        mark = {PASS: "  ok  ", FAIL: " FAIL ", SKIP: "  --  "}[status]
        print(("%s %-34s %s" % (mark, name, detail)).rstrip())
        return status == PASS

    def check(self, name, fn):
        try:
            detail = fn()
        except Exception as e:                                   # noqa: BLE001
            if self.verbose:
                import traceback
                traceback.print_exc()
            return self.add(name, FAIL, "%s: %s" % (type(e).__name__, e))
        if detail is None:
            return self.add(name, SKIP, "nothing to test")
        return self.add(name, PASS, detail)

    @property
    def failed(self):
        return [r for r in self.rows if r[1] == FAIL]


def run_tool(name, *args):
    p = subprocess.run([sys.executable, os.path.join(HERE, name)] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.stdout.decode("utf-8", "replace")


# --------------------------------------------------------- without a cartridge

def t_json():
    """Every shipped JSON parses, and the template documents its own keys."""
    files = (glob.glob(os.path.join(ROOT, "formats", "*.json"))
             + glob.glob(os.path.join(ROOT, "templates", "*.json"))
             + glob.glob(os.path.join(ROOT, "examples", "*.json")))
    def no_dupes(pairs):
        # json keeps the LAST of a repeated key, silently dropping the first --
        # which is how a template's explanation of "blocks" went missing
        keys = [k for k, _v in pairs]
        dup = sorted(set(k for k in keys if keys.count(k) > 1))
        if dup:
            raise AssertionError("duplicate key %s" % ", ".join(dup))
        return dict(pairs)
    for f in files:
        try:
            json.load(io.open(f, encoding="utf-8"), object_pairs_hook=no_dupes)
        except AssertionError as e:
            raise AssertionError("%s: %s" % (os.path.relpath(f, ROOT), e))
    tpl = json.load(io.open(os.path.join(ROOT, "templates", "format.json"),
                            encoding="utf-8"))
    # A key with no underscore twin is a key nobody explained.
    documented = set(k[1:] for k in tpl if k.startswith("_"))
    real = set(k for k in tpl if not k.startswith("_"))
    missing = sorted(real - documented - set(["name", "chip"]))
    if missing:
        raise AssertionError("template keys with no _explanation: %s"
                             % ", ".join(missing))
    return "%d files, template documents every key" % len(files)


def t_dlwalk():
    out = run_tool("dlwalk.py", "--selftest")
    if "5-byte entry decoded" not in out:
        raise AssertionError("selftest output changed")
    return "5-byte display-list entry decoded correctly"


def t_check_gaps():
    """--check-gaps must find real missed code, not just say "all clear".

    A checker that can only ever report "nothing found" is worthless, which
    is the toolkit's own read-tap pitfall in a different costume. So this
    builds a 16K image with exactly one hidden routine, reachable only
    through `JMP ($C900)` -- an indirect the tracer cannot follow -- and
    requires that the tool both flags it AND resolves the pointer to the
    real target. It also plants a $20 byte inside data, which must be
    dismissed as a coincidence rather than reported.
    """
    rom = bytearray([0xFF] * 16384)

    def put(addr, bs):
        rom[addr - 0xC000:addr - 0xC000 + len(bs)] = bytes(bs)

    put(0xC000, [0x6C, 0x00, 0xC9])                    # traced: JMP ($C900)
    put(0xC900, [0x00, 0xCF])                          # the pointer -> $CF00
    put(0xCF00, [0xA9, 0x01, 0x8D, 0x00, 0x20, 0x60])  # the hidden routine
    put(0xFFFA, [0x00, 0xC0, 0x00, 0xC0, 0x00, 0xC0])

    tmpdir = tempfile.mkdtemp(prefix="selftest-gaps-")
    rom_path = os.path.join(tmpdir, "synthetic.a78")
    cfg_path = os.path.join(tmpdir, "annotations.json")
    io.open(rom_path, "wb").write(bytes(rom))
    json.dump({"entries": ["rom:C000"], "labels": {}, "ram": {},
               "comments": {}, "blocks": []},
              io.open(cfg_path, "w", encoding="utf-8"))

    out = run_tool("disasm.py", rom_path, "-c", cfg_path,
                   "-o", os.path.join(tmpdir, "src"), "--check-gaps")
    if "MISSED CODE" not in out:
        raise AssertionError("did not flag the hidden routine:\n" + out)
    if "$CF00" not in out:
        raise AssertionError("did not dereference the pointer to $CF00:\n" + out)
    if "1 REAL call site" not in out:
        raise AssertionError("expected exactly one real call site:\n" + out)
    if "coincidence" not in out:
        raise AssertionError("did not dismiss the planted $20 byte:\n" + out)
    return "found code hidden behind an indirect jump; dismissed a decoy"


def t_newgame():
    """The scaffold must assemble to a real 16K cartridge, sprite included.

    Checked here rather than by eye because the failure this guards against
    is silent: store a direct-mode sprite as a flat bitmap and it still
    assembles, still boots, and draws one row of your art repeated eight
    times. So the test asserts the layout MARIA actually reads -- rows one
    page apart, bottom row at the address the display list names, top row
    at the highest page.
    """
    import newgame
    tmpdir = tempfile.mkdtemp(prefix="selftest-newgame-")
    out = run_tool("newgame.py", tmpdir, "--title", "Selftest", "--build",
                   "--force")
    a78 = os.path.join(tmpdir, "game.a78")
    if not os.path.exists(a78):
        raise AssertionError("no cartridge written:\n" + out)
    blob = io.open(a78, "rb").read()
    if len(blob) != 128 + 0x4000:
        raise AssertionError("cartridge is %d bytes, not 128 + 16K" % len(blob))
    rom = blob[128:]

    rows = newgame.sprite_rows()
    top, bottom = rows[0], rows[-1]
    base = newgame.GFX_PAGE << 8
    hi = base + (newgame.SPRITE_LINES - 1) * 0x100      # highest page
    at = lambda addr, n: rom[addr - 0xC000:addr - 0xC000 + n]
    if at(base, len(bottom)) != bottom:
        raise AssertionError("bottom row is not at the DL's own address")
    if at(hi, len(top)) != top:
        raise AssertionError("top row is not at the highest page -- the "
                             "sprite is stored the wrong way up")
    if top == bottom:
        raise AssertionError("test art is symmetric, so it cannot detect "
                             "an inverted sprite")

    reset = rom[0x3FFC - 0x0000] | (rom[0x3FFD] << 8)
    if reset != 0xC000:
        raise AssertionError("RESET vector is $%04X, not $C000" % reset)
    return "assembles to 16K; sprite stored bottom-up, a page per scanline"


def t_dmabudget():
    """The cost model must still reproduce the measurements it was fitted to.

    These seven numbers came off real MAME runs (see dmabudget.py for the
    method). They are here because a plausible-looking edit to one constant
    would otherwise go unnoticed -- the tool prints a confident table either
    way.
    """
    import dmabudget as d
    d.set_timing("mame0264")        # the numbers below came off MAME 0.264
    cases = [                       # 12 zones x 16 lines, 2 objects
        (8, 0, 0, 4177), (1, 0, 0, 2242), (16, 0, 0, 6489),
        (8, 0, 1, 4373),                       # 5-byte entries
        (4, 1, 0, 4372), (8, 1, 0, 6685),      # character mode, 1 byte/char
        (4, 2, 0, 5515),                       # character mode, 2 bytes/char
    ]
    # 24 zones x 8 lines, 2 objects: holey DMA and display interrupts
    extra = [
        (dict(lines=8, count=2, width=20), 24, 1412),
        (dict(lines=8, count=2, width=20, holey=True), 24, 1825),
        (dict(lines=8, count=2, width=8), 24, 1660),
        (dict(lines=8, count=2, width=20, dli=True), 24, 1383),
        (dict(lines=8, count=2, width=8, dli=True), 24, 1631),
    ]
    worst = 0.0
    for kw, n, iters in extra:
        measured = (1960 - iters) * 14.0156      # the counting cartridge
        model = sum(d.Zone(**kw).cycles() for _ in range(n))
        err = abs(model - measured) / measured
        worst = max(worst, err)
        if err > 0.03:
            raise AssertionError("%s: model %.0f vs measured %.0f (%.1f%%)"
                                 % (kw, model, measured, 100 * err))
    if d.Zone(8, 2, 20, holey=True).cycles() >= d.Zone(8, 2, 20).cycles():
        raise AssertionError("holey DMA must be cheaper, not dearer")
    if d.DLI_COST <= 0:
        raise AssertionError("a display interrupt is not free")
    for width, chars, five, measured in cases:
        zones = [d.Zone(16, 2, width, five=bool(five), chars=chars)
                 for _ in range(12)]
        model = sum(z.cycles() for z in zones)
        err = abs(model - measured) / measured
        worst = max(worst, err)
        if err > 0.03:   # the model's honest worst case, at width 1
            raise AssertionError(
                "width %d chars %d five %d: model %.0f vs measured %d (%.1f%%)"
                % (width, chars, five, model, measured, 100 * err))
    lines, hz, fps = d.REGIONS["ntsc"]
    if lines != 263 or abs(hz / fps - 29850.5) > 0.5:
        raise AssertionError("NTSC frame is 263 lines x 113.5 = 29,850.5 cycles")
    # the a7800 fork's MARIA (and MAME built with it): iteration counts off the counting cartridge
    # by tools/dmameasure.py, (zones, zone lines, zones with objects, objects, width, extras)
    d.set_timing("fork")
    fork = [
        (12, 16, 0, 1, 4, {}, 1882),
        (12, 16, 6, 1, 4, {}, 1847),
        (12, 16, 6, 2, 4, {}, 1813),
        (12, 16, 6, 2, 12, {}, 1730),
        (12, 16, 6, 4, 8, {}, 1661),
        (12, 16, 6, 3, 16, {}, 1591),
        (12, 16, 12, 1, 4, {}, 1813),
        (12, 16, 12, 2, 4, {}, 1744),
        (12, 16, 12, 2, 12, {}, 1579),
        (12, 16, 12, 4, 8, {}, 1441),
        (12, 16, 12, 3, 16, {}, 1304),
        (12, 16, 12, 2, 8, {'holey': True}, 1799),
        (12, 16, 12, 2, 8, {'dli': True}, 1647),
        (12, 16, 12, 2, 20, {'holey': True}, 1799),
        (12, 16, 12, 2, 20, {'dli': True}, 1399),
        (12, 16, 12, 4, 8, {'holey': True}, 1731),
        (12, 16, 12, 4, 8, {'dli': True}, 1426),
        (24, 8, 24, 2, 8, {'holey': True}, 1798),
        (24, 8, 24, 2, 8, {'dli': True}, 1629),
        (24, 8, 24, 2, 20, {'holey': True}, 1798),
        (24, 8, 24, 2, 20, {'dli': True}, 1382),
        (24, 8, 24, 4, 8, {'holey': True}, 1729),
        (24, 8, 24, 4, 8, {'dli': True}, 1409),
    ]
    fworst = 0.0
    for nz, zl, used, nobj, width, ex, iters in fork:
        zones = [d.Zone(zl, nobj if z < used else 0, width, holey=ex.get("holey", False),
                        dli=ex.get("dli", False)) for z in range(nz)]
        model = sum(z.cycles() for z in zones)
        measured = (1960 - iters) * 14.0156
        err = abs(model - measured) / measured
        fworst = max(fworst, err)
        if err > 0.02:
            raise AssertionError("fork timing, %s: model %.0f vs measured %.0f (%.1f%%)"
                                 % ((nz, zl, used, nobj, width, ex), model, measured, 100 * err))
    # eight 16-byte objects a line saturate MARIA's DMA: the cost stops growing
    heavy = sum(d.Zone(16, 8, 16).cycles() for _ in range(12))
    if abs(heavy - (1960 - 472) * 14.0156) / ((1960 - 472) * 14.0156) > 0.02 or not d.Zone(16, 8, 16).saturated():
        raise AssertionError("a saturated screen: model %.0f" % heavy)
    d.set_timing("fork")
    return ("12 measured configurations reproduced (MAME 0.264, worst %.1f%%), and %d for the "
            "fork's timing (worst %.1f%%)" % (100 * worst, len(fork), 100 * fworst))


def t_dmameasure():
    """tools/dmameasure.py's counting cartridges all assemble, differ from one another where
    the shapes differ, and its fit shapes cover what the model's constants need."""
    import dmameasure as dm
    work = tempfile.mkdtemp(prefix="selftest-dmam-")
    try:
        cc = dm.costcart()
        seen = set()
        for name, kw, nz, zl in dm.SHAPES:
            kw = dict(kw)
            nobj, width = kw.pop("nobj"), kw.pop("width")
            kw.setdefault("zones_used", nz)
            rom = os.path.join(work, "t.a78")
            cc.build(rom, nobj, width, **kw)
            seen.add(io.open(rom, "rb").read())
        assert len(seen) == len(dm.SHAPES), "two shapes built the same cartridge"
        shapes = dm.fit_shapes()
        assert any(s[5].get("holey") for s in shapes) and any(s[5].get("dli") for s in shapes)
        assert {s[0] for s in shapes} >= {12, 16, 24}, "zone counts must vary to separate the constants"
    finally:
        shutil.rmtree(work, True)
    return "%d shapes build distinct cartridges; %d shapes to fit from" % (len(dm.SHAPES), len(shapes))


def t_mksprite():
    """Packing must invert, and must come out bottom-first.

    The orientation half matters more than the packing half: a sprite stored
    the wrong way up still assembles and still draws, just wrongly, so the
    test art here is deliberately asymmetric top-to-bottom and the check is
    that the LAST scanline is emitted first.
    """
    try:
        from PIL import Image
    except ImportError:
        return SKIP, "needs Pillow"
    import mksprite

    for mode, ncol in (("160A", 4), ("320A", 2)):
        bpp, ppb = mksprite.MODES[mode]
        w, h = ppb * 2, 4
        img = Image.new("RGB", (w, h))
        shades = [(0, 0, 0), (90, 90, 90), (180, 180, 180), (255, 255, 255)][:ncol]
        want = []
        for y in range(h):
            row = []
            for x in range(w):
                idx = (x + y) % ncol
                row.append(idx)
                img.putpixel((x, y), shades[idx])
            want.append(row)
        cmap = mksprite.build_map(img, bpp, None)
        rows, width = mksprite.pack(img, mode, 1, cmap)
        if width != w // ppb:
            raise AssertionError("%s: width %d, expected %d" % (mode, width, w // ppb))
        got = mksprite.unpack(rows, width, 1, mode)
        if got != want:
            raise AssertionError("%s: pack/unpack did not round-trip" % mode)

        text = mksprite.emit(rows, width, 1, "art", mode, "test.png")
        body = [l for l in text.split("\n") if l.strip().startswith(".byte")]
        first = [int(t, 16) for t in body[0].split(";")[0].replace(".byte", "").replace("$", "").split(",")]
        if bytes(first) != rows[-1]:
            raise AssertionError("%s: emitted top row first; MARIA reads "
                                 "bottom-first" % mode)

    # frames pack side by side at a stride of one frame's width
    img = Image.new("RGB", (16, 2))
    for x in range(16):
        img.putpixel((x, 0), (255, 255, 255) if x < 8 else (0, 0, 0))
        img.putpixel((x, 1), (255, 255, 255) if x < 8 else (0, 0, 0))
    cmap = mksprite.build_map(img, 2, None)
    rows, width = mksprite.pack(img, "160A", 2, cmap)
    if width != 2:
        raise AssertionError("two frames of 8 pixels should be 2 bytes wide")
    if len(rows[0]) != 4:
        raise AssertionError("a scanline of 2 frames x 2 bytes should be 4")
    if rows[0][:2] == rows[0][2:]:
        raise AssertionError("the two frames packed identically; the split "
                             "is in the wrong place")
    return "160A and 320A round-trip; rows emitted bottom-first; frames stride"


def t_sim_compare():
    """sim.py's score must measure the player, not the phase of the clock.

    The first version of it counted rows landing on the same frame with the
    same values, which conflated "plays the right notes" with "keeps
    playing", and was chaotic: two builds of the simulator differing by 17
    cycles a frame scored 6.3% and 0.1%. This checks the replacement is
    insensitive to frame numbering and separates the two questions.
    """
    import sim

    def log(rows):
        return [(f, tuple(v.split())) for f, v in rows]

    ref = log([(10, "00 01"), (20, "00 02"), (30, "00 03"),
               (40, "00 04"), (50, "00 05"), (60, "00 06")])

    same = sim.compare(ref, ref)
    if same["agreement"] < 0.999 or same["progress"] < 0.999:
        raise AssertionError("a log does not match itself: %r" % same)

    # Same states, every frame number shifted. Must score identically.
    shifted = log([(f + 977, v) for f, v in
                   [(10, "00 01"), (20, "00 02"), (30, "00 03"),
                    (40, "00 04"), (50, "00 05"), (60, "00 06")]])
    sh = sim.compare(shifted, ref)
    if sh["agreement"] < 0.999 or sh["progress"] < 0.999:
        raise AssertionError("frame offset changed the score: %r" % sh)

    # Correct as far as it goes, then stops: agreement high, progress low.
    short = log([(10, "00 01"), (20, "00 02")])
    st = sim.compare(short, ref)
    if st["agreement"] < 0.999:
        raise AssertionError("a correct prefix should agree fully: %r" % st)
    if not 0.2 < st["progress"] < 0.5:
        raise AssertionError("a third of the way through should read as such: "
                             "%r" % st)

    # Wrong notes: agreement must collapse even though the count matches.
    wrong = log([(10, "0A 0B"), (20, "0C 0D"), (30, "0E 0F"),
                 (40, "10 11"), (50, "12 13"), (60, "14 15")])
    wr = sim.compare(wrong, ref)
    if wr["agreement"] > 0.01:
        raise AssertionError("unrelated states should not agree: %r" % wr)

    # A state held for many frames is one event, not many.
    held = log([(10, "00 01"), (11, "00 01"), (12, "00 01"), (20, "00 02")])
    if len(sim.states(held)) != 2:
        raise AssertionError("repeated rows should collapse to one state")
    return "frame-shift invariant; separates agreement from progress"


def t_sim_random():
    """POKEY's RANDOM must not be a constant.

    Ballblazer generates its music instead of playing a score, and asks
    POKEY for the entropy: `CMP $400A / BCS` skips the note when the
    comparison fails. A simulator returning zero there makes the branch
    always skip, so the engine runs, emits nothing, and the game plays
    silence through a player that is working perfectly. That was a real bug
    here and it took a long time to find, so this pins the register down.
    """
    import sim

    class FakeCart(object):
        nbanks = 1
        def pokeys(self):
            return [0x4000]
        def space_of(self, a, b):
            return None
        def byte(self, sp, a):
            return 0xFF

    bus = sim.Bus(FakeCart())
    cyc = [0]
    bus.cpu_cycles = lambda: cyc[0]
    seen = set()
    for step in range(64):
        cyc[0] += 37
        seen.add(bus.read(0x400A))
    if len(seen) < 8:
        raise AssertionError("RANDOM returned %d distinct values in 64 reads; "
                             "it is effectively a constant" % len(seen))
    if seen == {0} or seen == {0xFF}:
        raise AssertionError("RANDOM is stuck at a single value")
    return "%d distinct values over 64 reads" % len(seen)


def t_sim_bus():
    """sim.py's bus: RAM whose address merely ends in $28 is RAM, not MSTAT; MSTAT
    answers at $28 and its mirror $128; RAM through both views of the zero page and
    stack is the same bytes; a BIOS hand-over loads where it should."""
    import sim

    class FakeCart(object):
        nbanks = 1
        def pokeys(self):
            return []
        def space_of(self, a, b):
            return None
        def byte(self, sp, a):
            return 0xFF

    bus = sim.Bus(FakeCart())
    bus.vblank = True
    for a in (0x1928, 0x1A28, 0x2028, 0x2728):
        bus.ram[a] = 0x5A
        assert bus.read(a) == 0x5A, "RAM at $%04X reads back as MSTAT" % a
    assert bus.read(0x28) == 0x80 and bus.read(0x128) == 0x80
    bus.vblank = False
    assert bus.read(0x28) == 0x00
    bus.write(0x93, 0x77)                       # zero page, through its low view
    assert bus.ram[0x2093] == 0x77 and bus.read(0x2093) == 0x77
    # a hand-over from probes/handover.lua: registers and RAM
    work = tempfile.mkdtemp(prefix="selftest-ho-")
    log = os.path.join(work, "ho.log")
    io.open(log, "w").write("handover at frame 322: A=60 X=FF Y=01 SP=FF P=B5 (D=0 I=1)\n")
    ram = bytearray(0x1C0 + 0x1000)
    ram[0x93 - 0x40] = 0x42
    ram[0x1C0 + (0x1928 - 0x1800)] = 0xB0
    io.open(log + ".ram", "wb").write(bytes(ram))
    st = sim.load_handover(log)
    assert st["regs"] == {"a": 0x60, "x": 0xFF, "y": 1, "s": 0xFF, "p": 0xB5}
    assert st["ram"][0x93] == 0x42 and st["ram"][0x1928] == 0xB0
    shutil.rmtree(work, True)
    # the console switches: constant unless exploring, then every one of them moves
    assert bus.read(0x0282) == 0x0B and bus.read(0x0008) == 0x00
    seen = {sim.explore_switches(f) for f in range(2400)}
    assert {(v >> 6) & 3 for v in seen} == {0, 1, 2, 3}, "difficulty switches do not cycle"
    for bit in (0, 1, 3):
        assert any(not v & (1 << bit) for v in seen), "switch bit %d never pressed" % bit
        assert any(v & (1 << bit) for v in seen)
    # pause is pressed twice per cycle so a toggling game resumes
    pulses = sum(1 for f in range(1, 600)
                 if not sim.explore_switches(f) & 8 and sim.explore_switches(f - 1) & 8)
    assert pulses == 2, "pause pressed %d times in a cycle" % pulses
    assert any(sim.explore_second_button(f, 1) for f in range(200)) and \
        not all(sim.explore_second_button(f, 1) for f in range(200))
    bus.frame = 105
    bus.drive = "explore"
    assert bus.read(0x0282) == 0x0B, "the plain sweep moves the console switches"
    bus.drive = "switches"
    assert not bus.read(0x0282) & 2, "select not held at frame 105 while exploring"
    return ("$xx28 is RAM, MSTAT at $28/$128, zero-page mirror, hand-over loads, "
            "explore moves the console switches")


def t_cycles():
    import m6502
    if len(m6502.CYCLES) != 256:
        raise AssertionError("cycle table has %d entries" % len(m6502.CYCLES))
    for op, want in ((0xEA, 2), (0x20, 6), (0x6C, 5), (0x00, 7), (0xBD, 4)):
        if m6502.CYCLES[op] != want:
            raise AssertionError("opcode $%02X: %d cycles, expected %d"
                                 % (op, m6502.CYCLES[op], want))
    return "256 opcodes, spot-checked against the datasheet"


def t_examples():
    """Every example song loads, renders, and survives a text round trip."""
    import tracker
    n = 0
    tmpdir = tempfile.mkdtemp(prefix="selftest-trk-")
    for f in sorted(glob.glob(os.path.join(ROOT, "examples", "*.trk"))):
        song = tracker.load(f)
        again_path = os.path.join(tmpdir, os.path.basename(f))
        io.open(again_path, "w", encoding="utf-8").write(tracker.dump(song))
        again = tracker.load(again_path)
        if again.rows != song.rows:
            raise AssertionError("%s does not survive dump/load"
                                 % os.path.basename(f))
        tracker.render(song, os.path.join(tmpdir, "out.wav"))
        n += 1
    if not n:
        return None
    return "%d songs parse, render, and round-trip as text" % n


def t_notes():
    """The note tables build, and a named divider re-parses to itself."""
    import tracker
    out = []
    for region in ("ntsc", "pal"):
        rows = tracker.note_table(region)
        if not rows:
            raise AssertionError("no TIA notes for %s" % region)
        out.append("%s %d" % (region, len(rows)))
    return "TIA %s" % ", ".join(out)


def t_refusals():
    """The tool says no where it should, and says why."""
    import songfmt
    base = {"chip": "tia", "songs": [{"n": 0, "voices": [{"order": []}]}],
            "patterns": {}, "durations": [1], "waveforms": [0],
            "instruments": [[0] * 16], "instrument_fields": {},
            "engine": "adsr5"}
    cases = [
        ("unknown chip", dict(base, chip="sid"), "unknown chip"),
        ("unimplemented engine", dict(base, engine="nope"), "not implemented"),
        ("wrong voice count", dict(base, chip="pokey",
                                   songs=[{"n": 0, "voices": [{"order": []}]*5}]),
         "voices but"),
        ("no duration table", dict(base, durations=None), "cannot be rendered"),
    ]
    for name, songs, want in cases:
        try:
            songfmt.render(songs, 0)
        except songfmt.FormatError as e:
            if want not in str(e):
                raise AssertionError("%s: message was %r" % (name, str(e)))
        else:
            raise AssertionError("%s was NOT refused" % name)
    # Every AUDCTL bit is modelled now, so nothing exercises the refusal path
    # by default. Test the mechanism anyway by marking a bit unsupported: it is
    # the thing that stops a future gap being rendered as something plausible.
    import tracker
    saved = dict(tracker.POKEY_UNSUPPORTED)
    tracker.POKEY_UNSUPPORTED[0x80] = "a feature nobody has modelled"
    try:
        song = tracker.Song(chip="pokey")
        song.add([(5, 10, 8), None, None, None], audctl=0x80)
        if not song.unsupported:
            raise AssertionError("an unsupported AUDCTL bit was not noticed")
    finally:
        tracker.POKEY_UNSUPPORTED.clear()
        tracker.POKEY_UNSUPPORTED.update(saved)
    return ("%d bad inputs refused, and the AUDCTL refusal path still works"
            % len(cases))


def t_dualpokey():
    """Eight voices: each chip keeps its own AUDCTL, and the format carries it."""
    import tracker
    if tracker.CHANNELS.get("pokey2") != 8:
        raise AssertionError("pokey2 is not eight channels")
    song = tracker.Song(chip="pokey2", region="ntsc")
    for i in range(8):
        song.add([(5, 10 + c, 8) for c in range(8)], audctl=(0x10, 0x04))
    # channel 0-3 belong to the first chip, 4-7 to the second
    if song.ctl_of(0, 0) != 0x10 or song.ctl_of(0, 5) != 0x04:
        raise AssertionError("a channel got the wrong chip's AUDCTL")
    # 16-bit pairing on chip 0 must not silence a voice on chip 1
    tmp = tempfile.mkdtemp(prefix="selftest-p2-")
    path = os.path.join(tmp, "s.trk")
    io.open(path, "w", encoding="utf-8").write(tracker.dump(song))
    again = tracker.load(path)
    if again.rows != song.rows:
        raise AssertionError("an eight-voice song does not survive dump/load")
    if again.audctl != song.audctl:
        raise AssertionError("the second chip's AUDCTL was lost in the file")
    tracker.render(song, os.path.join(tmp, "s.wav"))
    # and the exporter must refuse rather than emit a one-chip player
    try:
        tracker.export_asm(song)
    except ValueError as e:
        if "two POKEYs" not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    else:
        raise AssertionError("export of a two-chip song was allowed")
    return "8 voices, per-chip AUDCTL, round-trips; export refused"


def t_trackeredit():
    """The tracker UI must survive every chip, including two POKEYs.

    This exists because it did not. When AUDCTL became one value per chip,
    `trackeredit` kept indexing it as a single number and broke for every POKEY
    song -- and nothing noticed, because the browser tools had no test at all.
    """
    import tracker
    import trackeredit as TE
    tmp = tempfile.mkdtemp(prefix="selftest-te-")
    checked = []
    for chip, nch in (("tia", 2), ("pokey", 4), ("pokey2", 8)):
        song = tracker.Song(title=chip, chip=chip, region="ntsc")
        ctl = (0x00, 0x01) if chip == "pokey2" else 0x00
        for _ in range(8):
            song.add([(5 if chip != "tia" else 4, 20 + c, 8)
                      for c in range(nch)], audctl=ctl)
        path = os.path.join(tmp, chip + ".trk")
        io.open(path, "w", encoding="utf-8").write(tracker.dump(song))
        TE.SONG, TE.PATH = tracker.load(path), path
        j = TE.song_json()
        if j["nch"] != nch:
            raise AssertionError("%s: %d channels, expected %d"
                                 % (chip, j["nch"], nch))
        if len(j["rows"][0]["audctl"]) != max(1, TE.SONG.nchips):
            raise AssertionError("%s: wrong number of AUDCTL values" % chip)
        # every channel must format and re-parse
        for c in range(nch):
            text = j["rows"][0]["text"][c]
            TE.set_cell(0, c, text)
        TE.render_range(0, 4)
        checked.append(chip)
    return "%s all format, edit and render" % ", ".join(checked)


def t_midi():
    """MIDI in, notes out -- including the two things that break naive parsers."""
    import struct
    import midi as M
    import tracker

    def vlq(n):
        out = [n & 0x7F]
        n >>= 7
        while n:
            out.append((n & 0x7F) | 0x80)
            n >>= 7
        return bytes(reversed(out))

    def trk(events, name):
        body = vlq(0) + bytes([0xFF, 0x03]) + vlq(len(name)) + name
        for d, data in events:
            body += vlq(d) + data
        body += vlq(0) + bytes([0xFF, 0x2F, 0x00])
        return b"MTrk" + struct.pack(">I", len(body)) + body

    div = 480
    mel = []
    for n in (60, 62, 64):
        mel.append((0, bytes([0x90, n, 100])))
        mel.append((div, bytes([0x80, n, 0])))
    # the chord track omits status bytes after the first: running status, which
    # is the classic way to lose two thirds of a chord without noticing
    chord = [(0, bytes([0x91, 48, 90])), (0, bytes([52, 90])),
             (0, bytes([55, 90])), (div, bytes([0x81, 48, 0])),
             (0, bytes([52, 0])), (0, bytes([55, 0]))]
    data = (b"MThd" + struct.pack(">IHHh", 6, 1, 2, div)
            + trk(mel, b"lead") + trk(chord, b"chord"))
    tmp = tempfile.mkdtemp(prefix="selftest-midi-")
    path = os.path.join(tmp, "t.mid")
    with open(path, "wb") as f:
        f.write(data)

    doc = M.read(path)
    lead, ch = doc["tracks"][0], doc["tracks"][1]
    if len(lead["notes"]) != 3:
        raise AssertionError("melody: %d notes, expected 3" % len(lead["notes"]))
    if len(ch["notes"]) != 3:
        raise AssertionError("running status lost notes: got %d of 3"
                             % len(ch["notes"]))
    if M.max_poly(ch["notes"]) != 3:
        raise AssertionError("the chord did not read as three-voice")
    if abs(lead["notes"][0]["end"] - 0.5) > 0.01:
        raise AssertionError("a quarter note at 120bpm is not 0.5s: %.3f"
                             % lead["notes"][0]["end"])

    # a note-on with velocity 0 is a note-off, not a note that never ends
    off0 = trk([(0, bytes([0x90, 60, 100])), (div, bytes([0x90, 60, 0]))], b"v0")
    p2 = os.path.join(tmp, "v0.mid")
    with open(p2, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHh", 6, 1, 1, div) + off0)
    n = M.read(p2)["tracks"][0]["notes"]
    if len(n) != 1 or abs(n[0]["end"] - 0.5) > 0.01:
        raise AssertionError("velocity-0 note-on was not treated as note-off")

    # and the monophonic fold picks the note it claims to
    hi, dropped = tracker.midi_voice(ch["notes"], 30, 60.0, "high")
    lo, _ = tracker.midi_voice(ch["notes"], 30, 60.0, "low")
    if hi[0]["note"] != 55 or lo[0]["note"] != 48:
        raise AssertionError("--pick did not choose the stated note")
    if not dropped:
        raise AssertionError("dropped notes were not counted")

    # --- and the same import through the tracker UI, which is where it is
    # actually used: one track into one voice, leaving the others alone.
    import base64
    import trackeredit as TE
    song = tracker.Song(title="host", chip="pokey", region="ntsc")
    for _ in range(200):
        song.add([(5, 40, 8), (5, 41, 8), (5, 42, 8), (5, 43, 8)])
    TE.SONG, TE.PATH = song, os.path.join(tmp, "host.trk")
    keep = [song.rows[i][3] for i in range(0, 50)]

    info = TE.midi_open(base64.b64encode(data).decode())
    if len(info["tracks"]) != 2:
        raise AssertionError("the UI saw %d importable tracks, expected 2"
                             % len(info["tracks"]))
    if info["tracks"][1]["poly"] != 3:
        raise AssertionError("the UI did not report the chord as polyphonic")

    r = TE.midi_apply(0, 0, "high", 5, 8, 0, True)
    if not r["placed"]:
        raise AssertionError("importing into a voice placed nothing")
    if [TE.SONG.rows[i][3] for i in range(0, 50)] != keep:
        raise AssertionError("importing into voice 1 disturbed voice 4")

    r2 = TE.midi_apply(1, 1, "low", 5, 8, 0, True)
    if not r2["dropped"]:
        raise AssertionError("the chord's dropped notes were not counted")

    for bad, why in (((99, 0), "a track that does not exist"),
                     ((0, 9), "a voice that does not exist")):
        try:
            TE.midi_apply(bad[0], bad[1], "high", 5, 8, 0, True)
        except ValueError:
            pass
        else:
            raise AssertionError("%s was accepted" % why)
    return ("running status, tempo, velocity-0 offs; --pick honoured; "
            "UI fills one voice without touching the others")


def t_helps():
    """Every tool answers --help without blowing up."""
    bad = []
    for f in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        name = os.path.basename(f)
        if name.startswith("_"):
            continue
        p = subprocess.run([sys.executable, f, "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode != 0 and b"No module named 'PIL'" in p.stdout:
            continue    # Pillow is optional; a tool that needs it says so
        if p.returncode != 0:
            bad.append("%s (exit %d)" % (name, p.returncode))
    if bad:
        raise AssertionError("; ".join(bad))
    return "every tool has working --help"


def t_tia_periods():
    """The TIA model's period for every AUDC is the one measured on MAME with
    mktone.py --tia (docs/audio.md): pure tones 2, 6, 31, 93 divider ticks;
    polys 15, 465, 31, 93, 511; the old model had $C, $D, $6, $A, $E and $F an
    octave low."""
    import tracker
    measured = {0x4: 2, 0x5: 2, 0xC: 6, 0xD: 6, 0x6: 31, 0xA: 31, 0xE: 93,
                0x1: 15, 0x2: 465, 0x3: 465, 0x7: 31, 0x9: 31, 0xF: 93,
                0x8: 511}
    for audc, period in sorted(measured.items()):
        for audf in (0, 3, 9):
            want = tracker.CLOCK["ntsc"] / ((audf + 1) * period)
            got = tracker.frequency(audc, audf)
            assert abs(got - want) < 1e-6, \
                "AUDC $%X AUDF %d: %.3f Hz, measured period %d gives %.3f" % (
                    audc, audf, got, period, want)
    assert tracker.frequency(0x0, 3) is None and tracker.frequency(0xB, 3) is None
    # the 18-high, 13-low pattern, not a square
    assert tracker.DIV31.count(1) == 18 and len(tracker.DIV31) == 31
    return "14 AUDCs match the measured periods at 3 dividers each"


def t_origins():
    """origins.py groups where addresses came from: words, split tables at a stride,
    immediates and computed pointers; a shared high byte is no table; blocks are
    added once and never over an existing one."""
    import cart
    import origins as O
    c = cart.Cart(os.path.join(ROOT, "tests", "carts", "synth128.a78"))
    log = "\n".join(
        ["P ptr f7:C100 f7:E00%X f7:E00%X x3 %04X %04X" % (k * 2 + 1, k * 2, 0x2000 + k, 0x2000 + k)
         for k in range(3)] +
        ["P ptr f7:C200 f7:E11%X f7:E10%X x3 %04X %04X" % (k, k, 0x3000 + k, 0x3000 + k)
         for k in range(1, 3)] +
        ["P ptr f7:C250 f7:E200 f7:E10%X x1 4000 4000" % k for k in range(4, 7)] +
        ["P jmpind f7:C300 f7:C0D0 f7:C0CC x5 C000 C000", "I f7:C0CC", "I f7:C0D0",
         "P rts f7:C320 r:0040 r:0041 x1 C000 C000"])
    uses, imms = O.parse_log(log)
    f = O.analyse(uses, imms, c)
    shapes = sorted((t["shape"], t["lo"], t["n"]) for t in f["tables"])
    assert shapes == [("split", ("f7", 0xE101), 2), ("words", ("f7", 0xE000), 3)], shapes
    assert f["immediates"] == [("f7:C0CC", "jmpind"), ("f7:C0D0", "jmpind")], f["immediates"]
    assert len(f["computed"]) == 1 and f["computed"][0][0] == "rts"
    doc = {"blocks": []}
    added = O.merge(doc, f, "t.log")
    assert len(added) == 2 and len(doc["blocks"]) == 3, doc["blocks"]
    assert doc["blocks"][0] == {"loc": "f7:E000", "len": 6, "type": "words",
                                "note": "address table, as far as the run read it"}
    assert O.merge(doc, f, "t.log") == [] and len(doc["blocks"]) == 3     # idempotent
    mine = {"blocks": [{"loc": "f7:E000", "len": 2, "type": "bytes"}]}
    O.merge(mine, f, "t.log")
    assert mine["blocks"][0]["type"] == "bytes" and \
        not any(b["loc"] == "f7:E000" and b["type"] == "words" for b in mine["blocks"])
    return "words, split tables, immediates, computed; constants left out; merge idempotent"


def t_pokey2tia():
    """pokey2tia.py: the loudest two voices play and keep their channels, groups
    keep to their channel, arp shares a channel in turn, noise becomes AUDC 8, a
    note above the TIA is silent, and the whole command writes its files."""
    import tracker as T
    import pokey2tia as P

    def song(rows):
        s = T.Song(chip="pokey")
        for r in rows:
            s.add(list(r), audctl=0)
        return s

    tone = lambda f, v: (5, f, v)           # noqa: E731
    quiet = (0, 0, 0)
    # four voices: 20 and 30 loud, 40 and 50 quiet, then a noise voice
    four = [[tone(0x20, 9), tone(0x30, 8), tone(0x40, 3), tone(0x50, 2)]] * 4
    tia, st = P.convert(song(four))
    row = list(tia.states())[0]
    assert {row[0][2], row[1][2]} == {9, 8}, row        # the two loudest
    assert st["over"] == 4 and st["dropped"] == 8, st
    # a voice that stays chosen stays on its channel when the other changes
    rows = [[tone(0x20, 9), tone(0x30, 8), quiet, quiet],
            [tone(0x20, 9), quiet, tone(0x40, 8), quiet]]
    out = list(P.convert(song(rows))[0].states())
    assert out[0][0][1] == out[1][0][1] and out[0][0][2] == 9, out
    # groups: channel 1 only ever plays voices 1 and 2
    g, _ = P.convert(song([[quiet, quiet, tone(0x20, 9), tone(0x30, 9)]]),
                     mash="loudest", groups=[{1, 2}, {3, 4}])
    first = list(g.states())[0]
    assert first[0][2] == 0 and first[1][2] == 9, first
    # arp: three voices on a shared channel take turns, one a frame
    arp = song([[tone(0x20, 9), tone(0x30, 8), tone(0x40, 7), quiet]] * 4)
    got = [r[1][1] for r in P.convert(arp, mash="arp")[0].states()]
    assert len(set(got)) == 2 and got[0] != got[1], got
    held = [r[1][1] for r in P.convert(arp, mash="arp", arp=2)[0].states()]
    assert held[0] == held[1] and held[1] != held[2], held
    # noise -> the 9-bit poly; a tone far above 15.7 kHz is silent
    n, _ = P.convert(song([[(0, 0x20, 8), tone(0x00, 8), quiet, quiet]]))
    cells = list(n.states())[0]
    assert any(c[0] == P.NOISE_MODE and c[2] == 8 for c in cells), cells
    # a tune already on the TIA's pitches is not moved by --fit
    hz = T.frequency(0x4, 10)
    audf = int(round(T.pokey_rate(0, [0, 0, 0, 0], 0) / 2.0 / hz)) - 1
    on = song([[tone(audf, 8), quiet, quiet, quiet]] * 3)
    _t, stf = P.convert(on, fit=True)
    _t, st0 = P.convert(on)
    assert abs(stf["offset"]) <= 60 and \
        abs(stf["errors"][0][0]) <= abs(st0["errors"][0][0]), (stf["offset"], stf["errors"])
    # the command, end to end
    work = tempfile.mkdtemp(prefix="p2t-")
    log = os.path.join(work, "pokey.log")
    with io.open(log, "w", encoding="utf-8") as f:
        f.write("# chip pokey\n")
        for fr in range(30):
            f.write("%d 20 A8 30 A5 40 A3 50 8A 00\n" % fr)
    out_dir = os.path.join(work, "out")
    p = subprocess.run([sys.executable, os.path.join(HERE, "pokey2tia.py"), log, "-o",
                        out_dir, "--fit"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    assert p.returncode == 0, p.stdout.decode()
    # the defaults are groups 1+2,3+4, the loudest voice winning a shared channel
    explicit = os.path.join(work, "explicit")
    subprocess.run([sys.executable, os.path.join(HERE, "pokey2tia.py"), log, "-o", explicit,
                    "--fit", "--map", "groups", "--groups", "1+2,3+4", "--mash",
                    "loudest"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    same = [io.open(os.path.join(d, "tia.trk"), encoding="utf-8").read()
            for d in (out_dir, explicit)]
    assert same[0] == same[1], "the defaults are not groups 1+2,3+4, loudest wins"
    for name in ("tia.trk", "tia.asm", "tia.wav", "orig.wav", "report.txt"):
        assert os.path.getsize(os.path.join(out_dir, name)) > 0, name
    bad = subprocess.run([sys.executable, os.path.join(HERE, "pokey2tia.py"), log, "-o",
                          out_dir, "--groups", "1+2"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    assert bad.returncode != 0 and b"two channels" in bad.stdout and \
        b"Traceback" not in bad.stdout
    return "loudest-two with sticky channels, groups, arp, noise, fit, and the command"


def t_sim_machine():
    """The simulator's machine, against what MAME was measured doing: WSYNC halts the CPU to
    the next scanline (263 a frame, not thousands), the RIOT interval timer counts and flags,
    RAM on the cartridge keeps what is written to it, and decimal-mode flags are the binary
    ones (0x99 + 0x01 leaves Z clear)."""
    import asm
    import cart as cart_module
    import sim
    src = """
    .org $C000
reset:
    SEI
    CLD
    LDX #$FF
    TXS
    LDA #$00
    STA $1800
    STA $1801
    STA $1802
    LDA #$AB
    STA $4000            ; cartridge RAM, when there is some
    LDA $4000
    STA $1803
    LDA #$10
    STA $0296            ; TIM64T
wait:
    BIT $0285
    BPL wait             ; TIMINT bit 7
    LDA #$01
    STA $1802
    SED
    LDA #$99
    CLC
    ADC #$01             ; decimal: A = 0 but the binary sum is $9A, so Z stays clear
    PHP
    PLA
    AND #$02
    STA $1804
    CLD
loop:
    STA $24              ; WSYNC
    INC $1800
    BNE loop
    INC $1801
    JMP loop
nmi:
    RTI
vectors_pad:
    .res $FFFA-vectors_pad,$00
    .word nmi
    .word reset
    .word nmi
"""
    data = asm.Assembler().assemble(src.splitlines())
    hdr = bytearray(128)
    hdr[0] = 1
    hdr[1:10] = b"ATARI7800"
    hdr[49:53] = (0x4000 + len(data)).to_bytes(4, "big")
    hdr[55] = 1
    work = tempfile.mkdtemp(prefix="selftest-simmachine-")
    try:
        rom = os.path.join(work, "m.a78")
        io.open(rom, "wb").write(bytes(hdr) + bytes(0x4000) + bytes(data))   # SuperGame: bank 1 is fixed at $C000
        cart = cart_module.Cart(rom, mapper="supergame", low="ram")
        bus = sim.run(cart, 12, "ntsc", drive=False)
        n = bus.ram[0x2000 + 0x1800 - 0x2000] if False else bus.ram[0x1800] + 256 * bus.ram[0x1801]
        assert 150 <= n <= 270 * 12, "WSYNC loop ran %d times in 12 frames (263 a frame)" % n
        assert bus.ram[0x1802] == 1, "the RIOT timer never flagged"
        assert bus.ram[0x1803] == 0xAB, "RAM on the cartridge did not keep a write"
        assert bus.ram[0x1804] == 0, "decimal ADC set Z from the decimal result"
    finally:
        shutil.rmtree(work, True)
    # ... and the RIOT timer and WSYNC against what MAME 0.264 itself returned for a synthetic
    # cartridge that samples INTIM/TIMINT (tests/carts/riot.a78, dump in tests/golden/): the
    # first samples of each prescale, the flag clear-on-INTIM-read sequence and the WSYNC
    # phase rows are exact; later samples after an expiry can sit one count off at a prescale
    # boundary, which is the one thing not yet modelled (6532 divider phase)
    gold = io.open(os.path.join(ROOT, "tests", "golden", "riot-mame.bin"), "rb").read()
    rc = cart_module.Cart(os.path.join(ROOT, "tests", "carts", "riot.a78"))
    rb = sim.run(rc, 40, "ntsc", drive=False)
    got = bytes(rb.ram[0x1800:0x2800])
    for name, lo, n in (("TIM64T", 0x000, 48), ("TIM64T flag", 0x100, 64), ("TIM1T", 0x200, 20),
                        ("TIM1T flag", 0x300, 20), ("TIM8T", 0x400, 7), ("TIM8T flag", 0x500, 20),
                        ("INTIM clears TIMINT", 0x600, 0x15), ("WSYNC", 0x700, 0x20)):
        assert got[lo:lo + n] == gold[lo:lo + n], (name, gold[lo:lo + n].hex(), got[lo:lo + n].hex())
    off = sum(1 for i in range(0x300) if got[i] != gold[i])
    assert off < 0x180, "RIOT samples differ from MAME's in %d places" % off
    return "WSYNC pacing, RIOT timer (against MAME's own dump), cartridge RAM and decimal flags behave"


def t_sim_window():
    """sim.window_hint says when the simulation ran past the end of the capture."""
    import sim
    ref = [(1, "z"), (334, "a"), (700, "b"), (1199, "c")]       # power-on frames
    near = [(1, "z"), (12, "a"), (378, "b"), (877, "c")]        # 322 earlier
    assert sim.window_hint(ref, near, 882) is None
    longer = sim.window_hint(ref, near, 1200)
    assert longer and 870 <= longer <= 920, longer
    assert sim.window_hint(ref[:1], near, 1200) is None
    return "a 1,200-frame run against a capture that covers 877 is flagged"


def t_sim_timing():
    """sim.py's frame is MAME's: 263 lines of 113.5 cycles, VBLANK rising 242 lines
    after the display starts, and a display interrupt taken as its flagged zone
    begins -- measured with probes/dlitimes.lua on the synthetic cartridge, whose one
    DLI is on the zone that starts 80 lines in."""
    import sim
    import cart
    assert sim.LINES["ntsc"] == 263 and sim.CYCLES_PER_LINE == 113.5
    assert sim.LINES["ntsc"] - sim.VBLANK_LINES["ntsc"] == 242
    log = []
    sim.run(cart.Cart(os.path.join(ROOT, "tests", "carts", "synth128.a78")), 130, log=log)
    nmi = [c / sim.CYCLES_PER_LINE for k, f, c in log if k == "nmi" and f == 120]
    vb = [c / sim.CYCLES_PER_LINE for k, f, c in log if k == "vblank" and f == 120]
    assert len(nmi) == 1 and 80.0 <= nmi[0] < 80.5, nmi
    assert len(vb) == 1 and 242.0 <= vb[0] < 242.2, vb
    return "NMI at line %.2f, VBLANK at line %.2f of 263" % (nmi[0], vb[0])


def _wb_setup(tag):
    """Point the workbench module at the synthetic cartridge and a fresh project."""
    import cart as cart_module
    import workbench as WB
    work = tempfile.mkdtemp(prefix="selftest-wb-%s-" % tag)
    rom = os.path.join(work, "game.a78")
    shutil.copy(os.path.join(ROOT, "tests", "carts", "synth128.a78"), rom)
    WB.ROM, WB.CART = rom, cart_module.Cart(rom)
    WB.PROJECT = os.path.join(work, "game-workbench")
    WB.JOBS.clear()
    return WB, work


def _wb_serve(WB):
    """The workbench's HTTP server on a free port, in a thread: (url, stop)."""
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), WB.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return "http://127.0.0.1:%d" % srv.server_address[1], stop


def _wb_call(url, path, body=None):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url + path, method="POST" if body is not None else "GET",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw.decode("utf-8"))
    except ValueError:
        return code, raw


def _wb_wait(url, jid, seconds=240):
    import time
    end = time.time() + seconds
    while time.time() < end:
        code, d = _wb_call(url, "/api/job?id=%d&since=0" % jid)
        if d["status"] in ("done", "failed", "cancelled"):
            return d
        time.sleep(0.2)
    raise AssertionError("job %d never finished" % jid)


def t_workbench_handoff():
    """What the workbench hands to the editors and takes back: the sprite editor gets the
    address in hex (it once got decimal and opened the wrong place), the annotation stamp
    is a string that survives JavaScript, a check with a typo'd key fails, and a song or
    path of the wrong type is refused."""
    WB, work = _wb_setup("handoff")
    seen = []
    real = WB.launch
    WB.launch = lambda tool, args, what: (seen.append((tool, list(args))) or {"url": "x"})
    try:
        os.makedirs(WB.PROJECT)
        url, stop = _wb_serve(WB)
        try:
            sp = WB.CART.spaces()[-1]
            code, r = _wb_call(url, "/api/open", {"kind": "sprite", "space": sp,
                                                  "base": 0xE000, "height": 8})
            assert code == 200 and seen, (code, r)
            a = seen[-1][1]
            assert a[a.index("--base") + 1] == "E000", a
            code, r = _wb_call(url, "/api/annotations", {"text": '{"entries": []}'})
            assert code == 200 and isinstance(r["stamp"], str), r
            code, r = _wb_call(url, "/api/annotations", {"text": '{}', "base": r["stamp"]})
            assert code == 200, r                       # the stamp it handed out is the stamp it accepts
            code, r = _wb_call(url, "/api/annotations", {"text": '[1]'})
            assert code == 400, (code, r)
            code, r = _wb_call(url, "/api/annotations", {"text": '{}', "base": "1"})
            assert code == 409, (code, r)
            # check my work looks at the annotations first, and strictly
            _wb_call(url, "/api/annotations", {"text": '{"entrys": []}'})
            code, r = _wb_call(url, "/api/job", {"kind": "check", "params": {}})
            assert code == 200, r
            d = _wb_wait(url, r["id"], 120)
            assert d["status"] == "failed", d["status"]
        finally:
            stop()
    finally:
        WB.launch = real
        shutil.rmtree(work, True)
        WB.JOBS.clear()
    import localserver
    for bad in (5, ["a"], None):
        try:
            localserver.confine(bad, [work])
        except ValueError:
            continue
        raise AssertionError("confine accepted %r" % (bad,))
    return "sprite address in hex, string stamp accepted, typo'd key fails the check, non-string paths refused"


def t_workbench_hardening():
    """The workbench answers only its own page: a foreign Host, a foreign Origin and a
    non-JSON POST are refused, bodies are capped; probe settings are A7800_* with no
    paths; one job of a kind at a time; a job that cannot start fails instead of
    hanging; cancel stops the whole process tree; finished jobs survive a restart;
    saving annotations keeps the file's line endings."""
    import http.client
    import socket
    import time
    WB, work = _wb_setup("hard")
    try:
        os.makedirs(WB.PROJECT)
        url, stop = _wb_serve(WB)
        port = int(url.rsplit(":", 1)[1])
        try:
            def send(method, path, headers, body=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                c.request(method, path, body=body, headers=headers)
                r = c.getresponse()
                r.read()
                c.close()
                return r.status
            js = {"Content-Type": "application/json"}
            assert send("GET", "/api/info", {}) == 200
            assert send("GET", "/api/info", {"Host": "evil.example.com"}) == 403
            assert send("POST", "/api/job", dict(js, Origin="http://evil.com"), b"{}") == 403
            assert send("POST", "/api/job", {"Content-Type": "text/plain"}, b"{}") == 415
            assert send("POST", "/api/job", js, b"[1]") == 400
            assert send("POST", "/api/job", js, b'{"kind":"lint","params":"x"}') == 400
            for line, code in (("Content-Length: abc", 400), ("Content-Length: 99999999999", 413)):
                s = socket.create_connection(("127.0.0.1", port), timeout=10)
                s.sendall(("POST /api/job HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                           "Content-Type: application/json\r\n%s\r\n\r\n" % (port, line)).encode())
                got = s.recv(200)
                s.close()
                assert (" %d " % code).encode() in got, (line, got)
        finally:
            stop()
        # probe settings
        for bad in ("LD_PRELOAD=/tmp/x.so", "A7800_AUDIO_LOG=/tmp/x.log", "A7800_AUDIO_OUT=..\\x"):
            try:
                WB.build_probe({"probe": "audio", "env": bad})
            except ValueError:
                continue
            raise AssertionError("accepted probe setting %r" % bad)
        WB.build_probe({"probe": "audio", "env": "A7800_AUDIO_FRAMES=100"})
        # one job of a kind at a time
        busy = WB.Job("census", "census", [{"cmd": [sys.executable, "-c", "pass"]}], WB.PROJECT)
        busy.status = "running"
        WB.JOBS[busy.id] = busy
        try:
            WB.start_job("census", {})
        except ValueError as e:
            assert "already running" in str(e), e
        else:
            raise AssertionError("two census jobs at once")
        WB.JOBS.clear()
        # a job whose command cannot even be started ends as failed, not "running"
        j = WB.Job("lint", "bad", [{"cmd": [sys.executable, "-c", "pass\0"]}], WB.PROJECT)
        WB.JOBS[j.id] = j
        j.start()
        end = time.time() + 20
        while j.status in ("queued", "running") and time.time() < end:
            time.sleep(0.1)
        assert j.status == "failed", j.status
        # cancel takes the grandchild with it
        if os.name != "nt":
            code = ("import subprocess,sys,time;"
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                    "print(p.pid,flush=True);time.sleep(60)")
            j = WB.Job("probe", "slow", [{"cmd": [sys.executable, "-c", code]}], WB.PROJECT)
            WB.JOBS[j.id] = j
            j.start()
            end = time.time() + 20
            # the first log line is the "$ command" echo; the pid arrives after the child starts
            while not any(ln.strip().isdigit() for ln in j.log) and time.time() < end:
                time.sleep(0.1)
            pid = int([ln for ln in j.log if ln.strip().isdigit()][0])
            j.cancel()
            end = time.time() + 15
            alive = True
            while alive and time.time() < end:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.2)
                except OSError:
                    alive = False
                else:
                    # a zombie still answers kill 0; check its state
                    try:
                        st = io.open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0]
                        alive = st != "Z"
                    except OSError:
                        alive = False
            assert not alive, "cancel left the grandchild running"
            assert j.status == "cancelled", j.status
        # finished jobs are remembered across a restart
        WB.save_history()
        WB.JOBS.clear()
        WB.load_history()
        assert any(x.status == "failed" for x in WB.JOBS.values()), WB.JOBS
        # a Notepad-saved file (BOM) reads; the page's draft is not overwritten blindly;
        # a bad census request starts nothing; first look is adopted without losing pins
        with io.open(WB.config_path(), "w", encoding="utf-8-sig") as f:
            f.write('{"entries": []}')
        assert WB.read_annotations()["text"].startswith("{"), "BOM not stripped"
        base = WB.read_annotations()["stamp"]
        time.sleep(0.02)
        with io.open(WB.config_path(), "w", encoding="utf-8") as f:
            f.write('{"entries": ["f7:C000"]}')
        os.utime(WB.config_path(), (time.time() + 5, time.time() + 5))
        url2, stop2 = _wb_serve(WB)
        try:
            code, r = _wb_call(url2, "/api/annotations", {"text": '{"entries": []}', "base": base})
            assert code == 409 and r.get("conflict"), (code, r)
            code, r = _wb_call(url2, "/api/annotations", {"text": '{"entries": []}', "base": base,
                                                          "force": True})
            assert code == 200 and "stamp" in r, (code, r)
            gone = os.path.join(WB.PROJECT, "annotations.json")
            os.remove(gone)
            code, r = _wb_call(url2, "/api/census/apply", {"items": "ab"})
            assert code == 400 and not os.path.exists(gone), (code, r)
            code, r = _wb_call(url2, "/api/census/apply", {"items": [{"kind": "x", "loc": "q:1"}]})
            assert code == 200 and r["added"] == 0 and not os.path.exists(gone), (code, r)
        finally:
            stop2()
        os.makedirs(os.path.join(WB.PROJECT, "firstlook"), exist_ok=True)
        with io.open(os.path.join(WB.PROJECT, "firstlook", "annotations.json"), "w") as f:
            json.dump({"entries": ["f7:C100"], "banksw": {"f7:C0DC": [1, 2]},
                       "blocks": [{"loc": "f7:D000", "len": 4}]}, f)
        with io.open(WB.config_path(), "w") as f:
            json.dump({"entries": ["f7:C000"]}, f)
        WB.adopt_firstlook()
        got = json.load(io.open(WB.config_path()))
        assert got["entries"] == ["f7:C000", "f7:C100"] and got["banksw"] == {"f7:C0DC": [1, 2]} \
            and got["blocks"], got
        # a damaged jobs.json does not stop the workbench starting
        for junk in ("null", '[{"id": 1e400, "kind": "x"}]', "[1, 2]", '[{"outdir": "/etc"}]'):
            with io.open(WB.history_path(), "w") as f:
                f.write(junk)
            WB.JOBS.clear()
            WB.load_history()
        # saving annotations keeps the file's line ending
        with io.open(WB.config_path(), "w", encoding="utf-8", newline="") as f:
            f.write('{\r\n  "entries": []\r\n}\r\n')
        WB.write_annotations('{\n  "entries": ["f7:C000"]\n}')
        raw = io.open(WB.config_path(), "rb").read()
        assert raw.count(b"\r\n") == raw.count(b"\n") >= 3, raw
    finally:
        shutil.rmtree(work, True)
    return "foreign Host/Origin/non-JSON refused, bodies capped, probe settings limited, one job per kind, failures finish, cancel kills the tree, history survives, line endings kept"


def t_localserver():
    """The rules every local server shares (localserver.py): own Host only, no foreign
    Origin, POSTs are JSON objects of sane size, and a save path stays in the folders the
    user is working in. Also that the three editors really call them."""
    import localserver

    class H(object):
        class server(object):
            server_address = ("127.0.0.1", 8140)

        def __init__(self, headers, body=b""):
            import io as _io
            self.headers = headers
            self.rfile = _io.BytesIO(body)
            self.sent = None

        def _send(self, code, body, ctype=None):
            self.sent = code

    ok = {"Host": "127.0.0.1:8140", "Content-Type": "application/json"}
    assert localserver.guard(H(dict(ok)), True)
    for bad, code in (({"Host": "evil.com", "Content-Type": "application/json"}, 403),
                      (dict(ok, Origin="http://evil.com"), 403),
                      (dict(ok, **{"Content-Type": "text/plain"}), 415),
                      ({"Content-Type": "application/json"}, 403)):
        h = H(bad)
        assert not localserver.guard(h, True) and h.sent == code, (bad, h.sent)
    assert localserver.guard(H(dict(ok, Origin="http://localhost:8140")), True)
    h = H(dict(ok, **{"Content-Length": "2"}), b"[]")
    assert localserver.read_json(h) is None and h.sent == 400
    h = H(dict(ok, **{"Content-Length": "x"}))
    assert localserver.read_json(h) is None and h.sent == 400
    h = H(dict(ok, **{"Content-Length": "99999999999"}))
    assert localserver.read_json(h) is None and h.sent == 413
    h = H(dict(ok, **{"Content-Length": "7"}), b'{"a":1}')
    assert localserver.read_json(h) == {"a": 1}
    work = tempfile.mkdtemp(prefix="selftest-confine-")
    try:
        roots = [work]
        assert localserver.confine(os.path.join(work, "x", "y.bin"), roots)
        for bad in ("/etc/passwd", os.path.join(work, "..", "elsewhere")):
            try:
                localserver.confine(bad, roots)
            except ValueError:
                continue
            raise AssertionError("accepted %s" % bad)
    finally:
        shutil.rmtree(work, True)
    for name in ("spriteedit", "trackeredit", "explore", "workbench"):
        text = io.open(os.path.join(HERE, name + ".py"), encoding="utf-8").read()
        assert "localserver.guard" in text or "localserver.read_json" in text, name
    return "Host, Origin, Content-Type, body limits and save-path confinement; all four servers use them"


def t_workbench_jobs():
    """The workbench runs the toolkit's tools as jobs: the right command lines in the
    right order, project files and no others served, annotations saved and checked,
    emulator jobs refused (with the reason) when there is no emulator, and the whole
    thing over HTTP."""
    WB, work = _wb_setup("jobs")
    try:
        # the command lines
        job = WB.build_observe({"seconds": 12})
        tools = [os.path.basename(c["cmd"][1]) for c in job.steps]
        assert tools == ["simprobe.py", "init.py", "dyn.py", "disasm.py"], tools   # no emulator
        job = WB.build_observe({"seconds": 12, "engine": "mame"})
        tools = [os.path.basename(c["cmd"][1]) for c in job.steps]
        assert tools == ["runprobe.py", "init.py", "dyn.py", "disasm.py"], tools
        assert job.steps[0]["cmd"][3] == "exectrace" and "A7800_XT_BANKS=8" in job.steps[0]["cmd"]
        assert job.steps[-1].get("soft"), "the listing refresh must not fail the job"
        os.makedirs(WB.PROJECT)
        io.open(WB.config_path(), "w").write("{}")
        job = WB.build_addresses({})
        assert "init.py" not in [os.path.basename(c["cmd"][1]) for c in job.steps], \
            "an existing annotations file must not be re-created"
        for bad in ({"seconds": "x"}, {"seconds": 1}, {"seconds": 99999}):
            try:
                WB.build_observe(bad)
            except ValueError:
                continue
            raise AssertionError("accepted %r" % bad)
        # arguments that name probes and settings are checked, not passed through
        try:
            WB.build_probe({"probe": "nosuch"})
        except ValueError:
            pass
        else:
            raise AssertionError("an unknown probe was accepted")
        try:
            WB.build_probe({"probe": "audio", "env": "BAD LINE"})
        except ValueError:
            pass
        else:
            raise AssertionError("a malformed setting was accepted")
        # the project folder is a wall
        os.makedirs(os.path.join(WB.PROJECT, "src"))
        io.open(os.path.join(WB.PROJECT, "src", "a.asm"), "w").write("x")
        io.open(os.path.join(work, "secret.txt"), "w").write("no")
        assert WB.project_file("src/a.asm")
        for rel in ("../secret.txt", "src/../../secret.txt", os.path.join(work, "secret.txt"),
                    "", "src", "nosuch"):
            assert WB.project_file(rel) is None, rel
        # jobs that need MAME say why not
        real = WB.environment
        WB.environment = lambda: {"ready": False, "problem": "MAME was not found. selftest"}
        try:
            WB.start_job("observe", {"engine": "mame"})
        except ValueError as e:
            assert "MAME was not found" in str(e)
        else:
            raise AssertionError("an emulator job started without an emulator")
        finally:
            WB.environment = real
        shutil.rmtree(WB.PROJECT)

        # over HTTP
        url, stop = _wb_serve(WB)
        try:
            code, kinds = _wb_call(url, "/api/kinds")
            assert code == 200 and {"firstlook", "disasm", "observe", "probe"} <= \
                {k["kind"] for k in kinds["kinds"]}
            assert all("build" not in k for k in kinds["kinds"])
            code, j = _wb_call(url, "/api/job", {"kind": "disasm", "params": {}})
            assert code == 200 and j["status"] in ("running", "queued", "done"), j
            d = _wb_wait(url, j["id"])
            assert d["status"] == "done", d["lines"][-5:]
            assert any(o["path"] == "src/f7.asm" for o in d["outputs"]), d["outputs"]
            code, listing = _wb_call(url, "/api/listing")
            assert listing["files"][0]["path"] == "src/f7.asm", listing   # fixed bank first
            code, text = _wb_call(url, "/api/file?path=src/f7.asm")
            assert code == 200 and b".org $C000" in text
            for rel in ("../secret.txt", "%2e%2e/secret.txt"):
                assert _wb_call(url, "/api/file?path=" + rel)[0] == 404, rel
            assert _wb_call(url, "/api/job", {"kind": "nosuch"})[0] == 400
            assert _wb_call(url, "/api/job", {"kind": "lint"})[0] == 400      # nothing to check yet
            assert _wb_call(url, "/favicon.ico")[0] == 204
            # annotations: start, break, mistype, fix
            code, a = _wb_call(url, "/api/annotations")
            assert a["exists"] is False
            code, j = _wb_call(url, "/api/job", {"kind": "newannot", "params": {}})
            assert _wb_wait(url, j["id"])["status"] == "done"
            code, a = _wb_call(url, "/api/annotations")
            assert a["exists"] and a["findings"] == [], a["findings"]
            assert _wb_call(url, "/api/annotations", {"text": "{ nope"})[0] == 400
            assert json.loads(open(WB.config_path()).read()), "a refused save must not touch the file"
            code, r = _wb_call(url, "/api/annotations", {"text": '{"entrys": []}'})
            assert any("entries" in f["message"] for f in r["findings"]), r
            code, r = _wb_call(url, "/api/annotations", {"text": '{"entries": ["f7:C000"]}'})
            assert code == 200 and r["findings"] == [], r
            code, j = _wb_call(url, "/api/job", {"kind": "lint", "params": {}})
            assert _wb_wait(url, j["id"])["status"] == "done"
            # observe code with no emulator: the simulator runs it, dyn.py merges it
            WB.environment = lambda: {"ready": False, "problem": "no MAME in this test"}
            code, j = _wb_call(url, "/api/job", {"kind": "observe", "params": {"seconds": 5}})
            assert code == 200, j
            d = _wb_wait(url, j["id"])
            assert d["status"] == "done", d["lines"][-6:]
            code, a = _wb_call(url, "/api/annotations")
            assert any("C0E5" in e for e in json.loads(a["text"])["entries"]), a["text"][:300]
            # and the address tables, likewise
            code, j = _wb_call(url, "/api/job", {"kind": "addresses", "params": {"seconds": 5}})
            d = _wb_wait(url, j["id"])
            assert d["status"] == "done", d["lines"][-6:]
            assert any(o["path"] == "addresses/addrorigin.log" for o in d["outputs"]), d["outputs"]
            # the census, with no emulator: dark areas named, maps drawn, and a second run adds
            code, j = _wb_call(url, "/api/job", {"kind": "census", "params": {"seconds": 5}})
            d = _wb_wait(url, j["id"])
            assert d["status"] == "done", d["lines"][-6:]
            paths = [o["path"] for o in d["outputs"]]
            assert "census/census.md" in paths and "census/census.json" in paths, paths
            code, md = _wb_call(url, "/api/file?path=census/census.md")
            assert b"Dark areas" in md and b"SYNTH CART BANK FIVE" in md, md[:400]
            code, j = _wb_call(url, "/api/job", {"kind": "census", "params": {"seconds": 5}})
            d2 = _wb_wait(url, j["id"])
            assert d2["status"] == "done" and "--merge" in d2["commands"][0], d2["commands"]
            # its suggestions are offered, can be added to the annotations once, and a
            # second press changes nothing
            code, cs = _wb_call(url, "/api/census")
            blocks = [x for x in cs["suggestions"] if x["kind"] == "block" and x["type"] == "text"]
            assert blocks and not blocks[0]["applied"], cs
            pick = [{"kind": blocks[0]["kind"], "loc": blocks[0]["loc"]}]
            code, ap = _wb_call(url, "/api/census/apply", {"items": pick})
            assert ap["added"] == 1, ap
            code, ap = _wb_call(url, "/api/census/apply", {"items": pick})
            assert ap["added"] == 0, ap
            code, an = _wb_call(url, "/api/annotations")
            assert blocks[0]["loc"] in an["text"] and '"text"' in an["text"], an["text"]
        finally:
            WB.environment = real
            stop()
    finally:
        shutil.rmtree(work, True)
        WB.JOBS.clear()
    return ("observe/addresses command chains, parameter checks, project folder walled, "
            "emulator jobs refused cleanly, disassemble / annotations round trip over HTTP")


def t_workbench_mame():
    """An emulator job through the workbench: cycle budget and a generic probe."""
    ctx = _mame_ctx()
    if not ctx:
        return None
    WB, work = _wb_setup("mame")
    try:
        url, stop = _wb_serve(WB)
        try:
            code, j = _wb_call(url, "/api/job", {"kind": "budget", "params": {
                "seconds": 5, "from_frame": 100, "frames": 150}})
            assert code == 200, j
            d = _wb_wait(url, j["id"])
            assert d["status"] == "done", d["lines"][-6:]
            code, txt = _wb_call(url, "/api/file?path=budget/cyclebudget.log")
            m = re.search(rb"f\d+ executed (\d+) nmi (\d+) slow (\d+) dma (\d+)", txt)
            assert m and abs(sum(int(x) for x in (m.group(1), m.group(3), m.group(4))) - 29850.5) < 3, txt
            code, j = _wb_call(url, "/api/job", {"kind": "probe", "params": {
                "probe": "dlitimes", "seconds": 5,
                "env": "A7800_DT_FRAME=100\nA7800_DT_FROM=100\nA7800_DT_END=110"}})
            d = _wb_wait(url, j["id"])
            assert d["status"] == "done" and any(o["path"].endswith("dlitimes.log")
                                                 for o in d["outputs"]), d["lines"][-5:]
            # music capture on a POKEY cartridge converts to TIA when asked
            code, j = _wb_call(url, "/api/job", {"kind": "music", "params": {
                "seconds": 5, "drive": False, "to_tia": True}})
            d = _wb_wait(url, j["id"])
            names = [o["path"] for o in d["outputs"]]
            assert d["status"] == "done" and "music/tia/tia.wav" in names and \
                "music/song.wav" in names, (d["status"], names, d["lines"][-4:])
        finally:
            stop()
    finally:
        shutil.rmtree(work, True)
        WB.JOBS.clear()
    return "cycle budget, a generic probe, and POKEY-to-TIA music capture run as jobs"


def t_workbench_browser():
    """Drive the real page in a headless browser: tabs, a job run from its form with
    live results, the listing search, and the annotations check -- the layer the
    parse check cannot see. Skipped without Playwright and a browser."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    WB, work = _wb_setup("browser")
    url, stop = _wb_serve(WB)
    errors = []
    try:
        with sync_playwright() as p:
            b = None
            exes = [None] + sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
            if os.environ.get("A7800_CHROME"):
                exes.insert(0, os.environ["A7800_CHROME"])
            for exe in exes:
                try:
                    b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
                    break
                except Exception:                              # noqa: BLE001
                    continue
            if b is None:
                return None
            pg = b.new_page(viewport={"width": 1250, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(url + "/#run")
            pg.wait_for_selector(".card")
            assert pg.locator("#tabs button").count() == 5
            card = pg.locator(".card", has_text="Disassemble").first
            card.locator("button.go").click()
            pg.wait_for_function("document.querySelector('#jobdetail h3 span') && "
                                 "document.querySelector('#jobdetail h3 span').textContent==='done'",
                                 timeout=60000)
            pg.wait_for_function("document.querySelector('.job .dot').classList.contains('done')",
                                 timeout=10000)
            pg.wait_for_selector(".file", timeout=10000)   # the pane is redrawn once more when a job ends
            pg.click("#tabs button[data-tab=listing]")
            pg.wait_for_selector("#t-listing pre.code div")
            pg.fill("#t-listing input[type=text]", "C000")
            pg.click("#t-listing button:has-text('find')")
            pg.wait_for_selector("#t-listing pre.code .hit")
            pg.click("#tabs button[data-tab=annotations]")
            pg.click("#t-annotations button:has-text('start one')")
            pg.wait_for_selector("#t-annotations textarea", timeout=20000)
            pg.fill("#t-annotations textarea", '{"entrys": []}')
            pg.click("#t-annotations button:has-text('save and check')")
            # one press, no tolerance: the stamp is a string, so it survives the round
            # trip through JavaScript (a 19-digit number did not)
            pg.wait_for_selector("#t-annotations .find.error", timeout=6000)
            assert "did you mean" in pg.inner_text("#t-annotations .find.error")
            pg.fill("#t-annotations textarea", '{"entries": []}')
            pg.click("#t-annotations button:has-text('save and check')")
            pg.wait_for_function("document.querySelector('#t-annotations .muted, #t-annotations span') && "
                                 "/saved/.test(document.querySelector('#t-annotations').innerText)",
                                 timeout=6000)
            b.close()
    finally:
        stop()
        shutil.rmtree(work, True)
        WB.JOBS.clear()
    assert not errors, errors
    return "tabs, a job from its form, live results, listing search and annotation checks"


def t_simprobe():
    """simprobe.py reports what a run of the synthetic cartridge is known to do: the
    instructions, the JMP (vec) and the hand-pushed RTS, the computed bank switch, the
    table it reads as data (and not the immediates), and the dump firstlook reads."""
    import simprobe
    synth = _synth()
    _data, facts = synth.build()
    rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    out = tempfile.mkdtemp(prefix="selftest-simprobe-")
    try:
        r = simprobe.probe(rom, out, frames=120)
        read = lambda n: io.open(os.path.join(out, n), encoding="utf-8").read()   # noqa: E731
        xt = read("exectrace.log")
        for sym in ("reset", "handler_a", "rts_target"):
            assert "X f7:%04X" % facts[sym] in xt, sym
        assert "J f7:C0E2 f7:%04X" % facts["handler_a"] in xt, xt
        assert "J f7:%04X f7:%04X" % (facts["trick_rts"], facts["rts_target"]) in xt, \
            "the hand-pushed RTS was not reported as a computed jump"
        sw = "f7:%04X" % facts["computed_switch"]
        for b in facts["executed_banks"]:
            assert "S %s %d x" % (sw, b) in xt, (b, xt)
        for b in facts["never_executed_banks"]:
            assert "X b%d:" % b not in xt, b
        # data reads: the tune table, and never an immediate operand
        import asm
        a = asm.Assembler()
        a.assemble(synth.bank_source(3).splitlines())
        dr = read("dataread.log")
        # bank 3 is the third table entry: selected when FRAME & 3 == 2, so entries 2 and 6
        for k in range(8):
            assert ("D b3:%04X " % (a.sym["tune_f"] + k) in dr) == (k in (2, 6)), (k, dr)
        import cart as cart_module
        c = cart_module.Cart(rom)
        for ln in dr.splitlines():
            sp, _, ad = ln.split()[1].partition(":")
            ad = int(ad, 16)
            before = (sp, ad - 1)
            assert not (c.byte(sp, ad - 1) == 0xA9 and "X %s:%04X" % before in xt), \
                "an immediate operand was counted as a data read: " + ln
        import firstlook
        d = firstlook.parse_regs(os.path.join(out, "regs.txt"))
        assert d["dll"] == 0x1800 and d["frame"] == 120 and d["writes"] is not None, d
        assert len(open(os.path.join(out, "ram.bin"), "rb").read()) == 0x1000
        assert r["frames_with_display_list"] > 100 and r["audio_writes"] > 20, r
        # the display-interrupt timing in probes/dlitimes.lua's format, from the simulator
        simprobe.probe(rom, out, frames=130, drive=True, interrupts=100)
        dl = read("dlitimes.log")
        nmis = [float(m) for m in re.findall(r"nmi\s+frame \d+\s+line ([\d.]+)", dl)]
        assert "zone  0  line   0" in dl and len(nmis) >= 10, dl[:400]
        assert all(abs(x - nmis[0]) < 1.5 for x in nmis), nmis      # the same line every frame
        assert "vblank begins  line 258" in dl, dl[-200:]      # raster lines, as the probe prints them
        assert all(abs(x - 96) < 1.5 for x in nmis), nmis   # zone 0 is raster 16; the flagged zone starts 80 lines in
    finally:
        shutil.rmtree(out, True)
    return "instructions, JMP (vec), computed RTS, computed switch, table reads (no immediates), dump, interrupt timing"


def t_simorigins():
    """simorigins.py (addrorigin.lua without MAME) finds the same facts on the synthetic
    cartridge: the JMP (vec) halves and the hand-pushed return address, both immediates,
    and agrees with what origins.py reads."""
    import cart as cart_module
    import origins
    import simorigins
    synth = _synth()
    _d, facts = synth.build()
    rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    out = tempfile.mkdtemp(prefix="selftest-simorg-")
    try:
        simorigins.trace(rom, out, frames=100)
        uses, imms = origins.parse_log(io.open(os.path.join(out, "addrorigin.log"),
                                               encoding="utf-8").read())
        found = origins.analyse(uses, imms, cart_module.Cart(rom))
        sc = cart_module.Cart(rom)
        byte = lambda loc: sc.byte(*origins.split_loc(loc))         # noqa: E731
        ha, rt = facts["handler_a"], facts["rts_target"] - 1
        assert sorted(byte(l) for l, k in found["immediates"] if k == "jmpind") == \
            sorted([ha & 0xFF, ha >> 8]), found["immediates"]
        assert sorted(byte(l) for l, k in found["immediates"] if k == "rts") == \
            sorted([rt & 0xFF, rt >> 8]), found["immediates"]
        assert sorted(u[0] for u in uses) == ["jmpind", "rts"], uses
        assert found["computed"] == [] and found["tables"] == [], found
    finally:
        shutil.rmtree(out, True)
    return "JMP (vec) and hand-pushed RTS traced to their immediates, as the MAME probe does"


def t_census():
    """census.py finds what the synthetic cartridge was built with: the dark areas are
    exactly the text bank and the two banks nothing touches, an executed bank is not
    dark, the hand-built tables count as read, bank 7 is reported once, and RAM roles
    are right (the frame and interrupt counters are counters)."""
    import census
    import cart as cart_module
    synth = _synth()
    _d, facts = synth.build()
    rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    out = tempfile.mkdtemp(prefix="selftest-census-")
    try:
        r = census.build(rom, 200, False)
        cart = r["cart"]
        assert "b7" not in census.canon_spaces(cart) and "f7" in census.canon_spaces(cart)
        dark = census.dark_areas(cart, r["cls"])
        # the branch the run never takes leads to code nothing reaches: dark, until
        # branchforce finds it (t_branchforce)
        lo = facts["sym"]["rare_path"]
        assert any(a["space"] == "f7" and a["lo"] == lo for a in dark), dark
        # (the forcing demo's table is only partly read, so its unread tail is dark too)
        end = facts["sym"]["bank_table"]
        dark = [a for a in dark if not (a["space"] == "f7" and facts["fdata"] <= a["lo"] < end)]
        got = {(a["space"], a["guess"]) for a in dark}
        want = {("b%d" % facts["text_bank"], "text")} | \
               {("b%d" % b, "graphics-like") for b in facts["never_executed_banks"]
                if b != facts["text_bank"]}
        assert got == want, (got, want)
        assert not any(a["space"] in ("b%d" % b for b in facts["executed_banks"])
                       for a in dark), "an executed bank was called dark"
        per, tot = census.summary(cart, r["cls"])
        assert per["f7"][census.GFX] >= facts["sprite_width"], per["f7"]   # MARIA read the sprite
        assert per["f7"][census.UNRUN] > 0                                 # code the run did not exercise
        # RAM roles
        rows, uninit, free = census.ram_report(r["col"])
        role = {x["addr"]: x["role"] for x in rows}
        for name in ("frame_counter", "nmi_counter"):
            assert role[0x2000 + facts[name]] == "counter", (name, role.get(0x2000 + facts[name]))
        assert any(lo <= 0x2200 <= hi for lo, hi in free), free      # nothing used $2200
        assert r["col"].minsp > 0xE0
        # the command and its files, and merging only adds
        o2 = os.path.join(out, "run")
        assert census.main([rom, "-o", o2, "--frames", "120"]) == 0
        j = json.load(io.open(os.path.join(o2, "census.json"), encoding="utf-8"))
        assert j["totals"]["DARK"] > 0 and os.path.exists(os.path.join(o2, "census.md"))
        m = census.build(rom, 60, False, merge=[os.path.join(o2, "census.json")])
        _p, tot2 = census.summary(cart_module.Cart(rom), m["cls"])
        assert tot2[census.EXEC] >= j["totals"]["executed"], "merging lost coverage"
        j["rom_sha1"] = "0" * 40
        other = os.path.join(out, "other.json")
        io.open(other, "w", encoding="utf-8").write(json.dumps(j))
        try:
            census.build(rom, 60, False, merge=[other])
        except ValueError as e:
            assert "different cartridge" in str(e), e
        else:
            raise AssertionError("a census of another cartridge was merged")
    finally:
        shutil.rmtree(out, True)
    return "dark = the text bank and the two untouched banks; RAM counters found; merge only grows"


def t_census_bankset():
    """A bankset cartridge is two halves: the CPU runs the first and MARIA draws from the
    second. The census reports both, and the sprite MARIA reads shows up in MARIA's half
    (`mf7`) and not in the CPU's."""
    import census
    import cart as cart_module
    synth = _synth()
    data, facts = synth.build()
    body = bytearray(data[128:])
    maria = bytearray(body)
    row = facts["sprite_row"] - 0xC000 + 0x1C000          # the sprite, in the fixed bank
    for i in range(facts["sprite_width"]):
        maria[row + i] ^= 0xFF                              # make the MARIA copy differ
    hdr = bytearray(data[:128])
    ctype = (hdr[53] << 8) | hdr[54]
    hdr[53], hdr[54] = (ctype | 0x2000) >> 8, (ctype | 0x2000) & 0xFF
    hdr[49:53] = (2 * len(body)).to_bytes(4, "big")
    work = tempfile.mkdtemp(prefix="selftest-bankset-")
    try:
        rom = os.path.join(work, "bankset.a78")
        io.open(rom, "wb").write(bytes(hdr) + bytes(body) + bytes(maria))
        cart = cart_module.Cart(rom)
        assert cart.bankset and cart.for_maria() is not cart
        assert cart.for_maria().byte("f7", facts["sprite_row"]) != cart.byte("f7", facts["sprite_row"])
        assert "mf7" in cart.sides().spaces() and cart.sides()._file_base("mf7") == len(body) + 0x1C000
        r = census.build(rom, 120, False)
        per, tot = census.summary(r["cart"], r["cls"])
        assert per["mf7"][census.GFX] >= facts["sprite_width"], per["mf7"]
        assert per["f7"][census.GFX] == 0, per["f7"]
        assert per["f7"][census.EXEC] > 0 and per["mf7"][census.EXEC] == 0
        assert not any(x["loc"].startswith("m") for x in census.suggestions(
            census.dark_areas(r["cart"], r["cls"])))
    finally:
        shutil.rmtree(work, True)
    return "both halves reported; the sprite is MARIA's, in mf7; no suggestions for MARIA's half"


def t_bankset_roundtrip():
    """A bankset cartridge disassembles into the CPU's listing plus MARIA's half as data
    (`m<space>.asm`), and build.py puts the two back together byte for byte."""
    import asm
    synth = _synth()
    fixed = asm.Assembler().assemble(synth.fixed_source("Bankset").splitlines())
    assert len(fixed) == 0x4000
    maria = bytes((i * 7 + 3) & 0xFF for i in range(0x4000))
    hdr = bytearray(128)
    hdr[0] = 1
    hdr[1:10] = b"ATARI7800"
    hdr[17:24] = b"Bankset"
    hdr[49:53] = (0x8000).to_bytes(4, "big")
    hdr[53], hdr[54] = 0x20, 0x00
    hdr[55] = 1
    work = tempfile.mkdtemp(prefix="selftest-bsrt-")
    try:
        rom = os.path.join(work, "b.a78")
        io.open(rom, "wb").write(bytes(hdr) + bytes(fixed) + maria)
        src = os.path.join(work, "src")
        for cmd in ([sys.executable, os.path.join(HERE, "disasm.py"), rom, "-o", src],):
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert r.returncode == 0, r.stdout[-400:]
        assert sorted(f for f in os.listdir(src) if f.endswith(".asm")) == ["mrom.asm", "rom.asm"]
        out = os.path.join(work, "out.a78")
        r = subprocess.run([sys.executable, os.path.join(HERE, "build.py"), rom, "-d", src,
                            "-o", out], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert r.returncode == 0 and b"identical to reference ROM: YES" in r.stdout, r.stdout[-400:]
        assert io.open(out, "rb").read() == io.open(rom, "rb").read()
        # the editors' idea of where a byte lives must match the reader's on both halves:
        # the sprite editor once saved MARIA's artwork into the CPU's half
        import spriteedit as SE
        import cart as cart_module
        raw = io.open(rom, "rb").read()
        for side in ("sally", "maria"):
            c = cart_module.Cart(rom, side=side)
            sp = c.spaces()[0]
            SE.CART, SE.PATH, SE.DATA = c, rom, bytearray(raw)
            SE.REGION = SE.Region(c, sp, c.base_of(sp), 1, 8, 256, 256, "160")
            for a in range(c.base_of(sp), c.base_of(sp) + c.size_of(sp), 397):
                assert SE.DATA[SE.REGION.file_offset(a)] == c.byte(sp, a), (side, sp, a)
        # and the disassembler's file start for MARIA's listing is the second half
        sides = cart_module.Cart(rom).sides()
        off = sides._offset("mrom", sides.base_of("mrom"))
        assert raw[128 + off] == maria[0], "the MARIA listing starts in the second half"
    finally:
        shutil.rmtree(work, True)
    return "CPU listing plus MARIA's half as data rebuilds the image byte for byte; both halves' editor offsets agree with the reader"


def t_bankset_fork_model():
    """The bankset layout against the `a7800` fork's own cartridge code (bankset.cpp, read_40xx,
    transcribed here): for every CPU or MARIA read in $4000-$FFFF, in every window bank, the
    byte the fork returns is the byte `Cart` returns, on synthetic images of the flat 2x32K,
    2x48K, 2x52K and the SuperGame 2x128K and 2x256K forms. This is the evidence for MARIA's
    half using the same bank number as the CPU's, which no run of mainline MAME can give."""
    import random
    import cart as cart_module

    def fork_read(rom, mapper, addr, dma, bank):
        size = len(rom)
        mask = size // 0x4000 - (2 if (size // 0x4000) & 1 else 1)
        off, half = addr - 0x4000, (mask // 2 + 1) * 0x4000
        base = half if dma else 0
        if mapper == 0x2002:                                   # SuperGame bankset, no RAM
            if off < 0x4000:
                return rom[off + (mask // 2) * 0x4000 - 0x4000 + base]
            if off < 0x8000:
                return rom[(off & 0x3FFF) + bank * 0x4000 + base]
            return rom[(off & 0x3FFF) + (mask // 2) * 0x4000 + base]
        if size >= 0x1A000:                                    # 52K: the image starts at $3000
            return rom[off + 0x1000 + (0xD000 if dma else 0)]
        start = 0xC000 - size // 2
        return 0xFF if off < start else rom[off - start + (size // 2 if dma else 0)]

    rng = random.Random(7)
    work = tempfile.mkdtemp(prefix="selftest-bsfork-")
    tested = 0
    try:
        for mapper, size in ((0x2000, 0x10000), (0x2000, 0x18000), (0x2000, 0x1A000),
                             (0x2002, 0x40000), (0x2002, 0x80000)):
            body = bytes(rng.randrange(256) for _ in range(size))
            hdr = bytearray(128)
            hdr[0] = 1
            hdr[1:10] = b"ATARI7800"
            hdr[49:53] = size.to_bytes(4, "big")
            hdr[53], hdr[54] = mapper >> 8, mapper & 0xFF
            hdr[55] = 1
            rom = os.path.join(work, "b%X-%X.a78" % (mapper, size))
            io.open(rom, "wb").write(bytes(hdr) + body)
            sg = mapper == 0x2002
            for who, dma in (("sally", 0), ("maria", 1)):
                c = cart_module.Cart(rom, side=who)
                for b in (range(c.map.nwindow) if sg else [0]):
                    for addr in list(range(0x4000, 0x10000, 0x2F7)) + [0x4000, 0x7FFF, 0x8000,
                                                                       0xBFFF, 0xC000, 0xFFFF]:
                        sp = c.space_of(addr, b if sg else None)
                        got = c.byte(sp, addr) if sp else 0xFF
                        want = fork_read(body, mapper, addr, dma, b)
                        assert got == want, (hex(mapper), hex(size), who, b, hex(addr), want, got)
                        tested += 1
    finally:
        shutil.rmtree(work, True)
    return "%d reads on 5 bankset forms, CPU and MARIA, agree with the a7800 fork's code" % tested


def t_bankset_ram():
    """A bankset's bank RAM, as the a7800 fork's bankset.cpp has it and as the simulator now
    models it: 16K of RAM at $4000 for each chip, the CPU's writes to $C000-$FFFF landing in
    MARIA's, MARIA reading the other half of the ROM with the CPU's bank, and (flat banksets
    with POKEY at $4000) ROM, not the chip, answering reads at $4000. Observed on the fork:
    "BANKRAM 1" on the 2x128K RAM demo is text the CPU wrote into MARIA's RAM."""
    import random
    import cart as cart_module
    import sim
    rng = random.Random(3)
    work = tempfile.mkdtemp(prefix="selftest-bsram-")

    def image(name, mapper, size):
        body = bytes(rng.randrange(256) for _ in range(size))
        hdr = bytearray(128)
        hdr[0] = 1
        hdr[1:10] = b"ATARI7800"
        hdr[49:53] = size.to_bytes(4, "big")
        hdr[53], hdr[54] = mapper >> 8, mapper & 0xFF
        hdr[55] = 1
        path = os.path.join(work, name)
        io.open(path, "wb").write(bytes(hdr) + body)
        return path
    try:
        c = cart_module.Cart(image("sg.a78", 0x6002, 0x40000))
        assert c.bankram and not cart_module.Cart(image("sg0.a78", 0x2002, 0x40000)).bankram
        bus = sim.Bus(c)
        assert bus.mram is not None and bus.mcart is not None
        bus.write(0x8000, 3)                                   # the CPU selects bank 3
        assert bus.bank == 3
        bus.write(0xC123, 0x5A)                                # ... and writes MARIA's RAM
        assert bus.bank == 3 and bus.mram[0x123] == 0x5A, "a write to $C000+ switched or was lost"
        bus.write(0x4123, 0x77)                                # the CPU's own RAM
        assert bus.read(0x4123) == 0x77 and bus.mem(0x4123) == 0x5A, "the two RAMs are one"
        m = cart_module.Cart(c.path, side="maria")
        assert bus.mem(0x8000) == m.byte("b3", 0x8000), "MARIA did not read its half, bank 3"
        assert bus.mem(0xC000) == m.byte(m.space_of(0xC000, None), 0xC000)
        # a flat 2x48K with POKEY at $4000: the ROM answers at $4000
        f = cart_module.Cart(image("flat.a78", 0x2001, 0x18000))
        fb = sim.Bus(f)
        assert fb.mram is None
        assert fb.read(0x4000) == f.byte(f.space_of(0x4000, None), 0x4000), "POKEY hid the ROM"
        # ... but with no ROM there (2x32K) it is the chip's, as before
        g = cart_module.Cart(image("flat32.a78", 0x2001, 0x10000))
        assert sim.Bus(g).read(0x4000) == 0xFF
        # a flat bankset with bank RAM: RAM at $4000 for the CPU, MARIA's through $C000
        r = cart_module.Cart(image("flatram.a78", 0x6000, 0x10000))
        rb = sim.Bus(r)
        assert r.bankram and rb.mram is not None and r.space_of(0x4000, None) is None
        rb.write(0x4000, 5)
        rb.write(0xC001, 9)
        assert rb.read(0x4000) == 5 and rb.mram[1] == 9 and rb.mem(0x4001) == 9
    finally:
        shutil.rmtree(work, True)
    return "bank RAM per chip, CPU writes through $C000, MARIA's half and bank, ROM over POKEY at $4000"


def t_mamecheck():
    """mamecheck.py's judgement, without MAME: a display list kept live on half the frames
    counts as running, and the four verdicts come out right and report without error."""
    import mamecheck
    assert mamecheck.live_frames([(0x18, 0, 0x4B)] * 3 + [(0x00, 0, 0x4B), (0x18, 0, 0x7B)]) == (5, 3)
    ran = lambda live: {"ran": True, "frames": 100, "live": live}      # noqa: E731
    cases = {"both": (ran(90), ran(80)), "sim only": (ran(2), ran(90)),
             "mame only": (ran(90), ran(0)), "neither": ({"ran": False}, ran(0))}
    recs = []
    for want, (m, s_) in cases.items():
        rec = {"name": want, "ok": True, "mapper": "linear", "mame": m, "sim": s_}
        assert mamecheck.verdict(rec) == want, (want, mamecheck.verdict(rec))
        recs.append(rec)
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        mamecheck.report(recs)
    finally:
        sys.stdout = old
    assert "SIM ONLY" in buf.getvalue() and "MAME ONLY" in buf.getvalue()
    return "live-display-list rule and the four verdicts"


def t_exrom_layout():
    """The `$0008` (EXROM) SuperGame layout as measured in MAME and as the a7800 fork codes it
    (a78_rom_sg9_device): file bank 0 at $4000, the last bank at $C000, and the window showing
    file bank (value & (n-2)) + 1."""
    import cart as cart_module
    work = tempfile.mkdtemp(prefix="selftest-exrom-")
    try:
        nb = 9
        body = bytearray()
        for b in range(nb):
            body += bytes([b]) * 0x4000
        body[-6:] = bytes([0x00, 0xC0, 0x00, 0xC0, 0x00, 0xC0])
        hdr = bytearray(128)
        hdr[0] = 1
        hdr[1:10] = b"ATARI7800"
        hdr[49:53] = len(body).to_bytes(4, "big")
        hdr[53], hdr[54] = 0x00, 0x0A                 # SuperGame + EXROM
        hdr[55] = 1
        rom = os.path.join(work, "e.a78")
        io.open(rom, "wb").write(bytes(hdr) + bytes(body))
        c = cart_module.Cart(rom)
        assert c.byte(c.space_of(0x4000, None), 0x4000) == 0, "$4000 is not file bank 0"
        assert c.byte(c.space_of(0xC000, None), 0xC000) == nb - 1, "$C000 is not the last bank"
        assert c.map.bank_from_write(0x8000, 5) == 6 and c.map.bank_from_write(0x8000, 0) == 1
        assert c.map.bank_from_write(0x8000, 7) == nb - 1, "value 7 is the last bank, also fixed at $C000"
        assert c.map.bank_from_write(0x8000, 8) == 1, "the window has nb-1 banks and wraps"
        sp = c.spaces()
        assert sp[0] == "f0" and "b1" in sp and "b0" not in sp and "b%d" % (nb - 1) in sp and sp[-1] == "f%d" % (nb - 1), sp
        assert c.byte("b6", 0x8000) == 6
        # the Lua probes are told the same layout, or they name the wrong banks
        pe = c.probe_env()
        assert (pe["A7800_XT_FIRST"], pe["A7800_XT_WBANKS"], pe["A7800_XT_LOWBANK"]) == ("1", str(nb - 1), "0"), pe
    finally:
        shutil.rmtree(work, True)
    return "EXROM: f0 at $4000, last bank at $C000, window value v -> file bank v+1"


def t_om_file_offsets():
    """An (OM) Activision dump is read in the usual block order, so a write by address must
    be mapped back to the file: songfmt and the sprite editor once wrote into the XOR-1
    neighbour 8K block."""
    import cart as cart_module
    import songfmt
    import spriteedit as SE
    am = bytearray()
    for b in range(16):
        am += bytes([b]) * 0x2000
    am[0x1DFFA:0x1E000] = bytes([0x00, 0xA0] * 3)       # AM order: block 14 ends with the vectors
    am[-6:] = bytes([0xFF] * 6)
    fl = bytearray(len(am))
    for i in range(16):
        fl[i * 0x2000:(i + 1) * 0x2000] = am[(i ^ 1) * 0x2000:((i ^ 1) + 1) * 0x2000]
    hdr = bytearray(128)
    hdr[0] = 1
    hdr[1:10] = b"ATARI7800"
    hdr[49:53] = len(fl).to_bytes(4, "big")
    hdr[53], hdr[54] = 0x01, 0x00
    hdr[55] = 1
    work = tempfile.mkdtemp(prefix="selftest-om-")
    try:
        rom = os.path.join(work, "om.a78")
        raw = bytes(hdr) + bytes(fl)
        io.open(rom, "wb").write(raw)
        c = cart_module.Cart(rom)
        assert c.om_order, "the (OM) order was not recognised"
        for sp in c.spaces():
            for a in range(c.base_of(sp), c.base_of(sp) + c.size_of(sp), 97):
                assert raw[128 + c.file_offset(sp, a)] == c.byte(sp, a), (sp, a)
        sp = c.spaces()[0]
        a = c.base_of(sp) + 0x10
        out = songfmt.apply_writes(raw, c, [(sp, a, b"\xA5\x5A", "test")])
        io.open(rom, "wb").write(out)
        c2 = cart_module.Cart(rom)
        assert c2.slice(sp, a, 2) == b"\xA5\x5A", "the write did not land where the reader looks"
        assert sum(1 for x, y in zip(raw, out) if x != y) <= 2
        SE.CART, SE.PATH, SE.DATA = c, rom, bytearray(raw)
        SE.REGION = SE.Region(c, sp, c.base_of(sp), 1, 8, 256, 256, "160")
        assert SE.DATA[SE.REGION.file_offset(a)] == c.byte(sp, a)
    finally:
        shutil.rmtree(work, True)
    return "(OM) dumps: addresses map back to the file's block order for songs and sprites"


def t_dispatch_tables():
    """Table-driven `JMP (zp)` dispatch is followed: handlers named by an interleaved word
    table and by split low/high tables are traced, the table stops at the first entry that is
    not plausible code, and nothing is added when the flag is off."""
    import asm
    import disasm
    src = """
    .org $C000
reset:
    SEI
    CLD
    JSR disp2
    LDX #$02
    LDA wtab,X
    STA $B0
    LDA wtab+1,X
    STA $B1
    JMP ($00B0)
disp2:
    LDY #$01
    LDA lo_tab,Y
    STA $B2
    LDA hi_tab,Y
    STA $B3
    JMP ($00B2)
h0:
    NOP
    NOP
    NOP
    NOP
    RTS
h1:
    INX
    INX
    INX
    INX
    RTS
h2:
    INY
    INY
    INY
    INY
    RTS
h3:
    DEX
    DEX
    DEX
    DEX
    RTS
wtab:
    .word h0
    .word h1
    .word h2
    .byte $00,$00,$00,$00      ; the table ends: BRK is not code
lo_tab:
    .byte <h0, <h3
hi_tab:
    .byte >h0, >h3
nmi:
    RTI
vectors_pad:
    .res $FFFA-vectors_pad,$00
    .word nmi
    .word reset
    .word nmi
"""
    data = asm.Assembler().assemble(src.splitlines())
    a = asm.Assembler()
    a.assemble(src.splitlines())
    work = tempfile.mkdtemp(prefix="selftest-dispatch-")
    try:
        rom = os.path.join(work, "d.a78")
        hdr = bytearray(128)
        hdr[0] = 1
        hdr[1:10] = b"ATARI7800"
        hdr[49:53] = (0x4000).to_bytes(4, "big")
        hdr[55] = 1
        io.open(rom, "wb").write(bytes(hdr) + bytes(data))
        cart = disasm.Cart(rom)
        old = disasm.AUTO_DISPATCH
        try:
            disasm.AUTO_DISPATCH = False
            off, *_ = disasm.analyse(cart, disasm.Config())
            disasm.AUTO_DISPATCH = True
            on, *_ = disasm.analyse(cart, disasm.Config())
        finally:
            disasm.AUTO_DISPATCH = old
        sp = "rom"
        for h in ("h0", "h1", "h2", "h3"):
            assert (sp, a.sym[h]) not in off.code, "found without the rule: " + h
            assert (sp, a.sym[h]) in on.code, "not followed: " + h
        assert (sp, a.sym["wtab"] + 6) not in on.code, "the table's terminator was traced"
    finally:
        shutil.rmtree(work, True)
    return "interleaved and split tables followed; the table stops at implausible code"


def t_ram_vector_runs():
    """`LDA #<x / STA v / LDA #>x / STA v+1` installs one after another each find their own
    handler: every store is equidistant from two others, which a nearest-and-mutual rule
    resolved to the first only."""
    import asm
    import disasm
    src = """
    .org $C000
reset:
    SEI
    CLD
    LDA #<ha
    STA $0200
    LDA #>ha
    STA $0201
    LDA #<hb
    STA $0200
    LDA #>hb
    STA $0201
    LDA #<hc
    STA $0200
    LDA #>hc
    STA $0201
    JMP ($0200)
ha:
    INX
    INX
    INX
    RTS
hb:
    INY
    INY
    INY
    RTS
hc:
    DEX
    DEX
    DEX
    RTS
nmi:
    RTI
vectors_pad:
    .res $FFFA-vectors_pad,$00
    .word nmi
    .word reset
    .word nmi
"""
    a = asm.Assembler()
    data = a.assemble(src.splitlines())
    work = tempfile.mkdtemp(prefix="selftest-ramvec-")
    try:
        rom = os.path.join(work, "v.a78")
        hdr = bytearray(128)
        hdr[0] = 1
        hdr[1:10] = b"ATARI7800"
        hdr[49:53] = (0x4000).to_bytes(4, "big")
        hdr[55] = 1
        io.open(rom, "wb").write(bytes(hdr) + bytes(data))
        an, *_ = disasm.analyse(disasm.Cart(rom), disasm.Config())
        for h in ("ha", "hb", "hc"):
            assert ("rom", a.sym[h]) in an.code, "handler not found: " + h
        # and none that is half of one and half of another
        assert not [x for x in an.code if x[1] not in range(0xC000, 0xC040)], sorted(an.code)[-3:]
    finally:
        shutil.rmtree(work, True)
    return "consecutive RAM-vector installs each find their own handler"


def t_branchforce():
    """branchforce.py on the synthetic cartridge: a branch the run never takes leads to a
    hand-pushed RTS and code the static tracer cannot reach (kept, as joined), and its
    twin leads into JAM bytes (trimmed); starts at known data are all dead."""
    import branchforce
    import census
    import disasm
    synth = _synth()
    _d, facts = synth.build()
    rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    r = branchforce.explore(rom, frames=120)
    target = ("f7", facts["forced_target"])
    assert r["kept"].get(target) == "joined", r["kept"]
    an, _g, _w, _v = disasm.analyse(r["cart"], disasm.Config())
    assert target not in an.code, "the static tracer was not meant to reach it"
    # the path through the table dies: it runs into bytes the run read as data
    assert any("read as data" in why or "opcode" in why for why in r["dead"].values()), r["dead"]
    assert not any(l[1] in range(facts["forcing_demo"], facts["forced_target"] - 8)
                   and l not in r["real"].x and l in r["kept"] and
                   r["cart"].byte(l[0], l[1]) == 0x02 for l in r["kept"]), "JAM kept"
    nl = branchforce.null_rate(r["cart"], r["real"], r["watcher"], n=40)
    assert nl["dead"] > 0 and nl["joined"] <= nl["dead"], nl      # a control that can fail
    # in the census it is its own class, apart from what executed
    c = census.build(rom, 120, False, force=True)
    per, tot = census.summary(c["cart"], c["cls"])
    assert tot[census.FORCED] > 0 and tot[census.EXEC] > 0, tot
    sug = census.forced_suggestions(c["cart"], c["cls"])
    assert any(x["kind"] == "entry" and x["loc"].startswith("f7:") and
               int(x["loc"][3:], 16) <= facts["forced_target"] <
               int(x["loc"][3:], 16) + x["size"] for x in sug), sug
    return "forced branch found the computed-RTS target, junk path trimmed, data starts all dead"


def t_dyn_sim():
    """What a simulated run hands the disassembler beyond jump targets: with --explore
    --force the code only a forced branch reached arrives as `F` lines and becomes an
    entry marked as a proposal; bytes the run read as data but the listing prints as
    instructions are cut out as blocks, without losing a reached instruction; RAM
    trampolines count as jumps."""
    import dyn
    import simprobe
    synth = _synth()
    _d, facts = synth.build()
    rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    out = tempfile.mkdtemp(prefix="selftest-dynsim-")
    try:
        simprobe.probe(rom, out, frames=120, drive=True, explore=True, force=True)
        log = dyn.parse_log(io.open(os.path.join(out, "exectrace.log"), encoding="utf-8").read())
        assert any(facts["forced_target"] in v for v in log["f"].values()), log["f"]
        doc = {}
        doc, lines = dyn.apply(rom, doc, log, "exectrace.log")
        assert "f7:%04X" % facts["forced_target"] not in doc.get("entries", []), doc["entries"]
        assert "f7:%04X" % facts["forced_target"] in doc["_dynamic"]["exectrace.log"]["forced_proposals"]
        doc, lines = dyn.apply(rom, {}, log, "exectrace.log", adopt_forced=True)
        assert "f7:%04X" % facts["forced_target"] in doc["entries"], doc["entries"]
        assert "forced_entries" in doc["_dynamic"]["exectrace.log"], doc["_dynamic"]
        # reads over code the run never executed turn into one block, and nothing is lost
        lo = facts["fdata"]
        reads = {"f7": set(range(lo, lo + 8))}
        blocks = dyn.data_blocks(rom, doc, log, reads)
        assert len(blocks) == 1 and blocks[0]["loc"] == "f7:%04X" % lo and blocks[0]["len"] == 8, blocks
        # a table straddling the end of one space and the start of the next is split there,
        # never proposed as one block that overruns its listing
        fe = facts["fdata"]
        assert not [b for b in dyn.data_blocks(rom, doc, log, {"f7": set(range(fe, fe + 8)),
                                                                "b0": set(range(0x8000, 0x8008))})
                    if b["len"] > 8], "a block crossed a space boundary"
        # the piece before the boundary is judged like any other: bytes the listing does not
        # show as code are not "listed as code", so nothing is proposed for them
        assert not [b for b in dyn.data_blocks(rom, doc, log, {"b0": set(range(0xBFFA, 0xC000)),
                                                                "f7": set(range(0xC000, 0xC004))},
                                               min_len=4) if b["loc"].startswith("b0:")], \
            "the piece before a space boundary skipped the vetoes"
        # nor over a forced entry, which is code
        ft = facts["forced_target"]
        assert not dyn.data_blocks(rom, doc, log, {"f7": set(range(ft, ft + 3))}, min_len=3), \
            "a block was cut over an entry point"
        # ... but not a range a retained branch goes to: that is code the game also reads
        rp = facts["sym"]["rare_path"]
        assert not dyn.data_blocks(rom, doc, log, {"f7": set(range(rp, rp + 4))}, min_len=3), \
            "a block was cut over the target of a retained BNE"
        added, msg = dyn.apply_blocks(rom, doc, log, reads)
        assert not added or doc["blocks"], msg
        # a read of an executed byte is code, not data
        ex = sorted(log["x"]["f7"])[0]
        assert not dyn.data_blocks(rom, doc, log, {"f7": set(range(ex, ex + 8))})
    finally:
        shutil.rmtree(out, True)
    # a jump from a RAM trampoline is recorded, by where it went
    c = simprobe.Collector()
    c.kind = {0xC1: "f7"}
    c.bus = type("B", (), {"bank": 0})()
    c.jump_indirect(0x20EA, 0xC123)
    assert (("ram", 0x20EA), ("f7", 0xC123)) in c.j, c.j
    return "forced entries marked as proposals, read-as-data tables cut out, RAM trampolines recorded"


def t_census_guess():
    """The dark-area guesses on bytes whose nature is known."""
    import census
    code = bytes([0xA9, 0x01, 0x8D, 0x00, 0x20, 0xA2, 0x00, 0xBD, 0x00, 0x30, 0x9D, 0x00, 0x31,
                  0xE8, 0xD0, 0xF7, 0x60])
    assert census.guess(code)[0] == "code-like"
    assert census.guess(b"PRESS FIRE TO START")[0] == "text"
    assert census.guess(bytes([0xFF] * 40))[0] == "fill"
    # printable is not text: a staircase of values and a short run of letters in a table are not
    assert census.guess(b"-NOPQRST")[0] != "text" and census.guess(bytes(range(0x30, 0x3A)))[0] != "text"
    assert census.guess(bytes([0x1E, 0x28, 0x32, 0x3C, 0x46, 0x50, 0x5A, 0x64, 0x6E, 0x78]))[0] != "text"
    # real code in the middle of an area that starts and ends with bytes that are not
    mid = bytes([0x02] * 40) + bytes([0xA9, 0x01, 0x85, 0x10, 0xD0, 0xFA] * 12) + bytes([0x02] * 40)
    lab, why = census.guess(mid)
    assert lab == "code-like" and "from offset 40" in why, (lab, why)
    assert census.guess(bytes([0x02] * 20 + [0xA9, 0x01, 0x85, 0x10, 0xD0, 0xFA] * 3 + [0x02] * 100))[0] != "code-like"
    table = b"".join(bytes([lo, 0xC0 + i]) for i, lo in enumerate(range(0x10, 0x30, 4)))
    assert census.guess(table)[0].startswith("address table"), census.guess(table)
    gfx = bytes([0, 0, 0x18, 0x3C, 0x7E, 0xFF, 0x7E, 0x3C, 0x18, 0, 0, 0, 0x18, 0x3C, 0xFF, 0])
    assert census.guess(gfx)[0] == "graphics-like", census.guess(gfx)
    # given what the census has seen used, pixel bytes are not an address table, and a real
    # table is one only if its words point at used bytes
    pix = bytes([0x50, 0x55, 0xFA, 0x50, 0x57, 0xFA, 0x4C, 0x55, 0xAA, 0x5A, 0x45, 0xCC])
    assert not census.guess(pix, lambda w: False)[0].startswith("address table"), census.guess(pix, lambda w: False)
    assert census.guess(table, lambda w: 0xC000 <= w < 0xC800)[0].startswith("address table")
    # code after a short table prefix, and code followed by data, are still code-like
    assert census.guess(bytes([1, 2, 3]) + code + code)[0] == "code-like"
    assert census.guess(code + code + code + bytes([0xFF] * 6))[0] == "code-like"
    sug = census.suggestions([{"space": "f7", "lo": 0xC000, "size": 17, "guess": "code-like",
                               "evidence": ""}, {"space": "b5", "lo": 0x8000, "size": 21,
                                                 "guess": "text", "evidence": ""}])
    assert [x["kind"] for x in sug] == ["entry", "block"] and sug[1]["type"] == "text"
    return "code, text, fill, address table and graphics told apart; suggestions shaped for annotations"


def t_corpus():
    """corpus.py grades the static tracer against a run: on the synthetic cartridge it
    must find the code the tracer cannot reach, and say how each was entered."""
    import corpus
    rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    rec = corpus.measure(rom, frames=120)
    assert rec["ok"], rec
    rc = rec["recall"]
    assert rc["reached"] < rc["executed"], "the tracer cannot have reached everything here"
    assert rc["assisted_reached"] == rc["executed"], rc
    how = rec["missed_how"]
    # the JMP (VEC) target is found statically now (the vector's own immediate stores are
    # followed), so what is left is the hand-pushed RTS and the interrupt-entered code
    assert how.get("rts") and not how.get("jmp-ind"), how
    assert rec["run"]["frames_with_display_list"] > 100
    assert rec["data"]["in_static_code"] == 0, rec["data"]       # the tables are not code
    assert rec["static"]["unresolved_switches"] == 1, rec["static"]
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        corpus.report([rec, {"name": "bad.a78", "ok": False, "error": "UnknownMapper: x"}])
    finally:
        sys.stdout = old
    assert "1 measured, 1 not" in buf.getvalue() and "rts" in buf.getvalue(), buf.getvalue()
    return "recall %d/%d, assisted %d/%d, entered by %s" % (
        rc["reached"], rc["executed"], rc["assisted_reached"], rc["executed"],
        ", ".join("%s x%d" % kv for kv in sorted(how.items())))


def t_firstlook_sim():
    """A first look with no emulator: the simulator runs the cartridge and every live
    section is filled in, including the screen rebuilt from its RAM."""
    import firstlook
    out = tempfile.mkdtemp(prefix="selftest-flsim-")
    try:
        rom = os.path.join(ROOT, "tests", "carts", "synth128.a78")
        old, sys.stdout = sys.stdout, io.StringIO()
        try:
            rc = firstlook.main([rom, "-o", out, "--seconds", "3", "--engine", "sim"])
        finally:
            sys.stdout = old
        assert rc == 0
        text = io.open(os.path.join(out, "report.md"), encoding="utf-8").read()
        facts = json.load(io.open(os.path.join(out, "firstlook.json"), encoding="utf-8"))
        assert facts["engine"] == "sim" and facts["steps"] == [
            "music", "screens", "graphics", "sprites", "code"], facts["steps"]
        for heading in ("What it sounds like", "What it looks like", "The code that ran"):
            assert "## " + heading in text, heading
        assert "0 after" in text or "reached by the disassembler" in text, text[-800:]
        ann = json.load(io.open(os.path.join(out, "annotations.json"), encoding="utf-8"))
        assert any("C0E5" in e for e in ann["entries"]), ann["entries"]   # handler_a, via JMP (vec)
        try:
            import PIL                                              # noqa: F401
            assert os.path.exists(os.path.join(out, "graphics", "screen.png"))
            assert glob.glob(os.path.join(out, "screens", "at-f*.png"))
        except ImportError:
            pass
    finally:
        shutil.rmtree(out, True)
    return "music, screens, graphics, sprites and code, from the simulator alone"


def t_readme():
    """The README lists the tools that exist, and no others."""
    s = io.open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    claimed = set(re.findall(r"^\| `([a-z0-9_]+\.py)`", s, re.M))
    actual = set(os.path.basename(f)
                 for f in glob.glob(os.path.join(HERE, "*.py")))
    libs = set(["a7800.py", "m6502.py", "addr.py"])
    missing = sorted(claimed - actual)
    undocumented = sorted(actual - claimed - libs)
    if missing:
        raise AssertionError("README lists tools that do not exist: %s"
                             % ", ".join(missing))
    if undocumented:
        raise AssertionError("tools missing from the README: %s"
                             % ", ".join(undocumented))
    return "%d tools documented, none missing" % len(claimed)


def t_links():
    """No markdown link points at a file that is not there."""
    bad = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(root, f)
            s = io.open(p, encoding="utf-8").read()
            for m in re.finditer(
                    r"\]\(([^)#:]+\.(?:md|json|py|png|html))[^)]*\)", s):
                t = os.path.normpath(os.path.join(root, m.group(1)))
                if not os.path.exists(t):
                    bad.append("%s -> %s"
                               % (os.path.relpath(p, ROOT), m.group(1)))
    if bad:
        raise AssertionError("; ".join(bad))
    return "every markdown link resolves"


def _md_files():
    out = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        out += [os.path.join(root, f) for f in files if f.endswith(".md")]
    return sorted(out)


# Names that docs mention on purpose and that are not files in this repo: a
# user's own file, a sibling project's script, an output a tool writes.
DOC_NAMES_OK = {
    "your_symbols.py", "your_health.py", "your_health.lua", "health.py",
    "rooms.py", "karateka.py", "game.a78",
}
_PATH_TOKEN = re.compile(
    r"^(?:tools|probes|docs|formats|templates|examples)/[A-Za-z0-9_./-]*[A-Za-z0-9_]$")
_BARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.(?:py|lua)$")


def doc_references(text):
    """Backticked repo paths and bare script names a markdown file mentions."""
    for m in re.finditer(r"`([^`\n]+)`", text):
        tok = m.group(1).strip()
        if _PATH_TOKEN.match(tok) or _BARE_TOKEN.match(tok):
            yield tok


def t_docrefs():
    """Every file a doc names in backticks exists (so docs cannot cite a probe
    or tool that was never committed, or was renamed)."""
    bad = []
    for p in _md_files():
        s = io.open(p, encoding="utf-8").read()
        for tok in doc_references(s):
            if tok in DOC_NAMES_OK or "NN" in tok:
                continue
            if "/" in tok:
                ok = os.path.exists(os.path.join(ROOT, tok))
            else:
                ok = any(os.path.exists(os.path.join(ROOT, d, tok))
                         for d in ("tools", "probes"))
            if not ok:
                bad.append("%s -> %s" % (os.path.relpath(p, ROOT), tok))
    if bad:
        raise AssertionError("; ".join(sorted(set(bad))[:12])
                             + (" ..." if len(set(bad)) > 12 else ""))
    # the check itself must be able to fail
    probe = "see `tools/nope_x.py`, `probes/nope.lua` and `nope_y.py`"
    assert list(doc_references(probe)) == ["tools/nope_x.py", "probes/nope.lua",
                                           "nope_y.py"], "scanner is broken"
    return "every file a doc names in backticks exists"


def t_probe_index():
    """docs/emulation.md lists every probe, and lists nothing that is not there."""
    idx = io.open(os.path.join(ROOT, "docs", "emulation.md"),
                  encoding="utf-8").read()
    idx = idx[idx.index("## Probe index"):]
    have = {f for f in os.listdir(os.path.join(ROOT, "probes"))
            if f.endswith((".lua", ".py"))}
    listed = set()
    for row in re.findall(r"^\|([^|\n]*)\|", idx, re.M):     # first column only
        listed |= set(re.findall(r"`([A-Za-z0-9_-]+\.(?:lua|py))`", row))
    missing = sorted(have - listed)
    stale = sorted(listed - have)
    if missing or stale:
        raise AssertionError("not in the index: %s; in the index but absent: %s"
                             % (missing or "-", stale or "-"))
    return "%d probes, index complete both ways" % len(have)


def t_bat_quotes():
    """No batch file ends a quoted path in a backslash (`"%~dp0"` eats the quote)."""
    bad = []
    for f in sorted(glob.glob(os.path.join(ROOT, "*.bat"))):
        for i, ln in enumerate(io.open(f, encoding="utf-8", errors="replace"), 1):
            code = ln.split("rem ", 1)[0] if ln.lstrip().lower().startswith("rem") \
                else ln
            if re.search(r'(?:%~dp0|%HERE%|%DIR%\\?)"', code) and "set " not in code.lower():
                bad.append("%s:%d" % (os.path.basename(f), i))
    if bad:
        raise AssertionError("trailing-backslash quote in " + ", ".join(bad))
    return "no .bat quotes a path that ends in a backslash"


def t_lualint():
    """The Lua lint passes every shipped probe, and fails the traps it names."""
    import lualint
    problems = []
    for f in sorted(glob.glob(os.path.join(ROOT, "probes", "*.lua"))):
        for n, rule, msg in lualint.lint_file(f):
            problems.append("%s:%d %s" % (os.path.basename(f), n, rule))
    if problems:
        raise AssertionError("; ".join(problems[:10]))
    leak = ("-- probe\nlocal TAPS = {}\nTAPS[#TAPS+1] = mem:install_read_tap("
            "0, 1, \"x\", function(o, d) return d end)\n")
    got = [r for _n, r, _m in lualint.lint_text(leak)]
    assert got == ["tap-held-in-local"], got
    dropped = "-- probe\nmem:install_write_tap(0, 1, \"x\", function() end)\n"
    assert [r for _n, r, _m in lualint.lint_text(dropped)] == ["tap-held-in-local"]
    held = ("-- probe\nTAPS = {\n  mem:install_write_tap(0, 1, \"x\", "
            "function(o, d) return d end),\n}\n")
    assert lualint.lint_text(held) == [], lualint.lint_text(held)
    bare = "local x = 1\n"
    assert [r for _n, r, _m in lualint.lint_text(bare)] == ["no-header"]
    env = "-- probe, env A7800_ONE\nlocal a = os.getenv(\"A7800_ONE\")\nlocal b = os.getenv(\"A7800_TWO\")\n"
    got = lualint.lint_text(env)
    assert [r for _n, r, _m in got] == ["undocumented-env"] and "A7800_TWO" in got[0][2]
    return "%d probes clean; leak, dropped tap, header and env rules each fire" \
        % len(glob.glob(os.path.join(ROOT, "probes", "*.lua")))


def t_verify_explains():
    """A one-byte difference names the source line and shows got vs want."""
    d = tempfile.mkdtemp(prefix="explain-")
    out = run_tool("newgame.py", os.path.join(d, "g"), "--build")
    rom = os.path.join(d, "g", "game.a78")
    assert os.path.exists(rom), out
    src = os.path.join(d, "src")
    run_tool("disasm.py", rom, "-o", src)
    assert "PASSED" in run_tool("verify.py", rom, "-d", src)
    path = os.path.join(src, "rom.asm")
    lines = io.open(path, encoding="utf-8").read().split("\n")
    k = [i for i, l in enumerate(lines) if l.strip().startswith("LDA       #$")][2]
    lines[k] = "    LDA       #$7E" + " " * 30 + ";" + lines[k].split(";", 1)[1]
    io.open(path, "w", encoding="utf-8").write("\n".join(lines))
    out = run_tool("verify.py", rom, "-d", src)
    assert "FAILED" in out and "rom.asm:%d" % (k + 1) in out, out
    assert "got  LDA #$7E" in out and "want LDA #$60" in out, out
    assert "Traceback" not in run_tool("verify.py", os.path.join(d, "nope.a78"))
    assert "Traceback" not in run_tool("verify.py", rom, "-d", os.path.join(d, "x"))
    assert "Traceback" not in run_tool("build.py", os.path.join(d, "nope.a78"))
    io.open(path, "w", encoding="utf-8").write("  bogus line\n")
    out = run_tool("build.py", rom, "-d", src)
    assert "rom.asm" in out and "Traceback" not in out, out
    return "source line, got/want decode, and friendly errors"


def t_flake8():
    """pyflakes-class errors (unused names, undefined names, syntax) stay out."""
    exe = shutil.which("flake8")
    cmd = [exe] if exe else [sys.executable, "-m", "flake8"]
    try:
        p = subprocess.run(cmd + ["--select=F,E9", os.path.join(ROOT, "tools"),
                                  os.path.join(ROOT, "probes")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        return None
    out = p.stdout.decode("utf-8", "replace")
    if "No module named flake8" in out:
        return None
    if p.returncode:
        raise AssertionError(out.strip().splitlines()[0] + " (%d total)"
                             % len(out.strip().splitlines()))
    return "no unused or undefined names"


def _synth():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    import synth
    return synth


def t_synth_static():
    """tests/synth.py builds a banked POKEY cartridge whose gaps are the known
    ones: the static tracer must find the reset code, call the computed bank
    switch unresolved, and stay blind to the handler behind the RAM vector."""
    synth = _synth()
    ntsc, facts = synth.build()
    assert ntsc == synth.build()[0], "the build is not deterministic"
    committed = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    if os.path.exists(committed):
        assert io.open(committed, "rb").read() == ntsc, (
            "tests/carts/synth128.a78 is stale: python tests/synth.py "
            "tests/carts/synth128.a78")
    d = tempfile.mkdtemp(prefix="synth-")
    rom = os.path.join(d, "s.a78")
    io.open(rom, "wb").write(ntsc)
    pal = os.path.join(d, "p.a78")
    io.open(pal, "wb").write(synth.build("pal")[0])
    import cart as cart_module
    c = cart_module.Cart(rom)
    assert c.nbanks == 8 and c.pokeys() == [0x4000], (c.nbanks, c.pokeys())
    assert (cart_module.Cart(pal).info or {}).get("region", "").lower() == "pal"
    src = os.path.join(d, "src")
    out = run_tool("disasm.py", rom, "-o", src)
    assert "f7:%04X    -> UNRESOLVED" % facts["computed_switch"] in out, out[-600:]
    assert "PASSED" in run_tool("verify.py", rom, "-d", src)
    listing = io.open(os.path.join(src, "f7.asm"), encoding="utf-8").read()
    # reached from the vectors: reset code. And the target of `JMP (VEC)`: the tracer names
    # the vector from the JMP itself and follows the immediate stores that fill it.
    assert "; %04X:" % facts["reset"] in listing
    assert "; %04X:" % facts["handler_a"] in listing, \
        "the handler behind the RAM vector was not followed from its immediate stores"
    return "128K SuperGame+POKEY, deterministic, PAL variant, switch unresolved, vector followed, round-trips"


def t_annotations_lint():
    """annotations.py reads a file as disasm.py does and reports what it would
    have ignored: a typo'd key, a repeated key, a name given to two places."""
    import annotations as ann

    def kinds(doc):
        r, _d = ann.lint(doc if isinstance(doc, str) else json.dumps(doc))
        return ([m for m in r.errors], [m for m in r.warnings])

    for shipped in ("templates/annotations.json", "examples/exo-annotations.json"):
        r, _d = ann.lint(io.open(os.path.join(ROOT, shipped), encoding="utf-8").read())
        assert not r.errors and not r.warnings, (shipped, r.items)
    errs, _w = kinds({"label": {"f7:C000": "RESET"}})
    assert errs and "did you mean 'labels'" in errs[0], errs
    errs, _w = kinds('{"labels": {}, "labels": {"f7:C000": "X"}}')
    assert errs and "repeated key" in errs[0], errs
    errs, _w = kinds({"labels": {"f7:C000": "A", "f7:C010": "A"}})
    assert errs and "both" in errs[0], errs
    errs, _w = kinds({"labels": {"f7:C000": "BACKGRND"}})
    assert errs and "hardware register" in errs[0], errs
    errs, _w = kinds({"labels": {"nowhere": "A"}, "entries": ["f7:ZZZZ"]})
    assert len(errs) == 2, errs
    errs, _w = kinds({"blocks": [{"loc": "f7:D000", "len": 0}]})
    assert errs and "positive" in errs[0], errs
    errs, _w = kinds({"banksw": {"f7:C000": "three"}})
    assert errs and "bank number" in errs[0], errs
    errs, w = kinds({"blocks": [{"loc": "f7:D000", "end": "f7:D010"},
                                {"loc": "f7:D008", "end": "f7:D018", "name": "B"}]})
    assert not errs and w and "inside block" in w[0], (errs, w)
    # against a cartridge
    synth = _synth()
    data, f = synth.build()
    d = tempfile.mkdtemp(prefix="annlint-")
    rom = os.path.join(d, "s.a78")
    io.open(rom, "wb").write(data)
    reset = "f7:%04X" % f["reset"]
    doc = {"entries": [reset], "blocks": [{"loc": reset, "len": 8}],
           "labels": {"f7:%04X" % (f["reset"] + 3): "MID"}, "comments": {"b9:8000": "x"}}
    r, parsed = ann.lint(json.dumps(doc))
    ann.check_rom(r, parsed, rom)
    text = " ".join(m for _k, m in r.items)
    assert "inside the data block" in text, text
    assert "no space 'b9'" in text, text
    ok = {"entries": [reset], "labels": {"f7:%04X" % (f["reset"] + 3): "MID"}}   # inside LDX #$FF
    r, parsed = ann.lint(json.dumps(ok))
    ann.check_rom(r, parsed, rom)
    assert any("middle of the instruction" in m for m in r.warnings), r.items
    # the real thing, when the sibling repositories are to hand
    sib = os.environ.get("A7800_SIBLINGS")
    n = 0
    if sib:
        for f_ in glob.glob(os.path.join(sib, "*", "annotations*.json")):
            r, _p = ann.lint(io.open(f_, encoding="utf-8").read())
            assert not r.errors, (f_, r.errors)
            n += 1
    return "typos, repeats, clashes, bad shapes and bad locations flagged; ROM checks%s" % (
        "; %d real files clean" % n if n else "")


def t_addresses():
    """$C000, 0xC000 and C000 mean the same everywhere an address is typed, and a
    tool copied on its own into an empty folder still accepts all three."""
    import addr
    for text in ("$C000", "0xC000", "0xc000", "C000", " c000 "):
        assert addr.parse_addr(text) == 0xC000, text
    for bad in ("zzz", "", "$", "0x"):
        try:
            addr.address(bad)
        except Exception as e:                               # noqa: BLE001
            assert "is not an address" in str(e), (bad, e)
        else:
            raise AssertionError("%r was accepted as an address" % bad)
    synth = _synth()
    data, f = synth.build()
    d = tempfile.mkdtemp(prefix="addr-")
    rom = os.path.join(d, "s.a78")
    io.open(rom, "wb").write(data)
    outs = set()
    for spelling in ("$D000", "0xD000", "D000"):
        png = os.path.join(d, "g%s.png" % spelling.strip("$"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "gfx.py"), rom, "--space",
                            "f7", "--base", spelling, "--direct", "4", "--lines", "8", "-o", png],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode and b"PIL" in r.stdout:
            return None                              # no Pillow: nothing to compare
        assert r.returncode == 0, r.stdout[-300:]
        outs.add(hashlib.md5(io.open(png, "rb").read()).hexdigest())
    assert len(outs) == 1, "three spellings of one address drew different pictures"
    # a standalone tool, copied alone, takes the same spellings
    alone = os.path.join(d, "alone")
    os.makedirs(alone)
    for tool, args in (("dlwalk.py", ["--selftest"]), ("modmap.py", ["--help"])):
        shutil.copy(os.path.join(HERE, tool), alone)
    ram = os.path.join(alone, "ram.bin")
    io.open(ram, "wb").write(bytes(4096))
    for spelling in ("$1800", "0x1800", "1800"):
        r = subprocess.run([sys.executable, os.path.join(alone, "dlwalk.py"), "--raw", ram,
                            "--at", spelling, "--dll", spelling],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert b"Traceback" not in r.stdout and b"invalid" not in r.stdout, \
            (spelling, r.stdout[-300:])
    r = subprocess.run([sys.executable, os.path.join(alone, "modmap.py"), "--help"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert r.returncode == 0 and b"Traceback" not in r.stdout
    return "three spellings give one answer; standalone tools carry their own copy of the rule"


def t_pcmap():
    """pcmap.py groups samples under the nearest label below them, in their own
    space, and says so honestly when there is no label."""
    import pcmap
    samples = pcmap.parse_samples("f7:C010 30\nf7:C012 10\nf7:C100 50\nb3:8004 5\nf7:BFFF 5\n")
    assert samples[("f7", 0xC010)] == 30
    labels = {("f7", 0xC000): "RESET", ("f7", 0xC100): "Update", ("b3", 0x8000): "Tune"}
    rows = pcmap.profile(samples, labels)
    got = {r[0]: r[3] for r in rows}
    assert got == {"Update": 50, "RESET": 40, "Tune": 5, "f7:BFFF?": 5}, got
    assert rows[0][0] == "Update" and rows[0][4] == {0xC100: 50}, rows[0]
    assert pcmap.labels_from_annotations({"labels": {"f7:$C000": "A", "b3:0x8000": "B",
                                                      "junk": "C"}}) == {
        ("f7", 0xC000): "A", ("b3", 0x8000): "B"}
    return "samples grouped under the nearest label in their own space"


def t_dynamic():
    """dyn.py turns an exectrace log into annotations that reach code the
    vectors alone cannot: the handler behind a RAM vector, and every bank a
    computed switch selects -- without disturbing what was already written."""
    synth = _synth()
    data, f = synth.build()
    d = tempfile.mkdtemp(prefix="dyn-")
    rom = os.path.join(d, "s.a78")
    io.open(rom, "wb").write(data)
    ann = os.path.join(d, "a.json")
    run_tool("init.py", rom, "-o", ann)
    doc = json.load(io.open(ann, encoding="utf-8"))
    doc["labels"]["f7:C000"] = "MY_OWN_NAME"            # hand work to be preserved
    doc["banksw"] = {"f7:FFF0": [1]}
    io.open(ann, "w", encoding="utf-8").write(json.dumps(doc))
    # a log of the kind probes/exectrace.lua writes
    sw = "f7:%04X" % f["computed_switch"]
    log = os.path.join(d, "exectrace.log")
    io.open(log, "w", encoding="utf-8").write("\n".join(
        ["X f7:%04X" % f["reset"], "J f7:C0E2 f7:%04X" % f["handler_a"]]
        + ["S %s %d x10" % (sw, b) for b in f["executed_banks"]]
        + ["X b%d:8000" % b for b in f["executed_banks"]]) + "\n")
    out = run_tool("dyn.py", rom, log, "-c", ann)
    assert "Traceback" not in out, out
    new = json.load(io.open(ann, encoding="utf-8"))
    assert "f7:%04X" % f["handler_a"] in new["entries"], new["entries"]
    assert new["banksw"][sw] == f["executed_banks"], new["banksw"]
    assert new["banksw"]["f7:FFF0"] == [1], "a pin that was already there changed"
    assert new["labels"]["f7:C000"] == "MY_OWN_NAME", "a hand-written label changed"
    assert new["_dynamic"]["exectrace.log"]["observed_not_proven"] is True
    src = os.path.join(d, "src")
    run_tool("disasm.py", rom, "-c", ann, "-o", src)
    assert "PASSED" in run_tool("verify.py", rom, "-d", src)
    for b in f["executed_banks"]:
        listing = io.open(os.path.join(src, "b%d.asm" % b), encoding="utf-8").read()
        assert "; 8000:" in listing, "bank %d was not traced" % b
    again = run_tool("dyn.py", rom, log, "-c", ann, "--dry-run")
    assert "entry point" not in again and "bank switch" not in again, again
    return "RAM-vector handler and %d banks reached; hand work kept; idempotent" \
        % len(f["executed_banks"])


def t_firstlook_static():
    """firstlook's static half names what the cartridge is, with no MAME."""
    import firstlook
    synth = _synth()
    data, f = synth.build()
    d = tempfile.mkdtemp(prefix="firstlook-")
    rom = os.path.join(d, "s.a78")
    io.open(rom, "wb").write(data)
    out = os.path.join(d, "out")
    assert firstlook.main([rom, "-o", out, "--no-live"]) == 0
    text = io.open(os.path.join(out, "report.md"), encoding="utf-8").read()
    facts = json.load(io.open(os.path.join(out, "firstlook.json"), encoding="utf-8"))
    for want in ("Synth128", "supergame, 8 banks of 16K", "POKEY at $4000",
                 "## Music player", "## What static analysis finds"):
        assert want in text, (want, text[:500])
    assert facts["identity"]["pokeys"] == [0x4000], facts["identity"]
    assert facts["player_signature"], "no player fingerprint for a cartridge that plays"
    assert "ONE run" in text, "the report must say what its live sections are"
    assert not os.path.exists(os.path.join(out, "music")), "no-live ran something"
    assert firstlook.looks_like_text("PRESS FIRE TO START")
    assert not firstlook.looks_like_text("OMKKKKKKKKKKKMOQ")
    assert not firstlook.looks_like_text("!#%')+-/13579;=?ACEG")
    return "identity, player fingerprint, honest about its limits; text filter works"


MAME_MEASURED = 0.287        # what docs/emulation.md was measured on
MAME_OLDEST = 0.250          # below this the Lua API the probes use is not there


def mame_version(exe):
    """MAME's version as a float (0.264), or None if it will not say."""
    try:
        out = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=30
                             ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*(\d+)\.(\d+)", out)
    return float("%s.%s" % (m.group(1), m.group(2).zfill(3))) if m else None


def _fmt_ver(v):
    return "%.3f" % v


def _mame_ctx():
    """(command, env, workdir) to run the committed synthetic cartridge under
    MAME, or None if there is no usable MAME and BIOS."""
    import capture
    exe = capture.find_mame()
    cart = os.path.join(ROOT, "tests", "carts", "synth128.a78")
    roms = capture.find_rompath(cart) if os.path.exists(cart) else None
    if not (exe and roms):
        return None
    ver = mame_version(exe)
    if ver is not None and ver < MAME_OLDEST:
        return None
    work = tempfile.mkdtemp(prefix="mame-")
    base = [exe, "a7800"] + capture.bios_args() + [
        "-rompath", roms, "-cart", cart, "-video", "none", "-sound", "none",
        "-skip_gameinfo", "-nothrottle", "-seconds_to_run", "2"]
    return base, dict(os.environ, XDG_RUNTIME_DIR=work, SDL_AUDIODRIVER="dummy"), work


def _forced_row(ctx, name, writes, ctrl):
    """Force a display-list entry (probes/forcedl.lua); return the framebuffer
    values MAME drew on the first line it touched, from the first drawn pixel on
    (one per emulated pixel: a 160-mode pixel is two, a 320-mode pixel one)."""
    base, env, work = ctx
    out = subprocess.run(
        base + ["-autoboot_script", os.path.join(ROOT, "probes", "forcedl.lua")],
        cwd=work, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=dict(env, A7800_FD_WRITES=writes, A7800_FD_CTRL=ctrl,
                 A7800_FD_ROW="30-140")).stdout.decode("utf-8", "replace")
    rows = [l for l in out.splitlines() if l.startswith("ROW ")]
    assert rows and not rows[0].startswith("ROW none"), \
        "%s: nothing was drawn (%s)" % (name, out[-200:])
    return [int(v, 16) for v in rows[0].split()[4:]]


def _labels(pixels, background=0):
    """Values (or (palette, colour) pairs) as '.ABC': first appearance order."""
    seen, out = {}, []
    for px in pixels:
        out.append("." if px == background else
                   chr(65 + seen.setdefault(px, len(seen))))
    return "".join(out)


def t_pixel_formats():
    """tools/mariapix.py predicts what MAME draws: 160A, 160B, and one- and
    two-byte characters. This is the check that found a wrong bit in dlwalk."""
    ctx = _mame_ctx()
    if ctx is None:
        return None
    import mariapix
    cols = [0x46, 0x86, 0xC6, 0x1E, 0x7E, 0xAE, 0x34, 0x94, 0xD4, 0x56, 0xB6,
            0xE6, 0x2C, 0x6C, 0xAC, 0x4A, 0x8A, 0xCA, 0x3A, 0x9A, 0xDA, 0x5E,
            0xBE, 0xEE]
    pal = "; ".join("%X=%s" % (0x21 + 4 * n, " ".join("%02X" % c for c in cols[3 * n:3 * n + 3]))
                    for n in range(1, 8))
    data = [0x1B, 0xE4, 0x6C, 0xC6]
    P = 5                                        # base palette; group 4-7 is ours
    hexs = lambda bs: " ".join("%02X" % b for b in bs)      # noqa: E731
    direct = "1930=00 %02X 20 %02X 28 00; 2000*16/100=%s; " + pal
    chars = "1930=00 60 1A %02X 28 00; 1A00=01 02 03; 2000*16/100=00 %s; 34=20; " + pal
    cases = [
        ("160A", direct % (0x40, (P << 5) | 0x1C, hexs(data)), "40", "160A", data),
        ("160B", direct % (0xC0, (P << 5) | 0x1C, hexs(data)), "40", "160B", data),
        ("320A", direct % (0x40, (P << 5) | 0x1C, hexs([0xA5, 0x3C, 0xF0, 0x0F])), "43",
         "320A", [0xA5, 0x3C, 0xF0, 0x0F]),
        ("chars, one byte", chars % ((P << 5) | 29, "1B E4 6C C6"), "40", "160A",
         [0x1B, 0xE4, 0x6C]),
        ("chars, two bytes", chars % ((P << 5) | 29, "1B E4 6C C6"), "50", "160A",
         [0x1B, 0xE4, 0xE4, 0x6C, 0x6C, 0xC6]),
    ]
    done = []
    for name, writes, ctrl, fmt, bytes_ in cases:
        w = mariapix.width(fmt)
        predicted = []
        for pl, c in mariapix.row_pixels(fmt, bytes_, P):
            predicted += [None if c == 0 else (pl, c)] * w
        want = _labels(predicted, None).lstrip(".").rstrip(".")
        got = _labels(_forced_row(ctx, name, writes, ctrl))
        assert got.startswith(want), "%s: MAME drew %r, mariapix predicts %r" % (name, got, want)
        done.append(name)
    shutil.rmtree(ctx[2], True)
    return "mariapix matches MAME for " + ", ".join(done)


def t_probes_mame(rom):
    """Run the generic probes under real MAME on a cartridge we know the answers
    for. Skipped when there is no MAME or no BIOS to boot it."""
    import capture
    exe = capture.find_mame()
    roms = capture.find_rompath(rom or "x") if rom else None
    if not (exe and roms and rom):
        return None
    ver = mame_version(exe)
    if ver is not None and ver < MAME_OLDEST:
        return None          # too old for the Lua these probes use: skip, say so below
    work = tempfile.mkdtemp(prefix="mame-")
    base = [exe, "a7800"] + capture.bios_args() + [
        "-rompath", roms, "-cart", os.path.abspath(rom), "-video", "none",
        "-sound", "none", "-skip_gameinfo"]
    env = dict(os.environ, XDG_RUNTIME_DIR=work, SDL_AUDIODRIVER="dummy")

    def run(probe, seconds, extra=(), **vars):
        e = dict(env, **vars)
        p = subprocess.run(base + ["-nothrottle", "-seconds_to_run", str(seconds),
                                   "-autoboot_script",
                                   os.path.join(ROOT, "probes", probe)] + list(extra),
                           cwd=work, env=e, timeout=180,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.stdout.decode("utf-8", "replace")

    # the synthetic cartridge: sprite row at $D000 (width 4), frame counter $81
    out = run("reclength.lua", 4)
    m = re.search(r"recording length: (\d+) frames", out)
    assert m and 235 <= int(m.group(1)) <= 245, out[-300:]
    run("liveslots.lua", 5, A7800_SETTLE="30")
    d = json.load(io.open(os.path.join(work, "liveslots-out.json"), encoding="utf-8"))
    assert {"addr": 0xD000, "width": 4} in d["refs"], d
    run("ramsnap.lua", 5, A7800_PAGES="0000", A7800_EVERY="30")
    d = json.load(io.open(os.path.join(work, "ramsnap-out.json"), encoding="utf-8"))
    row = d["pages"]["0000"].get("129")
    assert row and row[1] - row[0] == 30, d       # the frame counter at $81
    out = run("freeram.lua", 4, A7800_CANDIDATES="80-83", A7800_CONTROL="81")
    assert "control $81 saw" in out and "taps worked" in out, out[-300:]
    run("pcwrites.lua", 3, A7800_PW_LO="0x81", A7800_PW_HI="0x81",
        A7800_PW_FROM="60", A7800_PW_TO="62")
    log = io.open(os.path.join(work, "pcwrites.log"), encoding="utf-8").read().split("\n")
    assert len(log) > 4 and "$0081" in log[1], log[:4]
    run("inputreaders.lua", 3, A7800_IR_NOBANK="1")
    log = io.open(os.path.join(work, "inputreaders.log"), encoding="utf-8").read()
    assert "SWCHA <-" in log, log
    # a recording, then its length: -exit_after_playback must stop it at the end
    os.mkdir(os.path.join(work, "rec"))
    subprocess.run(base + ["-seconds_to_run", "3", "-input_directory",
                           os.path.join(work, "rec"), "-record", "t.inp"],
                   cwd=work, env=env, timeout=120,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = run("reclength.lua", 60,
              ["-input_directory", os.path.join(work, "rec"),
               "-playback", "t.inp", "-exit_after_playback"])
    m = re.search(r"recording length: (\d+) frames\n", out)
    assert m and 175 <= int(m.group(1)) <= 185 and "CAP" not in out, out[-300:]
    # the banked synthetic cartridge: every fact it was built with, observed
    synth = _synth()
    data, facts = synth.build()
    srom = os.path.join(work, "synth.a78")
    io.open(srom, "wb").write(data)
    sbase = [srom if a == os.path.abspath(rom) else a for a in base]
    sp = subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "5",
                                 "-autoboot_script",
                                 os.path.join(ROOT, "probes", "peek.lua")],
                        cwd=work, env=dict(env, A7800_PEEK_FRAMES="240",
                                           A7800_PEEK_ADDRS="81,92,B1,B2,B3,B4,A0,A1"),
                        timeout=180, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    line = [l for l in sp.stdout.decode("utf-8", "replace").splitlines()
            if l.startswith("frame 240")]
    assert line, sp.stdout[-300:]
    v = {int(k, 16): int(x) for k, x in re.findall(r"\$00([0-9A-F]{2})=(\d+)", line[0])}
    for bank, addr in facts["hit_counters"].items():
        assert 50 <= v[addr] <= 70, ("bank %d ran %d times in 240 frames" % (bank, v[addr]), v)
    assert v[0xB3] == 0 and v[0x92] > 200, v      # bank 3 plays; NMI runs per frame
    assert v[0xA0] | (v[0xA1] << 8) == facts["handler_a"], v
    sp = subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "4",
                                 "-autoboot_script",
                                 os.path.join(ROOT, "probes", "exectrace.lua")],
                        cwd=work, env=env, timeout=300,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    xt = io.open(os.path.join(work, "exectrace.log"), encoding="utf-8").read()
    sw = "f7:%04X" % facts["computed_switch"]
    assert "J f7:C0E2 f7:%04X" % facts["handler_a"] in xt, xt[:400]
    for b in facts["executed_banks"]:
        assert re.search(r"^S %s %d x\d+$" % (sw, b), xt, re.M), (b, xt[:400])
        assert "X b%d:8000" % b in xt, b
    assert not any("X b%d:" % b in xt for b in facts["never_executed_banks"]), xt
    sp = subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "4",
                                 "-autoboot_script",
                                 os.path.join(ROOT, "probes", "pcprof.lua")],
                        cwd=work, env=env, timeout=300,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    import pcmap
    prof = pcmap.profile(pcmap.parse_samples(io.open(os.path.join(work, "pcprof.log"),
                                                     encoding="utf-8").read()),
                         {("f7", facts["sym"][k]): k for k in ("reset", "wait_low", "wait_high", "synth_tick")})
    # the synthetic cartridge spends the visible frame waiting for vertical blank
    assert prof[0][0] in ("wait_high", "wait_low") and prof[0][3] > 0.9 * sum(r[3] for r in prof), prof[:3]
    # hangsnap and rates, against what the cartridge is known to do: it waits for
    # vertical blank, adds one to $81 once a frame, reads the stick every other frame
    # and the button every frame, and takes one display-list interrupt a frame
    wh = facts["sym"]["wait_high"]
    subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "5", "-autoboot_script",
                            os.path.join(ROOT, "probes", "hangsnap.lua")],
                   cwd=work, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   env=dict(env, A7800_HANG_AT="100 200", A7800_HANG_PEEK="81,92"))
    hs = io.open(os.path.join(work, "hangsnap.log"), encoding="utf-8").read().splitlines()
    snap = [l for l in hs if l.startswith("f")]
    assert len(snap) == 2 and snap[0].startswith("f100 pc="), hs
    for l in snap:
        pc = int(re.search(r"pc=([0-9A-F]{4})", l).group(1), 16)
        assert wh - 4 <= pc <= wh + 3, (l, hex(wh))          # in the wait loop
        assert "sp=FF" in l and "sp=1FF" not in l, l
    assert 95 <= int(re.search(r"nmis=(\d+)", snap[1]).group(1)) <= 105, snap[1]
    inc = wh + 8                                    # INC FRAME, one a frame
    subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "5", "-autoboot_script",
                            os.path.join(ROOT, "probes", "rates.lua")],
                   cwd=work, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   env=dict(env, A7800_RATE_SITES="%04X" % inc, A7800_RATE_PORTS="0280,000C",
                            A7800_RATE_FROM="60", A7800_RATE_TO="260"))
    rt = io.open(os.path.join(work, "rates.log"), encoding="utf-8").read()
    m = re.search(r"site %04X: (\d+) runs" % inc, rt)
    assert m and 199 <= int(m.group(1)) <= 203, rt
    assert re.search(r"port 0280: .*mean 2\.00 min 2 max 2", rt), rt
    assert re.search(r"port 000C: .*mean 1\.00 min 1 max 1", rt), rt
    subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "5", "-autoboot_script",
                            os.path.join(ROOT, "probes", "cyclebudget.lua")],
                   cwd=work, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   env=dict(env, A7800_CB_FROM="100", A7800_CB_END="160",
                            A7800_CB_BLOCK="20", A7800_CB_RANGES="all=C000-FFFF"))
    cb = io.open(os.path.join(work, "cyclebudget.log"), encoding="utf-8").read().splitlines()
    assert len(cb) == 3, cb
    m = re.match(r"f\d+ executed (\d+) nmi (\d+) slow (\d+) dma (\d+) all (\d+)", cb[1])
    ex, nm, sl, dm, al = [int(x) for x in m.groups()]
    assert abs(ex + sl + dm - 29850.5) < 2 and 0 < dm < 0.2 * 29850.5, cb[1]  # the rest is MARIA's
    # the frame length itself: with MARIA never turned on, the CPU gets all of it.
    # 263 lines x 113.5 cycles = 29,850.5 (the older 262 x 114 = 29,868 was wrong)
    import mktone
    tone = os.path.join(work, "idle.a78")
    mktone.build_tia(tone, 4, 5)
    subprocess.run([tone if a == srom else a for a in sbase] +
                   ["-nothrottle", "-seconds_to_run", "5", "-autoboot_script",
                    os.path.join(ROOT, "probes", "cyclebudget.lua")],
                   cwd=work, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   env=dict(env, A7800_CB_FROM="60", A7800_CB_END="160", A7800_CB_BLOCK="50"))
    idle = io.open(os.path.join(work, "cyclebudget.log"), encoding="utf-8").read().splitlines()
    got = int(re.match(r"f\d+ executed (\d+)", idle[-1]).group(1))
    assert 29849 <= got <= 29852, idle
    assert 10 < nm < 200 and abs(al - ex) < 50, cb[1]   # one short NMI a frame; all code is up here
    subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "10", "-autoboot_script",
                            os.path.join(ROOT, "probes", "addrorigin.lua")],
                   cwd=work, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   env=dict(env, A7800_AO_END="300"))
    import cart
    import origins
    uses, imms = origins.parse_log(io.open(os.path.join(work, "addrorigin.log"),
                                           encoding="utf-8").read())
    found = origins.analyse(uses, imms, cart.Cart(os.path.join(ROOT, "tests", "carts",
                                                               "synth128.a78")))
    # the RAM vector is filled by LDA #<handler_a / LDA #>handler_a: both halves are
    # immediates, and the operand bytes in the ROM are the handler's address
    sc = cart.Cart(os.path.join(ROOT, "tests", "carts", "synth128.a78"))
    ha = facts["sym"]["handler_a"]
    byte = lambda loc: sc.byte(*origins.split_loc(loc))             # noqa: E731
    got = sorted(byte(loc) for loc, k in found["immediates"] if k == "jmpind")
    assert got == sorted([ha & 0xFF, ha >> 8]), (found["immediates"], hex(ha))
    # ... and the hand-pushed return address: PHA / PHA / RTS built from two immediates
    rt = facts["sym"]["rts_target"] - 1
    got = sorted(byte(loc) for loc, k in found["immediates"] if k == "rts")
    assert got == sorted([rt & 0xFF, rt >> 8]), (found["immediates"], hex(rt))
    assert sorted(u[0] for u in uses) == ["jmpind", "rts"], uses
    subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "5", "-autoboot_script",
                            os.path.join(ROOT, "probes", "dlitimes.lua")],
                   cwd=work, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   env=dict(env, A7800_DT_FRAME="100", A7800_DT_FROM="100", A7800_DT_END="110"))
    dt = io.open(os.path.join(work, "dlitimes.log"), encoding="utf-8").read()
    zones = [(int(m.group(1)), int(m.group(2)), int(m.group(3))) for m in
             re.finditer(r"zone +\d+ +line +(\d+) +height +(\d+) +dli (\d)", dt)]
    flagged = [first for first, _h, dli in zones if dli]
    nm = [float(x) for x in re.findall(r"nmi +frame \d+ +line ([\d.]+)", dt)]
    vbe = [float(x) for x in re.findall(r"vblank begins +line ([\d.]+)", dt)]
    # MARIA's zone 0 is raster 16: the interrupt arrives as the flagged zone begins,
    # not as it ends; VBLANK rises at raster 258
    assert flagged and nm and all(abs(x - (16 + flagged[0])) < 1.0 for x in nm), (flagged, nm[:3])
    assert vbe and all(abs(x - 258) < 0.5 for x in vbe), vbe[:3]
    sp = subprocess.run(sbase + ["-nothrottle", "-seconds_to_run", "3",
                                 "-autoboot_script",
                                 os.path.join(ROOT, "probes", "audio.lua")],
                        cwd=work, env=dict(env, A7800_POKEY="0x4000"),
                        timeout=180, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    rows = [l.split() for l in io.open(os.path.join(work, "a7800-audio.log"),
                                       encoding="utf-8") if l[0].isdigit()]
    heard = {int(r[1], 16) for r in rows if int(r[2], 16) == facts["tune_c"]}
    assert heard and heard <= set(facts["tune_f"]), heard
    # the whole first look, live, on the banked cartridge
    import firstlook
    fl = os.path.join(work, "firstlook")
    assert firstlook.main([srom, "-o", fl, "--seconds", "8", "--code-seconds", "4",
                           "--graphics-at", "120", "--engine", "mame"]) == 0
    text = io.open(os.path.join(fl, "report.md"), encoding="utf-8").read()
    ff = json.load(io.open(os.path.join(fl, "firstlook.json"), encoding="utf-8"))
    assert ff["music"]["changes"] > 5, ff.get("music")
    assert os.path.getsize(os.path.join(fl, "music", "song.wav")) > 1000
    assert ff["graphics"]["direct_objects"] == 1, ff["graphics"]
    assert ff["graphics"]["formats"] == ["160A"], ff["graphics"]
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image:
        # the synthetic cartridge repaints BACKGRND from its display-list interrupt
        # on zone 5: black above, $92 below. A reconstruction that cannot tell
        # zones apart would paint one colour over the lot.
        import palette
        scr = Image.open(os.path.join(fl, "graphics", "screen.png")).convert("RGB")
        assert scr.getpixel((2, 4)) == palette.mame7800(0x00), scr.getpixel((2, 4))
        assert scr.getpixel((2, scr.height - 4)) == palette.mame7800(0x92), \
            scr.getpixel((2, scr.height - 4))
    assert ff["sprite_refs"] == 1, ff["sprite_refs"]
    assert ff["screens"], "no screenshots"
    ann = json.load(io.open(os.path.join(fl, "annotations.json"), encoding="utf-8"))
    assert "f7:%04X" % facts["handler_a"] in ann["entries"], ann["entries"]
    assert ann["banksw"]["f7:%04X" % facts["computed_switch"]] == facts["executed_banks"]
    for want in ("What it sounds like", "The artwork on screen", "The code that ran"):
        assert want in text, want
    shutil.rmtree(work, True)
    note = ""
    if ver is not None and abs(ver - MAME_MEASURED) > 1e-9:
        note = " (WARNING: MAME %s; these notes were measured on %s)" % (
            _fmt_ver(ver), _fmt_ver(MAME_MEASURED))
    return ("reclength, liveslots, ramsnap, freeram, pcwrites, inputreaders, "
            "exectrace, pcprof, hangsnap, rates, cyclebudget, addrorigin, dlitimes, a recording, the banked cart's facts, and a whole first look, under MAME"
            + note)


# ------------------------------------------------------------ with a cartridge

def _pull(rom, fmt):
    import cart as cart_module
    import songfmt
    cart = cart_module.Cart(rom)
    f = json.load(io.open(fmt, encoding="utf-8"))
    return cart, f, songfmt.pull(cart, f)


def t_pull_push(rom, fmt):
    """Pull then push unedited must give back the same bytes."""
    if not (rom and fmt):
        return None
    import songfmt
    cart, f, songs = _pull(rom, fmt)
    writes = songfmt.push(cart, f, songs)
    raw = bytearray(io.open(rom, "rb").read())
    out = songfmt.apply_writes(raw, cart, writes)
    original = io.open(rom, "rb").read()
    if bytes(out) != original:
        n = sum(1 for a, b in zip(out, original) if a != b)
        raise AssertionError("a no-op push changed %d bytes" % n)
    return "%d patterns, no-op push changes 0 bytes" % len(songs["patterns"])


def t_grow(rom, fmt):
    """A pattern that grew must be refused, not written."""
    if not (rom and fmt):
        return None
    import songfmt
    cart, f, songs = _pull(rom, fmt)
    keys = sorted(songs["patterns"])
    if not keys:
        return None
    pat = songs["patterns"][keys[0]]
    pat["notes"].append(dict(pat["notes"][0]))
    try:
        songfmt.push(cart, f, songs)
    except songfmt.FormatError as e:
        if "cannot grow" not in str(e):
            raise AssertionError("refused, but for the wrong reason: %s" % e)
        return "growing a pattern is refused"
    raise AssertionError("growing a pattern was allowed")


def t_verify(rom, fmt, log, song_n, region):
    """The renderer must reproduce what the hardware actually played."""
    if not (rom and fmt and log):
        return None
    import songfmt
    import tracker
    _cart, _f, songs = _pull(rom, fmt)
    tracks, _ctl = songfmt.render(songs, song_n)
    played = list(tracker.read_capture(log, region).states())
    best = (0, -1, 0)
    for ch, ours in enumerate(tracks):
        for off in range(max(1, len(played) - 50)):
            n = min(len(ours), len(played) - off)
            if n < 100:
                break
            same = sum(1 for i in range(n) if ours[i] == played[off + i][ch])
            if same > best[1]:
                best = (off, same, n)
    off, same, n = best
    if not n:
        return None
    pct = 100.0 * same / n
    if pct < 100.0:
        raise AssertionError("song %d matches on only %d of %d frames (%.1f%%) "
                             "at offset %d" % (song_n, same, n, pct, off))
    return "song %d: %d of %d frames identical to hardware" % (song_n, same, n)


def _unterminated_strings(js):
    """Lines where a JS string literal opens and the line ends still inside it.

    Single- and double-quoted JS strings cannot span lines, so this is exact
    rather than heuristic: reaching a newline inside one is always a syntax
    error. Template literals may legitimately span lines and are skipped, as
    are both kinds of comment -- an apostrophe in a comment is fine, the same
    apostrophe in `'the player's own format'` is not.
    """
    bad, i, line, instr, start = [], 0, 1, None, None
    while i < len(js):
        ch = js[i]
        nxt = js[i + 1] if i + 1 < len(js) else ""
        if ch == "\n":
            line += 1
            if instr in ("'", '"'):
                bad.append((start, instr))
                instr = None
        if instr:
            if ch == "\\":
                i += 2
                continue
            if ch == instr:
                instr = None
        elif ch in "'\"`":
            instr, start = ch, line
        elif ch == "/" and nxt == "/":
            while i < len(js) and js[i] != "\n":
                i += 1
            continue
        elif ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(js) and not (js[i] == "*" and js[i + 1] == "/"):
                if js[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        i += 1
    return bad


def t_handler_attributes():
    """No page may build a double-quoted HTML attribute out of JSON.

    `JSON.stringify("b6:A951")` returns `"b6:A951"` *including the quotes*, so
    concatenating it into `onclick="handler(...)"` closes the attribute early:

        <button onclick="openExplore("b6:A951")">

    which the browser reads as `onclick="openExplore("`. The button renders,
    looks right, and does nothing when clicked. Nothing else notices -- the
    script parses, the server is healthy, and a test that drives the API over
    HTTP never renders the button at all. That is how this shipped.

    The invariant is exact rather than stylistic: JSON output always contains
    double quotes, so it can never appear inside a double-quoted attribute. It
    is perfectly legal inside a single-quoted one, which is how the format
    explorer passes whole states to its handlers, so only the double-quoted
    case is an error.

    This does not check that the page works -- there is no JavaScript engine
    here to run it. It checks one specific way of breaking a page that no
    server-side test can see.
    """
    import glob
    import re
    checked = 0
    # an on*="  attribute, then anything up to the quote that closes it
    pattern = re.compile(r"""on[a-z]+="[^"]*?\+\s*JSON\.stringify\(""", re.I)
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        if os.path.basename(path) == os.path.basename(__file__):
            continue        # this file quotes the tags it searches for
        src = io.open(path, encoding="utf-8").read()
        for m in re.finditer(r"<script>(.*?)</script>", src, re.S):
            checked += 1
            hit = pattern.search(m.group(1))
            if hit:
                line = src[:m.start(1) + hit.start()].count("\n") + 1
                raise AssertionError(
                    "%s line %d builds a double-quoted HTML attribute with "
                    "JSON.stringify, whose output contains double quotes. The "
                    "attribute ends early and the handler never runs. Use "
                    "&quot; around an escaped value, or single-quote the "
                    "attribute." % (os.path.basename(path), line))
    if not checked:
        raise AssertionError("found no embedded <script> to check")
    return "%d scripts, no attribute built from quote-bearing JSON" % checked


def t_direct_format():
    """A reading saved by explore.py reads back as the same notes.

    This is the join that makes the explorer worth using rather than merely
    interesting: work out what the bytes mean by ear, press save, and the
    tracker opens them. If the emitted file and the reader disagree about what
    a field means, the saved finding is quietly wrong and nobody notices until
    it is played back weeks later.

    Both samples are the real bytes whose layout is already established, so the
    expected notes are known independently of either side of this round trip.
    """
    import songfmt
    import explore

    PARALLEL = bytes.fromhex("00EA0003C0B2A5B2A5B20014141414140A0A0A0A0A00")
    SERIAL = bytes.fromhex(
        "093D09370933093A09340931"
        "093D09370933093A09340931"
        "093809330930093A0933092F"
        "093D09370933042E")

    class FakeCart(object):
        """Enough cartridge for the reader: a slice at an address."""

        def __init__(self, blob, base):
            self.blob, self.base, self.rom = blob, base, blob
            self.info = {"title": "test"}

        def slice(self, _space, addr, n):
            i = addr - self.base
            if i < 0 or i + n > len(self.blob):
                raise IndexError("past the sample")
            return self.blob[i:i + n]

    # --- parallel: two streams, a zero duration ends the part ---------------
    cart = FakeCart(PARALLEL, 0xC333)
    fmt = {"name": "test", "reader": "direct", "chip": "pokey",
           "guessed": True,
           "voice": {"shape": "parallel", "pitch_at": "rom:C333",
                     "dur_at": "rom:C33E", "count": 32},
           "playback": {"audc": 12, "volume": 8, "rate": 60.0}}
    doc = songfmt.pull(cart, fmt)
    notes = doc["voices"][0]["notes"]
    want_p = [0x00, 0xEA, 0x00, 0x03, 0xC0, 0xB2, 0xA5, 0xB2, 0xA5, 0xB2]
    want_d = [0x14] * 5 + [0x0A] * 5
    if [n["pitch"] for n in notes] != want_p:
        raise AssertionError("parallel pitches read back as %r, expected %r"
                             % ([n["pitch"] for n in notes], want_p))
    if [n["duration"] for n in notes] != want_d:
        raise AssertionError("parallel durations read back as %r"
                             % [n["duration"] for n in notes])
    frames = songfmt.render_direct(doc)[0]
    if len(frames) != sum(want_d):
        raise AssertionError("rendered %d frames for %d frames of notes"
                             % (len(frames), sum(want_d)))

    # --- serial: fixed-size records, pitch in the second byte --------------
    cart = FakeCart(SERIAL, 0x7D23)
    fmt = {"name": "test", "reader": "direct", "chip": "tia", "guessed": True,
           "voice": {"shape": "serial", "at": "f6:7D23", "count": 22,
                     "stride": 2, "pitch": [1, 0, 8], "duration": [0, 0, 4]},
           "playback": {"audc": 12, "volume": 8, "rate": 60.0}}
    doc = songfmt.pull(cart, fmt)
    notes = doc["voices"][0]["notes"]
    if len(notes) != 22:
        raise AssertionError("expected 22 notes, got %d" % len(notes))
    if [n["pitch"] for n in notes[:6]] != [0x3D, 0x37, 0x33, 0x3A, 0x34, 0x31]:
        raise AssertionError("serial pitches read back as %r"
                             % [n["pitch"] for n in notes[:6]])
    if notes[0]["duration"] != 9:
        raise AssertionError("duration field read as %d, expected 9"
                             % notes[0]["duration"])

    # --- what explore writes is what the reader expects --------------------
    saved = explore.CART, explore.ROM
    try:
        explore.CART = cart
        explore.ROM = os.path.join(tempfile.gettempdir(), "sample.a78")
        st = explore.default_state("f6", 0x7D23)
        st.update({"shape": "serial", "stride": 2, "pitch_byte": 1,
                   "pitch_shift": 0, "pitch_bits": 8, "dur_byte": 0,
                   "dur_shift": 0, "dur_bits": 4, "count": 22, "chip": "tia"})
        emitted = explore.emit_format(st)
        back = songfmt.pull(cart, emitted)["voices"][0]["notes"]
        if [n["pitch"] for n in back] != [n["pitch"] for n in notes]:
            raise AssertionError(
                "explore.py emits a format that reads back different notes "
                "than the reading it was written from")
        if not emitted.get("guessed"):
            raise AssertionError("an emitted reading must be marked guessed")
    finally:
        explore.CART, explore.ROM = saved

    return ("parallel and serial readings round-trip through the saved "
            "format, and what explore writes is what songfmt reads")


def t_browser_js():
    """Every browser tool's embedded script must actually parse.

    These tools serve a page whose JavaScript is written inline in a Python
    string, and one stray apostrophe -- `'the player's own format'` -- closes
    the string early and makes the whole script a syntax error. The server
    stays perfectly healthy, answering every request with correct JSON, while
    the page renders blank. That is precisely why no server-side test caught
    it: the workbench passed its checks and was blank for every cartridge.

    So this parses what the browser parses. It is not a full JS parser, but
    unterminated string literals are the failure this class of tool actually
    ships, because the page is assembled by concatenating quoted fragments.
    """
    import glob
    import re
    checked = 0
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        if os.path.basename(path) == os.path.basename(__file__):
            continue        # this file quotes the tags it searches for
        src = io.open(path, encoding="utf-8").read()
        for m in re.finditer(r"<script>(.*?)</script>", src, re.S):
            checked += 1
            bad = _unterminated_strings(m.group(1))
            if bad:
                line, quote = bad[0]
                before = src[:m.start(1)].count("\n")
                raise AssertionError(
                    "%s: a %s-quoted string opens on line %d of its script "
                    "(about line %d of the file) and never closes, so the "
                    "page renders blank."
                    % (os.path.basename(path), quote, line, before + line))
    if not checked:
        raise AssertionError("found no embedded <script> to check")
    return "%d embedded scripts, every string literal closed" % checked


def t_explore():
    """The format guesser, against two layouts whose answers are known.

    Both samples are real bytes lifted from cartridges whose players were
    established the hard way and confirmed against `tracker.py capture`: 22
    notes of a Midnight Mutants pattern (serial, two bytes a note, pitch in
    the second) and one Arkanoid part (parallel, pitches then durations, the
    two streams laid end to end so the gap is 11). Carrying the bytes here
    rather than reading a cartridge keeps the package ROM-free and tests the
    algorithm instead of one image.

    The bar is that the true reading comes out *first*, not merely somewhere
    in the list -- the tool exists to save a person from auditioning fifty
    wrong readings, so a truth ranked fourth is a failure.
    """
    import explore

    SERIAL = bytes.fromhex(
        "093D09370933093A09340931"      # the phrase, twice
        "093D09370933093A09340931"
        "093809330930093A0933092F"      # then a variation
        "093D09370933042E")             # and out on a new instrument
    PARALLEL = bytes.fromhex("00EA0003C0B2A5B2A5B20014141414140A0A0A0A0A00")

    class FakeCart(object):
        """Just enough cartridge for the guesser: bytes at an address."""

        def __init__(self, blob, base):
            self.blob, self.base = blob, base

        def byte(self, _space, addr):
            i = addr - self.base
            if 0 <= i < len(self.blob):
                return self.blob[i]
            raise IndexError("past the sample")

    saved = explore.CART
    try:
        explore.CART = FakeCart(SERIAL, 0x7D23)
        stride, _ = explore.record_stride("f6", 0x7D23)
        if stride != 2:
            raise AssertionError("record size of a 2-byte serial format read "
                                 "as %d" % stride)
        moves, holds = explore.varying_column("f6", 0x7D23, 2)
        if (moves, holds) != (1, 0):
            raise AssertionError("the moving column of `09 3D 09 37 ...` is "
                                 "byte 1, not byte %d" % moves)
        top = explore.suggest("f6", 0x7D23, "tia")[0][1]
        if "2 bytes a note" not in top or "pitch in byte 1" not in top:
            raise AssertionError("best reading of the serial sample was %r, "
                                 "expected 2 bytes a note with the pitch in "
                                 "byte 1" % top)

        explore.CART = FakeCart(PARALLEL, 0xC333)
        top = explore.suggest("rom", 0xC333, "pokey")[0][1]
        if "parallel" not in top or "durations 11 bytes" not in top:
            raise AssertionError("best reading of the parallel sample was "
                                 "%r, expected parallel with a gap of 11"
                                 % top)
    finally:
        explore.CART = saved
    return ("both known layouts ranked first: 2-byte serial records with the "
            "pitch in byte 1, and parallel streams 11 apart")



def t_sprites(rom):
    """A painted pixel lands in the right byte, and only in that byte."""
    if not rom:
        return None
    import spriteedit as SE
    import cart as cart_module
    c = cart_module.Cart(rom)
    space = c.spaces()[0]
    SE.CART, SE.PATH = c, rom
    SE.DATA = bytearray(io.open(rom, "rb").read())
    SE.REGION = SE.Region(c, space, c.base_of(space), 1, 8, 256, 256, "160")

    # every offset the region computes must agree with the cart's own reader
    for a in range(c.base_of(space), c.base_of(space) + c.size_of(space), 811):
        if SE.DATA[SE.REGION.file_offset(a)] != c.byte(space, a):
            raise AssertionError("file offset for $%04X disagrees with the "
                                 "cartridge reader" % a)

    # MARIA counts a zone offset down, so line 0 is the HIGHEST page. Getting
    # this backwards renders every sprite upside down and still looks like a
    # sprite, which is why it went unnoticed until someone eyeballed a sheet.
    r = SE.REGION
    top = r.addr(0, 0, 0)
    bottom = r.addr(0, r.height - 1, 0)
    if top <= bottom:
        raise AssertionError(
            "line 0 is at $%04X and the last line at $%04X -- the zone offset "
            "must count down" % (top, bottom))
    if top != r.base + (r.height - 1) * r.stride:
        raise AssertionError("line 0 is not at base + (height-1)*stride")
    flat = SE.Region(c, space, c.base_of(space), 1, 8, 256, 256, "160",
                     descending=False)
    if flat.addr(0, 0, 0) != flat.base:
        raise AssertionError("--ascending did not restore the flat order")

    # palettes: greys by default, a chosen palette applies, and it goes back
    if SE.PALETTE != SE.GREYS:
        raise AssertionError("the default palette is not the greys")
    SE.set_palette([None, 0x24, 0x76, 0x7C])
    if SE.PALETTE == SE.GREYS or SE.PALETTE[0] != SE.GREYS[0]:
        raise AssertionError("a chosen palette did not apply, or index 0 moved")
    SE.set_palette([None, None, None, None])
    if SE.PALETTE != SE.GREYS:
        raise AssertionError("clearing the palette did not restore the greys")

    before = SE.REGION.pixels(70)
    want = 3 if before[0][0] != 3 else 1
    SE.REGION.set_pixel(70, 0, 0, want)
    if SE.REGION.pixels(70)[0][0] != want:
        raise AssertionError("a painted pixel did not read back")


    out = os.path.join(tempfile.mkdtemp(prefix="selftest-spr-"), "out.a78")
    r = SE.save(out)
    if r["changed"] != 1:
        raise AssertionError("one pixel changed %d bytes" % r["changed"])

    # the whole-cell path every drawing tool uses
    wide = SE.REGION.width * SE.REGION.ppb
    grid = [[(x + y) % (1 << SE.REGION.bpp) for x in range(wide)]
            for y in range(SE.REGION.height)]
    if SE.REGION.set_cell(71, grid) != grid:
        raise AssertionError("a whole-cell write did not read back")
    for bad, why in (([[0] * wide], "too few lines"),
                     ([[0] * (wide + 1)] * SE.REGION.height, "wrong width")):
        try:
            SE.REGION.set_cell(71, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("a cell with the %s was accepted" % why)

    # and a byte outside the region must stop the save
    SE.DATA[9] ^= 0xFF
    blocked = os.path.join(os.path.dirname(out), "blocked.a78")
    try:
        SE.save(blocked)
    except ValueError:
        pass
    else:
        raise AssertionError("a write outside the region was allowed")
    if os.path.exists(blocked):
        raise AssertionError("the refused save still wrote a file")
    return ("zone offset counts down; palette applies and clears; pixel "
            "and whole-cell writes land correctly; bad shapes and "
            "out-of-region writes refused")


def t_workbench(rom):
    """The launcher describes a cartridge, scans it, and cleans up after itself."""
    if not rom:
        return None
    import cart as cart_module
    import workbench as WB
    WB.ROM = os.path.abspath(rom)
    WB.CART = cart_module.Cart(WB.ROM)
    info = WB.cart_info()
    if not info["spaces"] or not info["vectors"]:
        raise AssertionError("the cartridge summary came out empty")
    m = WB.scan()
    if "graphics" not in m or "audio" not in m:
        raise AssertionError("the scan returned nothing usable")

    # A launched editor must actually serve, and must actually stop. A child
    # left holding its port looks exactly like a stale server on the next run.
    import socket
    import time
    r = WB.launch("spriteedit.py",
                  ["--space", info["spaces"][0]["name"],
                   "--base", str(info["spaces"][0]["start"])], "selftest")
    port = r["port"]

    def up():
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    for _ in range(40):
        if up():
            break
        time.sleep(0.15)
    else:
        WB.stop_all()
        raise AssertionError("a launched editor never came up on %d" % port)
    WB.stop_all()
    for _ in range(30):
        if not up():
            break
        time.sleep(0.15)
    else:
        raise AssertionError("a launched editor was still holding port %d "
                             "after stop_all" % port)
    return ("summarises, scans (%d graphics, %d audio), launches and stops "
            "cleanly" % (len(m["graphics"]), len(m["audio"])))


def t_songs_from_rom(rom, fmt):
    """A cartridge's songs read out of the ROM, with no emulator involved.

    `capture` records whatever a game happens to play in the window you gave
    it. This reads what it *can* play -- every song, in a moment rather than a
    minute -- and it is the route that should be tried first whenever a format
    file describes the cartridge.
    """
    if not (rom and fmt):
        return None
    import trackeredit as TE

    found = TE.find_format(rom)
    if not found:
        raise AssertionError(
            "no shipped format matched this cartridge, so the tracker would "
            "fall back to recording it. Check the `match` block in %s."
            % os.path.basename(fmt))

    songs, pulled = TE.songs_from_rom(rom, found)
    usable = [s for s in songs if s.get("rows")]
    if not usable:
        raise AssertionError("reading the ROM produced no playable songs")
    if not any(s["sounding"] for s in usable):
        raise AssertionError("every song read out of the ROM is silent")

    # the match block has to actually describe this cartridge
    import json
    import audiotrace
    import cart as cart_module
    doc = json.load(io.open(found, encoding="utf-8"))
    m = doc.get("match") or {}
    if not m:
        raise AssertionError("%s has no match block, so nothing would find it"
                             % os.path.basename(found))
    c = cart_module.Cart(rom)
    if "player" in m:
        sig = audiotrace.player_signature(c.rom)
        if sig != m["player"]:
            raise AssertionError("the match block's player fingerprint (%s) is "
                                 "not this ROM's (%s)" % (m["player"], sig))
    if "size" in m and int(m["size"]) != len(c.rom):
        raise AssertionError("the match block's size (%d) is not this ROM's (%d)"
                             % (int(m["size"]), len(c.rom)))
    # and it must not match something unrelated
    sig = audiotrace.player_signature(c.rom)
    if sig and audiotrace.player_signature(bytes(len(c.rom))) == sig:
        raise AssertionError("the fingerprint is not discriminating")
    return ("%d songs read from the ROM, %d with sound, no emulator"
            % (len(usable), sum(1 for s in usable if s["sounding"])))


def t_formats():
    """Every shipped format file is well formed and says what it matches."""
    import glob as _glob
    import json
    seen = {}
    for path in sorted(_glob.glob(os.path.join(ROOT, "formats", "*.json"))):
        doc = json.load(io.open(path, encoding="utf-8"))
        name = os.path.basename(path)
        m = doc.get("match")
        if not m:
            raise AssertionError("%s has no match block, so nothing finds it"
                                 % name)
        key = m.get("player") or (("magic:" + m["magic"]) if "magic" in m
                                  else None)
        if not key:
            raise AssertionError("%s matches on title/size only. A format file "
                                 "describes an engine or a container; use "
                                 "audiotrace.py --signature, or `magic` for "
                                 "something that identifies itself." % name)
        if key in seen:
            raise AssertionError("%s and %s both claim %s"
                                 % (name, seen[key], key))
        seen[key] = name
        reader = doc.get("reader", "nested")
        if reader not in ("nested", "parallel", "rmt"):
            raise AssertionError("%s wants reader %r, which songfmt does not "
                                 "implement" % (name, reader))
    return "%d format files, each fingerprinted and unique" % len(seen)


def t_disasm(rom):
    if not rom:
        return None
    out = tempfile.mkdtemp(prefix="selftest-")
    p = subprocess.run([sys.executable, os.path.join(HERE, "disasm.py"), rom,
                        "-o", out], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise AssertionError("disasm exited %d" % p.returncode)
    listings = glob.glob(os.path.join(out, "*.asm"))
    if not listings:
        raise AssertionError("no listings written")
    return "%d listings written" % len(listings)


# ----------------------------------------------------------------- merged in
# These seven come from the tree this toolkit was forked into for
# the Karateka work. They cover the tools that arrived with them
# -- a8dis, portscan, forth, patchset, portkit -- plus the two
# guarantees those tools make: that a conversion recipe carries
# coordinates and not content, and that a published patch carries
# none of the cartridge it patches.


def t_engine_finder():
    """The engine hunter, against a cartridge built to have exactly one.

    `audiotrace.py --engine` searches a cartridge for the Atari in-house music
    engine by the shape of its tables. A search like that is only worth having
    if a wrong answer is impossible rather than merely unlikely, so this builds
    an image whose engine is at known addresses and checks all four are found.

    Synthetic rather than a real cartridge, so the package stays ROM-free and
    the test states the format instead of assuming a particular game.
    """
    import audiotrace

    DUR = [0x60, 0x48, 0x40, 0x30, 0x24, 0x20, 0x18, 0x12,
           0x10, 0x0C, 0x09, 0x08, 0x06, 0x04, 0x03, 0x02]
    BASE, SIZE = 0x4000, 0x1000
    DURS, INSTR = 0x4100, 0x4110
    PAT_A, PAT_B = 0x4300, 0x4320
    TRK_1, TRK_2 = 0x4400, 0x4410
    SONGS = 0x4500

    img = bytearray(SIZE)

    def put(addr, data):
        img[addr - BASE:addr - BASE + len(data)] = bytes(data)

    def word(addr, value):
        put(addr, [value & 0xFF, value >> 8])

    put(DURS, DUR)
    for k in range(16):
        # ten bytes used, six of padding -- the shape the search keys on
        put(INSTR + k * 16, [0xA0, 0x01, 0x80, 0x80, 0x05,
                             0x02, 0x13, 0x01, 0x28, k + 1] + [0] * 6)
    # Fence the table with words that cannot resolve, so its start and end are
    # unambiguous. Without this the run can begin a couple of bytes early on
    # padding, and -- because every song here has the same two pointers -- a
    # four-voice reading of the same bytes validates just as well as the real
    # two-voice one and outscores it. Real tables are not that uniform; a test
    # image has to be fenced deliberately to stand in for that.
    for a in range(0x44F0, 0x4500):
        img[a - BASE] = 0xFF
    put(PAT_A, [4] + [0x16, 0x60, 0x1B, 0x48, 0x1B, 0x48, 0x13, 0x55])
    put(PAT_B, [3] + [0x1C, 0x23, 0x1C, 0x2A, 0x13, 0x90])
    word(TRK_1, PAT_A)
    word(TRK_1 + 2, PAT_B)
    word(TRK_1 + 4, 0x0009)          # high byte zero ends the list
    word(TRK_2, PAT_B)
    word(TRK_2 + 2, 0x0000)
    for n in range(6):               # six songs, two voices, stride 4
        word(SONGS + n * 4, TRK_1)
        word(SONGS + n * 4 + 2, TRK_2)
    # Six two-voice songs is 24 bytes, which is three four-voice entries and a
    # remainder -- so a four-voice reading runs into the fence after three and
    # falls short of the minimum, while the two-voice reading gets all six.
    for a in range(SONGS + 24, SONGS + 40):
        img[a - BASE] = 0xFF

    class FakeCart(object):
        """One fixed space, which is all the search needs."""

        def __init__(self, blob):
            self.rom = bytes(blob)
            self.info = {"title": "synthetic"}

        def spaces(self):
            return ["f0"]

        def base_of(self, _sp):
            return BASE

        def size_of(self, _sp):
            return SIZE

        def pokeys(self):
            return []

        def byte(self, _sp, addr):
            i = addr - BASE
            if not 0 <= i < SIZE:
                raise IndexError("outside the image")
            return self.rom[i]

        def slice(self, _sp, addr, n):
            i = addr - BASE
            if i < 0 or i + n > SIZE:
                raise IndexError("outside the image")
            return self.rom[i:i + n]

        def space_of(self, addr, _bank=None):
            return "f0" if BASE <= addr < BASE + SIZE else None

    cart = FakeCart(img)
    found = audiotrace.find_engine(cart)
    if not found:
        raise AssertionError("the engine hunter found nothing in an image "
                             "built to contain exactly one")
    for name, want, got in (("instruments", INSTR, found["instruments"]),
                            ("durations", DURS, found["durations"]),
                            ("song table", SONGS, found["songs"])):
        if got != want:
            raise AssertionError("%s found at $%04X, expected $%04X"
                                 % (name, got, want))
    if found["voices"] != 2 or found["stride"] != 4:
        raise AssertionError("read %d voices at stride %d, expected 2 at 4"
                             % (found["voices"], found["stride"]))
    if found["verified"] < 6:
        raise AssertionError("only %d of 6 songs followed down to patterns"
                             % found["verified"])
    if found["duration_values"] != DUR:
        raise AssertionError("duration table read back wrong")

    # ...and the format it writes must describe the same thing
    doc = audiotrace.engine_format(cart, "synthetic.a78", found)
    import songfmt
    pulled = songfmt.pull(cart, doc)
    if len(pulled["songs"]) != 6:
        raise AssertionError("the emitted format pulls %d songs, not 6"
                             % len(pulled["songs"]))
    if set(pulled["patterns"]) != {"f0:%04X" % PAT_A, "f0:%04X" % PAT_B}:
        raise AssertionError("the emitted format resolves the wrong patterns: "
                             "%s" % sorted(pulled["patterns"]))
    return ("engine located at its known addresses, and the format it emits "
            "pulls the same songs back")


def t_forth_decompiler():
    """The Forth decompiler, against an image built to be one.

    `forth.py` finds a threaded image's interpreter by shape -- the inner loop
    every primitive jumps to, the routine that saves the thread pointer, the
    one that restores it, the word that eats the following cell -- and then
    walks the thread. All four have to be right together: get the literal
    wrong and every number in the program decompiles as a call to whatever
    address it happens to equal, which reads perfectly and means nothing.

    So this assembles a small indirect-threaded image with a known kernel and
    two known definitions, and checks the tool recovers all of it. Synthetic,
    so the package stays ROM-free and the test states the format rather than
    depending on one cartridge.
    """
    import forth

    BASE, SIZE = 0x4000, 0x1000
    IP = 0xE8
    NEXT = 0x4000
    DOCOL = 0x4030
    BRTAIL = 0x4050          # IP += inline cell
    SKIPTAIL = 0x4060        # IP += 2
    LIT = 0x4070             # word; its code is at LIT+2
    EXIT = 0x4080
    BRANCH = 0x4090
    ADD = 0x40A0             # an ordinary primitive
    DEF1 = 0x4100
    DEF2 = 0x4140

    img = bytearray(b"\xFF" * SIZE)

    def put(addr, data):
        img[addr - BASE:addr - BASE + len(data)] = bytes(data)

    def word(addr, v):
        put(addr, [v & 0xFF, v >> 8])

    # NEXT: LDY #1 / LDA (IP),Y / STA $EC / DEY / LDA (IP),Y / STA $EB
    #       / CLC / LDA IP / ADC #2 / STA IP / BCC / INC IP+1 / JMP ($00EA)
    put(NEXT, [0xA0, 0x01, 0xB1, IP, 0x85, 0xEC, 0x88, 0xB1, IP, 0x85, 0xEB,
               0x18, 0xA5, IP, 0x69, 0x02, 0x85, IP, 0x90, 0x02, 0xE6, IP + 1,
               0x4C, 0xEA, 0x00])
    # DOCOL: LDA IP+1 / PHA / LDA IP / PHA / ... / JMP NEXT
    put(DOCOL, [0xA5, IP + 1, 0x48, 0xA5, IP, 0x48, 0x18, 0xA5, 0xEB,
                0x69, 0x02, 0x85, IP, 0x98, 0x65, 0xEC, 0x85, IP + 1,
                0x4C, NEXT & 0xFF, NEXT >> 8])
    # the two tails that mean "the next cell is data"
    put(BRTAIL, [0x18, 0xB1, IP, 0x65, IP, 0x85, IP,
                 0x4C, NEXT & 0xFF, NEXT >> 8])
    put(SKIPTAIL, [0x18, 0xA5, IP, 0x69, 0x02, 0x85, IP,
                   0x4C, NEXT & 0xFF, NEXT >> 8])
    # LIT: reads through IP and steps it, then NEXT
    word(LIT, LIT + 2)
    put(LIT + 2, [0xB1, IP, 0x48, 0xE6, IP, 0xD0, 0x02, 0xE6, IP + 1,
                  0x4C, NEXT & 0xFF, NEXT >> 8])
    # EXIT: pulls the saved thread pointer back
    word(EXIT, EXIT + 2)
    put(EXIT + 2, [0x68, 0x85, IP, 0x68, 0x85, IP + 1,
                   0x4C, NEXT & 0xFF, NEXT >> 8])
    # BRANCH: jumps straight to the tail that adds the inline cell
    word(BRANCH, BRANCH + 2)
    put(BRANCH + 2, [0x4C, BRTAIL & 0xFF, BRTAIL >> 8])
    # an ordinary primitive, which must NOT be read as taking a cell
    word(ADD, ADD + 2)
    put(ADD + 2, [0xB5, 0x00, 0x75, 0x02, 0x95, 0x02, 0xE8, 0xE8,
                  0x4C, NEXT & 0xFF, NEXT >> 8])

    # : DEF1  LIT 1234  ADD  DEF2  BRANCH <8>  EXIT ;
    word(DEF1, DOCOL)
    for i, cell in enumerate([LIT, 0x1234, ADD, DEF2, BRANCH, 0x0008, EXIT]):
        word(DEF1 + 2 + i * 2, cell)
    # : DEF2  ADD  EXIT ;
    word(DEF2, DOCOL)
    for i, cell in enumerate([ADD, EXIT]):
        word(DEF2 + 2 + i * 2, cell)

    class FakeCart(object):
        def __init__(self, blob):
            self.rom = bytes(blob)
            self.info = {"title": "synthetic forth"}

        def spaces(self):
            return ["rom"]

        def base_of(self, _s):
            return BASE

        def size_of(self, _s):
            return SIZE

        def byte(self, _s, addr):
            i = addr - BASE
            if not 0 <= i < SIZE:
                raise IndexError("outside the image")
            return self.rom[i]

        def slice(self, _s, addr, n):
            i = addr - BASE
            if i < 0 or i + n > SIZE:
                raise IndexError("outside the image")
            return self.rom[i:i + n]

    cart = FakeCart(img)
    im = forth.Image(cart, "rom").discover()

    for name, want, got in (("NEXT", NEXT, im.next), ("DOCOL", DOCOL, im.docol),
                            ("EXIT", EXIT, im.exit), ("literal", LIT, im.lit)):
        if got != want:
            raise AssertionError("%s found at %s, expected $%04X"
                                 % (name, ("$%04X" % got) if got else "nothing",
                                    want))
    if im.ip_pointer() != IP:
        raise AssertionError("thread pointer read as $%02X, expected $%02X"
                             % (im.ip_pointer() or 0, IP))

    # the branch must be seen to eat a cell, and the ordinary word must not
    if not im.is_branch(BRANCH):
        raise AssertionError("the branch was not recognised as taking an "
                             "inline cell, so its destination decompiles as a "
                             "call")
    if im.is_branch(ADD):
        raise AssertionError("an ordinary primitive was read as taking an "
                             "inline cell, which swallows the word after it")

    cells = im.body(DEF1)
    kinds = [(c, k) for _a, c, k in cells]
    want = [(LIT, "word"), (0x1234, "data"), (ADD, "word"), (DEF2, "word"),
            (BRANCH, "word"), (0x0008, "data"), (EXIT, "word")]
    if kinds != want:
        raise AssertionError("the definition decompiled as %r, expected %r"
                             % (kinds, want))

    defs = im.definitions()
    if DEF1 not in defs or DEF2 not in defs:
        raise AssertionError("found definitions %s, expected both $%04X and "
                             "$%04X" % (["$%04X" % d for d in defs], DEF1, DEF2))
    callers = im.xref(defs)
    if callers.get(DEF2) != [DEF1]:
        raise AssertionError("cross-reference says $%04X is named by %s, "
                             "expected [$%04X]"
                             % (DEF2, callers.get(DEF2), DEF1))
    if im.kind_of(DEF1) != "colon" or im.kind_of(ADD) != "code":
        raise AssertionError("a definition and a primitive were not told apart")

    return ("interpreter located by shape, thread walked, literals and branch "
            "destinations kept out of the word stream")


def t_a8dis():
    """The 8-bit cartridge tracer, against a cartridge built to a known answer.

    Two bugs made this worth pinning down, and both were silent rather than
    loud. An implied-mode instruction has no operand, and the walk treated the
    missing operand as running off the end of the window -- so every trace
    stopped at the first INY and reported it as data. And the bank peephole
    (LDA #n / STA $D500) is what lets the tracer follow code into a paged bank
    at all; when it misses, the trace simply stays small and looks honest.

    So the fixture is a cartridge that requires both: it switches banks, then
    runs an implied instruction before touching anything. If either mechanism
    breaks, the reached-byte count collapses and the hardware list empties.

    It also checks that a register is named for what the instruction does to
    it. $D010 written is GRAFP; $D010 read is TRIG0. Reporting a button poll
    as a write to player graphics is the kind of wrong that sends someone
    reading the wrong routine for an afternoon.
    """
    import a8dis

    BANK = a8dis.BANK_SIZE
    rom = bytearray(BANK * 16)

    # bank 3, at $8000 once selected
    prog = bytes([
        0xC8,                    # INY          -- implied; used to end the walk
        0xAD, 0x00, 0xD2,        # LDA $D200    -- POKEY AUDF1 read
        0xAD, 0x10, 0xD0,        # LDA $D010    -- TRIG0 (a read)
        0x8D, 0x10, 0xD0,        # STA $D010    -- GRAFP (a write)
        0x60,                    # RTS
    ])
    rom[3 * BANK:3 * BANK + len(prog)] = prog

    # bank 15 is fixed at $A000-$BFFF; the start vector lands at $B000
    boot = bytes([
        0xA9, 0x03,              # LDA #$03
        0x8D, 0x00, 0xD5,        # STA $D500    -- select bank 3
        0x4C, 0x00, 0x80,        # JMP $8000
    ])
    f = 15 * BANK
    rom[f + 0x1000:f + 0x1000 + len(boot)] = boot
    rom[f + 0x1FE0] = 0x60                       # init: RTS
    rom[f + 0x1FFA:f + 0x2000] = bytes([0x00, 0xB0, 0x00, 0x04, 0xE0, 0xBF])

    cart = a8dis.Cart(b"CART" + b"\x00\x00\x00\x0E" + b"\x00" * 8 + bytes(rom))
    if cart.banks != 16 or cart.fixed != 15:
        raise AssertionError("bank geometry read wrong: %d banks, fixed %d"
                             % (cart.banks, cart.fixed))
    v = cart.vectors()
    if v["start"] != 0xB000 or v["init"] != 0xBFE0:
        raise AssertionError("cartridge footer misread: start $%04X init $%04X"
                             % (v["start"], v["init"]))

    t = a8dis.walk(cart, [(0, v["start"], "start"), (0, v["init"], "init")])

    if not any(s["to"] == 3 for s in t.switches):
        raise AssertionError(
            "the bank switch was not read back; without it the tracer cannot "
            "follow code into a paged bank at all")
    # every instruction of the fixture, at the address it was placed at. The
    # bank-3 byte count would also cover the JMP that follows the switch, so
    # the addresses are checked rather than the total.
    want = [0x8000, 0x8001, 0x8004, 0x8007, 0x800A]
    missing = [a for a in want if (3, a) not in t.code]
    if missing:
        raise AssertionError(
            "never reached %s -- the walk stopped early, which is what the "
            "implied-operand bug looked like"
            % " ".join("$%04X" % a for a in missing))
    if t.bailed:
        raise AssertionError("the walk bailed on %s, and every instruction in "
                             "the fixture is legal" % (t.bailed,))

    names = {(h["op"], a8dis.reg_name(h["name"], h["write"])) for h in t.hw}
    if ("LDA", "TRIG0") not in names:
        raise AssertionError("a read of $D010 was not named TRIG0: %s" % names)
    if ("STA", "GRAFM/GRAFP") not in names:
        raise AssertionError("a write to $D010 was not named GRAFP: %s" % names)

    return ("bank switch followed, implied instructions do not end the walk, "
            "%d hardware accesses named by direction" % len(t.hw))


def t_asm_names():
    """A name defined twice is refused, unless it is the same value again.

    The assembler used to take a second definition silently and resolve every
    reference to it. In Pole Position II a new routine label collided with a
    text table of the same name, and a branch went 322 bytes astray with no
    error. The same value twice is allowed: disasm.py used to write every
    named byte block's label twice, and listings made then still exist.
    """
    import asm

    def refused(src, why):
        try:
            asm.Assembler().assemble(src)
        except asm.AsmError as e:
            if "already defined" not in str(e):
                raise AssertionError("%s: refused for the wrong reason: %s"
                                     % (why, e))
            return
        raise AssertionError("%s was accepted" % why)

    refused([".org $F000", "Loop:", "  NOP", "Loop:", "  BNE Loop"],
            "a label defined at two addresses")
    refused(["X = $10", ".org $F000", "X:", "  NOP"],
            "a name that is an equate and a label")
    refused(["X = $10", "X = $11", ".org $F000", "  LDA X"],
            "an equate given two values")
    a = asm.Assembler()
    if bytes(a.assemble([".org $F000", "Here:", "Here:", "  NOP"])) != b"\xea":
        raise AssertionError("a label repeated at one address changed the code")
    a = asm.Assembler()
    a.sym["Loop"] = 0x1234                   # carried in by the caller
    if bytes(a.assemble([".org $F000", "Loop:", "  BNE Loop"])) != b"\xd0\xfe":
        raise AssertionError("a seeded symbol could not be defined by the source")
    return ("a label at two addresses, an equate and label sharing a name, and "
            "an equate with two values are refused; the same value twice and a "
            "seeded symbol are allowed")


def t_bundle_reproducible():
    """The same manifest and patches give the same bundle, byte for byte.

    A zip member's header carries a date and the writing OS, and writestr()
    with a bare name fills both in from the moment and the machine. Every
    rebuild of a published bundle was then a new file with a new hash, though
    nothing in it had changed.
    """
    import zipfile
    import bps
    import patchset

    body = bytes(range(256))
    manifest = {
        "format": patchset.FORMAT, "name": "reproducible",
        "target": {"body_size": 256, "headers": [0], "base": "0x0000",
                   "anchors": [{"addr": "0x80", "length": 64, "crc32": "0x%08X"
                                % patchset.crc32(body[0x80:0xC0])}]},
        "sections": {"s_0010": {"addr": "0x10", "length": 4, "crc32": "0x%08X"
                                % patchset.crc32(body[0x10:0x14])}},
        "options": [{"id": "x", "title": "x",
                     "patches": {"s_0010": "p/x.bps"}}],
    }
    files = {"p/x.bps": bps.create(body[0x10:0x14], b"\xEA" * 4)}
    d = tempfile.mkdtemp(prefix="patchset-")
    a, b = os.path.join(d, "a.abp"), os.path.join(d, "b.abp")
    patchset.write_bundle(a, json.loads(json.dumps(manifest)), files)
    patchset.write_bundle(b, json.loads(json.dumps(manifest)), files)
    for info in zipfile.ZipFile(a).infolist():
        if info.date_time != (1980, 1, 1, 0, 0, 0) or info.create_system != 3:
            raise AssertionError("%s carries %r from system %d; the header "
                                 "should say nothing about when or where"
                                 % (info.filename, info.date_time,
                                    info.create_system))
    if io.open(a, "rb").read() != io.open(b, "rb").read():
        raise AssertionError("two writes of one bundle differ")
    return "fixed date and system on every member; two writes identical"


def t_bundle_from_images():
    """A bundle built from each option's finished cartridge, not by hand.

    A generator that already makes the cartridge for each option should not
    have to work out sections, spans, anchors and which patch starts from
    which state. Two options here: `base` grows a 2K body to 4K and changes
    bytes in the old and new space; `detail` is built on it and changes some
    of the same bytes and some of its own -- the shape of Pole Position II
    VS and its higher-detail car.
    """
    import patchset

    dump = bytes(((i * 29 + 7) & 0xFF) for i in range(0x800))
    grown = b"\xFF" * 0x800 + dump                      # at the front, $F000
    base_img = bytearray(grown)
    base_img[0x100:0x108] = bytes(range(0x30, 0x38))    # new space
    base_img[0xA00:0xA04] = b"\x4C\x00\xF1\xEA"         # old space, calls it
    detail_img = bytearray(base_img)
    detail_img[0x102:0x104] = b"\x99\x98"               # on top of base's bytes
    detail_img[0xC00:0xC02] = b"\x55\xAA"               # its own
    options = [{"id": "base", "title": "grows", "image": bytes(base_img)},
               {"id": "detail", "title": "on base", "on": "base",
                "image": bytes(detail_img)}]
    d = tempfile.mkdtemp(prefix="patchset-")
    out = patchset.bundle_from_images(
        os.path.join(d, "b.abp"), dump, 0xF800, options, "builder test",
        grow={"size": 0x1000, "at": "front", "fill": "0xFF"})
    ps = patchset.PatchSet(out)
    if ps.m["format"] != patchset.FORMAT_GROW:
        raise AssertionError("a bundle that grows was written as %r" % ps.m["format"])
    if ps.needs("detail") != {"base"}:
        raise AssertionError("detail's dependency on base was not read from "
                             "the patches: %r" % ps.needs("detail"))
    for o, img in (("base", base_img), ("detail", detail_img)):
        if patchset.PatchSet(out).apply(dump, [o]) != bytes(img):
            raise AssertionError("%s applied to the dump is not its image" % o)
    ps2 = patchset.PatchSet(out)
    _h, b = ps2.find_body(bytes(detail_img))
    st = ps2.survey(b)
    if st != {"base": "applied", "detail": "applied"}:
        raise AssertionError("a cartridge with both reads as %r" % st)
    for bad, why in (([dict(options[1], on="nothing")], "an option on nothing"),
                     ([dict(options[0], image=bytes(base_img[:-1]))],
                      "an image of the wrong size")):
        try:
            patchset.bundle_from_images(os.path.join(d, "x.abp"), dump, 0xF800,
                                        bad, "x", grow={"size": 0x1000})
        except patchset.PatchSetError:
            continue
        raise AssertionError("%s was accepted" % why)
    return ("sections, shared spans, anchors and the dependency worked out "
            "from two finished images; each applies to its image, both read "
            "as applied, and a bad option is refused")


def t_modmap():
    """modmap.py sorts a grown mod's bytes into kept, overwritten and new.

    An invented 2K original grown to 4K: two bytes overwritten, some new
    code, a labelled table, a read $FF that must count as data rather than
    empty, and coverage saying which original bytes were read.
    """
    import modmap
    orig = bytes(((i * 13 + 1) & 0xFF) for i in range(0x800))
    mod = bytearray(b"\xFF" * 0x800 + orig)          # $F000-$FFFF
    mod[0x800 + 0x10:0x800 + 0x12] = b"\xEA\xEA"      # overwritten, $F810
    mod[0x000:0x020] = bytes(range(1, 0x21))          # new, $F000
    mod[0x100:0x110] = bytes(range(0x40, 0x50))       # labelled, $F100
    read = [False] * 0x1000
    for i in range(0x800, 0x900):
        read[i] = True                                # $F800-$F8FF read
    read[0x200] = True                                # a read $FF at $F200
    cat = modmap.classify(orig, bytes(mod), read,
                          [("tables", modmap.parse_ranges("F100-F10F"))])
    got = collections.Counter(cat)
    want = {"original, read": 0x100 - 2, "original, not read": 0x700,
            "overwritten": 2, "tables": 0x10, "new": 0x20 + 1,
            "new, empty": 0x800 - 0x20 - 0x10 - 1}
    if dict(got) != want:
        raise AssertionError("classes %r, want %r" % (dict(got), want))
    return ("kept (read and not), overwritten, labelled, new and empty counted; "
            "a read $FF is data")


def t_zonebill():
    """zonebill.py walks a RAM dump's display list list and bills each zone.

    An invented dump: 31 zones of 8 lines, each pointing at one display list
    with a single 4-byte object 4 bytes wide.
    """
    import zonebill
    ram = bytearray(0x1000)                           # $1800-$27FF
    for z in range(31):
        ram[z * 3:z * 3 + 3] = bytes([0x07, 0x19, 0x00])   # 8 lines, DL at $1900
    ram[0x100:0x106] = bytes([0x00, 0x1C, 0xA0, 0x10, 0x00, 0x00])
    d = tempfile.mkdtemp(prefix="zonebill-")
    path = os.path.join(d, "t-ram-00001.bin")
    open(path, "wb").write(bytes(ram))
    open(os.path.join(d, "t-ram-00001.txt"), "w").write("dpph=18 dppl=00 ctrl=40\n")
    zones, _ctrl = zonebill.bill(path)
    if len(zones) != 31 or sum(z["lines"] for z, _, _, _ in zones) != 248:
        raise AssertionError("walked %d zones" % len(zones))
    if any(len(o) != 1 or o[0]["width"] != 4 for _, o, _, _ in zones):
        raise AssertionError("objects misread: %r" % [len(o) for _, o, _, _ in zones])
    return "31 zones of 8 lines, one 4-byte-wide object each, billed"


def t_regress():
    """regress.py expands jobs, fills variables, and compares verdicts.

    MAME is replaced by a stand-in that writes the file the job names, so the
    job file, the substitution, the verdict rules and --against are tested
    without an emulator.
    """
    import regress
    d = tempfile.mkdtemp(prefix="regress-")
    jobs = {
        "vars": {"out": d.replace("\\", "/")},
        "cart": "{rom}",
        "jobs": [
            {"name": "echo {k}", "for": {"k": ["a", "b"]}, "script": "x.lua",
             "env": {"WRITE": "{out}/{k}.txt", "SAY": "line one|verdict {k}"},
             "verdict_file": "{out}/{k}.txt"},
            {"name": "grep", "script": "x.lua",
             "env": {"WRITE": "{out}/g.txt", "SAY": "noise|CC23=100|more"},
             "verdict_file": "{out}/g.txt", "verdict_grep": "CC23=([0-9]+)"},
        ]}
    jf = os.path.join(d, "jobs.json")
    json.dump(jobs, open(jf, "w"))
    stand_in = ("import os; open(os.environ['WRITE'], 'w').write("
                "os.environ['SAY'].replace('|', chr(10)) + chr(10))")
    real = regress.mame_line
    regress.mame_line = lambda job, cfg: [sys.executable, "-c", stand_in]
    try:
        base = os.path.join(d, "base.json")
        saved_argv = sys.argv
        sys.argv = ["regress.py", jf, "--var", "rom=x.a78", "--save", base]
        if regress.main() != 0:
            raise AssertionError("a first run failed")
        got = json.load(open(base))
        want = {"echo a": "verdict a", "echo b": "verdict b", "grep": "100"}
        if got != want:
            raise AssertionError("verdicts %r, want %r" % (got, want))
        json.dump(dict(want, grep="99"), open(base, "w"))
        sys.argv = ["regress.py", jf, "--var", "rom=x.a78", "--against", base]
        if regress.main() != 1:
            raise AssertionError("a changed verdict was not reported")
    finally:
        regress.mame_line = real
        sys.argv = saved_argv
    return "two expanded jobs and a grep verdict; a changed verdict fails --against"


def t_mame_palette():
    """palette.py carries MAME's colour table, and A7800_PALETTE switches to it."""
    import palette
    if palette.mame7800(0x17) != (145, 126, 9) or palette.mame7800(0x0D) != (221, 221, 221):
        raise AssertionError("MAME's table reads %r and %r"
                             % (palette.mame7800(0x17), palette.mame7800(0x0D)))
    if len(palette.MAME_NTSC) != 256 or len(palette.MAME_PAL) != 256:
        raise AssertionError("a table is not 256 entries")
    keep = os.environ.get("A7800_PALETTE")
    try:
        os.environ["A7800_PALETTE"] = "mame"
        if palette.ntsc7800(0x17) != (145, 126, 9):
            raise AssertionError("A7800_PALETTE=mame did not switch ntsc7800()")
        os.environ["A7800_PALETTE"] = ""
        if palette.ntsc7800(0x17) == (145, 126, 9):
            raise AssertionError("the approximation is no longer the default")
    finally:
        if keep is None:
            os.environ.pop("A7800_PALETTE", None)
        else:
            os.environ["A7800_PALETTE"] = keep
    return "MAME's $17 gold and $0D grey; A7800_PALETTE=mame switches ntsc7800()"


def t_patchset():
    """The patch-set format, against a cartridge invented for the purpose.

    Four things have to hold, and each is a way the format could look fine and
    be wrong:

    **A float finds real free space and everything that calls it learns where.**
    The whole reason floats exist is that a hardcoded address goes stale. This
    checks the blob lands inside the declared free run, that the byte after the
    run is untouched, and that the JSR operand which held a placeholder now
    holds the address actually chosen.

    **Two settings of one knob are refused.** Not applied in file order, which
    is what a plain stack of BPS files does, and which produces a ROM that is
    neither.

    **A header is carried through untouched.** The same body ships headered and
    bare; a bundle that only works on one of them is half a bundle.

    **Applying in two goes equals applying in one.** This is the claim that
    makes per-section checksums worth having: a ROM already patched elsewhere
    is still a valid target for an option whose own bytes nobody has touched.
    If those two paths ever diverge, the sections are not independent and the
    format is lying.
    """
    import json
    import bps
    import patchset

    # A cartridge: code at 0, a JSR with a placeholder operand, a run of free
    # space, and a byte just past it that must survive.
    rom = bytearray(0x400)
    rom[0x00:0x08] = b"\xA9\x01\x20\xFF\xFF\x60\xEA\xEA"   # JSR $FFFF
    rom[0x10:0x18] = bytes(range(0x10, 0x18))
    rom[0x20:0x28] = b"\x08\x08\x08\x08\x08\x08\x08\x08"
    rom[0x340] = 0x99                                       # just past the free run
    body = bytes(rom)

    def sec(at, n):
        return {"addr": "0x%04X" % at, "length": n,
                "crc32": "0x%08X" % patchset.crc32(body[at:at + n])}

    sections = {"s_0000": sec(0, 8), "s_0010": sec(0x10, 8),
                "s_0020": sec(0x20, 8)}

    def patch(at, n, edit):
        before = body[at:at + n]
        after = bytearray(before)
        edit(after)
        return bps.create(bytes(before), bytes(after))

    files = {
        "p/loud.s_0010.bps": patch(0x10, 8, lambda b: b.__setitem__(0, 0xAA)),
        "p/quiet.s_0010.bps": patch(0x10, 8, lambda b: b.__setitem__(0, 0xBB)),
        "p/routine.s_0020.bps": patch(0x20, 8, lambda b: b.__setitem__(7, 0x60)),
        "f/helper.bin": b"\xEE" * 12,
    }
    manifest = {
        "format": patchset.FORMAT,
        "name": "test",
        "target": {"body_size": len(body), "headers": [0, 16], "base": "0x0000",
                   "body_sha256": hashlib.sha256(body).hexdigest(),
                   "anchors": [{"addr": "0x0030", "length": 16,
                                "crc32": "0x%08X"
                                         % patchset.crc32(body[0x30:0x40])}]},
        "knobs": {"volume": "how loud"},
        "sections": sections,
        "options": [
            {"id": "loud", "knob": "volume", "title": "loud",
             "patches": {"s_0010": "p/loud.s_0010.bps"}},
            {"id": "quiet", "knob": "volume", "title": "quiet",
             "patches": {"s_0010": "p/quiet.s_0010.bps"}},
            {"id": "routine", "title": "a routine that needs somewhere to live",
             "patches": {"s_0020": {"bps": "p/routine.s_0020.bps"}},
             "floats": [{"id": "helper", "length": 12, "fill": 0, "from": "end",
                         "blob": "f/helper.bin",
                         "search": [{"addr": "0x0300", "length": 0x40}],
                         "fixups": [{"addr": "0x0003", "encode": "abs16",
                                     "expect": "0xFFFF"}]}]},
        ],
    }
    out = os.path.join(tempfile.mkdtemp(prefix="patchset-"), "t.patchset")
    patchset.write_bundle(out, manifest, files)
    ps = patchset.PatchSet(out)

    # 1. the float finds room, and the caller learns where
    got = ps.apply(body, ["routine"])
    where = got[3] | (got[4] << 8)
    if not (0x300 <= where <= 0x340 - 12):
        raise AssertionError("the float landed at $%04X, outside the free run "
                             "it was told to search" % where)
    if got[where:where + 12] != b"\xEE" * 12:
        raise AssertionError("the float's bytes are not at the address the "
                             "fixup was given")
    if got[0x340] != 0x99:
        raise AssertionError("the float overran the free range")
    if got[0x27] != 0x60:
        raise AssertionError("the section patch did not apply")

    # 2. two settings of one knob is a choice, not an order of application
    try:
        ps.apply(body, ["loud", "quiet"])
    except patchset.PatchSetError as e:
        if "alternative" not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    else:
        raise AssertionError(
            "two settings of one knob were applied one after the other, which "
            "produces a ROM that is neither")

    # 3. a header rides along untouched
    headered = b"\x7F" * 16 + body
    out2 = ps.apply(headered, ["loud"])
    if out2[:16] != b"\x7F" * 16 or len(out2) != len(headered):
        raise AssertionError("the header was not carried through intact")
    if out2[16 + 0x10] != 0xAA:
        raise AssertionError("the body was located at the wrong offset")

    # 4. one go and two goes agree
    both = ps.apply(body, ["loud", "routine"])
    step = ps.apply(ps.apply(body, ["routine"]), ["loud"])
    if both != step:
        raise AssertionError(
            "applying two options together and one after the other gave "
            "different ROMs, so the sections are not independent")

    # 5. an option already on the cartridge is recognised, not refused
    once = ps.apply(body, ["loud"])
    if ps.option_state(bytearray(once), "loud") != "applied":
        raise AssertionError(
            "a cartridge that plainly has this option was not recognised as "
            "having it, so applying twice would look like damage")
    twice = ps.apply(once, ["loud"])
    if twice != once:
        raise AssertionError("applying an option twice changed the ROM the "
                             "second time; it is not idempotent")

    # 6. and the anchors identify the game whether or not it is pristine
    if not ps.pristine and ps.find_body(once)[1] is None:
        raise AssertionError("unreachable")
    ps.find_body(once)
    if ps.pristine:
        raise AssertionError("a patched ROM was reported as the pristine dump")
    wrong = bytearray(body)
    wrong[0x30] ^= 0xFF                      # break an anchor
    try:
        ps.find_body(bytes(wrong))
    except patchset.PatchSetError as e:
        if "anchor" not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    else:
        raise AssertionError(
            "a cartridge failing an anchor was accepted; anchors are what "
            "identify the target once a whole-file hash cannot")

    # 7. a dependency nobody declared, read out of the patches themselves
    #
    # "louder" is authored on top of "loud": its patch starts from the bytes
    # loud leaves behind. Nothing says so in the manifest -- the BPS headers
    # say it, because one patch's source CRC is the other's target CRC.
    loud_out = bytearray(body[0x10:0x18])
    loud_out[0] = 0xAA
    louder = bytearray(loud_out)
    louder[1] = 0xCC
    files2 = dict(files)
    files2["p/louder.s_0010.bps"] = bps.create(bytes(loud_out), bytes(louder))
    orphan = bytearray(body[0x20:0x28])
    orphan[0] = 0x77                      # a state nothing in the bundle makes
    files2["p/orphan.s_0020.bps"] = bps.create(
        bytes(orphan), bytes(bytearray(b"" * 8)))
    m2 = json.loads(json.dumps(manifest))
    m2["options"].append({"id": "louder", "title": "louder still",
                          "patches": {"s_0010": "p/louder.s_0010.bps"}})
    m2["options"].append({"id": "orphan", "title": "built on nothing here",
                          "patches": {"s_0020": "p/orphan.s_0020.bps"}})
    out2 = os.path.join(tempfile.mkdtemp(prefix="patchset-"), "t2.patchset")
    patchset.write_bundle(out2, m2, files2)
    ps2 = patchset.PatchSet(out2)

    derived, unknown = ps2.derived_requires()
    if derived.get("louder") != {"loud"}:
        raise AssertionError(
            "the dependency of louder on loud is written in the two patches' "
            "CRCs and was not found: %r" % (derived.get("louder"),))
    if derived.get("loud"):
        raise AssertionError("loud starts from the pristine bytes and should "
                             "need nothing: %r" % (derived["loud"],))
    if "orphan" not in unknown:
        raise AssertionError(
            "a patch starting from a state nothing in the bundle produces was "
            "not flagged; it can never apply and should say so")

    # asking for louder alone brings loud along, in the right order
    chained = ps2.apply(body, ["louder"])
    if chained[0x10] != 0xAA or chained[0x11] != 0xCC:
        raise AssertionError(
            "louder was applied without loud beneath it, so the derived "
            "dependency was found and then ignored")
    # on the untouched dump, louder reads as applicable -- after loud, which
    # apply brings along -- not as "something else edited its bytes"
    ps2u = patchset.PatchSet(out2)
    _hu, bu = ps2u.find_body(body)
    if ps2u.survey(bu).get("louder") != "applies":
        raise AssertionError("an option built on another reads as %r on the "
                             "dump both apply to" % ps2u.survey(bu).get("louder"))
    # ...and the chained result is recognised: loud is on it, underneath
    # louder, so a survey calls both applied and applying again is a no-op
    # (it once read loud as "blocked" and refused the cartridge it had made)
    ps2c = patchset.PatchSet(out2)
    _hc, bc = ps2c.find_body(chained)
    st = ps2c.survey(bc)
    if st.get("loud") != "applied" or st.get("louder") != "applied":
        raise AssertionError("a chained result reads as %r; both options "
                             "are on it" % ({k: st.get(k) for k in ("loud", "louder")},))
    if patchset.PatchSet(out2).apply(chained, ["louder"]) != chained:
        raise AssertionError("applying a chain a second time changed the "
                             "cartridge")
    try:
        ps2.apply(body, ["orphan"])
    except patchset.PatchSetError as e:
        if "bundle does not describe" not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    else:
        raise AssertionError("an option built on an unknown state applied")

    # 8. one patch across several sections
    #
    # Six two-byte edits scattered over a ROM are one change, not six. A span
    # is the sections concatenated in address order, so the BPS is one delta
    # with one pair of checksums -- and the pre-image of a span cannot be
    # worked out from the sections' own CRCs, so it has to be stated.
    span_before = body[0x00:0x08] + body[0x10:0x18]
    span_after = bytearray(span_before)
    span_after[1] = 0x42
    span_after[9] = 0x43
    files3 = dict(files)
    files3["p/wide.bps"] = bps.create(bytes(span_before), bytes(span_after))
    m3 = json.loads(json.dumps(manifest))
    m3["options"] = [o for o in m3["options"] if o["id"] == "loud"]
    m3["options"].append({
        "id": "wide", "title": "one patch, two sections",
        "patches": [{"sections": ["s_0010", "s_0000"], "bps": "p/wide.bps",
                     "before": "0x%08X" % patchset.crc32(bytes(span_before))}]})
    out3 = os.path.join(tempfile.mkdtemp(prefix="patchset-"), "t3.abp")
    patchset.write_bundle(out3, m3,
                          {k: v for k, v in files3.items()
                           if k in ("p/loud.s_0010.bps", "p/wide.bps")})
    ps3 = patchset.PatchSet(out3)
    wide = ps3.apply(body, ["wide"])
    if wide[0x01] != 0x42 or wide[0x11] != 0x43:
        raise AssertionError(
            "a patch spanning two sections did not reach both of them")
    if wide[0x08:0x10] != body[0x08:0x10]:
        raise AssertionError(
            "the span wrote over the gap between its two sections; the pieces "
            "are concatenated for the delta, not treated as one extent")
    if ps3.option_state(bytearray(wide), "wide") != "applied":
        raise AssertionError("a span was not recognised as already applied")

    m4 = json.loads(json.dumps(m3))
    del m4["options"][-1]["patches"][0]["before"]
    out4 = os.path.join(tempfile.mkdtemp(prefix="patchset-"), "t4.abp")
    patchset.write_bundle(out4, m4,
                          {k: v for k, v in files3.items()
                           if k in ("p/loud.s_0010.bps", "p/wide.bps")})
    try:
        patchset.PatchSet(out4).apply(body, ["wide"])
    except patchset.PatchSetError as e:
        if "before" not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    else:
        raise AssertionError(
            "a multi-section span with no stated pre-image was accepted, so "
            "nothing could tell whether it had been applied")

    # 9. two options that differ only inside a float stay distinguishable
    #
    # Sections cover the code that calls a float; nothing covers the float. Two
    # knockback strengths have byte-identical call sites and blobs differing in
    # one operand, so on sections alone each reports "already applied" about a
    # cartridge carrying the other. The fixup wrote the address down, so the
    # blob can be checked where it actually landed.
    m5 = json.loads(json.dumps(manifest))
    m5["options"] = [o for o in m5["options"] if o["id"] == "routine"]
    m5["knobs"]["strength"] = "how hard"
    files5 = {"p/routine.s_0020.bps": files["p/routine.s_0020.bps"],
              "f/helper.bin": bytes([0xEE]) * 12,
              "f/helper2.bin": bytes([0xEF]) * 12}
    m5["options"][0]["knob"] = "strength"
    m5["options"][0]["floats"][0]["crc32"] = ("0x%08X"
        % patchset.crc32(files5["f/helper.bin"]))
    other = json.loads(json.dumps(m5["options"][0]))
    other["id"] = "routine2"
    other["floats"][0]["blob"] = "f/helper2.bin"
    other["floats"][0]["crc32"] = ("0x%08X"
        % patchset.crc32(files5["f/helper2.bin"]))
    m5["options"].append(other)
    out5 = os.path.join(tempfile.mkdtemp(prefix="patchset-"), "t5.abp")
    patchset.write_bundle(out5, m5, files5)
    ps5 = patchset.PatchSet(out5)
    done = bytearray(ps5.apply(body, ["routine"]))
    if ps5.option_state(done, "routine") != "applied":
        raise AssertionError("an option was not recognised as applied")
    if ps5.option_state(done, "routine2") == "applied":
        raise AssertionError(
            "an option whose float is not the one on the cartridge reported "
            "itself already applied; applying it would look like a no-op and "
            "silently leave the other one in place")

    return ("floats placed in real free space with their callers fixed up, "
            "alternatives refused, headers preserved, incremental "
            "application converges, already-applied options recognised, and "
            "anchors identify the target, and a dependency written only in "
            "two patches' checksums is found and honoured, and one patch "
            "can span several sections, and two options differing only "
            "inside a float stay apart")

def t_patchset_lint():
    """patchset.py lint says what a bundle's manifest gets wrong: references to
    nothing, sections over the same bytes, options that cannot be combined."""
    import bps
    import patchset
    body = bytes(range(256)) * 2

    def build(sections, options, extra_files=()):
        d = tempfile.mkdtemp(prefix="lint-")
        os.makedirs(os.path.join(d, "p"))
        made = {}
        for o in options:
            for sid in (o.get("_sections") or []):
                at, n = sections[sid]
                before = body[at:at + n]
                after = bytearray(before)
                after[0] ^= 0xFF
                made[(o["id"], sid)] = bps.create(before, bytes(after))
        man = {"format": patchset.FORMAT, "name": "t",
               "target": {"body_size": len(body), "base": "0x0000", "headers": [0],
                          "body_sha256": hashlib.sha256(body).hexdigest(),
                          "anchors": []},
               "knobs": {"k": "a knob"},
               "sections": {sid: {"addr": "0x%04X" % at, "length": n,
                                  "crc32": "0x%08X" % patchset.crc32(body[at:at + n])}
                            for sid, (at, n) in sections.items()},
               "options": []}
        for o in options:
            o = dict(o)
            sids = o.pop("_sections", [])
            if sids:
                o["patches"] = {sid: "p/%s.%s.bps" % (o["id"], sid) for sid in sids}
                for sid in sids:
                    io.open(os.path.join(d, "p", "%s.%s.bps" % (o["id"], sid)),
                            "wb").write(made[(o["id"], sid)])
            man["options"].append(o)
        io.open(os.path.join(d, "patchset.json"), "w", encoding="utf-8").write(json.dumps(man))
        return patchset.PatchSet(d)

    def kinds(ps):
        out = patchset.lint(ps)
        return ([m for k, m in out if k == "error"], [m for k, m in out if k == "warning"],
                [m for k, m in out if k == "note"])

    sec = {"s_a": (0x10, 8), "s_b": (0x40, 8)}
    errs, warns, notes = kinds(build(sec, [
        {"id": "a", "title": "a", "_sections": ["s_a"]},
        {"id": "b", "title": "b", "_sections": ["s_b"]}]))
    assert not (errs or warns or notes), (errs, warns, notes)
    # two sections over the same bytes, options that can be asked for together
    over = {"s_a": (0x10, 8), "s_b": (0x14, 8)}
    errs, _w, _n = kinds(build(over, [
        {"id": "a", "title": "a", "_sections": ["s_a"]},
        {"id": "b", "title": "b", "_sections": ["s_b"]}]))
    assert errs and "overlap" in errs[0] and "'a' and 'b'" in errs[0], errs
    # ... but only a note when they are alternatives and can never meet
    errs, _w, notes = kinds(build(over, [
        {"id": "a", "title": "a", "knob": "k", "_sections": ["s_a"]},
        {"id": "b", "title": "b", "knob": "k", "_sections": ["s_b"]}]))
    assert not errs and any("overlap" in n for n in notes), (errs, notes)
    # exactly the same sections, no knob: probably forgotten alternatives
    _e, warns, notes = kinds(build(sec, [
        {"id": "a", "title": "a", "_sections": ["s_a"]},
        {"id": "b", "title": "b", "_sections": ["s_a"]},
        {"id": "c", "title": "c", "_sections": ["s_b"]}]))
    assert any("exactly the same sections" in w for w in warns), warns
    assert any("'a' cannot be combined with 'b'" in n for n in notes), notes
    _e, warns, _n = kinds(build(sec, [
        {"id": "a", "title": "a", "knob": "k", "_sections": ["s_a"]},
        {"id": "b", "title": "b", "knob": "k", "_sections": ["s_a"]}]))
    assert not any("exactly" in w for w in warns), warns
    # references to things that are not there
    errs, warns, _n = kinds(build(sec, [
        {"id": "a", "title": "a", "requires": ["ghost"], "_sections": ["s_a"]},
        {"id": "b", "title": "b", "knob": "undescribed", "_sections": ["s_a"]}]))
    assert any("ghost" in e for e in errs), errs
    assert any("undescribed" in w for w in warns), warns
    assert any("not patched by any option" in w and "s_b" in w for w in warns), warns
    ps = build(sec, [{"id": "a", "title": "a", "_sections": ["s_a"]}])
    os.remove(os.path.join(ps.path, "p", "a.s_a.bps"))
    errs, _w, _n = kinds(ps)
    assert any("holds no file" in e for e in errs), errs
    return "overlap hazards, alternatives, exclusions, missing references and leftovers"


def t_patchset_grow():
    """A bundle that grows the cartridge (patchset/3), against an invented one.

    A 2K body at $F800 grows at the front to 4K at $F000 -- the shape of a
    linear 7800 cartridge going from 32K to 48K -- with one section in the old
    space and one in the new, patched by a single span. Six things have to
    hold:

    **The body grows where the option says, and the old bytes move with it.**
    **The .a78 header's ROM size follows the body** -- an emulator believes
    the header, and maps a grown body under a stale size wrong.
    **A grown cartridge is still recognised**: check calls the option
    applied, and applying again changes nothing.
    **Headered and bare give the same body.**
    **An option that does not grow leaves the size alone**, and an ungrown
    dump still reads as a target for the one that does.
    **A header that puts something at $4000 refuses to grow**, and a /2
    manifest that grows is refused as a format error rather than misread.
    """
    import json
    import bps
    import patchset

    old = bytes(((i * 7 + 3) & 0xFF) for i in range(0x800))
    fill = bytes([0xFF])
    grown_pre = fill * 0x800 + old
    base0, base1 = 0xF800, 0xF000

    def sec(addr, n):
        at = addr - base1
        return {"addr": "0x%04X" % addr, "length": n,
                "crc32": "0x%08X" % patchset.crc32(grown_pre[at:at + n])}

    sections = {"s_F100": sec(0xF100, 8), "s_F810": sec(0xF810, 4),
                "s_F900": sec(0xF900, 2)}
    before = grown_pre[0x100:0x108] + grown_pre[0x810:0x814]
    jsr = bytes([0x20, 0x00, 0xF1, 0xEA])                # a JSR into the new space
    after = bytes(range(0x40, 0x48)) + jsr
    nops = bytes([0xEA, 0xEA])
    files = {"p/big.bps": bps.create(before, after),
             "p/small.bps": bps.create(grown_pre[0x900:0x902], nops)}
    manifest = {
        "format": patchset.FORMAT,              # write_bundle must raise it to /3
        "name": "grow test",
        "target": {"body_size": 0x800, "headers": [0, 128], "base": "0x%04X" % base0,
                   "anchors": [{"addr": "0xFC00", "length": 64,
                                "crc32": "0x%08X" % patchset.crc32(old[0x400:0x440])}]},
        "sections": sections,
        "options": [
            {"id": "big", "title": "needs the new space",
             "grow": {"size": 0x1000, "at": "front", "fill": "0xFF"},
             "patches": [{"sections": ["s_F100", "s_F810"], "bps": "p/big.bps",
                          "before": "0x%08X" % patchset.crc32(before)}]},
            {"id": "small", "title": "fits as it is",
             "patches": {"s_F900": "p/small.bps"}},
        ],
    }
    out = os.path.join(tempfile.mkdtemp(prefix="patchset-"), "g.abp")
    patchset.write_bundle(out, manifest, files)
    ps = patchset.PatchSet(out)
    if ps.m["format"] != patchset.FORMAT_GROW:
        raise AssertionError("a growing bundle was written as %r" % ps.m["format"])

    def a78(size, ctype=0):
        hd = bytearray(128)
        hd[0] = 4
        hd[1:10] = b"ATARI7800"
        hd[49:53] = size.to_bytes(4, "big")
        hd[53:55] = ctype.to_bytes(2, "big")
        hd[100:128] = b"ACTUAL CART DATA STARTS HERE"
        return bytes(hd)

    # 1-2. grows at the front, the header follows, the new space is patched,
    #      the old bytes move up
    got = patchset.PatchSet(out).apply(a78(0x800) + old, ["big"])
    if len(got) != 128 + 0x1000:
        raise AssertionError("grown image is %d bytes, want %d"
                             % (len(got), 128 + 0x1000))
    if int.from_bytes(got[49:53], "big") != 0x1000:
        raise AssertionError("the header still says %d bytes"
                             % int.from_bytes(got[49:53], "big"))
    body = got[128:]
    if body[0x100:0x108] != bytes(range(0x40, 0x48)):
        raise AssertionError("the patch did not land in the new space")
    if body[0x000:0x100] != fill * 0x100 or body[0x108:0x800] != fill * 0x6F8:
        raise AssertionError("the rest of the new space is not the fill")
    if body[0xC00:0xC40] != old[0x400:0x440] or body[0x810:0x814] != jsr:
        raise AssertionError("the old bytes did not move up with the growth")

    # 3. recognised when grown, and idempotent
    ps3 = patchset.PatchSet(out)
    _hdr3, body3 = ps3.find_body(got)
    if ps3.base != base1 or ps3.survey(body3).get("big") != "applied":
        raise AssertionError("a grown cartridge is not recognised as "
                             "carrying the option")
    if patchset.PatchSet(out).apply(got, ["big"]) != got:
        raise AssertionError("applying twice changed the cartridge")

    # 4. headered and bare give the same body
    if patchset.PatchSet(out).apply(old, ["big"]) != body:
        raise AssertionError("a headerless dump grows to a different body")

    # 5. an option that does not grow leaves the size alone, and an ungrown
    #    dump still reads as a target for the one that does
    small = patchset.PatchSet(out).apply(a78(0x800) + old, ["small"])
    if len(small) != 128 + 0x800 or small[128 + 0x100:128 + 0x102] != nops:
        raise AssertionError("a non-growing option changed the size or "
                             "missed its bytes")
    ps5 = patchset.PatchSet(out)
    _h5, b5 = ps5.find_body(a78(0x800) + old)
    if ps5.survey(ps5.grown_view(b5)).get("big") != "applies":
        raise AssertionError("an ungrown dump does not read as a target "
                             "for growth")

    # 6. a header with something at $4000 refuses; a /2 manifest that grows
    #    is refused by name
    try:
        patchset.PatchSet(out).apply(a78(0x800, 0x0004) + old, ["big"])
        raise AssertionError("grew a cartridge whose header puts RAM at $4000")
    except patchset.PatchSetError as e:
        if "$4000" not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    d = tempfile.mkdtemp(prefix="patchset-")
    with open(os.path.join(d, "patchset.json"), "w") as f:
        f.write(json.dumps(dict(manifest, format=patchset.FORMAT)))
    try:
        patchset.PatchSet(d)
        raise AssertionError("a /2 manifest that grows was accepted")
    except patchset.PatchSetError as e:
        if patchset.FORMAT_GROW not in str(e):
            raise AssertionError("refused for the wrong reason: %s" % e)
    return ("grows at the front with the header's size following, the new "
            "space patched and the old moved up, recognised and idempotent "
            "once grown, headered and bare alike, refused under a header "
            "with something at $4000 and as a /2 manifest")


def t_portkit_refuses_payload():
    """A conversion recipe must carry coordinates, never content.

    `portkit.py` exists because a BPS patch cannot express a build that draws
    on two sources: the delta from a 7800 cartridge to a conversion using Atari
    8-bit artwork would contain all of that artwork, so the "patch" would be a
    redistribution wearing a diff's clothes. A recipe avoids that by holding
    only hashes, offsets and lengths -- and that only holds while nobody
    embeds "just one table" inline.

    So the rule is enforced in code, and this checks the enforcement works in
    both directions: it fires on embedded data, and it does not fire on an
    ordinary recipe. A guard that cannot be shown to trip is decoration.
    """
    import base64
    import json
    import portkit

    good = {
        "name": "test",
        "sources": {"disk": {"what": "a disk", "sha256": "00" * 32}},
        "regions": {"art": {"from": "disk", "sector": 10, "sectors": 2,
                            "sha256": "11" * 32, "what": "some artwork"}},
        "new": ["src/main.s"],
    }
    path = os.path.join(tempfile.gettempdir(), "portkit-good.json")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(good))
    portkit.load_recipe(path)          # must not raise

    for key in ("data", "bytes", "payload", "base64", "hex"):
        bad = json.loads(json.dumps(good))
        bad["regions"]["art"][key] = base64.b64encode(b"\xAA" * 400).decode()
        p2 = os.path.join(tempfile.gettempdir(), "portkit-bad.json")
        with io.open(p2, "w", encoding="utf-8") as f:
            f.write(json.dumps(bad))
        try:
            portkit.load_recipe(p2)
        except portkit.RecipeError:
            continue
        raise AssertionError(
            "a recipe carrying %d bytes under %r was accepted; the rule that "
            "makes this safe to publish is not being enforced"
            % (400, key))

    # a long prose note is not payload, and must still be allowed
    wordy = json.loads(json.dumps(good))
    wordy["note"] = "why this exists. " * 40
    p3 = os.path.join(tempfile.gettempdir(), "portkit-wordy.json")
    with io.open(p3, "w", encoding="utf-8") as f:
        f.write(json.dumps(wordy))
    portkit.load_recipe(p3)

    return ("a recipe carrying embedded data is refused under every name "
            "tried, and ordinary recipes still load")


def t_dist_carries_no_rom():
    """The published patches must not smuggle the cartridge out with them.

    `dist/` is the one directory in this repository that holds build output,
    and it holds it because both formats there are meant to travel without
    the game. That is a claim about bytes, so it is checked rather than
    believed -- the same standard `recipes carry no payload` holds
    `portkit.py` to.

    For a BPS the question is what its literals are. The encoder emits a
    literal only for a run that differs from the source, so in principle
    every stored byte is the patch author's; this confirms it by decoding
    each patch and comparing every literal against the original at the same
    address. One match would mean a byte of the game riding along.

    For the patch set the question is different, because it stores whole
    blobs. Its sections must describe their pre-image with a CRC32 and never
    quote it, and its float blobs -- code with no fixed home -- must be
    authored rather than lifted, so none of them may appear anywhere in the
    cartridge.

    Skips without a dump, like the other cartridge-dependent checks: with no
    original to compare against there is nothing to be sure of.
    """
    import json
    import zipfile

    root = os.path.dirname(HERE)
    dist = os.path.join(root, "dist")
    if not os.path.isdir(dist):
        return None

    import importlib.util
    kp = os.path.join(root, "patches", "karateka.py")
    if not os.path.exists(kp):
        return None
    spec = importlib.util.spec_from_file_location("karateka_dist", kp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        _src, _hdr, orig = mod.load_source()
    except SystemExit:
        return None

    sys.path.insert(0, HERE)
    import bps as bpsmod

    literals = leaked = 0
    for name in sorted(os.listdir(dist)):
        if not name.endswith(".bps"):
            continue
        patch = io.open(os.path.join(dist, name), "rb").read()
        h = bpsmod.read_header(patch)
        i, pos = h["actions_at"], 0
        while i < h["body_end"]:
            v, i = bpsmod.decode_number(patch, i)
            act, ln = v & 3, (v >> 2) + 1
            if act == bpsmod.SOURCE_READ:
                pos += ln
            elif act == bpsmod.TARGET_READ:
                for k in range(ln):
                    literals += 1
                    if pos + k < len(orig) and orig[pos + k] == patch[i + k]:
                        leaked += 1
                i += ln
                pos += ln
            else:
                _o, i = bpsmod.decode_number(patch, i)
                pos += ln
    if leaked:
        raise AssertionError(
            "%d of %d literal bytes in dist/*.bps are the original "
            "cartridge's own; these patches are not safe to publish"
            % (leaked, literals))

    # every bundle, not just the NTSC one: the structural guarantee holds
    # regardless of which cartridge a bundle targets, and a PAL bundle
    # would otherwise go unchecked
    for abp in sorted(f for f in os.listdir(dist) if f.endswith(".abp")):
        abp = os.path.join(dist, abp)
        z = zipfile.ZipFile(abp)
        man = json.loads(z.read("patchset.json"))
        rows = man["sections"]
        rows = rows if isinstance(rows, list) else list(rows.values())
        for r in rows:
            for k, v in r.items():
                if k != "crc32" and isinstance(v, str) and len(v) >= 8 \
                        and all(c in "0123456789abcdefABCDEF" for c in v):
                    raise AssertionError(
                        "section %r stores what looks like byte data in %r; "
                        "sections must carry a CRC32 of the pre-image, not "
                        "the pre-image" % (r.get("what", "?"), k))
        for n in z.namelist():
            if n.startswith("f/") and z.read(n) in orig:
                raise AssertionError(
                    "float blob %s appears verbatim in the cartridge, so it "
                    "is lifted rather than authored" % n)

    return "%d literal bytes across dist/, none of them the cartridge's" % literals


def hermetic(keep_env=False):
    """Make a run depend on nothing outside the repo, and leave nothing behind.

    Every temporary file -- ours, the tools' we import, and the subprocesses
    we start -- goes under one directory that is removed at exit. (Each run
    used to leave dozens of selftest-* and regress-* directories in /tmp.)
    A7800_* settings are dropped, so a developer who exports
    A7800_PALETTE=mame or A7800_MAME gets the same result as everyone else;
    --keep-env opts out.
    """
    if not keep_env:
        # A7800_MAME / _ROMPATH / _BIOS only say where things are; they cannot
        # change a result, and the MAME checks need them to find anything.
        keep = ("A7800_MAME", "A7800_ROMPATH", "A7800_BIOS")
        for k in [k for k in os.environ if k.startswith("A7800_") and k not in keep]:
            del os.environ[k]
    root = tempfile.mkdtemp(prefix="selftest-")
    tempfile.tempdir = root
    for k in ("TMPDIR", "TEMP", "TMP"):
        os.environ[k] = root
    atexit.register(shutil.rmtree, root, True)
    return root


def synthetic_cart(root):
    """A real 16K cartridge from newgame.py, for checks that need any ROM."""
    out = os.path.join(root, "synthetic")
    r = subprocess.run([sys.executable, os.path.join(HERE, "newgame.py"),
                        out, "--build"], capture_output=True, text=True)
    path = os.path.join(out, "game.a78")
    return path if r.returncode == 0 and os.path.exists(path) else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", help="a cartridge, to run the round trips")
    ap.add_argument("--format", help="a player-format file to go with --rom")
    ap.add_argument("--log", help="an audio.lua capture of --rom")
    ap.add_argument("--song", type=int, default=0,
                    help="which song --log recorded (default 0)")
    ap.add_argument("--region", default="ntsc", choices=["ntsc", "pal"])
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep-env", action="store_true",
                    help="do not drop A7800_* environment variables")
    args = ap.parse_args()

    root = hermetic(args.keep_env)
    user_rom = bool(args.rom)
    if not args.rom:
        args.rom = synthetic_cart(root)

    r = Results(args.verbose)
    print("without a cartridge")
    r.check("shipped json", t_json)
    r.check("format files", t_formats)
    r.check("display lists", t_dlwalk)
    r.check("POKEY random", t_sim_random)
    r.check("sim compare", t_sim_compare)
    r.check("sprite import", t_mksprite)
    r.check("DMA cost model", t_dmabudget)
    r.check("game scaffold", t_newgame)
    r.check("gap checker", t_check_gaps)
    r.check("6502 cycle table", t_cycles)
    r.check("example songs", t_examples)
    r.check("note tables", t_notes)
    r.check("refusals", t_refusals)
    r.check("two POKEYs", t_dualpokey)
    r.check("tracker UI", t_trackeredit)
    r.check("midi import", t_midi)
    r.check("format guesser", t_explore)
    r.check("browser page scripts", t_browser_js)
    r.check("click handlers", t_handler_attributes)
    r.check("saved readings", t_direct_format)
    r.check("engine finder", t_engine_finder)
    r.check("forth decompiler", t_forth_decompiler)
    r.check("8-bit cartridge tracer", t_a8dis)
    r.check("assembler names", t_asm_names)
    r.check("patch sets", t_patchset)
    r.check("patch sets that grow", t_patchset_grow)
    r.check("patch set lint", t_patchset_lint)
    r.check("bundles are reproducible", t_bundle_reproducible)
    r.check("bundles built from images", t_bundle_from_images)
    r.check("mod maps", t_modmap)
    r.check("zone bills", t_zonebill)
    r.check("regression runner", t_regress)
    r.check("MAME palette", t_mame_palette)
    r.check("recipes carry no payload", t_portkit_refuses_payload)
    r.check("published patches carry no ROM", t_dist_carries_no_rom)
    r.check("tool --help", t_helps)
    r.check("TIA periods", t_tia_periods)
    r.check("address origins", t_origins)
    r.check("POKEY to TIA", t_pokey2tia)
    r.check("workbench jobs", t_workbench_jobs)
    r.check("local server rules", t_localserver)
    r.check("workbench hardening", t_workbench_hardening)
    r.check("workbench handoff", t_workbench_handoff)
    r.check("(OM) file offsets", t_om_file_offsets)
    r.check("RAM vector runs", t_ram_vector_runs)
    r.check("dmameasure", t_dmameasure)
    r.check("workbench in a browser", t_workbench_browser)
    r.check("simulator probe", t_simprobe)
    r.check("simulated address origins", t_simorigins)
    r.check("census", t_census)
    r.check("census guesses", t_census_guess)
    r.check("dynamic evidence from the sim", t_dyn_sim)
    r.check("census, bankset", t_census_bankset)
    r.check("bankset round trip", t_bankset_roundtrip)
    r.check("bankset vs a7800 source", t_bankset_fork_model)
    r.check("bankset RAM", t_bankset_ram)
    r.check("mamecheck", t_mamecheck)
    r.check("EXROM layout", t_exrom_layout)
    r.check("dispatch tables", t_dispatch_tables)
    r.check("branch forcing", t_branchforce)
    r.check("corpus measure", t_corpus)
    r.check("first look, simulated", t_firstlook_sim)
    r.check("sim bus", t_sim_bus)
    r.check("sim window", t_sim_window)
    r.check("sim timing", t_sim_timing)
    r.check("sim machine", t_sim_machine)
    r.check("README tool list", t_readme)
    r.check("doc links", t_links)
    r.check("flake8", t_flake8)
    r.check("verify explains", t_verify_explains)
    r.check("lua lint", t_lualint)
    r.check("batch quoting", t_bat_quotes)
    r.check("probe index", t_probe_index)
    r.check("doc references", t_docrefs)
    r.check("synthetic banked cart", t_synth_static)
    r.check("annotations lint", t_annotations_lint)
    r.check("address spellings", t_addresses)
    r.check("profile mapping", t_pcmap)
    r.check("dynamic annotations", t_dynamic)
    r.check("first look (static)", t_firstlook_static)

    print("")
    print("with a cartridge")
    r.check("songfmt no-op push", lambda: t_pull_push(args.rom, args.format))
    r.check("songfmt refuses growth", lambda: t_grow(args.rom, args.format))
    r.check("render vs hardware",
            lambda: t_verify(args.rom, args.format, args.log, args.song,
                             args.region))
    r.check("sprite edit round trip", lambda: t_sprites(args.rom))
    r.check("songs from the ROM",
            lambda: t_songs_from_rom(args.rom, args.format))
    r.check("workbench", lambda: t_workbench(args.rom))
    r.check("disassembler runs", lambda: t_disasm(args.rom))
    r.check("probes under MAME", lambda: t_probes_mame(args.rom))
    r.check("workbench with MAME", t_workbench_mame)
    r.check("pixel formats vs MAME", t_pixel_formats)

    n_ok = sum(1 for x in r.rows if x[1] == PASS)
    n_skip = sum(1 for x in r.rows if x[1] == SKIP)
    print("")
    print("%d passed, %d failed, %d skipped" % (n_ok, len(r.failed), n_skip))
    if n_skip and not user_rom:
        print("Pass --rom, --format and --log to run the rest; the tests marked nothing-to-test need MAME and its BIOS (docs/emulation.md).")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
