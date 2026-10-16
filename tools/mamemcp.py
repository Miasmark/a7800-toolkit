#!/usr/bin/env python3
"""An MCP server that puts a running MAME in an assistant's hands, for disassembly work.

Model Context Protocol, over stdio: an MCP client (Claude Code, Claude Desktop, ...) starts this
and calls its tools. They start MAME on a cartridge, run it a frame or an instruction at a time,
read and write memory and registers, set breakpoints and watchpoints, trace which code runs (with
its bank) and which bytes are read as data (and by whom), ask who last wrote an address and how
the CPU got where it is, find RAM variables by how they change, press buttons, save and load states,
and take screenshots -- the questions a static disassembly cannot answer, asked of the machine.

    claude mcp add a7800-mame -- python /path/to/a7800-toolkit/tools/mamemcp.py

or in a client's JSON configuration:

    {"mcpServers": {"a7800-mame": {"command": "python",
                                   "args": ["/path/to/a7800-toolkit/tools/mamemcp.py"],
                                   "env": {"A7800_MAME": "/path/to/mame",
                                           "A7800_ROMPATH": "/path/to/bios",
                                           "A7800_BIOS": "a7800pr"}}}}

MAME, the BIOS and the rompath are found as every tool here finds them (`A7800_MAME`,
`A7800_ROMPATH`, `A7800_BIOS`; docs/emulation.md).

Breakpoints, watchpoints, stepping and tracing need a MAME built with the a7800 patch
(`bankset-full.patch`), which adds `-debugger script`: a debugger with no window whose stops
hold until the script says go. Stock MAME's `-debugger none` resumes every stop at once. On a MAME
without it the server says so and runs without the debugger: frames, memory, registers, inputs,
states and screenshots still work.

The MAME side is `probes/mcp-server.lua`, which listens on 127.0.0.1 only.

`--selftest` starts MAME on a cartridge, exercises every tool, and stops it.
"""
import argparse
import base64
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import a7800  # noqa: E402
import capture  # noqa: E402
import cart as cartlib  # noqa: E402
import m6502  # noqa: E402

LUA = os.path.join(ROOT, "probes", "mcp-server.lua")
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    pass


def log(*a):
    print("mamemcp:", *a, file=sys.stderr, flush=True)


def addr(v, name="address"):
    """12345, "$C000", "0xC000" or "C000" -> int."""
    if isinstance(v, int):
        return v & 0xFFFF
    if not isinstance(v, str) or not v.strip():
        raise ToolError("%s is required" % name)
    s = v.strip()
    if s.startswith("$"):
        s = s[1:]
    elif s.lower().startswith("0x"):
        s = s[2:]
    try:
        return int(s, 16) & 0xFFFF
    except ValueError:
        raise ToolError("%s %r is not an address" % (name, v))


def pct(s):
    return "".join(c if (c.isalnum() or c in "-_.:/") else "%%%02X" % ord(c) for c in str(s))


class Mame(object):
    """One MAME process and the socket to the Lua server inside it."""

    def __init__(self):
        self.proc = None
        self.sock = None
        self.file = None
        self.seq = 0
        self.work = None
        self.rom = None
        self.cart = None
        self.debug = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, rom, debug=True, boot_frames=0, record=None, playback=None, bios=None,
              playback_offset=None, playback_force=None):
        self.stop()
        rom = os.path.abspath(os.path.expanduser(rom))
        if not os.path.isfile(rom):
            raise ToolError("no such file: %s" % rom)
        exe = capture.find_mame()
        if not exe:
            raise ToolError("MAME was not found: set A7800_MAME (docs/emulation.md)")
        roms = capture.find_rompath(rom, bios_name=bios)
        if not roms:
            raise ToolError("the 7800 BIOS was not found: set A7800_ROMPATH, and A7800_BIOS=a7800pr "
                            "for the open BIOS, or start with bios \"skip\" (docs/emulation.md)")
        machine = capture.inspect(rom)["machine"]
        bios, roms = capture.machine_setup(machine, roms, bios)
        try:
            self.cart = cartlib.Cart(rom)
        except Exception:                                        # noqa: BLE001
            self.cart = None
        self.work = tempfile.mkdtemp(prefix="mamemcp-")
        os.chmod(self.work, 0o700)
        for d in ("snap", "sta", "trace"):
            os.makedirs(os.path.join(self.work, d))
        notes = []
        for attempt in ((True,) if debug else ()) + (False,):
            port = self._free_port()
            cmd = [exe, machine] + bios + [
                "-rompath", roms, "-cart", rom, "-video", "none", "-sound", "none",
                "-skip_gameinfo", "-nothrottle",
                "-snapshot_directory", os.path.join(self.work, "snap"),
                "-state_directory", os.path.join(self.work, "sta"),
                "-autoboot_script", LUA]
            if attempt:
                cmd += ["-debug", "-debugger", "script"]
            if record:
                rec = os.path.abspath(os.path.expanduser(record))
                cmd += ["-input_directory", os.path.dirname(rec), "-record", os.path.basename(rec)]
            elif playback:
                pb = os.path.abspath(os.path.expanduser(playback))
                if not os.path.isfile(pb):
                    raise ToolError("no such recording: %s" % pb)
                cmd += ["-input_directory", os.path.dirname(pb), "-playback", os.path.basename(pb)]
                cmd += capture.playback_args(playback_offset, playback_force)
            env = dict(os.environ, A7800_MCP_PORT=str(port))
            env.setdefault("XDG_RUNTIME_DIR", self.work)
            env.setdefault("SDL_AUDIODRIVER", "dummy")
            self.out = open(os.path.join(self.work, "mame.log"), "w")
            self.proc = subprocess.Popen(cmd, cwd=self.work, env=env, stdout=self.out,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self._connect(port)
            hello = self.ask("hello")
            if attempt and "state=running" in hello:
                # the stop did not hold: this MAME has no `-debugger script`
                notes.append("this MAME has no `-debugger script` (build it with the a7800 patch, "
                             "bankset-full.patch): breakpoints, watchpoints, stepping and tracing "
                             "are off")
                self.stop(keep_work=True)
                continue
            self.debug = attempt
            break
        self.rom = rom
        self.ram_snap = None
        self.setup_banks(notes)
        if self.debug:
            self.ask("console", "trackmem 1")            # so mame_who_wrote can answer later
        if boot_frames:
            hello = self.ask("run", int(boot_frames), timeout=60 + boot_frames / 20.0)
        return hello, notes

    def setup_banks(self, notes):
        """Tell the Lua side how this cartridge's banks are named and switched, the way
        probes/exectrace.lua is told (cart.py's probe_env), so its logs name spaces as cart.py does."""
        c = self.cart
        if c is None:
            return
        name = getattr(c.map, "name", "")
        if name == "supergame" and c.nbanks > 1:
            env = c.probe_env()
            sw = c.map.switch or (0x8000, 0xBFFF)
            win = next(((r[0], r[1] - 1) for r in c._region if r[2] == "window"), (0x8000, 0xBFFF))
            self.ask("banks", "%04X-%04X" % sw, "%04X-%04X" % win, env["A7800_XT_BANKS"],
                     env.get("A7800_XT_FIRST", "0"), env.get("A7800_XT_WBANKS", env["A7800_XT_BANKS"]),
                     env.get("A7800_XT_LOWBANK", "-"), "-")
        elif name == "linear":
            self.ask("banks", "8000-BFFF", "8000-BFFF", "1", "0", "1", "-", "rom")
        else:
            self.ask("banks", "8000-BFFF", "8000-BFFF", "1", "0", "1", "-", "-")
            notes.append("bank tracking follows SuperGame-style carts; this one (%s) is traced "
                         "without banks" % (name or "unknown"))

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _connect(self, port):
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ToolError("MAME exited before it was ready:\n" + self.tail())
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                self.file = self.sock.makefile("rw", encoding="latin-1", newline="\n")
                return
            except OSError:
                time.sleep(0.2)
        raise ToolError("MAME did not open its socket within 60 seconds:\n" + self.tail())

    def tail(self, n=15):
        try:
            with open(os.path.join(self.work, "mame.log"), encoding="utf-8", errors="replace") as f:
                lines = [l for l in f.read().splitlines() if "XDG_RUNTIME_DIR" not in l]
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def ask(self, cmd, *args, timeout=60.0):
        if not self.running() or self.file is None:
            raise ToolError("MAME is not running: call mame_start first")
        self.seq += 1
        line = " ".join([str(self.seq), cmd] + [pct(a) for a in args])
        self.sock.settimeout(timeout)
        try:
            self.file.write(line + "\n")
            self.file.flush()
            got = self.file.readline()
        except socket.timeout:
            raise ToolError("MAME did not answer %r within %d seconds" % (cmd, timeout))
        except OSError as e:
            raise ToolError("lost MAME: %s\n%s" % (e, self.tail()))
        if not got:
            raise ToolError("MAME closed the connection:\n" + self.tail())
        sid, ok, text = (got.rstrip("\n").split(" ", 2) + ["", ""])[:3]
        text = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), text)
        if ok != "ok":
            raise ToolError(text)
        return text

    def stop(self, keep_work=False):
        if self.running():
            try:
                self.ask("quit", timeout=5)
            except ToolError:
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.proc = self.sock = self.file = None
        if self.work and not keep_work:
            shutil.rmtree(self.work, True)
            self.work = None


