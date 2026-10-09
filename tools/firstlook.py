#!/usr/bin/env python3
"""A first look at a cartridge: what it is, what is in it, and what it does.

    python tools/firstlook.py game.a78                 # -> game-firstlook/
    python tools/firstlook.py game.a78 --playback run-01.inp
    python tools/firstlook.py game.a78 --no-live       # the static half only

Everything the toolkit can pull out of an unknown cartridge without being told
anything about it, in one command, as one report (`report.md`, and the same
facts in `firstlook.json`).

  WHAT IT IS      header, mapper, region, sound chip, controllers; whether a
                  format file in formats/ describes this cartridge's music
                  player, or the player's fingerprint if none does; text found
                  in the ROM.
  WHAT IT HOLDS   the artwork and music tables static analysis can find.
  WHAT IT DOES    five short MAME runs, headless, each with one probe:
                    music     captured to a .log, a .trk song and a .wav
                    screens   screenshots at a few moments
                    graphics  the live display list, decoded, with every
                              direct-mode object in ROM rendered as a picture
                    sprites   every ROM address the display lists reference
                    code      which code ran in which bank, merged into a
                              starter annotations.json (tools/dyn.py)

Static analysis alone found 2 graphics blocks and 1 audio table in a 128K
banked cartridge; one 25-second run of the same cartridge found 36 sheets of
graphics, 40 seconds of music, and 438 executed instructions the tracer had
missed. That is why the runs are here.

WHAT IT CANNOT DO. A cartridge that waits at a title screen shows you its title
screen: it presses fire for you (--no-drive to stop that), but real play needs a
recording -- `tools/session.py record`, then `--playback`. Everything the runs
learn is what THIS run did, and the report says so. Needs MAME and a BIOS
(docs/emulation.md); without them you get the static half and a note. Picture
output needs Pillow, and is skipped without it.
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import cart as cartlib      # noqa: E402
import capture              # noqa: E402

PROBES = os.path.join(ROOT, "probes")


class Report(object):
    def __init__(self, title):
        self.title = title
        self.sections = []          # (heading, [lines])
        self.facts = {}             # machine-readable
        self.notes = []

    def add(self, heading, lines):
        self.sections.append((heading, [l for l in lines]))

    def markdown(self):
        out = ["# First look: %s" % self.title, ""]
        if self.notes:
            out += ["> " + n for n in self.notes] + [""]
        for h, lines in self.sections:
            out += ["## " + h, ""] + lines + [""]
        return "\n".join(out)


# ---------------------------------------------------------------- the static half
def identity(c, rom):
    info = c.info or {}
    desc = [l.strip() for l in c.describe().splitlines()]
    mapper = next((l for l in desc if l.startswith("mapper")), "")
    mapper = re.sub(r"^mapper\s+", "", mapper) or "?"
    ctype = next((re.sub(r"^cart type\s+", "", l) for l in desc
                  if l.startswith("cart type")), "")
    lines = [
        "| | |", "|---|---|",
        "| title | %s |" % (info.get("title") or "(none)"),
        "| size | %d bytes of ROM |" % len(c.rom),
        "| mapper | %s |" % mapper,
    ]
    if ctype:
        lines.append("| header | %s |" % ctype)
    lines.append("| region | %s |" % info.get("region", "?"))
    pk = c.pokeys()
    lines.append("| sound | %s |" % (
        ("POKEY at " + ", ".join("$%04X" % b for b in pk)) if pk else "TIA"))
    v = c.vectors()
    lines.append("| vectors | %s |" % "  ".join(
        "%s $%04X" % (k.upper(), a) for k, a in sorted(v.items())))
    return lines, {"title": info.get("title"), "region": info.get("region"),
                   "mapper": mapper, "pokeys": pk,
                   "vectors": {k: a for k, a in v.items()}}


def identify_player(rom, c):
    """Which music player this is, as far as the toolkit knows."""
    out, facts = [], {}
    try:
        import audiotrace
        sig = audiotrace.player_signature(c.rom)
    except Exception:                                        # noqa: BLE001
        sig = None
    facts["player_signature"] = sig
    fmt = None
    try:
        import trackeredit
        fmt = trackeredit.find_format(rom)
    except Exception:                                        # noqa: BLE001
        pass
    if fmt:
        doc = json.load(io.open(fmt, encoding="utf-8"))
        facts["format_file"] = os.path.relpath(fmt, ROOT)
        out.append("**Music player recognised.** `%s` -- %s. "
                   "`python tools/songfmt.py pull %s -f %s` reads its songs "
                   "straight out of the ROM." % (
                       facts["format_file"], doc.get("name", ""), rom,
                       facts["format_file"]))
    elif sig:
        out.append("The code that writes to the sound chip has fingerprint "
                   "`%s`. No file in `formats/` describes it, so the songs "
                   "cannot be read out of the ROM yet; the captured music "
                   "below needs no description at all. `audiotrace.py "
                   "--engine` checks for the Atari in-house engine; "
                   "`explore.py` works a new format out by ear." % sig)
    else:
        out.append("Nothing writes to a sound register by an absolute store the "
                   "scanner knows, so no player fingerprint exists.")
    return out, facts


def looks_like_text(t):
    """Words, not table bytes that happen to be printable."""
    if len(t) < 6:
        return False
    letters = sum(ch.isalpha() or ch == " " for ch in t)
    vowels = sum(ch in "AEIOUaeiou" for ch in t)
    commonest = max(t.count(ch) for ch in set(t))
    return (letters >= 0.8 * len(t) and len(set(t)) >= 5 and vowels >= 2
            and commonest <= 0.4 * len(t))


def strings_found(c, limit=12):
    """Readable text, longest first, once each however many banks repeat it."""
    import survey
    found, seen = [], set()
    for sp in c.spaces():
        blk = bytes(c.slice(sp, c.base_of(sp), c.size_of(sp)))
        for off, t in survey.strings(blk, 6):
            if t not in seen and looks_like_text(t.strip()):
                seen.add(t)
                found.append((len(t), sp, c.base_of(sp) + off, t))
    found.sort(reverse=True)
    out = ["- `%s:%04X`  %s" % (sp, addr, t[:60]) for _n, sp, addr, t in found[:limit]]
    if not out:
        out.append("No readable ASCII words. Text in a tile-based game is not "
                   "ASCII -- the alphabet is whatever order the tiles were drawn "
                   "in (docs/pitfalls.md); `survey.py --strings` also tries the "
                   "bit-7 form, and a custom alphabet is found by looking at the "
                   "screen text below against the character set.")
    return out, {"strings": len(found)}


def static_assets(rom):
    p = subprocess.run([sys.executable, os.path.join(HERE, "assets.py"), rom],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace").strip().splitlines()
    keep = [l for l in text if l.strip()][:40]
    return ["```"] + keep + ["```"]


# ---------------------------------------------------------------- MAME
class Mame(object):
    def __init__(self, rom, args):
        self.rom = os.path.abspath(rom)
        self.exe = capture.find_mame(args.mame)
        self.roms = capture.find_rompath(rom, args.rompath)
        self.machine = capture.inspect(rom)["machine"]
        self.playback = args.playback
        self.problem = None
        if not self.exe:
            self.problem = "MAME was not found (set A7800_MAME or pass --mame)"
        elif not self.roms:
            self.problem = ("the 7800 BIOS was not found (set A7800_ROMPATH, and "
                            "A7800_BIOS=a7800pr for the open BIOS; "
                            "docs/emulation.md)")

    def run(self, probe, seconds, workdir, env=None, extra=()):
        """Run one probe headless; return MAME's output text."""
        bios, roms = capture.machine_setup(self.machine, self.roms)
        cmd = [self.exe, self.machine] + bios + [
            "-rompath", roms, "-cart", self.rom, "-video", "none",
            "-sound", "none", "-skip_gameinfo", "-nothrottle",
            "-seconds_to_run", str(int(seconds))]
        if self.playback:
            pb = os.path.abspath(self.playback)
            cmd += ["-input_directory", os.path.dirname(pb), "-playback",
                    os.path.basename(pb), "-exit_after_playback"]
        cmd += ["-autoboot_script", os.path.join(PROBES, probe)] + list(extra)
        e = dict(os.environ)
        e.setdefault("XDG_RUNTIME_DIR", workdir)
        e.setdefault("SDL_AUDIODRIVER", "dummy")
        e.update(env or {})
        p = subprocess.run(cmd, cwd=workdir, env=e, timeout=1800,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.stdout.decode("utf-8", "replace")


def step_music(rom, mame, out, c, args, rep):
    import tracker
    stem = os.path.join(out, "music")
    os.makedirs(stem, exist_ok=True)
    log = os.path.join(stem, "capture.log")
    trk = os.path.join(stem, "song.trk")
    song = None
    if mame.playback:
        info = capture.inspect(rom)
        env = {"A7800_AUDIO_LOG": log, "A7800_AUDIO_FRAMES": "999999"}
        if info["pokeys"]:
            env["A7800_POKEY"] = ",".join("0x%04X" % b for b in info["pokeys"])
        mame.run("audio.lua", args.seconds, out, env)
        song = tracker.read_capture(log, info["region"])
    else:
        r = capture.capture(rom, trk, args.seconds, None, 0, not args.no_drive,
                            args.mame, args.rompath, log, quiet=True)
        song = r["song"]
    return describe_music(song, stem, trk, c, rep)


def describe_music(song, stem, trk, c, rep):
    """Write the song, render it, and say what it holds."""
    import tracker
    io.open(trk, "w", encoding="utf-8").write(tracker.dump(song))
    voiced = sum(1 for row in song.rows if any(x for x in row))
    lines = ["%d frames captured, %d with a change in the sound registers."
             % (len(song), voiced),
             "`music/capture.log` is the register log; `music/song.trk` opens in "
             "`tools/trackeredit.py`."]
    wav = os.path.join(stem, "song.wav")
    try:
        tracker.render(song, wav)
        lines.append("`music/song.wav` is that song rendered (%.0f s)."
                     % (len(song) / (50.0 if c.info.get("region") == "PAL" else 60.0)))
    except Exception as e:                                   # noqa: BLE001
        lines.append("Not rendered to audio: %s" % e)
    if voiced <= 2:
        lines.append("**Almost nothing changed.** The game probably waits for "
                     "input this run did not give it; record a session and use "
                     "`--playback`.")
    rep.facts["music"] = {"frames": len(song), "changes": voiced}
    return lines


def step_screens(mame, out, args, rep):
    shots = os.path.join(out, "screens")
    os.makedirs(shots, exist_ok=True)
    when = sorted({max(2, int(args.seconds * f)) for f in (0.15, 0.5, 0.9)})
    names = []
    for i, t in enumerate(when):
        d = os.path.join(shots, "t%02d" % t)
        os.makedirs(d, exist_ok=True)
        mame.run("snapstop.lua", t, out, extra=["-snapshot_directory", d])
        pngs = sorted(os.path.join(root, f) for root, _dirs, files in os.walk(d)
                      for f in files if f.endswith(".png"))
        if pngs:                      # MAME may write more than one; the last is ours
            dst = os.path.join(shots, "at-%02ds.png" % t)
            shutil.move(pngs[-1], dst)
            names.append(os.path.relpath(dst, out))
        shutil.rmtree(d, True)
    rep.facts["screens"] = names
    if not names:
        return ["No screenshot was written."]
    return ["Taken at %s seconds into the run (headless, so a coarse look, not a "
            "measurement -- docs/pitfalls.md on `-video none` snapshots):" %
            ", ".join(str(t) for t in when), ""] + \
           ["![%s](%s)" % (n, n.replace(os.sep, "/")) for n in names]


def parse_regs(path):
    return parse_regs_text(io.open(path, encoding="utf-8").read())


def parse_regs_text(text):
    d = {"regs": {}}
    for ln in text.splitlines():
        ln = ln.strip()
        m = re.match(r"frame (\d+)", ln)
        if m:
            d["frame"] = int(m.group(1))
        m = re.match(r"bank (\d+)", ln)
        if m:
            d["bank"] = int(m.group(1))
        m = re.match(r"DPPH=\$(\w+) DPPL=\$(\w+)", ln)
        if m:
            d["dll"] = int(m.group(1), 16) * 256 + int(m.group(2), 16)
        m = re.match(r"CHARBASE=\$(\w+) OFFSET=\$(\w+) CTRL=\$(\w+) BACKGRND=\$(\w+)", ln)
        if m:
            d["charbase"], d["ctrl"] = int(m.group(1), 16), int(m.group(3), 16)
            d["regs"][0x20] = int(m.group(4), 16)
        m = re.match(r"\$(\w\w) = \$(\w\w)", ln)
        if m:
            d["regs"][int(m.group(1), 16)] = int(m.group(2), 16)
        m = re.match(r"s \$(\w\w) = \$(\w\w)", ln)
        if m:
            d.setdefault("start", {})[int(m.group(1), 16)] = int(m.group(2), 16)
        m = re.match(r"w (\d+) \$(\w\w) = \$(\w\w)", ln)
        if m:
            d.setdefault("writes", []).append(
                (int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16)))
    return d


