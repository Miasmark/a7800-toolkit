#!/usr/bin/env python3
"""
One place to open a cartridge and get at everything inside it.

    python tools/workbench.py game.a78

The toolkit is a few dozen commands that each want a space, a base address and a
format. That is the right shape for the tools and the wrong shape for the first
hour with an unfamiliar ROM, where the question is simply "what is in here, and
can I see it?"

So this reads the header, finds what it can, and lists it. Every row is a thing
you can open: artwork goes to `spriteedit`, music to the tracker, and both are
launched with the space, base and format already filled in -- which is the part
that is tedious to get right by hand and silent when you get it wrong.

It also runs the rest of the toolkit for you, on this cartridge, in a project
folder beside the ROM (`<rom>-workbench`): the first-look report, the
disassembly, observing what the code does and finding its address tables, the
checks on annotations.json, sampling profiles, cycle budgets, display-interrupt
timing, and capturing the music (with POKEY-to-TIA conversion). The tabs are
Overview, Run, Results (each job's command lines, live output and files, with
pictures and audio shown in place), Listing (the disassembly, searchable) and
Annotations (edit and check annotations.json). The jobs that need an emulator are
greyed out, with the reason, when MAME or the BIOS is not found.

It is a launcher, not a new tool. Nothing here reimplements anything: each job is
the command line you would have typed, shown beside its result, and each editor
runs in its own process on its own port so that closing one does not take the
others with it.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cart as cart_module
import runprobe

ROM = None
CART = None
MANIFEST = None
CHILDREN = {}          # port -> (Popen, description)
NEXT_PORT = [8140]


def cart_info():
    """What the header and the mapper say, as data rather than a paragraph."""
    c = CART
    info = dict(c.info or {})
    spaces = []
    for start, end, kind, arg in c._region:
        if kind == "fixed":
            spaces.append({"name": c._fixed_name(arg), "start": start,
                           "end": end, "kind": "fixed"})
        elif kind == "window":
            for i in range(c.map.nwindow):
                spaces.append({"name": "b%d" % i, "start": start, "end": end,
                               "kind": "window"})
        else:
            spaces.append({"name": "(ram)", "start": start, "end": end,
                           "kind": "ram"})
    return {
        "rom": os.path.basename(ROM),
        "title": info.get("title", ""),
        "size": len(c.rom),
        "cart_type": info.get("cart_type", 0),
        "flags": info.get("flags", []),
        "region": info.get("region", "?"),
        "mapper": c.map.name,
        "note": getattr(c.map, "note", ""),
        "bankset": c.bankset,
        "side": getattr(c, "side", "sally"),
        "pokeys": ["$%04X" % b for b in c.pokeys()],
        "chip": "pokey" if c.pokeys() else "tia",
        "spaces": spaces,
        "vectors": {k: "$%04X" % v for k, v in c.vectors().items()},
        "warnings": list(c.warnings),
        "format": format_for(ROM),
        "format_guessed": format_is_guess(ROM),
    }


def format_for(rom):
    """The shipped format file that describes this cartridge, if any."""
    try:
        sys.path.insert(0, HERE)
        import trackeredit
        f = trackeredit.find_format(rom)
        return os.path.basename(f) if f else None
    except Exception:                                        # noqa: BLE001
        return None


def format_is_guess(rom):
    """Whether the format describing this cartridge was worked out by ear.

    Worth reporting separately. A guessed format plays, which makes it look
    exactly as authoritative as one written from the player's code, and it is
    not -- it covers one stretch of notes somebody listened to, not the
    cartridge's music.
    """
    try:
        sys.path.insert(0, HERE)
        import trackeredit
        f = trackeredit.find_format(rom)
        if not f:
            return False
        return bool(json.load(io.open(f, encoding="utf-8")).get("guessed"))
    except Exception:                                        # noqa: BLE001
        return False


def scan(config=None, ram=None, dll=None):
    """Run assets.py over the cartridge and keep what it found."""
    global MANIFEST
    import assets
    import audiotrace
    an = audiotrace.analyse(ROM, config)
    cart = an.cart
    audio = []
    for g in audiotrace.cluster(audiotrace.find_writers(an, cart)):
        for t in audiotrace.tables_in(g):
            audio.append({"space": g["space"], "addr": t["addr"],
                          "regs": t["regs"], "how": t["how"]})
    gw = assets.graphics_writers(an)
    lists = assets.constant_pairs(gw)
    seen, hits = set(), []
    for sp, addr in lists:
        hits.extend(assets.walk_lists(cart, sp, addr, seen))
    if ram and dll is not None:
        try:
            hits.extend(assets.from_ram_dump(cart, ram, 0x1800, dll))
        except ValueError:
            pass
    chars = assets.charbases(gw)
    MANIFEST = {
        "graphics": assets.collect_graphics(hits, chars),
        "audio": assets.collect_audio(audio),
        "palettes": assets.palette_writes(an),
        "display_lists": ["%s:%04X" % (sp, a) for sp, a in lists],
        "in_ram": [a for _sp, a in lists if a < 0x4000],
    }
    return MANIFEST


def manifest_file():
    """The scan on disk, so a launched editor can read the palettes from it."""
    import tempfile
    if not MANIFEST:
        return None
    path = os.path.join(tempfile.gettempdir(), "a7800-workbench-assets.json")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(MANIFEST))
    return path


def free_port(start):
    """The first port from `start` that nothing is listening on.

    Not just `start + n`. Something else may already hold it -- an editor from
    a previous run, or another copy of this -- and a child that cannot bind
    looks exactly like a child that crashed. Ask the OS instead of assuming.
    """
    import socket
    for port in range(start, start + 200):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port in %d-%d" % (start, start + 200))


def launch(tool, args, what):
    """Start one of the editors on its own port and hand back its address."""
    port = free_port(NEXT_PORT[0])
    NEXT_PORT[0] = port + 1
    cmd = [sys.executable, os.path.join(HERE, tool), ROM] + list(args) + \
          ["--no-browser", "--port", str(port)]
    if tool == "trackeredit.py":
        # it takes a song, not a cartridge; the caller passes the path
        cmd = [sys.executable, os.path.join(HERE, tool)] + list(args) + \
              ["--no-browser", "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    CHILDREN[port] = (proc, what)
    return {"port": port, "url": "http://127.0.0.1:%d/" % port, "what": what}


def running():
    out = []
    for port, (proc, what) in sorted(CHILDREN.items()):
        alive = proc.poll() is None
        out.append({"port": port, "what": what, "alive": alive,
                    "url": "http://127.0.0.1:%d/" % port})
    return out


def stop_all():
    for _port, (proc, _what) in CHILDREN.items():
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


# ---------------------------------------------------------------- the project
#
# The launcher side above opens editors. The rest of this file runs the toolkit's
# other tools for you, on a cartridge, and keeps what they write in one folder so
# the results can be found again: a "project" next to the ROM, `<rom>-workbench`.
# Nothing in it is a new analysis. A job is a list of command lines -- the same
# ones the README tells you to type -- run in order with their output captured,
# and the command line is shown with the result, so the workbench teaches the
# commands instead of hiding them.

PROJECT = None          # the folder jobs write into
JOBS = {}               # id -> Job
JOB_SEQ = [0]
LOCK = threading.Lock()
LOG_LIMIT = 4000        # lines kept per job
TEXT_LIMIT = 2 * 1024 * 1024
TYPES = {".png": "image/png", ".wav": "audio/wav", ".jpg": "image/jpeg",
         ".json": "application/json; charset=utf-8"}
TEXT = (".txt", ".log", ".md", ".asm", ".trk", ".lua", ".csv", ".inc", ".sym")


def config_path():
    return os.path.join(PROJECT, "annotations.json")


def src_dir():
    return os.path.join(PROJECT, "src")


def mame_version(exe):
    """MAME's version as a string like '0.264', or None."""
    import re
    try:
        out = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+\.\d+)", out.decode("utf-8", "replace"))
    return m.group(1) if m else None