M = Mame()


# ----------------------------------------------------------------------------- disassembly
def read_bytes(a, n):
    return bytes.fromhex(M.ask("read", "0x%04X" % a, n))


def symbol(v, write=None):
    ctype = (M.cart.info or {}).get("cart_type", 0) if M.cart is not None else 0
    try:
        return a7800.sym_for(v, ctype, write)
    except Exception:                                            # noqa: BLE001
        return None


def decode(buf, pc, i):
    op = buf[i]
    entry = m6502.OPCODES.get(op)
    if not entry:
        return 1, ".byte $%02X" % op, None
    mn, mode = entry[0], entry[1]
    n = m6502.LENGTH[op]
    if i + n > len(buf):
        return 1, ".byte $%02X" % op, None
    if n == 1:
        v = None
        text = ""
    elif n == 2:
        v = buf[i + 1]
        if mode == "rel":
            v = (pc + 2 + (v - 256 if v >= 128 else v)) & 0xFFFF
            text = "$%04X" % v
        else:
            text = "$%02X" % v
    else:
        v = buf[i + 1] | (buf[i + 2] << 8)
        text = "$%04X" % v
    if v is not None and mode not in ("imm", "rel"):
        name = symbol(v, write=mn.startswith("ST"))
        if name:
            text = name
    if mode == "imm":
        text = "$%02X" % v
    operand = m6502.FMT[mode].format(v=text) if mode in m6502.FMT else text
    return n, ("%-4s %s" % (mn, operand)).rstrip(), v


def disassemble(a, count):
    buf = read_bytes(a, count * 3 + 3)
    out, i = [], 0
    for _ in range(count):
        if i >= len(buf) - 2:
            break
        pc = (a + i) & 0xFFFF
        n, text, _v = decode(buf, pc, i)
        raw = " ".join("%02X" % b for b in buf[i:i + n])
        out.append("%04X  %-9s %s" % (pc, raw, text))
        i += n
    return "\n".join(out)


# ----------------------------------------------------------------------------- trace
TRACE_LINE = re.compile(r"^([0-9A-Fa-f]{4}):\s+(\S+)\s*(.*)$")


