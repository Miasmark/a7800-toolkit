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
import glob
import io
import json
import os
import re
import subprocess
import sys
import tempfile

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
    for f in files:
        json.load(io.open(f, encoding="utf-8"))
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
    if d.REGIONS["ntsc"][0] != 262:
        raise AssertionError("NTSC scanline count changed")
    return ("12 measured configurations reproduced, worst error %.1f%%"
            % (100 * worst))


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
        if p.returncode != 0:
            bad.append("%s (exit %d)" % (name, p.returncode))
    if bad:
        raise AssertionError("; ".join(bad))
    return "every tool has working --help"


def t_readme():
    """The README lists the tools that exist, and no others."""
    s = io.open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    claimed = set(re.findall(r"^\| `([a-z0-9_]+\.py)`", s, re.M))
    actual = set(os.path.basename(f)
                 for f in glob.glob(os.path.join(HERE, "*.py")))
    libs = set(["a7800.py", "m6502.py"])
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
    args = ap.parse_args()

    r = Results(args.verbose)
    print("without a cartridge")
    r.check("shipped json", t_json)
    r.check("format files", t_formats)
    r.check("display lists", t_dlwalk)
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
    r.check("tool --help", t_helps)
    r.check("README tool list", t_readme)
    r.check("doc links", t_links)

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

    n_ok = sum(1 for x in r.rows if x[1] == PASS)
    n_skip = sum(1 for x in r.rows if x[1] == SKIP)
    print("")
    print("%d passed, %d failed, %d skipped" % (n_ok, len(r.failed), n_skip))
    if n_skip and not args.rom:
        print("Pass --rom, --format and --log to run the rest.")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