def environment():
    """What the jobs that need an emulator can rely on, and what is missing."""
    import capture
    exe = capture.find_mame()
    roms = capture.find_rompath(ROM) if exe else None
    try:
        import PIL                                           # noqa: F401
        pillow = True
    except ImportError:
        pillow = False
    problem = None
    if not exe:
        problem = ("MAME was not found. Install it, or set A7800_MAME to the "
                   "executable (docs/emulation.md).")
    elif not roms:
        problem = ("MAME is there but the 7800 BIOS is not. Set A7800_ROMPATH to "
                   "the folder holding it (and A7800_BIOS=a7800pr for the open "
                   "BIOS; docs/emulation.md, \"Running MAME with no Atari BIOS\").")
    return {"mame": exe, "mame_version": mame_version(exe) if exe else None,
            "rompath": roms, "bios": os.environ.get("A7800_BIOS") or "",
            "ready": problem is None, "problem": problem, "pillow": pillow,
            "python": "%d.%d.%d" % sys.version_info[:3],
            "project": PROJECT, "config_exists": os.path.isfile(config_path())}


def _py(tool, *args):
    return [sys.executable, os.path.join(HERE, tool)] + [str(a) for a in args]


def _probe(name, out, seconds, env=None, playback=None):
    cmd = _py("runprobe.py", ROM, name, "-o", out, "--seconds", seconds)
    for k, v in sorted((env or {}).items()):
        cmd += ["-e", "%s=%s" % (k, v)]
    if playback:
        cmd += ["--playback", playback]
    return cmd


def _banks():
    return str(max(CART.nbanks, 1))


class Job(object):
    """A list of command lines, run in order on a thread, with their output kept."""

    def __init__(self, kind, label, steps, outdir, note=""):
        with LOCK:
            JOB_SEQ[0] += 1
            self.id = JOB_SEQ[0]
        self.kind, self.label, self.steps, self.note = kind, label, steps, note
        self.outdir = outdir
        self.status = "queued"
        self.log = []
        self.dropped = 0            # lines dropped off the front of a long log
        self.step = 0
        self.started = self.finished = None
        self.proc = None
        self.cancelled = False

    def _say(self, line):
        with LOCK:
            self.log.append(line)
            if len(self.log) > LOG_LIMIT:
                cut = len(self.log) - LOG_LIMIT
                del self.log[:cut]
                self.dropped += cut

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.started = time.time()
        self.status = "running"
        os.makedirs(self.outdir, exist_ok=True)
        for i, st in enumerate(self.steps):
            self.step = i
            self._say("$ " + " ".join(_quote(c) for c in st["cmd"]))
            try:
                self.proc = subprocess.Popen(
                    st["cmd"], cwd=st.get("cwd") or PROJECT,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                for raw in iter(self.proc.stdout.readline, b""):
                    self._say(raw.decode("utf-8", "replace").rstrip("\r\n"))
                code = self.proc.wait()
            except OSError as e:
                self._say("could not run: %s" % e)
                code = 127
            if self.cancelled:
                self.status = "cancelled"
                break
            if code != 0:
                if st.get("soft"):
                    self._say("(that step reported a problem; carrying on)")
                    continue
                self._say("-- stopped: that step exited with status %d" % code)
                self.status = "failed"
                break
        else:
            self.status = "done"
        self.finished = time.time()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def outputs(self):
        """Files under the job's folder, as paths relative to the project."""
        out = []
        for root, dirs, files in os.walk(self.outdir):
            dirs.sort()
            depth = os.path.relpath(root, self.outdir).count(os.sep)
            if depth > 2:
                continue
            for f in sorted(files):
                full = os.path.join(root, f)
                out.append({"path": os.path.relpath(full, PROJECT).replace(os.sep, "/"),
                            "size": os.path.getsize(full)})
        return out

    def summary(self):
        end = self.finished or time.time()
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "status": self.status, "step": self.step + 1,
                "steps": len(self.steps), "note": self.note,
                "seconds": round(end - self.started, 1) if self.started else 0}

    def detail(self, since=0):
        d = self.summary()
        with LOCK:
            first = self.dropped
            lines = self.log[max(0, since - first):]
            d["next"] = first + len(self.log)
        d["lines"] = lines
        d["commands"] = [" ".join(_quote(c) for c in st["cmd"]) for st in self.steps]
        d["outputs"] = self.outputs()
        return d


def _quote(c):
    c = str(c)
    return c if c and not any(ch in c for ch in " \t\"'$&|;<>()") else '"%s"' % c.replace('"', '\\"')


def _need_config(steps):
    """Steps that make sure the annotations file exists before something edits it."""
    if os.path.isfile(config_path()):
        return
    steps.append({"cmd": _py("init.py", ROM, "-o", config_path())})


def _refresh_listing(steps):
    cmd = _py("disasm.py", ROM, "-o", src_dir(), "-c", config_path())
    steps.append({"cmd": cmd, "soft": True})


def _int(params, name, default, lo, hi):
    try:
        v = int(params.get(name, default))
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number" % name)
    if not lo <= v <= hi:
        raise ValueError("%s must be between %d and %d" % (name, lo, hi))
    return v


def _bool(params, name, default):
    v = params.get(name, default)
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


def _choice(params, name, default, choices):
    v = params.get(name, default)
    if v not in choices:
        raise ValueError("%s must be one of %s" % (name, ", ".join(choices)))
    return v


def _engine(p, default="sim"):
    return _choice(p, "engine", default, ("sim", "mame"))


def _frames(p, default_seconds):
    """Frames for a simulator run, from a `seconds` parameter."""
    fps = 50 if (CART.info or {}).get("region", "NTSC") == "PAL" else 60
    return _int(p, "seconds", default_seconds, 5, 600) * fps


def build_firstlook(p):
    live = _bool(p, "live", True)
    secs = _int(p, "seconds", 30, 5, 300)
    engine = _choice(p, "engine", "auto", ("auto", "sim", "mame"))
    out = os.path.join(PROJECT, "firstlook")
    cmd = _py("firstlook.py", ROM, "-o", out, "--seconds", secs, "--engine", engine)
    if not live:
        cmd.append("--no-live")
    return Job("firstlook", "first look" + ("" if live else " (static)"),
               [{"cmd": cmd}], out,
               "report.md in the output folder is the summary")


def build_disasm(p):
    out = src_dir()
    steps = [{"cmd": _py("disasm.py", ROM, "-o", out) +
              (["-c", config_path()] if os.path.isfile(config_path()) else []) +
              ["--gaps"]}]
    if os.path.isfile(config_path()):
        steps.append({"cmd": _py("annotations.py", config_path(), "--rom", ROM),
                      "soft": True})
    return Job("disasm", "disassemble", steps, out,
               "the listings open in the Listing tab")


def build_lint(p):
    if not os.path.isfile(config_path()):
        raise ValueError("there is no annotations file yet; make one, or run "
                         "'observe code' or 'find addresses', which start one")
    return Job("lint", "check annotations",
               [{"cmd": _py("annotations.py", config_path(), "--rom", ROM)}],
               os.path.join(PROJECT, "lint"))