def trace_summary(path, entries_file=None):
    pcs = {}
    loops = 0
    with open(path, encoding="latin-1") as f:
        for line in f:
            m = TRACE_LINE.match(line.strip())
            if m:
                pcs.setdefault(int(m.group(1), 16), (m.group(2).lower(), m.group(3)))
            elif "loops for" in line:
                loops += 1
    spaces = {}
    unresolved = []
    c = M.cart
    for pc, (mn, operands) in sorted(pcs.items()):
        sp = None
        if c is not None:
            sp = c.space_of(pc, None)
            if sp is None:
                sp = which_bank(c, pc, mn, operands)
        if sp is None:
            unresolved.append(pc)
        elif isinstance(sp, str):
            spaces.setdefault(sp, []).append(pc)
    lines = ["%d distinct instructions executed (%d loops collapsed by MAME's trace)"
             % (len(pcs), loops)]
    for sp, addrs in sorted(spaces.items()):
        lines.append("  %-6s %4d instructions, %s" % (sp, len(addrs), ranges(addrs)))
    if unresolved:
        lines.append("  ?      %4d instructions whose bank could not be told from their bytes: %s"
                     % (len(unresolved), ranges(unresolved)))
    if entries_file:
        locs = ["%s:%04X" % (sp, a) for sp, addrs in sorted(spaces.items()) for a in addrs
                if not sp.startswith("ram")]
        data = {}
        if os.path.isfile(entries_file):
            with open(entries_file, encoding="utf-8") as f:
                data = json.load(f)
        have = set(data.get("entries", []))
        add = [l for l in locs if l not in have]
        data["entries"] = data.get("entries", []) + add
        with open(entries_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        lines.append("added %d entry points to %s (for tools/disasm.py -c)" % (len(add), entries_file))
    lines.append("full trace: %s" % path)
    return "\n".join(lines)


def which_bank(c, pc, mn, operands):
    """MAME's trace has no bank. Tell it from the bytes: the bank whose instruction at `pc`
    disassembles to what the trace says ran."""
    if not (0x4000 <= pc < 0x10000):
        return "ram" if pc < 0x4000 else None
    nums = [int(x, 16) for x in re.findall(r"\$([0-9a-fA-F]+)", operands)]
    hits = []
    for b in range(getattr(c, "nbanks", 1)):
        sp = c.space_of(pc, b)
        if sp is None:
            continue
        try:
            buf = bytes(c.byte(sp, pc + k) for k in range(3))
        except Exception:                                        # noqa: BLE001
            continue
        entry = m6502.OPCODES.get(buf[0])
        if not entry or entry[0].lower() != mn:
            continue
        _n, _t, v = decode(buf + b"\0\0", pc, 0)
        if v is None or not nums or v in nums:
            hits.append(sp)
    hits = sorted(set(hits))
    return hits[0] if len(hits) == 1 else None


def ranges(addrs, gap=3):
    addrs = sorted(addrs)
    out, lo, hi = [], None, None
    for a in addrs:
        if lo is None:
            lo = hi = a
        elif a - hi <= gap:
            hi = a
        else:
            out.append((lo, hi))
            lo = hi = a
    if lo is not None:
        out.append((lo, hi))
    text = ", ".join("$%04X" % l if l == h else "$%04X-$%04X" % (l, h) for l, h in out[:40])
    return text + (" and %d more ranges" % (len(out) - 40) if len(out) > 40 else "")


# ----------------------------------------------------------------------------- tools
def t_start(a):
    hello, notes = M.start(a["rom"], debug=a.get("debug", True), boot_frames=a.get("boot_frames", 0),
                           record=a.get("record"), playback=a.get("playback"), bios=a.get("bios"),
                           playback_offset=a.get("playback_offset"), playback_force=a.get("playback_force"))
    c = M.cart
    desc = c.describe() if c is not None and hasattr(c, "describe") else ""
    text = "started MAME on %s\n%s" % (os.path.basename(M.rom), hello)
    if desc:
        text += "\n" + (desc if isinstance(desc, str) else str(desc))
    for n in notes:
        text += "\nnote: " + n
    return text


def t_stop(a):
    M.stop()
    return "stopped"


def t_status(a):
    return M.ask("status")


def t_run(a):
    n = int(a.get("frames", 1))
    held = []
    for h in a.get("hold", []) or []:
        M.ask("input", h["port"], h["field"], h.get("value", 1))
        held.append(h)
    try:
        return M.ask("run", n, timeout=60 + n / 10.0)
    finally:
        for h in held:
            M.ask("input", h["port"], h["field"], 0)


def need_debug(what):
    if not M.debug:
        raise ToolError("%s needs the patched MAME's -debugger script" % what)


def console(cmd, limit=600, timeout=120):
    return M.ask("console", cmd, int(limit), timeout=timeout)


def t_step(a):
    need_debug("stepping")
    mode = a.get("mode", "into")
    n = int(a.get("instructions", 1))
    if mode == "over":
        return console("over %d" % n)                    # a JSR counts as one instruction
    if mode == "out":
        return console("out")                            # to the RTS/RTI that leaves this routine
    return M.ask("step", n)


def t_until(a):
    need_debug("running to an event")
    ev = a.get("event", "interrupt")
    limit = int(a.get("frames_limit", 600))
    if ev == "interrupt":
        return console("gint", limit)
    if ev == "vblank":
        return console("gvblank", limit)
    if ev == "condition":
        cond = a.get("condition")
        if not cond:
            raise ToolError("a condition is needed, e.g. \"a==3\" or \"pc==C000 && x>2\"")
        console("rpclear")
        console("rp {%s}" % cond)
        try:
            return console("go", limit)
        finally:
            console("rpclear")
    raise ToolError("event is interrupt, vblank or condition")


def t_regs(a):
    for k, v in (a.get("set") or {}).items():
        M.ask("setreg", k.upper(), "0x%X" % (addr(v) if isinstance(v, str) else int(v)))
    return M.ask("regs")


def t_read(a):
    base, n = addr(a["address"]), max(1, min(int(a.get("length", 64)), 4096))
    data = read_bytes(base, n)
    out = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        out.append("%04X  %-47s  %s" % ((base + i) & 0xFFFF, " ".join("%02X" % b for b in row),
                                         "".join(chr(b) if 32 <= b < 127 else "." for b in row)))
    return "\n".join(out)


def t_write(a):
    hexs = re.sub(r"[\s,$]", "", a["bytes"])
    if not re.fullmatch(r"([0-9A-Fa-f]{2})+", hexs):
        raise ToolError("bytes must be hex, e.g. \"A9 00 8D 00 80\"")
    return M.ask("write", "0x%04X" % addr(a["address"]), hexs)


def t_dasm(a):
    if "address" in a and a["address"] not in (None, ""):
        base = addr(a["address"])
    else:
        base = int(re.search(r"PC=([0-9A-F]+)", M.ask("regs")).group(1), 16)
    return disassemble(base, max(1, min(int(a.get("count", 20)), 200)))


def t_snap(a):
    before = set(glob.glob(os.path.join(M.work, "snap", "**", "*.png"), recursive=True))
    M.ask("snap")
    deadline = time.time() + 10
    new = []
    while time.time() < deadline:
        new = sorted(set(glob.glob(os.path.join(M.work, "snap", "**", "*.png"), recursive=True)) - before,
                     key=os.path.getmtime)
        if new:
            break
        M.ask("status")                                          # let MAME write it
        time.sleep(0.1)
    if not new:
        raise ToolError("MAME wrote no snapshot")
    time.sleep(0.1)
    with open(new[-1], "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return [{"type": "image", "data": data, "mimeType": "image/png"},
            {"type": "text", "text": "%s\n%s" % (new[-1], M.ask("status"))}]


def t_bp(a):
    if not M.debug:
        raise ToolError("breakpoints need the patched MAME's -debugger script")
    act = a.get("action", "list")
    if act == "set":
        args = ["set", "0x%04X" % addr(a.get("address"))]
        if a.get("condition"):
            args.append(a["condition"])
        return M.ask("bp", *args)
    if act == "clear":
        return M.ask("bp", "clear", *(["0x%X" % int(a["id"])] if a.get("id") is not None else []))
    return M.ask("bp", "list") or "no breakpoints"


def t_wp(a):
    if not M.debug:
        raise ToolError("watchpoints need the patched MAME's -debugger script")
    act = a.get("action", "list")
    if act == "set":
        args = ["set", a.get("kind", "w"), "0x%04X" % addr(a.get("address")), int(a.get("length", 1)),
                a.get("condition") or "-", a.get("space", "cpu")]
        return M.ask("wp", *args)
    if act == "clear":
        return M.ask("wp", "clear")
    return M.ask("wp", "list") or "no watchpoints"


def t_trace(a):
    if not M.debug:
        raise ToolError("tracing needs the patched MAME's -debugger script")
    n = int(a.get("frames", 60))
    path = os.path.join(M.work, "trace", "trace-%d.log" % M.seq)
    M.ask("trace", path)
    try:
        status = M.ask("run", n, timeout=120 + n / 2.0)
    finally:
        M.ask("trace", "off")
    deadline = time.time() + 5
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.1)
    return status + "\n" + trace_summary(path, a.get("entries_file"))