class Screen(object):
    """Rebuild what MARIA draws from a RAM dump, the registers, and the ROM."""

    def __init__(self, c, ram, d):
        import dlwalk
        # MARIA fetches lists and graphics from its own half of a bankset cartridge
        self.c, self.d, self.dlwalk = c.for_maria(), d, dlwalk
        self.ram = ram
        self.skipped = {}                   # (read mode, write mode) -> zone count
        # display lists, character lists and graphics may be in RAM or in ROM
        # (several zones of a real game keep their lists in the fixed bank)
        self.src = type("Mem", (), {"byte": staticmethod(self.byte)})

    def byte(self, addr):
        addr = self.dlwalk.unmirror(addr)
        if 0x1800 <= addr <= 0x27FF:
            return self.ram[addr - 0x1800]
        sp = self.c.space_of(addr, bank=self.d.get("bank"))
        return self.c.byte(sp, addr) if sp else 0

    def rgb(self, palette, colour, st=None):
        import palette as pal
        reg = 0x21 + 4 * palette + colour - 1
        return pal.mame7800((st or self.d["regs"]).get(reg, 0))

    def states(self, zones):
        """The MARIA registers each zone was drawn with.

        The dump holds the registers as they stood when the frame began and every
        write in it, each tagged with how many display-list interrupts had fired
        before it. A zone is drawn with the start state plus the writes made
        after as many interrupts as there were DLI zones above it. Without the
        per-write record (an older dump) every zone gets the final registers.
        """
        base = dict(self.d.get("start") or self.d["regs"])
        writes = self.d.get("writes") or []
        out, fired = [], 0
        for _y, z in zones:
            if z["dli"]:
                fired += 1              # the handler runs before this zone
            st = dict(base)
            for n, a, v in writes:
                if n <= fired:
                    st[a] = v
            out.append(st)
        return out

    def zones(self):
        total = 0
        for i in range(40):
            z = self.dlwalk.decode_dll_entry(self.src, self.d["dll"] + 3 * i)
            if z["dl"] == 0 or total >= 242:
                break
            yield total, z
            total += z["lines"]

    def rows(self, e, lines, wm, st=None):
        """[(palette, colour)] per scanline of one entry, top line first."""
        import mariapix
        st = st or self.d["regs"]
        ctrl = st.get(0x3C, self.d["ctrl"])
        charbase = st.get(0x34, self.d["charbase"])
        fmt = mariapix.pixel_format(ctrl & 3, wm)
        if fmt is None:
            return None
        two = bool(ctrl & 0x10)
        out = []
        for ln in range(lines):
            page = lines - 1 - ln                  # MARIA counts the offset down
            if e["indirect"]:
                data = []
                for k in range(e["width"]):
                    code = self.src.byte(e["gfx"] + k)
                    a = (((charbase + page) & 0xFF) << 8) | code
                    data.append(self.byte(a))
                    if two:
                        data.append(self.byte(a + 1))
            else:
                data = [self.byte((e["gfx"] + page * 256 + k) & 0xFFFF)
                        for k in range(e["width"])]
            out.append(mariapix.row_pixels(fmt, data, e["palette"]))
        return out

    def objects(self):
        """Every distinct entry the display list holds: (entry, lines, wm)."""
        wm = 0
        seen, found = set(), []
        for _y, z in self.zones():
            try:
                entries = self.dlwalk.walk_dl(self.src, z["dl"])
            except IndexError:
                continue
            for e in entries:
                if e.get("write_mode") is not None:
                    wm = e["write_mode"]
                key = (e["gfx"], e["width"], z["lines"], e["palette"], e["indirect"], wm)
                if key not in seen:
                    seen.add(key)
                    found.append((e, z["lines"], wm))
        return found

    def image(self):
        """The reconstructed screen as a PIL image, or (None, why)."""
        from PIL import Image
        import palette as pal
        zones = list(self.zones())
        if not zones:
            return None, "no zones in the display list list"
        states = self.states(zones)
        height = zones[-1][0] + zones[-1][1]["lines"]
        img = Image.new("RGB", (320, height))
        px = img.load()
        import mariapix
        wm = 0
        for (y, z), st in zip(zones, states):
            bg = pal.mame7800(st.get(0x20, 0))
            for ln in range(z["lines"]):
                for x in range(320):
                    px[x, y + ln] = bg
            try:
                entries = self.dlwalk.walk_dl(self.src, z["dl"])
            except IndexError:
                continue
            for e in entries:
                if e.get("write_mode") is not None:
                    wm = e["write_mode"]
                rows = self.rows(e, z["lines"], wm, st)
                if rows is None:
                    key = (st.get(0x3C, 0) & 3, wm)
                    self.skipped[key] = self.skipped.get(key, 0) + 1
                    continue
                fmt = mariapix.pixel_format(st.get(0x3C, 0) & 3, wm)
                w = mariapix.width(fmt)
                for ln, row in enumerate(rows):
                    for j, (p, colour) in enumerate(row):
                        if not colour:
                            continue
                        for k in range(w):
                            x = e["hpos"] * 2 + j * w + k
                            if 0 <= x < 320 and y + ln < height:
                                px[x, y + ln] = self.rgb(p, colour, st)
        return img, None