def build_newannot(p):
    if os.path.isfile(config_path()):
        raise ValueError("annotations.json already exists in the project")
    return Job("newannot", "start annotations",
               [{"cmd": _py("init.py", ROM, "-o", config_path())}],
               os.path.join(PROJECT, "lint"))


def build_observe(p):
    secs = _int(p, "seconds", 30, 5, 600)
    out = os.path.join(PROJECT, "observe")
    if _engine(p) == "sim":
        steps = [{"cmd": _py("simprobe.py", ROM, "-o", out, "--frames", _frames(p, 30),
                             "--drive")}]
    else:
        steps = [{"cmd": _probe("exectrace", out, secs, {"A7800_XT_BANKS": _banks()})}]
    _need_config(steps)
    steps.append({"cmd": _py("dyn.py", ROM, os.path.join(out, "exectrace.log"),
                             "-c", config_path())})
    _refresh_listing(steps)
    return Job("observe", "observe code", steps, out,
               "jump targets and bank switches it saw, added to annotations.json")


def build_addresses(p):
    secs = _int(p, "seconds", 40, 5, 600)
    out = os.path.join(PROJECT, "addresses")
    if _engine(p) == "sim":
        steps = [{"cmd": _py("simorigins.py", ROM, "-o", out, "--frames", _frames(p, 10))}]
    else:
        steps = [{"cmd": _probe("addrorigin", out, secs, {"A7800_AO_BANKS": _banks()})}]
    _need_config(steps)
    steps.append({"cmd": _py("origins.py", os.path.join(out, "addrorigin.log"), ROM,
                             "-c", config_path())})
    _refresh_listing(steps)
    return Job("addresses", "find address tables", steps, out,
               "slow: every instruction is decoded in Lua")


def build_profile(p):
    secs = _int(p, "seconds", 30, 5, 600)
    out = os.path.join(PROJECT, "profile")
    if _engine(p) == "sim":
        steps = [{"cmd": _py("simprobe.py", ROM, "-o", out, "--frames", _frames(p, 20),
                             "--drive", "--profile")}]
    else:
        steps = [{"cmd": _probe("pcprof", out, secs, {"A7800_PC_BANKS": _banks()})}]
    cmd = _py("pcmap.py", os.path.join(out, "pcprof.log"))
    if os.path.isfile(config_path()):
        cmd += ["-c", config_path()]
    elif os.path.isdir(src_dir()):
        cmd += ["--listing", src_dir()]
    steps.append({"cmd": cmd})
    return Job("profile", "where the time goes", steps, out,
               "named better once the listing or annotations exist")


def build_census(p):
    frames = _frames(p, 30)
    out = os.path.join(PROJECT, "census")
    cmd = _py("census.py", ROM, "-o", out, "--frames", frames)
    if _bool(p, "explore", True):
        cmd.append("--explore")
    if os.path.isfile(config_path()):
        cmd += ["-c", config_path()]
    prev = os.path.join(out, "census.json")
    if _bool(p, "accumulate", True) and os.path.isfile(prev):
        cmd += ["--merge", prev]          # read before it is rewritten: coverage only grows
    return Job("census", "census (ROM and RAM)", [{"cmd": cmd}], out,
               "census.md is the report; the coverage maps colour every byte by what happened to it")