def t_writes(a):
    lo, hi = addr(a["start"], "start"), addr(a.get("end", a["start"]), "end")
    limit = int(a.get("limit", 2000))
    M.ask("tap", "0x%04X" % lo, "0x%04X" % hi, limit)
    try:
        status = M.ask("run", int(a.get("frames", 60)), timeout=60 + int(a.get("frames", 60)) / 10.0)
    finally:
        got = M.ask("untap")
    rows = [r.split() for r in got.splitlines() if r.strip()]
    by = {}
    for fr, pc, ad, v in rows:
        e = by.setdefault(ad, {"n": 0, "pcs": {}, "vals": []})
        e["n"] += 1
        e["pcs"][pc] = e["pcs"].get(pc, 0) + 1
        if len(e["vals"]) < 8 and v not in e["vals"]:
            e["vals"].append(v)
    out = ["%s\n%d writes to $%04X-$%04X%s" % (status, len(rows), lo, hi,
                                                 " (limit reached)" if len(rows) >= limit else "")]
    for ad in sorted(by, key=lambda k: int(k, 16)):
        e = by[ad]
        pcs = ", ".join("%s x%d" % (p, n) for p, n in sorted(e["pcs"].items(), key=lambda x: -x[1])[:6])
        out.append("  $%s  %5d writes  from %s  values %s" % (ad, e["n"], pcs, " ".join(e["vals"])))
    return "\n".join(out)


def t_input(a):
    if a.get("action", "list") == "list":
        return M.ask("inputs")
    return M.ask("input", a["port"], a["field"], int(a.get("value", 1)))


def t_save(a):
    return M.ask("save", a.get("name", "mcp"))


def t_load(a):
    return M.ask("load", a.get("name", "mcp"))


def parse_xtrace(text):
    """An exectrace log -> {kind: {key: value}}: sets for X/J/JR/M (M keeps the opcode), counts for
    S/C/I."""
    d = {k: {} for k in ("X", "J", "JR", "M", "S", "C", "I", "G")}
    for line in text.splitlines():
        p = line.split()
        if not p or p[0] not in d:
            continue
        if p[0] in ("S", "C", "I", "G") and p[-1].startswith("x"):
            k = " ".join(p[1:-1])
            d[p[0]][k] = d[p[0]].get(k, 0) + int(p[-1][1:])
        elif p[0] == "M":
            d["M"].setdefault(p[1], p[2] if len(p) > 2 else "")
        else:
            d[p[0]][" ".join(p[1:])] = True
    return d


def write_xtrace(d, path):
    lines = []
    for kind in ("X", "J", "JR"):
        lines += ["%s %s" % (kind, k) for k in d[kind]]
    lines += ["M %s %s" % (k, v) for k, v in d["M"].items()]
    lines += ["%s %s x%d" % (kind, k, n) for kind in ("S", "C", "I", "G") for k, n in d[kind].items()]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(lines)) + "\n")


def t_exectrace(a):
    need_debug("tracing")
    n = int(a.get("frames", 60))
    path = os.path.abspath(a.get("log") or os.path.join(M.work, "trace", "exectrace-%d.log" % (M.seq + 1)))
    M.ask("xtrace", "on", path)
    try:
        status = M.ask("run", n, timeout=120 + n / 2.0)
    finally:
        done = M.ask("xtrace", "off")
    d = parse_xtrace(open(path, encoding="utf-8").read())
    out = [status, done]
    xs = {}
    for k in d["X"]:
        sp, _, ad = k.partition(":")
        xs.setdefault(sp, []).append(int(ad, 16))
    for sp in sorted(xs):
        out.append("  %-5s %5d instructions: %s" % (sp, len(xs[sp]), ranges(xs[sp])))
    if d["J"] or d["JR"]:
        js = sorted(list(d["J"]) + list(d["JR"]))
        out.append("indirect jumps taken: " + "; ".join(j.replace(" ", " -> ") for j in js[:30])
                   + (" ..." if len(js) > 30 else ""))
    if d["S"]:
        out.append("bank switches: " + "; ".join("%s x%d" % kv for kv in sorted(d["S"].items())[:30]))
    if d["I"]:
        out.append("interrupts: " + "; ".join("%s -> %s x%d" % (k.split()[0], k.split()[1], v)
                                              for k, v in sorted(d["I"].items())))
    out.append(callgraph_summary(d, a.get("callgraph")))
    if d["M"]:
        out.append(ram_code(d, a.get("ram_dump")))
    if a.get("merge_into"):
        out.append(merge_trace(d, a["merge_into"]))
    if a.get("annotations"):
        out.append(run_dyn(path, a["annotations"], a.get("dataread")))
    else:
        out.append("python tools/dyn.py %s %s -c annotations.json  turns this into entry points and "
                   "bank pins" % (M.rom, path))
    return "\n".join(out)


def callgraph_summary(d, dot=None):
    """Who called whom (JSR), how often: per callee, its callers."""
    callers = {}
    for k, n in d["C"].items():
        frm, to = k.split()
        callers.setdefault(to, []).append((n, frm))
    if not callers:
        return "no JSR ran"
    lines = ["call graph: %d routines called from %d sites (JSR, as run)" % (
        len(callers), len({k.split()[0] for k in d["C"]}))]
    for to in sorted(callers, key=lambda t: -sum(n for n, _ in callers[t]))[:25]:
        cs = sorted(callers[to], reverse=True)
        lines.append("  %-11s x%-6d from %s" % (to, sum(n for n, _ in cs),
                                                 ", ".join("%s x%d" % (f, n) for n, f in cs[:5])
                                                 + (" and %d more" % (len(cs) - 5) if len(cs) > 5 else "")))
    if dot:
        dot = os.path.abspath(dot)
        with open(dot, "w", encoding="utf-8") as f:
            f.write("digraph calls {\n  node [shape=box, fontname=monospace];\n")
            for k, n in sorted(d["I"].items()):
                vec, to = k.split()
                f.write('  "%s" -> "%s" [label="%d"];\n' % ("IRQ/BRK" if vec == "$FFFE" else "NMI", to, n))
            for k, n in sorted(d["G"].items()):
                frm, to = k.split()
                frm = "entered before the trace" if frm == "?" else frm
                f.write('  "%s" -> "%s" [label="%d"];\n' % (frm, to, n))
            f.write("}\n")
        lines.append("wrote %s (Graphviz: dot -Tsvg %s -o calls.svg); each call is drawn from the routine "
                     "that made it, as the run's own call stack had it" % (dot, os.path.basename(dot)))
    return "\n".join(lines)


