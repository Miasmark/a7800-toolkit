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
        cmd = [self.exe, self.machine] + capture.bios_args() + [
            "-rompath", self.roms, "-cart", self.rom, "-video", "none",
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
    if mame.playback:
        info = capture.inspect(rom)
        env = {"A7800_AUDIO_LOG": log, "A7800_AUDIO_FRAMES": "999999"}
        if info["pokeys"]:
            env["A7800_POKEY"] = ",".join("0x%04X" % b for b in info["pokeys"])
        mame.run("audio.lua", args.seconds, out, env)
        song = tracker.read_capture(log, info["region"])
        io.open(trk, "w", encoding="utf-8").write(tracker.dump(song))
    else:
        r = capture.capture(rom, trk, args.seconds, None, 0, not args.no_drive,
                            args.mame, args.rompath, log, quiet=True)
        song = r["song"]
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
    d = {"regs": {}}
    for ln in io.open(path, encoding="utf-8"):
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
    return d


class Screen(object):
    """Rebuild what MARIA draws from a RAM dump, the registers, and the ROM."""

    def __init__(self, c, ram, d):
        import dlwalk
        self.c, self.d, self.dlwalk = c, d, dlwalk
        self.ram = ram
        # display lists, character lists and graphics may be in RAM or in ROM
        # (several zones of a real game keep their lists in the fixed bank)
        self.src = type("Mem", (), {"byte": staticmethod(self.byte)})

    def byte(self, addr):
        addr = self.dlwalk.unmirror(addr)
        if 0x1800 <= addr <= 0x27FF:
            return self.ram[addr - 0x1800]
        sp = self.c.space_of(addr, bank=self.d.get("bank"))
        return self.c.byte(sp, addr) if sp else 0

    def rgb(self, palette, colour):
        import palette as pal
        reg = 0x21 + 4 * palette + colour - 1
        return pal.mame7800(self.d["regs"].get(reg, 0))

    def zones(self):
        total = 0
        for i in range(40):
            z = self.dlwalk.decode_dll_entry(self.src, self.d["dll"] + 3 * i)
            if z["dl"] == 0 or total >= 242:
                break
            yield total, z
            total += z["lines"]

    def rows(self, e, lines, wm):
        """[(palette, colour)] per scanline of one entry, top line first."""
        import mariapix
        fmt = mariapix.pixel_format(self.d["ctrl"] & 3, wm)
        if fmt is None:
            return None
        two = bool(self.d["ctrl"] & 0x10)
        out = []
        for ln in range(lines):
            page = lines - 1 - ln                  # MARIA counts the offset down
            if e["indirect"]:
                data = []
                for k in range(e["width"]):
                    code = self.src.byte(e["gfx"] + k)
                    a = (((self.d["charbase"] + page) & 0xFF) << 8) | code
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
        if self.d["ctrl"] & 3:
            return None, ("CTRL read mode %d is a 320-pixel mode, which is not "
                          "decoded" % (self.d["ctrl"] & 3))
        zones = list(self.zones())
        if not zones:
            return None, "no zones in the display list list"
        height = zones[-1][0] + zones[-1][1]["lines"]
        img = Image.new("RGB", (160, height), pal.mame7800(self.d["regs"].get(0x20, 0)))
        px = img.load()
        wm = 0
        for y, z in zones:
            try:
                entries = self.dlwalk.walk_dl(self.src, z["dl"])
            except IndexError:
                continue
            for e in entries:
                if e.get("write_mode") is not None:
                    wm = e["write_mode"]
                rows = self.rows(e, z["lines"], wm)
                if rows is None:
                    return None, "unsupported pixel format"
                for ln, row in enumerate(rows):
                    for j, (p, colour) in enumerate(row):
                        x = e["hpos"] + j
                        if colour and 0 <= x < 160 and y + ln < height:
                            px[x, y + ln] = self.rgb(p, colour)
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
    big = img.resize((img.width * 4, img.height * 2), Image.NEAREST)
    big.save(os.path.join(gdir, "screen.png"))
    zl = [z for _y, z in scr.zones()]
    dli = sum(1 for z in zl if z["dli"])
    lines += ["", "![rebuilt screen](graphics/screen.png)", "",
              "The screen as the display list, character sets and graphics in "
              "the ROM describe it at that frame -- a reconstruction, not a "
              "screenshot. The registers are as they stood at the dump, once for "
              "the whole screen."]
    if dli:
        lines.append(
            "**%d of %d zones raise a display-list interrupt**, which is where a "
            "game repaints palettes and switches CHARBASE partway down the "
            "screen. Zones drawn after the first one may be wrong here -- wrong "
            "colours, or another font's characters read from this one's pages "
            "(the garbled rows below). Compare with the screenshots above; "
            "recording the registers per zone is not done." % (dli, len(zl)))
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
             (len(refs), args.seconds, sum(len(r) for r in runs if len(r) > 2))]
    for r in runs:
        if len(r) > 2:
            lines.append("- `$%04X`-`$%04X`: %d objects, %d bytes wide" % (
                r[0]["addr"], r[-1]["addr"], len(r), r[0]["width"]))
    return lines


def step_code(rom, mame, out, c, args, rep):
    import dyn
    log = os.path.join(out, "code", "exectrace.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    env = {"A7800_XT_LOG": log, "A7800_XT_BANKS": str(max(c.nbanks, 1))}
    mame.run("exectrace.lua", args.code_seconds, out, env)
    if not os.path.exists(log):
        return ["The run wrote no log."]
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
            "", "Next: `python tools/disasm.py %s -c %s -o src` then "
            "`python tools/verify.py %s -d src`." % (
                '"%s"' % rom, '"%s"' % ann, '"%s"' % rom)]


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
    if not args.no_live:
        mame = Mame(rom, args)
        if mame.problem:
            rep.notes.append("The live half did not run: %s." % mame.problem)
        else:
            if mame.playback and not os.path.isfile(mame.playback):
                sys.exit("firstlook: no such recording: %s" % mame.playback)
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
                except Exception as e:                       # noqa: BLE001
                    body = ["Did not complete: %s: %s" % (type(e).__name__, e)]
                    print("failed (%s)" % e)
                rep.add(heading, body)
                steps.append(key)
    rep.notes.append(
        "What the live sections show is what ONE run did -- %s. Nothing here is "
        "a complete inventory." % (
            "a replay of %s" % os.path.basename(args.playback) if args.playback
            else "the first %d seconds, with fire/Select pressed for it"
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