def build_budget(p):
    secs = _int(p, "seconds", 15, 5, 120)
    start = _int(p, "from_frame", 300, 0, 100000)
    frames = _int(p, "frames", 300, 50, 5000)
    out = os.path.join(PROJECT, "budget")
    env = {"A7800_CB_FROM": start, "A7800_CB_END": start + frames,
           "A7800_CB_BLOCK": max(10, frames // 3)}
    if (CART.info or {}).get("region", "NTSC") == "PAL":
        env["A7800_CB_FRAME"] = "35525.5"
    return Job("budget", "cycle budget",
               [{"cmd": _probe("cyclebudget", out, secs, env)}], out,
               "executed, slow, and what MARIA took, per frame")


def build_interrupts(p):
    frame = _int(p, "frame", 600, 10, 100000)
    out = os.path.join(PROJECT, "interrupts")
    env = {"A7800_DT_FRAME": frame, "A7800_DT_FROM": frame, "A7800_DT_END": frame + 20}
    return Job("interrupts", "display interrupts",
               [{"cmd": _probe("dlitimes", out, max(10, frame // 40 + 5), env)}],
               out, "which raster line each interrupt arrives on")


def build_music(p):
    secs = _int(p, "seconds", 40, 5, 600)
    drive = _bool(p, "drive", True)
    to_tia = _bool(p, "to_tia", False)
    out = os.path.join(PROJECT, "music")
    env = {"A7800_AUDIO_FRAMES": secs * 60, "A7800_POKEY": "auto"}
    if drive:
        env["A7800_DRIVE"] = "1"
    if _engine(p) == "sim":
        log = os.path.join(out, "audio.log")
        cmd = _py("simprobe.py", ROM, "-o", out, "--frames", _frames(p, 40))
        if drive:
            cmd.append("--drive")
        steps = [{"cmd": cmd}]
    else:
        log = os.path.join(out, "a7800-audio.log")
        steps = [{"cmd": _probe("audio", out, secs, env)}]
    pokey = bool(CART.pokeys())
    if pokey and to_tia:
        cmd = _py("pokey2tia.py", log, "-o", os.path.join(out, "tia"),
                  "--map", _choice(p, "mapping", "groups", ("groups", "loudest")),
                  "--mash", _choice(p, "mash", "loudest", ("loudest", "arp")),
                  "--arp", _int(p, "arp", 2, 1, 8))
        if _bool(p, "fit", True):
            cmd.append("--fit")
        steps.append({"cmd": cmd})
    steps.append({"cmd": _py("tracker.py", "capture", log, "-o",
                             os.path.join(out, "song.trk")), "soft": True})
    steps.append({"cmd": _py("tracker.py", "render", os.path.join(out, "song.trk"),
                             "-o", os.path.join(out, "song.wav")), "soft": True})
    return Job("music", "capture music" + (" and convert to TIA" if pokey and to_tia else ""),
               steps, out, "song.trk opens in the tracker; the .wav files play here")


def build_probe(p):
    names = [n for n, _s in runprobe.list_probes()]
    name = _choice(p, "probe", names[0], names)
    secs = _int(p, "seconds", 30, 5, 900)
    lines = [ln.strip() for ln in str(p.get("env") or "").splitlines() if ln.strip()]
    runprobe.parse_env(lines, ROM)            # ValueError on a malformed line
    out = os.path.join(PROJECT, "probes", name)
    cmd = _py("runprobe.py", ROM, name, "-o", out, "--seconds", secs)
    for ln in lines:
        cmd += ["-e", ln]
    return Job("probe", "probe: " + name, [{"cmd": cmd}], out,
               "settings are A7800_* environment variables; the probe's header lists them")


def _kinds():
    p = lambda name, label, typ="int", default=None, **kw: dict(   # noqa: E731
        name=name, label=label, type=typ, default=default, **kw)
    return [
        dict(kind="firstlook", label="First look", group="Look", mame=False,
             about="The one-command report: what it is, its music and screens, its "
                   "graphics, which code runs. Runs the cartridge in the simulator by "
                   "default (no emulator needed), falling back to MAME if the simulator "
                   "stalls and MAME is there.",
             build=build_firstlook,
             params=[p("live", "run it (not just the static half)", "bool", True),
                     p("engine", "run it in", "choice", "auto", choices=["auto", "sim", "mame"]),
                     p("seconds", "seconds per run", "int", 30, min=5, max=300)]),
        dict(kind="disasm", label="Disassemble", group="Annotate", mame=False,
             about="Write the listings (uses annotations.json if there is one) and "
                   "report the gaps nothing explains yet.",
             build=build_disasm, params=[]),
        dict(kind="newannot", label="Start annotations", group="Annotate", mame=False,
             about="Write a starter annotations.json: the file every judgement about "
                   "this ROM goes in.",
             build=build_newannot, params=[]),
        dict(kind="observe", label="Observe code", group="Annotate", mame=False,
             about="Run it and write down where indirect jumps went and which banks "
                   "were switched in: entry points the static tracer cannot find.",
             build=build_observe,
             params=[p("engine", "run it in", "choice", "sim", choices=["sim", "mame"]),
                     p("seconds", "seconds", "int", 30, min=5, max=600)]),
        dict(kind="addresses", label="Find address tables", group="Annotate", mame=False,
             about="Follow every address the CPU uses back to the ROM bytes it came "
                   "from, and add the tables to annotations.json. Slow.",
             build=build_addresses,
             params=[p("engine", "run it in", "choice", "sim", choices=["sim", "mame"]),
                     p("seconds", "seconds", "int", 10, min=5, max=600)]),
        dict(kind="lint", label="Check annotations", group="Annotate", mame=False,
             about="Catch the typos the disassembler would silently ignore.",
             build=build_lint, params=[]),
        dict(kind="music", label="Capture music", group="Audio", mame=False,
             about="Record the sound registers, render the song, and for a POKEY "
                   "game optionally turn it into two TIA voices.",
             build=build_music,
             params=[p("engine", "run it in", "choice", "sim", choices=["sim", "mame"]),
                     p("seconds", "seconds", "int", 40, min=5, max=600),
                     p("drive", "press fire to get past the title", "bool", True),
                     p("to_tia", "convert POKEY to TIA", "bool", False),
                     p("mapping", "voices", "choice", "groups", choices=["groups", "loudest"]),
                     p("mash", "when a channel is shared", "choice", "loudest",
                       choices=["loudest", "arp"]),
                     p("arp", "arpeggio frames", "int", 2, min=1, max=8),
                     p("fit", "fit the pitch to the TIA", "bool", True)]),
        dict(kind="census", label="Census: what is never reached", group="Annotate", mame=False,
             about="Run it in the simulator and sort every ROM byte by what happened to it "
                   "(executed, read, drawn by MARIA, never touched, copies) and every RAM "
                   "byte by how it was used. Dark areas are what nothing reached. Each run "
                   "adds to the last, so coverage only grows.",
             build=build_census,
             params=[p("seconds", "seconds of play", "int", 30, min=5, max=600),
                     p("explore", "sweep the joystick and console switches too", "bool", True),
                     p("accumulate", "add to the previous census", "bool", True)]),
        dict(kind="profile", label="Where the time goes", group="Measure", mame=False,
             about="Where the 6502 spends its cycles, grouped under the routine "
                   "names you have given it. In the simulator the count is exact; "
                   "in MAME it is a sample.",
             build=build_profile,
             params=[p("engine", "run it in", "choice", "sim", choices=["sim", "mame"]),
                     p("seconds", "seconds", "int", 20, min=5, max=600)]),
        dict(kind="budget", label="Cycle budget", group="Measure", mame=True,
             about="Cycles the 6502 ran, the NMI's share, and what MARIA's DMA took.",
             build=build_budget,
             params=[p("from_frame", "start at frame", "int", 300, min=0, max=100000),
                     p("frames", "frames", "int", 300, min=50, max=5000),
                     p("seconds", "seconds", "int", 15, min=5, max=120)]),
        dict(kind="probe", label="Run any probe", group="Measure", mame=True,
             about="Any of the Lua probes, headless, with its settings as KEY=VALUE "
                   "lines. Read the probe's header (docs/emulation.md has the index) "
                   "for what it takes.",
             build=build_probe,
             params=[p("probe", "probe", "choice", "audio", choices=[
                         n for n, _s in runprobe.list_probes()]),
                     p("seconds", "seconds", "int", 30, min=5, max=900),
                     p("env", "settings, one per line", "text", "",
                       placeholder="A7800_CB_END=600")]),
        dict(kind="interrupts", label="Display interrupts", group="Measure", mame=True,
             about="The raster line each display interrupt arrives on, beside the "
                   "display list's zones.",
             build=build_interrupts,
             params=[p("frame", "frame", "int", 600, min=10, max=100000)]),
    ]


KINDS = _kinds()


def start_job(kind, params):
    """Build and start a job; ValueError says why not."""
    spec = next((k for k in KINDS if k["kind"] == kind), None)
    if not spec:
        raise ValueError("no such job: %r" % kind)
    if spec["mame"] or (params or {}).get("engine") == "mame":
        env = environment()
        if not env["ready"]:
            raise ValueError(env["problem"])
    os.makedirs(PROJECT, exist_ok=True)
    job = spec["build"](params or {})
    JOBS[job.id] = job
    job.start()
    return job


def project_file(rel):
    """The real path of a file inside the project, or None if `rel` points outside it."""
    if not rel or not PROJECT:
        return None
    full = os.path.realpath(os.path.join(PROJECT, rel))
    root = os.path.realpath(PROJECT)
    if full != root and not full.startswith(root + os.sep):
        return None
    return full if os.path.isfile(full) else None


def read_annotations():
    path = config_path()
    if not os.path.isfile(path):
        return {"path": path, "exists": False, "text": "", "findings": []}
    text = io.open(path, encoding="utf-8").read()
    return {"path": path, "exists": True, "text": text,
            "findings": lint_annotations(text)}


def lint_annotations(text):
    import annotations
    r, d = annotations.lint(text, "annotations.json")
    if d is not None:
        try:
            annotations.check_rom(r, d, ROM)
        except Exception as e:                                # noqa: BLE001
            r.warn("could not check against the cartridge: %s" % e)
    return [{"level": k, "message": m} for k, m in r.items]


def write_annotations(text):
    """Save the file if it is JSON at all; return the lint findings either way."""
    json.loads(text)                  # ValueError if it is not JSON: nothing written
    os.makedirs(PROJECT, exist_ok=True)
    with io.open(config_path(), "w", encoding="utf-8", newline="\n") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return lint_annotations(text)


def listing_files():
    out = []
    if os.path.isdir(src_dir()):
        # the fixed bank first: it holds the reset code, which is where reading starts
        for f in sorted(os.listdir(src_dir()), key=lambda n: (not n.startswith("f"), n)):
            if f.endswith(".asm"):
                out.append({"path": "src/" + f,
                            "size": os.path.getsize(os.path.join(src_dir(), f))})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if ctype == "application/json":
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, rel):
        full = project_file(rel)
        if not full:
            return self._send(404, {"error": "no such file in the project"})
        ext = os.path.splitext(full)[1].lower()
        size = os.path.getsize(full)
        if ext in TEXT or ext not in TYPES:
            with open(full, "rb") as f:
                data = f.read(TEXT_LIMIT)
            if size > TEXT_LIMIT:
                data += b"\n... (truncated: %d bytes more)\n" % (size - TEXT_LIMIT)
            ctype = ("text/plain; charset=utf-8" if ext in TEXT or ext == ".json"
                     else "application/octet-stream")
            if ext == ".json":
                ctype = "application/json; charset=utf-8"
            return self._send(200, data, ctype)
        with open(full, "rb") as f:
            data = f.read()
        extra = {"Accept-Ranges": "bytes"}
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and ext == ".wav":
            a, _, b = rng[6:].partition("-")
            try:
                start = int(a or 0)
                end = int(b) if b else len(data) - 1
            except ValueError:
                start, end = 0, len(data) - 1
            end = min(end, len(data) - 1)
            extra["Content-Range"] = "bytes %d-%d/%d" % (start, end, len(data))
            return self._send(206, data[start:end + 1], TYPES[ext], extra)
        return self._send(200, data, TYPES[ext], extra)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]            # noqa: E731
        try:
            if p == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if p == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if p == "/api/info":
                return self._send(200, cart_info())
            if p == "/api/env":
                return self._send(200, environment())
            if p == "/api/kinds":
                keep = ("kind", "label", "group", "mame", "about", "params")
                return self._send(200, {"kinds": [{k: s[k] for k in keep} for s in KINDS]})
            if p == "/api/manifest":
                return self._send(200, MANIFEST or {"graphics": [], "audio": []})
            if p == "/api/running":
                return self._send(200, {"tools": running()})
            if p == "/api/jobs":
                return self._send(200, {"jobs": [j.summary() for j in
                                                 sorted(JOBS.values(), key=lambda j: -j.id)]})
            if p == "/api/job":
                job = JOBS.get(int(one("id", 0)))
                if not job:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, job.detail(int(one("since", 0))))
            if p == "/api/file":
                return self._file(one("path", ""))
            if p == "/api/listing":
                return self._send(200, {"files": listing_files()})
            if p == "/api/annotations":
                return self._send(200, read_annotations())
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:                                # noqa: BLE001
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        self._send(404, {"error": "no such thing"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad JSON"})
        try:
            if self.path == "/api/scan":
                return self._send(200, scan(body.get("config") or None,
                                            body.get("ram") or None,
                                            body.get("dll")))
            if self.path == "/api/job":
                return self._send(200, start_job(body.get("kind"),
                                                 body.get("params")).summary())
            if self.path == "/api/job/cancel":
                job = JOBS.get(int(body.get("id", 0)))
                if job:
                    job.cancel()
                return self._send(200, {"ok": bool(job)})
            if self.path == "/api/annotations":
                return self._send(200, {"findings": write_annotations(body.get("text", ""))})
            if self.path == "/api/open":
                kind = body.get("kind")
                if kind == "sprite":
                    args = ["--space", body["space"], "--base",
                            str(int(body["base"])), "--height",
                            str(int(body.get("height", 8))), "--width",
                            str(int(body.get("width", 1))), "--mode",
                            str(body.get("mode", "160"))]
                    if CART.bankset:
                        args += ["--side", body.get("side", "sally")]
                    mf = manifest_file()
                    if mf:
                        # so the picker can offer the palettes this cartridge
                        # writes, rather than only greys
                        args += ["--palette-from", mf]
                    return self._send(200, launch(
                        "spriteedit.py", args,
                        "sprites %s:$%04X" % (body["space"], int(body["base"]))))
                if kind == "explore":
                    # Where the sound data is, opened in the tool that works
                    # out what it means. The scan already knows the address;
                    # retyping it into a second tool is the step that made
                    # people give up.
                    loc = str(body.get("loc") or "")
                    sp, _, a = loc.partition(":")
                    if not sp or not a:
                        raise ValueError("explore needs a location like f6:76F6")
                    args = ["--at", loc, "--chip",
                            body.get("chip") or ("pokey" if CART.pokeys()
                                                 else "tia")]
                    return self._send(200, launch(
                        "explore.py", args, "explore %s" % loc))
                if kind == "tracker":
                    song = ROM
                    if body.get("song"):
                        # a song the workbench itself wrote, by its project path
                        song = project_file(str(body["song"]))
                        if not song:
                            raise ValueError("that song is not in the project folder")
                    extra = []
                    if body.get("song_number") is not None:
                        extra = ["--song-number", str(int(body["song_number"]))]
                    return self._send(200, launch(
                        "trackeredit.py", [song] + extra,
                        "tracker %s" % os.path.basename(song)))
                return self._send(400, {"error": "unknown kind %r" % kind})
        except (ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:                                # noqa: BLE001
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
        self._send(404, {"error": "no such thing"})


PAGE = r"""<!doctype html>
<meta charset="utf-8"><title>7800 workbench</title>
<style>
 :root{color-scheme:dark;--bg:#15151b;--fg:#e6e6ee;--dim:#8b8b9c;--line:#2c2c38;--accent:#d8a657;
       --panel:#1b1b23;--good:#8fbf7f;--bad:#e06c75;--warn:#e0a458}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:13px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace}
 header{padding:10px 18px;border-bottom:1px solid var(--line);
        display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
 h1{font-size:14px;margin:0;font-weight:600}
 h2{font-size:12px;margin:18px 0 6px;color:var(--accent);font-weight:600;
    text-transform:uppercase;letter-spacing:.08em}
 h3{font-size:13px;margin:0 0 4px}
 .muted{color:var(--dim)}
 nav{display:flex;gap:2px;padding:0 18px;border-bottom:1px solid var(--line)}
 nav button{background:none;border:0;border-bottom:2px solid transparent;
            border-radius:0;padding:8px 14px;color:var(--dim)}
 nav button.on{color:var(--fg);border-bottom-color:var(--accent)}
 main{padding:12px 18px 40px;max-width:1180px}
 table{border-collapse:collapse;width:100%;margin-bottom:4px}
 th{text-align:left;color:var(--dim);font-weight:500;padding:2px 10px 2px 0;
    border-bottom:1px solid var(--line)}
 td{padding:2px 10px 2px 0;border-bottom:1px solid #20202a;vertical-align:top}
 button{background:#22222c;color:var(--fg);border:1px solid var(--line);
        border-radius:3px;padding:2px 9px;font:inherit;cursor:pointer}
 button:hover:not(:disabled){border-color:var(--accent)}
 button:disabled{opacity:.45;cursor:not-allowed}
 input,select,textarea{background:#101015;color:var(--fg);border:1px solid var(--line);
        border-radius:3px;font:inherit;padding:2px 6px}
 input[type=number]{width:80px}
 .warn{color:var(--warn)} .err{color:var(--bad)} .ok{color:var(--good)}
 .pill{border:1px solid var(--line);border-radius:9px;padding:0 8px;color:var(--dim)}
 .pill.ok{color:var(--good)} .pill.bad{color:var(--bad)}
 a{color:var(--accent)}
 #msg{min-height:20px}
 .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:10px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:10px 12px}
 .card .about{color:var(--dim);margin:2px 0 8px}
 .card label{display:flex;gap:8px;align-items:center;margin:3px 0}
 .card label span{flex:1}
 .card .go{margin-top:8px}
 .need{color:var(--warn);margin-top:6px}
 .split{display:grid;grid-template-columns:260px 1fr;gap:14px}
 .jobs{border-right:1px solid var(--line);padding-right:10px}
 .job{display:flex;gap:8px;padding:3px 6px;border-radius:3px;cursor:pointer}
 .job:hover,.job.on{background:var(--panel)}
 .dot{width:9px;height:9px;border-radius:50%;margin-top:6px;background:var(--dim)}
 .dot.running{background:var(--accent)} .dot.done{background:var(--good)}
 .dot.failed{background:var(--bad)}
 pre{background:#101015;border:1px solid var(--line);border-radius:4px;padding:8px 10px;
     margin:6px 0;overflow:auto;max-height:420px;white-space:pre-wrap;word-break:break-word}
 pre.code{white-space:pre;word-break:normal;max-height:560px}
 pre.code .hit{background:#3a3320}
 .files{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
 .file{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:6px 8px}
 .file img{max-width:100%;image-rendering:pixelated;display:block;margin:4px 0}
 .file audio{width:100%;margin-top:4px}
 .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
 textarea{width:100%;height:420px;white-space:pre;tab-size:2}
 .find{margin:2px 0}
 .find.error{color:var(--bad)} .find.warning{color:var(--warn)}
 .tip{background:var(--panel);border-left:3px solid var(--accent);padding:6px 12px;margin:8px 0}
</style>
<header>
  <h1>7800 workbench</h1>
  <span class="muted" id="what"></span>
  <span style="flex:1"></span>
  <span class="pill" id="envpill"></span>
</header>
<nav id="tabs"></nav>
<main>
  <div id="msg" class="muted"></div>
  <section id="t-overview"></section>
  <section id="t-run" hidden></section>
  <section id="t-results" hidden></section>
  <section id="t-listing" hidden></section>
  <section id="t-annotations" hidden></section>
</main>
<script>
const $=id=>document.getElementById(id);
let INFO=null, ENV=null, KINDS=[], CUR=null, POLL=null, SEEN={};
const TABS=[['overview','Overview'],['run','Run'],['results','Results'],
            ['listing','Listing'],['annotations','Annotations']];

function el(tag,props,...kids){
  const e=document.createElement(tag);
  for(const k in (props||{})){
    if(k==='class') e.className=props[k];
    else if(k==='text') e.textContent=props[k];
    else if(k.startsWith('on')) e.addEventListener(k.slice(2),props[k]);
    else if(props[k]!==false && props[k]!=null) e.setAttribute(k,props[k]);
  }
  for(const c of kids.flat()){
    if(c==null||c===false) continue;
    e.append(c.nodeType?c:document.createTextNode(String(c)));
  }
  return e;
}
function msg(s,bad){const m=$('msg');m.textContent=s;m.className=bad?'err':'muted';}
async function api(path,body){
  const r=await fetch(path,body===undefined?{}:{method:'POST',body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error) throw new Error(j.error);
  return j;
}
function show(name){
  msg('');
  for(const [id] of TABS){ $('t-'+id).hidden=(id!==name); }
  for(const b of document.querySelectorAll('#tabs button'))
    b.classList.toggle('on',b.dataset.tab===name);
  if(name==='run') drawRun();
  if(name==='results') drawResults();
  if(name==='listing') drawListing();
  if(name==='annotations') drawAnnotations();
  if(name==='overview') drawOverview();
  if(location.hash.slice(1)!==name) history.replaceState(null,'','#'+name);
}
function bytes(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB';}

async function load(){
  INFO=await api('/api/info'); ENV=await api('/api/env'); KINDS=(await api('/api/kinds')).kinds;
  $('what').textContent=INFO.rom+'  '+INFO.mapper+'  '+Math.round(INFO.size/1024)+'K  '+
    INFO.region+'  '+INFO.chip.toUpperCase()+(INFO.pokeys.length?' at '+INFO.pokeys.join(', '):'');
  const pill=$('envpill');
  pill.textContent=ENV.ready?('MAME '+(ENV.mame_version||'')+' ready'):'no MAME: simulator only';
  pill.className='pill '+(ENV.ready?'ok':'');
  pill.title=ENV.ready?(ENV.mame+'\n'+ENV.rompath):ENV.problem;
  const nav=$('tabs');
  for(const [id,label] of TABS)
    nav.append(el('button',{'data-tab':id,text:label,onclick:()=>show(id)}));
  const first=location.hash.slice(1);
  show(TABS.some(t=>t[0]===first)?first:'overview');
}

// ------------------------------------------------------------------ overview
function row(k,v){return el('tr',{},el('td',{class:'muted',style:'width:120px',text:k}),el('td',{},v));}
async function drawOverview(){
  const box=$('t-overview'); box.replaceChildren();
  const t=el('table');
  t.append(row('title',INFO.title||'(none)'));
  t.append(row('cart type','$'+INFO.cart_type.toString(16).toUpperCase().padStart(4,'0')+
    (INFO.flags.length?'  '+INFO.flags.join(', '):'')));
  t.append(row('mapper',INFO.mapper+(INFO.note?'  ('+INFO.note+')':'')));
  t.append(row('vectors',Object.entries(INFO.vectors).map(x=>x[0]+' '+x[1]).join('  ')));
  t.append(row('spaces',INFO.spaces.map(s=>s.name).join(' ')));
  t.append(row('music',INFO.format
    ? (INFO.format_guessed
       ? el('span',{},el('b',{text:INFO.format}),' — a reading worked out by ear, not a description of the player. It plays, so you can check it; it covers one stretch of notes.')
       : el('span',{},'readable from the ROM with ',el('b',{text:INFO.format}),' — the tracker opens it without an emulator'))
    : el('span',{class:'muted',text:'no format file describes this cartridge; the tracker will record it in an emulator instead'})));
  if(INFO.bankset) t.append(row('bankset','two parallel sets; editing the '+INFO.side+' half.'));
  t.append(row('project',ENV.project));
  box.append(el('h2',{text:'cartridge'}),t);
  for(const w of INFO.warnings) box.append(el('div',{class:'warn',text:w}));
  if(!ENV.ready) box.append(el('div',{class:'tip'},el('b',{text:'No emulator. '}),
    'The simulator runs the cartridge for first look, observe code, find address tables, music and the profile, so most of this works without MAME. ',
    'Cycle budget, display-interrupt timing and running arbitrary probes need it: '+ENV.problem));
  else box.append(el('div',{class:'tip'},el('b',{text:'Start here: '}),
    'Run → First look gives a one-page report on an unfamiliar cartridge. ',
    'Then Disassemble, and Observe code / Find address tables to teach the listing what the static tracer cannot see.'));
  const bar=el('div',{class:'bar'},
    el('button',{text:'scan for assets',onclick:scan}),
    el('button',{text:'open tracker',onclick:openTracker}));
  box.append(el('h2',{text:'assets'}),bar,el('div',{id:'assets'}));
  if(MANIFEST_VIEW) showAssets(MANIFEST_VIEW);
  box.append(el('div',{id:'tools'}));
  refreshTools();
}
let MANIFEST_VIEW=null;
async function scan(){
  msg('scanning — this disassembles the ROM, so give it a moment');
  try{ MANIFEST_VIEW=await api('/api/scan',{}); showAssets(MANIFEST_VIEW); msg(''); }
  catch(e){ msg(e.message,true); }
}
function showAssets(m){
  const box=$('assets'); if(!box) return; box.replaceChildren();
  const sure=m.graphics.filter(g=>g.certain), maybe=m.graphics.filter(g=>!g.certain);
  box.append(el('h2',{text:'graphics ('+sure.length+' placed'+(maybe.length?', '+maybe.length+' bank-ambiguous':'')+')'}));
  if(!sure.length&&!maybe.length) box.append(el('div',{class:'muted',text:'Nothing placed. On most games the display list is built in RAM, so a static scan stops there — Run → First look captures it live.'}));
  if(sure.length){
    const t=el('table',{},el('tr',{},el('th',{text:'where'}),el('th',{text:'shape'}),el('th',{text:'evidence'}),el('th')));
    for(const g of sure){
      const parts=g.loc.split(':'); const w=g.width||1;
      t.append(el('tr',{},el('td',{text:g.loc}),el('td',{text:g.width?g.width+' bytes wide':'character set'}),
        el('td',{class:'muted',text:g.source}),
        el('td',{},el('button',{text:'edit',onclick:()=>openSprite(parts[0],parseInt(parts[1],16),w)}))));
    }
    box.append(t);
  }
  if(maybe.length) box.append(el('div',{class:'muted',text:maybe.length+' more sit in the paged window, where the capture does not record which bank MARIA read. They are candidates, not findings.'}));
  box.append(el('h2',{text:'audio ('+m.audio.length+' tables)'}));
  if(m.audio.length){
    const t=el('table',{},el('tr',{},el('th',{text:'where'}),el('th',{text:'feeds'}),el('th',{text:'how'}),el('th')));
    for(const a of m.audio)
      t.append(el('tr',{},el('td',{text:a.loc}),el('td',{text:a.regs.join(' ')}),el('td',{class:'muted',text:a.how}),
        el('td',{},el('button',{text:'explore',onclick:()=>openExplore(a.loc)}))));
    box.append(t);
    box.append(el('div',{class:'muted',text:INFO.format
      ? 'Those are the tables the sound code reads; '+INFO.format+' also describes how this player arranges them into songs, so the tracker opens them straight from the ROM.'
      : 'Finding these is not the same as being able to read the songs: they say where the sound data is, not what it means as music. Until a format file describes this player, the tracker records the game instead.'}));
  } else box.append(el('div',{class:'muted',text:'No audio tables reached. The player is usually behind an indirect jump; Observe code may find it, and the tracker still works because it watches the running machine.'}));
}
async function openSprite(space,base,width){
  try{ const j=await api('/api/open',{kind:'sprite',space:space,base:base,width:width,height:8,side:INFO.bankset?'maria':'sally'});
       window.open(j.url,'_blank'); setTimeout(refreshTools,600);}catch(e){msg(e.message,true);}
}
async function openExplore(loc){
  msg('opening the format explorer at '+loc);
  try{ const j=await api('/api/open',{kind:'explore',loc:loc,chip:INFO.chip});
       window.open(j.url,'_blank'); msg(''); setTimeout(refreshTools,600);}catch(e){msg(e.message,true);}
}
async function openTracker(song){
  msg(INFO.format?'reading the songs out of the ROM with '+INFO.format+' — no emulator'
     :'no format file describes this cartridge, so it will be recorded in an emulator; that takes about a minute');
  try{ const j=await api('/api/open',{kind:'tracker',song:song||null});
       msg('tracker starting at '+j.url); setTimeout(()=>{window.open(j.url,'_blank');refreshTools();},2500);}
  catch(e){ msg(e.message,true); }
}
async function refreshTools(){
  const box=$('tools'); if(!box) return;
  const j=await api('/api/running');
  box.replaceChildren();
  if(!j.tools.length) return;
  const t=el('table');
  for(const x of j.tools) t.append(el('tr',{},el('td',{text:x.what}),el('td',{},
    x.alive?el('a',{href:x.url,target:'_blank',text:x.url}):el('span',{class:'muted',text:'closed'}))));
  box.append(el('h2',{text:'open'}),t);
}

// ----------------------------------------------------------------------- run
function drawRun(){
  const box=$('t-run'); box.replaceChildren();
  const groups=[];
  for(const k of KINDS) if(!groups.includes(k.group)) groups.push(k.group);
  for(const g of groups){
    box.append(el('h2',{text:g}));
    const cards=el('div',{class:'cards'});
    for(const k of KINDS.filter(x=>x.group===g)) cards.append(card(k));
    box.append(cards);
  }
}
function card(k){
  const inputs={};
  const c=el('div',{class:'card'},el('h3',{text:k.label}),el('div',{class:'about',text:k.about}));
  for(const p of k.params){
    if(p.name==='to_tia'&&!INFO.pokeys.length) continue;
    if(['mapping','mash','arp','fit'].includes(p.name)&&!INFO.pokeys.length) continue;
    let inp;
    if(p.type==='bool') inp=el('input',{type:'checkbox',checked:p.default?'checked':false});
    else if(p.type==='choice') inp=el('select',{},p.choices.map(x=>el('option',{value:x,text:x,selected:x===p.default?'selected':false})));
    else if(p.type==='text') inp=el('textarea',{rows:'3',placeholder:p.placeholder||'',style:'width:100%;height:64px'});
    else inp=el('input',{type:'number',value:p.default,min:p.min,max:p.max});
    inputs[p.name]=[p,inp];
    c.append(p.type==='text'?el('div',{},el('div',{class:'muted',text:p.label}),inp):el('label',{},el('span',{text:p.label}),inp));
  }
  const need=k.mame&&!ENV.ready;
  const engSel=inputs.engine&&inputs.engine[1];
  if(engSel&&!ENV.ready) for(const o of engSel.options) if(o.value==='mame') o.textContent='mame (not found)';
  const go=el('button',{class:'go',text:'run',disabled:need?'disabled':false,onclick:async()=>{
    const body={kind:k.kind,params:{}};
    for(const n in inputs){ const [p,inp]=inputs[n];
      body.params[n]=p.type==='bool'?inp.checked:((p.type==='choice'||p.type==='text')?inp.value:Number(inp.value)); }
    try{ const j=await api('/api/job',body); CUR=j.id; show('results'); }
    catch(e){ msg(e.message,true); }
  }});
  c.append(go);
  if(need) c.append(el('div',{class:'need',text:ENV.problem}));
  return c;
}

// ------------------------------------------------------------------- results
async function drawResults(){
  const box=$('t-results');
  const j=await api('/api/jobs');
  box.replaceChildren();
  if(!j.jobs.length){ box.append(el('div',{class:'muted',text:'Nothing has been run yet. Run → First look is a good first one.'})); return; }
  if(CUR==null||!j.jobs.some(x=>x.id===CUR)) CUR=j.jobs[0].id;
  const left=el('div',{class:'jobs'});
  for(const x of j.jobs)
    left.append(el('div',{class:'job'+(x.id===CUR?' on':''),onclick:()=>{CUR=x.id;drawResults();}},
      el('span',{class:'dot '+x.status}),el('span',{text:'#'+x.id+' '+x.label}),
      el('span',{class:'muted',text:x.status==='running'?x.step+'/'+x.steps:x.seconds+'s'})));
  const right=el('div',{id:'jobdetail'});
  box.append(el('div',{class:'split'},left,right));
  await drawJob(true);
}
async function drawJob(first){
  const right=$('jobdetail'); if(!right||CUR==null) return;
  const d=await api('/api/job?id='+CUR+'&since=0');
  right.replaceChildren();
  right.append(el('h3',{},d.label+' ',el('span',{class:d.status==='done'?'ok':(d.status==='failed'?'err':'muted'),text:d.status})));
  if(d.note) right.append(el('div',{class:'muted',text:d.note}));
  right.append(el('div',{class:'muted',text:'commands:'}),el('pre',{text:d.commands.join('\n')}));
  const log=el('pre',{id:'joblog',text:d.lines.join('\n')});
  right.append(el('div',{class:'muted',text:'output:'}),log);
  log.scrollTop=log.scrollHeight;
  if(d.status==='running') right.append(el('button',{text:'stop',onclick:async()=>{await api('/api/job/cancel',{id:CUR});drawJob();}}));
  const outs=d.outputs;
  if(outs.length){
    right.append(el('h2',{text:'files ('+outs.length+')'}));
    const g=el('div',{class:'files'});
    for(const f of outs) g.append(fileCard(f));
    right.append(g);
  }
  clearTimeout(POLL);
  const live=d.status==='running'||d.status==='queued';
  if(SEEN[CUR]==='live' && !live && !$('t-results').hidden){ SEEN[CUR]='final'; drawResults(); return; }
  SEEN[CUR]=live?'live':'final';
  if(live) POLL=setTimeout(()=>{ if(!$('t-results').hidden) drawJob(); },1500);
}
function fileCard(f){
  const url='/api/file?path='+encodeURIComponent(f.path);
  const c=el('div',{class:'file'},el('div',{text:f.path.split('/').slice(1).join('/')||f.path}),el('div',{class:'muted',text:bytes(f.size)}));
  const low=f.path.toLowerCase();
  if(low.endsWith('.png')) c.append(el('a',{href:url,target:'_blank'},el('img',{src:url,loading:'lazy'})));
  else if(low.endsWith('.wav')) c.append(el('audio',{controls:'controls',preload:'none',src:url}));
  else{
    c.append(el('button',{text:'view',onclick:()=>viewText(f,c)}));
    if(low.endsWith('.trk')) c.append(' ',el('button',{text:'open in tracker',onclick:()=>openTracker(f.path)}));
    c.append(' ',el('a',{href:url,target:'_blank',text:'raw'}));
  }
  return c;
}
async function viewText(f,c){
  const old=c.querySelector('pre'); if(old){old.remove();return;}
  const r=await fetch('/api/file?path='+encodeURIComponent(f.path));
  c.append(el('pre',{text:await r.text()}));
}

// ------------------------------------------------------------------- listing
let LIST=null;
async function drawListing(){
  const box=$('t-listing'); box.replaceChildren();
  const j=await api('/api/listing');
  if(!j.files.length){
    box.append(el('div',{class:'muted',text:'No listing yet. Run → Disassemble writes one to the project folder.'}),
      el('div',{},el('button',{text:'disassemble now',onclick:async()=>{try{const r=await api('/api/job',{kind:'disasm',params:{}});CUR=r.id;show('results');}catch(e){msg(e.message,true);}}})));
    return;
  }
  const sel=el('select',{},j.files.map(f=>el('option',{value:f.path,text:f.path+'  '+bytes(f.size)})));
  const q=el('input',{type:'text',placeholder:'search, or an address like B29E',size:'28'});
  const info=el('span',{class:'muted'});
  const view=el('pre',{class:'code'});
  const bar=el('div',{class:'bar'},sel,q,
    el('button',{text:'find',onclick:()=>find(1)}),el('button',{text:'prev',onclick:()=>find(-1)}),
    el('button',{text:'▲ page',onclick:()=>move(-200)}),el('button',{text:'page ▼',onclick:()=>move(200)}),info);
  box.append(bar,view);
  let lines=[], top=0, hit=-1;
  function draw(){
    const end=Math.min(lines.length,top+260);
    view.replaceChildren();
    for(let i=top;i<end;i++) view.append(el('div',{class:i===hit?'hit':false,text:lines[i]||' '}));
    info.textContent='lines '+(top+1)+'–'+end+' of '+lines.length;
  }
  function move(d){ top=Math.max(0,Math.min(Math.max(0,lines.length-50),top+d)); draw(); }
  function find(dir){
    const s=q.value.trim(); if(!s) return;
    const addr=/^\$?[0-9a-fA-F]{4}$/.test(s)?s.replace('$','').toUpperCase():null;
    const test=addr?(l=>new RegExp('(^|[^0-9A-Fa-f])'+addr+'(?![0-9A-Fa-f])').test(l)&&(l.includes(addr+':')||l.includes('_'+addr)||l.includes('$'+addr)||l.includes('; '+addr))):(l=>l.toLowerCase().includes(s.toLowerCase()));
    let i=hit<0?(dir>0?-1:lines.length):hit;
    for(let n=0;n<lines.length;n++){
      i+=dir; if(i<0) i=lines.length-1; if(i>=lines.length) i=0;
      if(test(lines[i])){ hit=i; top=Math.max(0,i-8); draw(); return; }
    }
    info.textContent='not found';
  }
  q.addEventListener('keydown',e=>{if(e.key==='Enter') find(1);});
  async function open(){
    const r=await fetch('/api/file?path='+encodeURIComponent(sel.value));
    lines=(await r.text()).split('\n'); top=0; hit=-1; draw();
  }
  sel.addEventListener('change',open); open();
}

// --------------------------------------------------------------- annotations
async function drawAnnotations(){
  const box=$('t-annotations'); box.replaceChildren();
  const a=await api('/api/annotations');
  if(!a.exists){
    box.append(el('div',{class:'muted',text:'There is no annotations.json in the project yet. It is the file the disassembler reads: names, entry points, data blocks, bank pins.'}),
      el('div',{class:'bar'},el('button',{text:'start one',onclick:async()=>{try{await api('/api/job',{kind:'newannot',params:{}});setTimeout(drawAnnotations,1500);}catch(e){msg(e.message,true);}}})));
    return;
  }
  const ta=el('textarea',{spellcheck:'false'}); ta.value=a.text;
  const list=el('div');
  const status=el('span',{class:'muted'});
  function findings(fs){
    list.replaceChildren();
    if(!fs.length) list.append(el('div',{class:'ok',text:'no problems found'}));
    for(const f of fs) list.append(el('div',{class:'find '+f.level,text:f.level+': '+f.message}));
  }
  const save=el('button',{text:'save and check',onclick:async()=>{
    try{ const r=await api('/api/annotations',{text:ta.value}); findings(r.findings); status.textContent='saved'; }
    catch(e){ status.textContent=''; msg(e.message,true); }
  }});
  const lintbtn=el('button',{text:'reload',onclick:drawAnnotations});
  const dis=el('button',{text:'disassemble with it',onclick:async()=>{try{const r=await api('/api/job',{kind:'disasm',params:{}});CUR=r.id;show('results');}catch(e){msg(e.message,true);}}});
  box.append(el('div',{class:'bar'},save,lintbtn,dis,status),ta,el('h2',{text:'checks'}),list);
  findings(a.findings);
}
load().catch(e=>msg(e.message,true));
</script>
"""


def stop_jobs():
    for job in JOBS.values():
        job.cancel()


def main():
    global ROM, CART, PROJECT
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("--side", choices=["sally", "maria"], default="sally",
                    help="bankset cartridges: which parallel set to work on")
    ap.add_argument("--port", type=int, default=8120)
    ap.add_argument("--project", help="where jobs write (default: <rom>-workbench "
                                      "beside the cartridge)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ROM = os.path.abspath(args.rom)
    try:
        CART = cart_module.Cart(ROM, side=args.side)
    except (cart_module.UnknownMapper, cart_module.UnknownSpace, IOError) as e:
        sys.stderr.write("%s\n" % e)
        return 2
    PROJECT = os.path.abspath(args.project or
                              os.path.splitext(ROM)[0] + "-workbench")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % args.port
    print("%s -- %s, %dK, %s"
          % (os.path.basename(ROM), CART.map.name, len(CART.rom) // 1024,
             "POKEY" if CART.pokeys() else "TIA"))
    for w in CART.warnings:
        print("  note: %s" % w)
    print("project folder: %s" % PROJECT)
    print("open %s" % url)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        # The editors are separate processes on their own ports. Leaving them
        # running after the workbench closes would hold those ports and look
        # like a stale server on the next run, which is a genuinely confusing
        # way to waste an afternoon. Jobs are stopped for the same reason.
        stop_jobs()
        stop_all()
        print("stopped %d tool%s" % (len(CHILDREN),
                                     "" if len(CHILDREN) == 1 else "s"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