def ram_code(d, dump=None):
    """Code that ran from RAM: where, disassembled as it is now, and whether it has changed since."""
    addrs = sorted(int(k.split(":")[1], 16) for k in d["M"])
    lines = ["code ran from RAM: %d instructions, %s" % (len(addrs), ranges(addrs))]
    blocks = []
    for lo, hi in _runs(addrs):
        data = read_bytes(lo, hi - lo + 3)
        blocks.append((lo, data))
        changed = [a for a in addrs if lo <= a <= hi and
                   "%02X" % data[a - lo] != d["M"]["ram:%04X" % a].upper()]
        lines.append("  $%04X-$%04X%s:" % (lo, hi, "  (changed since it ran at %s)"
                                           % ", ".join("$%04X" % a for a in changed[:8]) if changed else ""))
        i = 0
        while lo + i <= hi:
            nb, text, _v = decode(data + b"\0\0", lo + i, i)
            mark = "" if ("ram:%04X" % (lo + i)) in d["M"] else "   ; not seen to run"
            lines.append("    %04X  %-9s %s%s" % (lo + i, " ".join("%02X" % b for b in data[i:i + nb]), text, mark))
            i += nb
    if dump:
        dump = os.path.abspath(dump)
        with open(dump, "w", encoding="utf-8") as f:
            for lo, data in blocks:
                f.write("$%04X: %s\n" % (lo, data.hex(" ").upper()))
        lines.append("wrote the bytes to %s" % dump)
    return "\n".join(lines)


def _runs(addrs, gap=3):
    out, lo, hi = [], None, None
    for a in sorted(addrs):
        if lo is None:
            lo = hi = a
        elif a - hi <= gap:
            hi = a
        else:
            out.append((lo, hi))
            lo = hi = a
    if lo is not None:
        out.append((lo, hi))
    return out


def merge_trace(d, path):
    """Fold this run into a cumulative log, so coverage builds up across runs and sessions."""
    path = os.path.abspath(path)
    old = parse_xtrace(open(path, encoding="utf-8").read()) if os.path.isfile(path) else parse_xtrace("")
    before = len(old["X"]) + len(old["M"])
    for kind in ("X", "J", "JR"):
        old[kind].update(d[kind])
    for k, v in d["M"].items():
        old["M"].setdefault(k, v)
    for kind in ("S", "C", "I", "G"):
        for k, n in d[kind].items():
            old[kind][k] = old[kind].get(k, 0) + n
    write_xtrace(old, path)
    after = len(old["X"]) + len(old["M"])
    return "merged into %s: %d instructions known, %d of them new from this run" % (path, after, after - before)


def t_rewind(a):
    need_debug("rewinding")
    return M.ask("rewind", int(a.get("steps", 1)), timeout=60)


def run_dyn(log, ann, dataread=None):
    ann = os.path.abspath(ann)
    if not os.path.exists(ann):
        with open(ann, "w", encoding="utf-8") as f:
            f.write("{}\n")                                # dyn.py adds to a file; start one
    cmd = [sys.executable, os.path.join(HERE, "dyn.py"), M.rom, log, "-c", ann]
    if dataread:
        cmd += ["--dataread", dataread]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=1800)
    return "dyn.py: " + "\n".join(p.stdout.strip().splitlines()[-12:])


READ_MODES = {"abs", "abx", "aby", "izx", "izy", "ind", "zp", "zpx", "zpy"}


def classify_read(target, insn_hex):
    """Was the instruction at CURPC able to read `target`? If not, the read was MARIA's DMA (it goes
    through the CPU's address space while the CPU is held)."""
    b = bytes.fromhex(insn_hex)
    e = m6502.OPCODES.get(b[0])
    if not e or e[1] not in READ_MODES or e[0] in ("STA", "STX", "STY", "JMP", "JSR") and e[1] != "ind":
        return False
    mode, op16 = e[1], b[1] | (b[2] << 8)
    if mode == "abs":
        return target == op16
    if mode in ("abx", "aby"):
        return 0 <= (target - op16) & 0xFFFF <= 0xFF
    if mode in ("zp", "zpx", "zpy"):
        return target < 0x100
    return True                                           # through a pointer: cannot tell, believe it