def step_graphics(rom, mame, out, c, args, rep):
    gdir = os.path.join(out, "graphics")
    os.makedirs(gdir, exist_ok=True)
    ram = os.path.join(gdir, "ram.bin")
    regs = os.path.join(gdir, "regs.txt")
    env = {"A7800_GFX_RAM": ram, "A7800_GFX_REGS": regs,
           "A7800_GFX_AT": str(args.graphics_at), "A7800_GFX_DELAY": "5"}
    if not args.no_drive and not mame.playback:
        env["A7800_GFX_SELECT"] = "1"
    if c.nbanks > 1:
        env["A7800_GFX_BANKSEL"] = "8000-BFFF"
        env["A7800_GFX_BANKS"] = str(c.nbanks)
    mame.run("dumpgfx.lua", args.graphics_at / 20.0 + 15, out, env)
    if not (os.path.exists(ram) and os.path.exists(regs)):
        return ["The graphics run wrote no dump."]
    d = parse_regs(regs)
    scr = Screen(c, io.open(ram, "rb").read(), d)
    return describe_graphics(c, gdir, scr, d, rep)


def describe_graphics(c, gdir, scr, d, rep):
    """The graphics section, from a dump already read into `scr` (a Screen)."""
    import mariapix
    objs = scr.objects()
    direct = [o for o in objs if not o[0]["indirect"] and o[0]["gfx"] >= 0x4000]
    chars = [o for o in objs if o[0]["indirect"]]
    wmodes = sorted({wm for _e, _l, wm in objs})
    lines = ["Display list list at `$%04X` at frame %d; CTRL `$%02X`, CHARBASE `$%02X`%s."
             % (d["dll"], d["frame"], d["ctrl"], d["charbase"],
                "" if "bank" not in d else
                "; bank %d was selected at that moment" % d["bank"]),
             "%d direct-mode objects in ROM, %d character-mode entries "
             "(%s per character)." % (len(direct), len(chars),
                                      "two bytes" if d["ctrl"] & 0x10 else "one byte")]
    fmt = [mariapix.pixel_format(d["ctrl"] & 3, wm) for wm in (wmodes or [0])]
    lines.append("Pixel format: %s (CTRL read mode %d, header write mode %s)." % (
        "/".join(sorted({f or "a 320 mode (not decoded)" for f in fmt})),
        d["ctrl"] & 3, ", ".join(map(str, wmodes)) or "never set"))
    rep.facts["graphics"] = {
        "direct_objects": len(direct), "char_entries": len(chars),
        "read_mode": d["ctrl"] & 3, "write_modes": wmodes,
        "two_byte_chars": bool(d["ctrl"] & 0x10),
        "formats": sorted({f for f in fmt if f})}
    try:
        from PIL import Image
    except ImportError:
        lines.append("Pictures skipped: Pillow is not installed "
                     "(`python -m pip install pillow`).")
        return lines
    img, why = scr.image()
    if img is None:
        lines.append("Screen not rebuilt: %s." % why)
        return lines
    big = img.resize((img.width * 2, img.height * 2), Image.NEAREST)
    big.save(os.path.join(gdir, "screen.png"))
    zl = [z for _y, z in scr.zones()]
    dli = sum(1 for z in zl if z["dli"])
    nw = len(d.get("writes") or [])
    lines += ["", "![rebuilt screen](graphics/screen.png)", "",
              "The screen as the display list, character sets and graphics in "
              "the ROM describe it at that frame -- a reconstruction, not a "
              "screenshot."]
    if d.get("writes") is not None and dli:
        lines.append(
            "%d of %d zones raise a display-list interrupt; the %d MARIA register "
            "writes made during the frame were applied zone by zone (palettes, "
            "CHARBASE and CTRL as each zone was drawn)." % (dli, len(zl), nw))
    elif dli:
        lines.append(
            "**%d of %d zones raise a display-list interrupt**, and this dump has "
            "no per-write record, so every zone is drawn with the final "
            "registers: zones below the first interrupt may be wrong." % (dli, len(zl)))
    if scr.skipped:
        lines.append("**Left blank: %s** -- 320-pixel formats that are not decoded "
                     "(`mariapix.py`)." % ", ".join(
                         "%d entries with read mode %d, write mode %d" % (n, rm, wm)
                         for (rm, wm), n in sorted(scr.skipped.items())))
    lines.append("Not modelled: a register written by the main program at an "
                 "unknown moment inside the frame, holey DMA, and the 320 modes "
                 "other than 320A.")
    tiles = []
    for e, ln, wm in direct[:64]:
        rows = scr.rows(e, ln, wm)
        if not rows:
            continue
        w = len(rows[0])
        if w == 0:
            continue
        im = Image.new("RGB", (w, ln), (255, 0, 255))
        ip = im.load()
        for y, row in enumerate(rows):
            for x, (p, colour) in enumerate(row):
                if colour:
                    ip[x, y] = scr.rgb(p, colour)
        name = "obj-%04X-%dx%d-pal%d.png" % (e["gfx"], e["width"], ln, e["palette"])
        im.resize((w * 4, ln * 4), Image.NEAREST).save(os.path.join(gdir, name))
        tiles.append(im)
    if tiles:
        cell = max(max(t.width, t.height) for t in tiles)
        cols = 8
        sheet = Image.new("RGB", (cols * (cell + 4), ((len(tiles) + cols - 1) // cols) * (cell + 4)),
                          (40, 40, 60))
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % cols) * (cell + 4) + 2, (i // cols) * (cell + 4) + 2))
        sheet.resize((sheet.width * 3, sheet.height * 3), Image.NEAREST).save(
            os.path.join(gdir, "contact.png"))
        lines += ["", "![objects](graphics/contact.png)", "",
                  "%d direct-mode objects, one file each in `graphics/` (pink is "
                  "transparent)." % len(tiles)]
    return lines


def step_sprites(mame, out, args, rep):
    log = os.path.join(out, "graphics", "liveslots.json")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    mame.run("liveslots.lua", args.seconds, out, {"A7800_OUT": log})
    if not os.path.exists(log):
        return ["The run wrote no list."]
    refs = sorted(json.load(io.open(log, encoding="utf-8"))["refs"],
                  key=lambda r: r["addr"])
    return describe_slots(refs, args.seconds, rep)


def describe_slots(refs, seconds, rep):
    rep.facts["sprite_refs"] = len(refs)
    runs, cur = [], []
    for r in refs:
        if cur and (r["addr"] - cur[-1]["addr"] != cur[-1]["width"]
                    or r["width"] != cur[-1]["width"]):
            runs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    lines = ["%d ROM addresses were drawn from in %d seconds (`graphics/"
             "liveslots.json`); %d of them in runs at a constant stride, "
             "which is what a sprite sheet looks like:" %
             (len(refs), seconds, sum(len(r) for r in runs if len(r) > 2))]
    for r in runs:
        if len(r) > 2:
            lines.append("- `$%04X`-`$%04X`: %d objects, %d bytes wide" % (
                r[0]["addr"], r[-1]["addr"], len(r), r[0]["width"]))
    return lines


def step_code(rom, mame, out, c, args, rep):
    log = os.path.join(out, "code", "exectrace.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    env = {"A7800_XT_LOG": log, "A7800_XT_BANKS": str(max(c.nbanks, 1))}
    mame.run("exectrace.lua", args.code_seconds, out, env)
    if not os.path.exists(log):
        return ["The run wrote no log."]
    return merge_code(rom, out, c, log, rep)


def merge_code(rom, out, c, log, rep):
    """Turn an exectrace log into a starter annotations.json and say what it found."""
    import dyn
    ann = os.path.join(out, "annotations.json")
    import init as initmod
    doc = initmod.build(c, rom)
    parsed = dyn.parse_log(io.open(log, encoding="utf-8").read())
    merged, lines = dyn.apply(rom, doc, parsed, "exectrace.log")
    if merged is None:
        return ["The merge failed: " + "; ".join(lines)]
    with io.open(ann, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    rep.facts["code"] = merged.get("_dynamic", {}).get("exectrace.log", {})
    return ["`annotations.json` is a starter file with what the run observed "
            "(marked observed, not proven):", ""] + ["- " + l.strip() for l in lines] + [
            "", "Next: copy that file to `annotations.json` beside your listing folder "
            "(or let `python tools/workbench.py` do it: it adopts it into its project), "
            "then `python tools/disasm.py %s -c annotations.json -o src` and "
            "`python tools/verify.py %s -d src`. The starter file is `%s`." % (
                '"%s"' % rom, '"%s"' % rom, ann)]


# ------------------------------------------------------- the simulator as the engine
class SimEngine(object):
    """The same five questions answered by sim.py: no emulator, no BIOS.

    One run of the cartridge in the simulator serves every step, where MAME is started
    once per probe: the music is the sound-chip writes, the screens are rebuilt from the
    display list and RAM at three moments, the graphics section reads the RAM and MARIA
    registers at `--graphics-at`, the sprite list is every graphics address the display
    list pointed at, and the code is every instruction that ran. See simprobe.py for how
    far to trust it: it is what `sim.py` scores against MAME, and a cartridge that waits
    on hardware it does not model shows as `stalled` (it never reached a display list).
    """

    SETTLE = 40            # frames before the sprite sampler starts (boot noise)

    def __init__(self, rom, c, args):
        import simprobe
        self.rom, self.c, self.args = rom, c, args
        self.region = ((c.info or {}).get("region", "NTSC")).lower()
        self.fps = 50 if self.region == "pal" else 60
        self.frames = max(180, args.seconds * self.fps)
        self.shots = sorted({max(30, int(self.frames * f)) for f in (0.15, 0.5, 0.9)})
        self.gfx_at = min(args.graphics_at, self.frames)
        self.refs = {}
        self.col = None
        self.bus = None
        self.problem = None
        self._simprobe = simprobe

    def collect(self):
        import sim
        simprobe = self._simprobe
        se = self

        class Col(simprobe.Collector):
            def frame_start(self, frame):
                simprobe.Collector.frame_start(self, frame)
                if frame >= se.SETTLE and frame % 15 == 0:
                    se._sample(self)

        self.col = Col(dump_frame=self.gfx_at, snap_frames=self.shots)
        self.cart = self.c
        self.bus = sim.run(self.c, self.frames, self.region, drive=not self.args.no_drive,
                           observer=self.col)
        self.col.finish()

    @property
    def stalled(self):
        return self.col.frames_with_list < self.frames * 0.5

    def _sample(self, col):
        bus = col.bus
        if bus.dpph is None or (bus.ctrl & 0x60) != 0x40:
            return
        d = parse_regs_text(self._simprobe.regs_text(bus.frame, bus, {}, []))
        scr = Screen(self.c, self._simprobe.ram_bytes(bus), d)
        try:
            objs = scr.objects()
        except Exception:                                    # noqa: BLE001
            return
        for e, _lines, _wm in objs:
            if e["gfx"] >= 0x4000:
                self.refs[e["gfx"]] = max(self.refs.get(e["gfx"], 0), e["width"])

    # the steps, in the same shapes the MAME ones return
    def music(self, out, rep):
        import tracker
        stem = os.path.join(out, "music")
        os.makedirs(stem, exist_ok=True)
        log = os.path.join(stem, "capture.log")
        import sim
        sim.write_log(self.bus, self.c, log, self.region)
        song = tracker.read_capture(log, self.region)
        return describe_music(song, stem, os.path.join(stem, "song.trk"), self.c, rep)

    def screens(self, out, rep):
        try:
            from PIL import Image                           # noqa: F401
        except ImportError:
            return ["Pictures skipped: Pillow is not installed "
                    "(`python -m pip install pillow`)."]
        shots = os.path.join(out, "screens")
        os.makedirs(shots, exist_ok=True)
        names = []
        for f in self.shots:
            snap = self.col.snaps.get(f)
            if not snap:
                continue
            d = parse_regs_text(snap["regs"])
            if "dll" not in d or d["dll"] is None:
                continue
            scr = Screen(self.c, snap["ram"], d)
            img, why = scr.image()
            if img is None:
                continue
            dst = os.path.join(shots, "at-f%05d.png" % f)
            img.resize((img.width * 2, img.height * 2), Image.NEAREST).save(dst)
            names.append(os.path.relpath(dst, out))
        rep.facts["screens"] = names
        if not names:
            return ["No screen could be rebuilt: the simulated run never pointed "
                    "MARIA at a display list at those moments."]
        return ["Rebuilt from the display list, the character sets and the graphics in "
                "the ROM at frames %s of the simulated run (a reconstruction, not "
                "a screenshot: no emulator ran):" % ", ".join(
                    str(f) for f in self.shots), ""] + \
               ["![%s](%s)" % (n, n.replace(os.sep, "/")) for n in names]

    def graphics(self, out, rep):
        snap = self.col.snaps.get(self.gfx_at)
        gdir = os.path.join(out, "graphics")
        os.makedirs(gdir, exist_ok=True)
        if not snap:
            return ["The simulated run wrote no dump."]
        ram, regs = os.path.join(gdir, "ram.bin"), os.path.join(gdir, "regs.txt")
        io.open(ram, "wb").write(snap["ram"])
        io.open(regs, "w", encoding="utf-8").write(snap["regs"])
        d = parse_regs_text(snap["regs"])
        if d.get("dll") is None:
            return ["The simulated run never pointed MARIA at a display list."]
        return describe_graphics(self.c, gdir, Screen(self.c, snap["ram"], d), d, rep)

    def sprites(self, out, rep):
        gdir = os.path.join(out, "graphics")
        os.makedirs(gdir, exist_ok=True)
        refs = [{"addr": a, "width": w} for a, w in sorted(self.refs.items())]
        io.open(os.path.join(gdir, "liveslots.json"), "w", encoding="utf-8").write(
            json.dumps({"frames": self.frames, "refs": refs}))
        return describe_slots(refs, self.frames // self.fps, rep)

    def code(self, rom, out, rep):
        log = os.path.join(out, "code", "exectrace.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        self._simprobe.write_exectrace(self.col, log)
        return merge_code(rom, out, self.c, log, rep)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-o", "--out", help="output folder (default <rom>-firstlook)")
    ap.add_argument("--seconds", type=int, default=30,
                    help="how long each music/sprite/screen run lasts (default 30)")
    ap.add_argument("--code-seconds", type=int, default=20,
                    help="the code trace is slow (about 4 s per emulated second); "
                         "default 20")
    ap.add_argument("--graphics-at", type=int, default=600,
                    help="frame at which to dump the display list (default 600)")
    ap.add_argument("--playback", help="a .inp recording to run every probe over")
    ap.add_argument("--no-drive", action="store_true",
                    help="do not press fire/Select to get past a title screen")
    ap.add_argument("--no-live", action="store_true", help="the static half only")
    ap.add_argument("--skip", action="append", default=[],
                    choices=["music", "screens", "graphics", "sprites", "code"],
                    help="leave a run out")
    ap.add_argument("--engine", choices=["auto", "sim", "mame"], default="auto",
                    help="what runs the cartridge: the simulator (no emulator needed), "
                         "MAME, or auto: the simulator, falling back to MAME when the "
                         "simulator stalls and MAME is there (default). --playback "
                         "needs MAME.")
    ap.add_argument("--mame")
    ap.add_argument("--rompath")
    args = ap.parse_args(argv)

    rom = args.rom
    if not os.path.isfile(rom):
        sys.exit("firstlook: no such cartridge: %s" % rom)
    try:
        c = cartlib.Cart(rom)
    except (cartlib.UnknownMapper, cartlib.UnknownSpace) as e:
        sys.exit("firstlook: %s" % e)
    out = os.path.abspath(args.out or os.path.splitext(rom)[0] + "-firstlook")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()

    rep = Report(os.path.basename(rom))
    lines, rep.facts["identity"] = identity(c, rom)
    rep.add("What it is", lines)
    pl, f = identify_player(rom, c)
    rep.facts.update(f)
    rep.add("Music player", pl)
    sl, f = strings_found(c)
    rep.facts.update(f)
    rep.add("Text in the ROM", sl)
    rep.add("What static analysis finds", static_assets(rom))

    steps = []
    engine = args.engine
    if args.playback and engine == "sim":
        sys.exit("firstlook: --playback needs MAME (the simulator does not replay "
                 "recordings); use --engine mame")
    if args.playback:
        engine = "mame"
    if not args.no_live:
        mame = None
        if engine in ("mame", "auto"):
            mame = Mame(rom, args)
        sim_engine = None
        if engine in ("sim", "auto"):
            sim_engine = SimEngine(rom, c, args)
            print("  simulating %d frames ..." % sim_engine.frames, end=" ", flush=True)
            t = time.time()
            sim_engine.collect()
            print("%.0fs" % (time.time() - t))
            if sim_engine.stalled:
                if engine == "auto" and mame and not mame.problem:
                    rep.notes.append(
                        "The simulator stalled (a display list on %d of %d frames: the "
                        "cartridge waits on hardware it does not model), so MAME ran "
                        "instead." % (sim_engine.col.frames_with_list, sim_engine.frames))
                    sim_engine = None
                else:
                    rep.notes.append(
                        "The simulator stalled: a display list on only %d of %d frames, "
                        "so the live sections below show an early stage of the "
                        "cartridge, if anything. Try --engine mame." % (
                            sim_engine.col.frames_with_list, sim_engine.frames))
        if sim_engine is not None:
            rep.facts["engine"] = "sim"
            rep.notes.append(
                "The live sections were produced by the simulator (no emulator ran). It is a "
                "TIA tool: the music of a cartridge with POKEY or YM2151 is NOT validated "
                "(Commando agrees with MAME 18%%), and it models no paddles, lightgun or IRQ.%s "
                "`--engine mame` runs MAME instead; `python tools/mamecheck.py` measures where "
                "the two disagree." % (
                    " THIS cartridge has POKEY: treat its music section as unreliable."
                    if c.pokeys() else ""))
            if getattr(sim_engine, "bus", None) is not None and \
                    getattr(sim_engine.bus, "jammed", None) is not None:
                rep.notes.append("The program ran a KIL opcode at $%04X and stopped itself "
                                 "(an error trap), so the run ends there."
                                 % sim_engine.bus.jammed)
            runs = [
                ("music", "What it sounds like", lambda: sim_engine.music(out, rep)),
                ("screens", "What it looks like", lambda: sim_engine.screens(out, rep)),
                ("graphics", "The artwork on screen", lambda: sim_engine.graphics(out, rep)),
                ("sprites", "Every graphics address it drew from",
                 lambda: sim_engine.sprites(out, rep)),
                ("code", "The code that ran", lambda: sim_engine.code(rom, out, rep)),
            ]
        elif mame is None or mame.problem:
            runs = []
            rep.notes.append("The live half did not run: %s." % (
                mame.problem if mame else "no engine"))
        else:
            if mame.playback and not os.path.isfile(mame.playback):
                sys.exit("firstlook: no such recording: %s" % mame.playback)
            rep.facts["engine"] = "mame"
            runs = [
                ("music", "What it sounds like",
                 lambda: step_music(rom, mame, out, c, args, rep)),
                ("screens", "What it looks like",
                 lambda: step_screens(mame, out, args, rep)),
                ("graphics", "The artwork on screen",
                 lambda: step_graphics(rom, mame, out, c, args, rep)),
                ("sprites", "Every graphics address it drew from",
                 lambda: step_sprites(mame, out, args, rep)),
                ("code", "The code that ran",
                 lambda: step_code(rom, mame, out, c, args, rep)),
            ]
        for key, heading, fn in runs:
            if key in args.skip:
                continue
            t = time.time()
            print("  %-9s ..." % key, end=" ", flush=True)
            try:
                body = fn()
                print("%.0fs" % (time.time() - t))
            except Exception as e:                           # noqa: BLE001
                body = ["Did not complete: %s: %s" % (type(e).__name__, e)]
                print("failed (%s)" % e)
            rep.add(heading, body)
            steps.append(key)
    rep.notes.append(
        "What the live sections show is what ONE run did -- %s. Nothing here is "
        "a complete inventory." % (
            "a replay of %s" % os.path.basename(args.playback) if args.playback
            else "the first %d seconds, with fire pressed now and then"
            % args.seconds if not args.no_drive
            else "the first %d seconds with no input" % args.seconds))
    rep.facts["steps"] = steps
    rep.facts["seconds"] = round(time.time() - t0)
    with io.open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(rep.markdown())
    with io.open(os.path.join(out, "firstlook.json"), "w", encoding="utf-8") as f:
        json.dump(rep.facts, f, indent=2, sort_keys=True, default=list)
        f.write("\n")
    print("wrote %s/report.md (%d s)" % (out, rep.facts["seconds"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
