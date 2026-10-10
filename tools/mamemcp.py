#!/usr/bin/env python3
"""An MCP server that puts a running MAME in an assistant's hands, for disassembly work.

Model Context Protocol, over stdio: an MCP client (Claude Code, Claude Desktop, ...) starts this
and calls its tools. They start MAME on a cartridge, run it a frame or an instruction at a time,
read and write memory and registers, set breakpoints and watchpoints, trace which code runs,
log who writes where, press buttons, save and load states, and take screenshots -- the questions a
static disassembly cannot answer, asked of the machine itself.

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

    def start(self, rom, debug=True, boot_frames=0):
        self.stop()
        rom = os.path.abspath(os.path.expanduser(rom))
        if not os.path.isfile(rom):
            raise ToolError("no such file: %s" % rom)
        exe = capture.find_mame()
        if not exe:
            raise ToolError("MAME was not found: set A7800_MAME (docs/emulation.md)")
        roms = capture.find_rompath(rom)
        if not roms:
            raise ToolError("the 7800 BIOS was not found: set A7800_ROMPATH, and A7800_BIOS=a7800pr "
                            "for the open BIOS (docs/emulation.md)")
        machine = capture.inspect(rom)["machine"]
        bios, roms = capture.machine_setup(machine, roms)
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
        if boot_frames:
            hello = self.ask("run", int(boot_frames), timeout=60 + boot_frames / 20.0)
        return hello, notes

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
    hello, notes = M.start(a["rom"], debug=a.get("debug", True), boot_frames=a.get("boot_frames", 0))
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


def t_step(a):
    if not M.debug:
        raise ToolError("stepping needs the patched MAME's -debugger script")
    return M.ask("step", int(a.get("instructions", 1)))


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
        return M.ask("wp", "set", a.get("kind", "w"), "0x%04X" % addr(a.get("address")),
                     int(a.get("length", 1)))
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


ADDR = {"type": ["string", "integer"], "description": "an address: \"$C000\", \"0xC000\", \"C000\" or a number"}
TOOLS = [
    ("mame_start", t_start, "Start MAME on a 7800 cartridge (.a78), stopped at the first instruction of "
     "the BIOS. Any MAME already started is stopped first.",
     {"rom": {"type": "string", "description": "path to the .a78 image"},
      "boot_frames": {"type": "integer", "description": "frames to run before returning (default 0)"},
      "debug": {"type": "boolean", "description": "use the debugger (default true; needs the patched MAME)"}},
     ["rom"]),
    ("mame_stop", t_stop, "Stop MAME.", {}, []),
    ("mame_status", t_status, "Frame, whether it is stopped, and the CPU registers.", {}, []),
    ("mame_run", t_run, "Run the machine for some frames (60 a second on NTSC) and stop; a breakpoint "
     "or watchpoint stops it early. `hold` presses inputs for the duration (see mame_input).",
     {"frames": {"type": "integer"},
      "hold": {"type": "array", "items": {"type": "object", "properties": {
          "port": {"type": "string"}, "field": {"type": "string"}, "value": {"type": "integer"}}}}}, []),
    ("mame_step", t_step, "Execute some instructions and stop.",
     {"instructions": {"type": "integer"}}, []),
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
      "length": {"type": "integer"}, "kind": {"type": "string", "enum": ["r", "w", "rw"]}}, []),
    ("mame_trace", t_trace, "Run some frames logging every instruction executed, and summarise which code "
     "ran, per cartridge bank (a bank is told from the bytes, since MAME's trace does not record it). "
     "`entries_file` adds the executed addresses as entry points to an annotations file for "
     "tools/disasm.py -c.",
     {"frames": {"type": "integer"}, "entries_file": {"type": "string"}}, []),
    ("mame_watch_writes", t_writes, "Run some frames and report every write to an address range: how many, "
     "from which PCs, with which values. Finds who owns a variable or a table in RAM.",
     {"start": ADDR, "end": ADDR, "frames": {"type": "integer"}, "limit": {"type": "integer"}}, ["start"]),
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