def t_reads(a):
    lo, hi = addr(a.get("start", "$4000"), "start"), addr(a.get("end", "$FFFF"), "end")
    n = int(a.get("frames", 60))
    M.ask("rtap", "0x%04X" % lo, "0x%04X" % hi, int(a.get("limit", 20000)))
    try:
        status = M.ask("run", n, timeout=120 + n / 2.0)
    finally:
        got = M.ask("untap_reads", timeout=120)
    insn, rows, mrows = {}, [], []
    for line in got.splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "I":
            insn[int(p[1], 16)] = p[2]
        elif len(p) == 3 and p[0] == "M":
            mrows.append((p[1], int(p[2])))
        elif len(p) == 3:
            rows.append((p[0], int(p[1], 16), int(p[2])))
    # on the a7800 patch MARIA has its own address space and its reads are counted there; the CPU's
    # tap then sees only the CPU. Elsewhere MARIA's fetches come through the CPU's space and are
    # told apart by what the instruction at CURPC could have read.
    exact = "maria_space=yes" in status
    cpu, maria = {}, {}
    for loc, cnt in mrows:
        e = maria.setdefault(loc, [0, {}])
        e[0] += cnt
    for loc, at, cnt in rows:
        sp, _, ad = loc.partition(":")
        target = int(ad, 16)
        c = M.cart
        if c is not None:
            try:
                if not c.in_space(sp, target):             # cart RAM, or a region the map lacks
                    continue
            except Exception:                              # noqa: BLE001 -- a space cart.py lacks
                continue
        ib = insn.get(at, "000000")
        n = m6502.LENGTH.get(int(ib[:2], 16), 1)
        if 0 <= (target - at) & 0xFFFF < n + 1:
            continue                                       # the instruction's own bytes, or a dummy read after them
        op = m6502.OPCODES.get(int(ib[:2], 16))
        if op and op[0] in ("RTS", "RTI", "JSR", "BRK"):
            continue                                       # the stack's and the return address's own reads
        if 0xFFFA <= target <= 0xFFFF and not (op and op[1] == "abs" and int(ib[4:6] + ib[2:4], 16) == target):
            continue                                       # an interrupt fetching its vector mid-instruction
        mine = True if exact else classify_read(target, ib)
        d = cpu if mine else maria
        e = d.setdefault(loc, [0, {}])
        e[0] += cnt
        if mine:
            e[1][at] = e[1].get(at, 0) + cnt
    out = [status, "%d bytes read as data by the CPU, %d by MARIA's DMA (graphics, display lists)%s"
           % (len(cpu), len(maria), " -- MARIA's own address space, so exact" if exact
              else " -- told apart by the reading instruction (this MAME has no MARIA address space)")]
    byreader = {}
    for loc, (cnt, pcs) in cpu.items():
        for at in pcs:
            byreader.setdefault(at, []).append(loc)
    for at in sorted(byreader, key=lambda k: -len(byreader[k]))[:25]:
        locs = byreader[at]
        sp = locs[0].split(":")[0]
        out.append("  $%04X  %-12s reads %4d bytes  %s: %s" % (
            at, disassemble_one(at, insn.get(at)), len(locs), sp,
            ranges([int(l.split(":")[1], 16) for l in locs if l.startswith(sp + ":")])))
    if maria:
        bysp = {}
        for loc in maria:
            sp, _, ad = loc.partition(":")
            bysp.setdefault(sp, []).append(int(ad, 16))
        for sp in sorted(bysp):
            out.append("  MARIA %-5s %5d bytes: %s" % (sp, len(bysp[sp]), ranges(bysp[sp])))
    path = os.path.abspath(a.get("dataread") or os.path.join(M.work, "trace", "dataread-%d.log" % M.seq))
    with open(path, "w", encoding="utf-8") as f:
        for loc in sorted(set(cpu) | set(maria)):
            f.write("D %s x%d\n" % (loc, (cpu.get(loc) or maria.get(loc))[0]))
    out.append("wrote %s (dataread.log format, CPU and MARIA reads: tools/dyn.py --dataread)" % path)
    return "\n".join(out)


def disassemble_one(at, insn_hex):
    if not insn_hex:
        return "?"
    return decode(bytes.fromhex(insn_hex) + b"\0\0", at, 0)[1]


def t_who_wrote(a):
    need_debug("who-wrote")
    ad = addr(a["address"])
    got = console("pcatmem 0x%04X" % ad).strip()
    m = re.fullmatch(r"([0-9A-Fa-f]{1,4})", got)
    if not m:
        return got or "MAME has no record of a write to $%04X since the session started" % ad
    pc = int(m.group(1), 16)
    return "$%04X was last written by the instruction at $%04X (as the bank now switched in has it: %s)" % (
        ad, pc, disassemble(pc, 1).split("  ", 1)[1].strip())


def t_history(a):
    need_debug("the history")
    n = int(a.get("count", 20))
    out = ["the last %d instructions, oldest first:" % n, console("history :maincpu,%d" % n).strip()]
    regs = dict(kv.split("=") for kv in M.ask("regs").split())
    sp = int(regs.get("SP", "1FF"), 16) & 0xFF
    stack = read_bytes(0x0100 + ((sp + 1) & 0xFF), 0xFF - sp) if sp < 0xFF else b""
    calls = []
    for i in range(len(stack) - 1):
        ret = stack[i] | (stack[i + 1] << 8)
        site = (ret - 2) & 0xFFFF
        try:
            if read_bytes(site, 1)[0] == 0x20:                   # JSR: this is a return address
                tgt = read_bytes((site + 1) & 0xFFFF, 2)
                calls.append("$%04X: JSR $%04X" % (site, tgt[0] | (tgt[1] << 8)))
        except ToolError:
            pass
    out.append("return addresses on the stack (innermost first): " + ("; ".join(calls) or "none that follow a JSR"))
    return "\n".join(out)


# the 7800's RAM once each: $0040-$00FF and $0140-$01FF are the same bytes as $2040-$20FF and
# $2140-$21FF, which are left out
RAM_RANGES = ((0x0040, 0x00FF), (0x0140, 0x01FF), (0x1800, 0x203F), (0x2100, 0x213F), (0x2200, 0x27FF))


def ram_now():
    out = {}
    for lo, hi in RAM_RANGES:
        data = read_bytes(lo, hi - lo + 1)
        for i, v in enumerate(data):
            out[lo + i] = v
    return out


def t_ram_search(a):
    act = a.get("action", "start")
    if act == "start" or M.ram_snap is None:
        M.ram_snap = ram_now()
        M.ram_cand = set(M.ram_snap)
        M.ram_hist = {k: [v] for k, v in M.ram_snap.items()}
        return "remembered %d bytes of RAM; now change something in the game and compare" % len(M.ram_cand)
    now = ram_now()
    op = a.get("op", "changed")
    want = a.get("value")
    keep = set()
    for ad in M.ram_cand:
        old, new = M.ram_snap[ad], now[ad]
        ok = {"changed": new != old, "unchanged": new == old, "increased": new > old,
              "decreased": new < old, "equal": want is not None and new == int(want),
              "by": want is not None and ((new - old) & 0xFF) == (int(want) & 0xFF)}.get(op)
        if ok is None:
            raise ToolError("op is changed, unchanged, increased, decreased, equal (value) or by (value)")
        if ok:
            keep.add(ad)
    M.ram_cand = keep
    for ad in keep:
        M.ram_hist[ad].append(now[ad])
    M.ram_snap = now
    lines = ["%d candidates" % len(keep)]
    for ad in sorted(keep)[:40]:
        lines.append("  $%04X  %s" % (ad, " ".join("%02X" % v for v in M.ram_hist[ad][-8:])))
    return "\n".join(lines)


def t_find(a):
    need_debug("searching memory")
    lo, hi = addr(a.get("start", "$0000"), "start"), addr(a.get("end", "$FFFF"), "end")
    data = re.sub(r"[\s,$]", "", a["bytes"])
    if not re.fullmatch(r"([0-9A-Fa-f]{2})+", data):
        raise ToolError("bytes must be hex, e.g. \"8D 00 80\"")
    args = ",".join("0x" + data[i:i + 2] for i in range(0, len(data), 2))
    return console("find 0x%04X,0x%X,%s" % (lo, hi - lo + 1, args)).strip() or "not found"


def t_debugger(a):
    need_debug("the debugger console")
    return console(a["command"], int(a.get("frames_limit", 600))).strip() or "(no output)"


ADDR = {"type": ["string", "integer"], "description": "an address: \"$C000\", \"0xC000\", \"C000\" or a number"}
TOOLS = [
    ("mame_start", t_start, "Start MAME on a 7800 cartridge (.a78), stopped at the first instruction of "
     "the BIOS (with bios \"skip\", of the cartridge). Any MAME already started is stopped first.",
     {"rom": {"type": "string", "description": "path to the .a78 image"},
      "bios": {"type": "string", "description": "MAME -bios name, overriding A7800_BIOS: \"skip\" (the patched "
               "MAME) boots straight into the cartridge, with no BIOS files and no BIOS frames"},
      "boot_frames": {"type": "integer", "description": "frames to run before returning (default 0)"},
      "debug": {"type": "boolean", "description": "use the debugger (default true; needs the patched MAME)"},
      "record": {"type": "string", "description": "record the session's input to this .inp file, to replay later"},
      "playback": {"type": "string", "description": "replay a recorded .inp file: the same play, again, "
                   "so a run can be traced (or traced more) without playing it twice"},
      "playback_offset": {"type": "integer", "description": "shift the playback for a game that starts on another "
                          "frame (another BIOS, bios \"skip\", a re-signed build): N > 0 drops the recording's first N "
                          "frames, N < 0 waits N frames; tools/inpoffset.py measures N (the patched MAME)"},
      "playback_force": {"type": "boolean", "description": "keep playing past \"Out of sync\" (the patched MAME)"}},
     ["rom"]),
    ("mame_stop", t_stop, "Stop MAME.", {}, []),
    ("mame_status", t_status, "Frame, whether it is stopped, and the CPU registers.", {}, []),
    ("mame_run", t_run, "Run the machine for some frames (60 a second on NTSC) and stop; a breakpoint "
     "or watchpoint stops it early. `hold` presses inputs for the duration (see mame_input).",
     {"frames": {"type": "integer"},
      "hold": {"type": "array", "items": {"type": "object", "properties": {
          "port": {"type": "string"}, "field": {"type": "string"}, "value": {"type": "integer"}}}}}, []),
    ("mame_step", t_step, "Execute some instructions and stop. mode \"over\" treats a JSR as one "
     "instruction; \"out\" runs to the RTS or RTI that leaves the current routine.",
     {"instructions": {"type": "integer"}, "mode": {"type": "string", "enum": ["into", "over", "out"]}}, []),
    ("mame_run_until", t_until, "Run until the next interrupt is taken (finds NMI/IRQ handlers, including "
     "ones reached through RAM vectors), the next vertical blank, or a register condition holds (a MAME "
     "debugger expression: \"a==3\", \"pc==C000 && x>2\"); gives up after frames_limit frames.",
     {"event": {"type": "string", "enum": ["interrupt", "vblank", "condition"]},
      "condition": {"type": "string"}, "frames_limit": {"type": "integer"}}, ["event"]),
    ("mame_registers", t_regs, "Read the 6502's registers, or set some first (e.g. {\"PC\": \"$C000\"}).",
     {"set": {"type": "object"}}, []),
    ("mame_read_memory", t_read, "Hex dump of the CPU's address space as it is now (RAM, TIA/MARIA/RIOT, the "
     "cartridge with its current bank). Reading a hardware register can have side effects.",
     {"address": ADDR, "length": {"type": "integer"}}, ["address"]),
    ("mame_write_memory", t_write, "Write bytes into the CPU's address space.",
     {"address": ADDR, "bytes": {"type": "string", "description": "hex, e.g. \"A9 00 8D 00 80\""}},
     ["address", "bytes"]),
    ("mame_disassemble", t_dasm, "Disassemble live memory (the bank now switched in) with hardware register "
     "names; at the PC if no address is given.",
     {"address": ADDR, "count": {"type": "integer"}}, []),
    ("mame_screenshot", t_snap, "What the screen shows now, as a PNG.", {}, []),
    ("mame_breakpoint", t_bp, "Set, clear or list execution breakpoints. A condition is a MAME debugger "
     "expression, e.g. \"a==3\".",
     {"action": {"type": "string", "enum": ["set", "clear", "list"]}, "address": ADDR,
      "condition": {"type": "string"}, "id": {"type": "integer"}}, []),
    ("mame_watchpoint", t_wp, "Set, clear or list memory watchpoints; the machine stops when the range is "
     "read or written.",
     {"action": {"type": "string", "enum": ["set", "clear", "list"]}, "address": ADDR,
      "length": {"type": "integer"}, "kind": {"type": "string", "enum": ["r", "w", "rw"]},
      "condition": {"type": "string", "description": "a MAME expression, e.g. \"wpdata==3\" to stop only "
                    "when 3 is written"},
      "space": {"type": "string", "enum": ["cpu", "maria"], "description": "whose accesses to watch: the "
                "CPU's (default) or MARIA's DMA (needs the a7800 patch's separate MARIA space)"}}, []),
    ("mame_trace", t_trace, "Run some frames with MAME's own instruction trace, and summarise which code "
     "ran, per cartridge bank (told from the bytes, since MAME's trace does not record it; "
     "mame_exectrace knows the bank). "
     "`entries_file` adds the executed addresses as entry points to an annotations file for "
     "tools/disasm.py -c.",
     {"frames": {"type": "integer"}, "entries_file": {"type": "string"}}, []),
    ("mame_watch_writes", t_writes, "Run some frames and report every write to an address range: how many, "
     "from which PCs, with which values. Finds who owns a variable or a table in RAM.",
     {"start": ADDR, "end": ADDR, "frames": {"type": "integer"}, "limit": {"type": "integer"}}, ["start"]),
    ("mame_exectrace", t_exectrace, "Run some frames recording every cartridge instruction fetched WITH THE "
     "BANK it ran in (the bank-select stores are watched), every indirect jump's target and every bank "
     "switch (probes/exectrace.lua's log), plus the call graph (every JSR, with counts), which handler "
     "each interrupt entered, and code that ran from RAM, disassembled. `annotations` runs tools/dyn.py "
     "on it, adding entry points and bank pins and re-running the disassembler.",
     {"frames": {"type": "integer"}, "log": {"type": "string"}, "annotations": {"type": "string"},
      "dataread": {"type": "string", "description": "a dataread log (mame_watch_reads) to pass to dyn.py too"},
      "merge_into": {"type": "string", "description": "a cumulative log this run is folded into, so "
                     "coverage builds up across runs and sessions (dyn.py reads it like any other)"},
      "callgraph": {"type": "string", "description": "write the call graph (JSRs and interrupts, with "
                    "counts) here as a Graphviz .dot file"},
      "ram_dump": {"type": "string", "description": "write the bytes of the code that ran from RAM here"}},
     []),
    ("mame_rewind", t_rewind, "Put the machine back as it was before one of the last runs or steps (a "
     "state is kept each time it is set going from a stop, up to 64).",
     {"steps": {"type": "integer"}}, []),
    ("mame_watch_reads", t_reads, "Run some frames and log every DATA read (not instruction fetches) of a "
     "range, by bank: which instruction reads which table, and which bytes MARIA's DMA reads (graphics, "
     "display lists) -- exactly, on the a7800 patch's MAME, where MARIA has its own address space. "
     "Writes a dataread log that tools/dyn.py --dataread uses to mark tables as data.",
     {"start": ADDR, "end": ADDR, "frames": {"type": "integer"}, "limit": {"type": "integer"},
      "dataread": {"type": "string"}}, []),
    ("mame_who_wrote", t_who_wrote, "The PC of the last instruction that wrote an address, any time since "
     "MAME started (MAME's memory tracking): where a value came from, without running again.",
     {"address": ADDR}, ["address"]),
    ("mame_history", t_history, "How the CPU got here: the last instructions it executed, and the return "
     "addresses on the stack that follow a JSR.", {"count": {"type": "integer"}}, []),
    ("mame_ram_search", t_ram_search, "Find a RAM variable by how it changes: `start` remembers RAM, then "
     "`compare` with op changed/unchanged/increased/decreased, equal (value) or by (value, the "
     "difference) keeps the bytes that did, after you play or run frames in between.",
     {"action": {"type": "string", "enum": ["start", "compare"]},
      "op": {"type": "string", "enum": ["changed", "unchanged", "increased", "decreased", "equal", "by"]},
      "value": {"type": "integer"}}, []),
    ("mame_find", t_find, "Search memory as the CPU sees it now (RAM, the switched-in bank) for a byte string.",
     {"start": ADDR, "end": ADDR, "bytes": {"type": "string"}}, ["bytes"]),
    ("mame_debugger", t_debugger, "Any MAME debugger console command, with what it printed (e.g. \"map 4000\", "
     "\"symlist\", \"dasm file,8000,100\"). One that sets the machine running is answered when it stops.",
     {"command": {"type": "string"}, "frames_limit": {"type": "integer"}}, ["command"]),
    ("mame_input", t_input, "List the inputs, or set one (value 1 pressed, 0 released) until changed.",
     {"action": {"type": "string", "enum": ["list", "set"]}, "port": {"type": "string"},
      "field": {"type": "string"}, "value": {"type": "integer"}}, []),
    ("mame_save_state", t_save, "Save the machine's state under a name.", {"name": {"type": "string"}}, []),
    ("mame_load_state", t_load, "Load a state saved under a name.", {"name": {"type": "string"}}, []),
]
HANDLERS = {name: fn for name, fn, _d, _p, _r in TOOLS}


def tool_list():
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req}}
            for name, _fn, desc, props, req in TOOLS]


def call(name, args):
    fn = HANDLERS.get(name)
    if fn is None:
        return {"content": [{"type": "text", "text": "no tool %s" % name}], "isError": True}
    try:
        got = fn(args or {})
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:                                       # noqa: BLE001
        return {"content": [{"type": "text", "text": "%s: %s" % (type(e).__name__, e)}], "isError": True}
    if isinstance(got, list):
        return {"content": got, "isError": False}
    return {"content": [{"type": "text", "text": got}], "isError": False}


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None                                              # a notification
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        result = {"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "a7800-mame", "version": "1.0"},
                  "instructions": "Drive MAME on an Atari 7800 cartridge: start it with mame_start, "
                                  "then run, step, read memory, disassemble, trace and watch writes."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        p = msg.get("params") or {}
        result = call(p.get("name"), p.get("arguments"))
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no method %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve():
    out = sys.stdout
    sys.stdout = sys.stderr                                      # nothing else may write to the pipe
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "parse error"}}) + "\n")
                out.flush()
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                r = handle(m)
                if r is not None:
                    out.write(json.dumps(r) + "\n")
                    out.flush()
    finally:
        M.stop()


def selftest(rom):
    """Every tool once, against a real MAME."""
    def t(tool, **a):
        r = call(tool, a)
        text = " | ".join(c.get("text", "<%s %d bytes>" % (c["type"], len(c.get("data", ""))))
                          for c in r["content"])
        print("%-18s %s %s" % (tool, "ERR" if r["isError"] else "ok ", text[:300].replace("\n", " / ")))
        return r
    try:
        t("mame_start", rom=rom, boot_frames=600)
        t("mame_status")
        t("mame_registers")
        t("mame_disassemble", count=8)
        t("mame_read_memory", address="$1800", length=32)
        t("mame_write_memory", address="$2600", bytes="DE AD BE EF")
        t("mame_read_memory", address="$2600", length=4)
        t("mame_step", instructions=5)
        t("mame_breakpoint", action="set", address=re.search(r"PC=([0-9A-F]+)",
                                                              call("mame_status", {})["content"][0]["text"]).group(1))
        t("mame_run", frames=60)
        t("mame_breakpoint", action="list")
        t("mame_breakpoint", action="clear")
        t("mame_watchpoint", action="set", address="$0080", length=1, kind="w")
        t("mame_run", frames=60)
        t("mame_watchpoint", action="clear")
        t("mame_trace", frames=10)
        t("mame_exectrace", frames=10, callgraph=os.path.join(M.work, "calls.dot"),
          merge_into=os.path.join(M.work, "cumulative.log"))
        t("mame_rewind", steps=1)
        t("mame_watch_reads", frames=10)
        t("mame_history", count=5)
        t("mame_who_wrote", address="$0040")
        t("mame_step", mode="over", instructions=2)
        t("mame_run_until", event="vblank")
        t("mame_run_until", event="condition", condition="a==0", frames_limit=30)
        t("mame_ram_search", action="start")
        t("mame_run", frames=10)
        t("mame_ram_search", action="compare", op="changed")
        t("mame_find", start="$C000", end="$FFFF", bytes="60")
        t("mame_debugger", command="map 8000")
        t("mame_watch_writes", start="$0040", end="$00FF", frames=10)
        t("mame_input", action="set", port=":BUTTONS", field="P1 Button 1", value=1)
        t("mame_run", frames=5, hold=[{"port": ":JOYSTICKS", "field": "P1 Up"}])
        t("mame_save_state", name="t1")
        t("mame_load_state", name="t1")
        t("mame_screenshot")
    finally:
        t("mame_stop")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", metavar="ROM", help="exercise every tool on ROM and exit")
    args = ap.parse_args(argv)
    if args.selftest:
        selftest(args.selftest)
        return 0
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
