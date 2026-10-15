-- The MAME side of tools/mamemcp.py: a small command server inside the running machine.
--
-- tools/mamemcp.py starts MAME with this as its -autoboot_script and talks to it over a TCP
-- socket on 127.0.0.1 (A7800_MCP_PORT, default 2461). You do not normally run it by hand; to
-- poke at it anyway:
--
--   A7800_MCP_PORT=2461 mame a7800 -cart game.a78 -debug -debugger script -video none \
--        -sound none -nothrottle -autoboot_script probes/mcp-server.lua
--   then connect with `nc 127.0.0.1 2461` and type `1 status`
--
-- The protocol is a line each way. A request is `<id> <command> <args...>`, arguments separated
-- by spaces and percent-encoded (`P1%20Button%201`). A reply is `<id> ok <text>` or
-- `<id> err <text>`, the text with backslash and newline escaped as \\ and \n.
--
-- With -debug -debugger script the machine starts stopped at its first instruction and
-- breakpoints, watchpoints, stepping and tracing work. `script` is a debugger module the a7800
-- MAME patch adds (src/osd/modules/debugger/script.cpp): stock MAME's `-debugger none` resumes
-- every stop at once, so a breakpoint is over before a script can see it. Without -debug, `run`
-- pauses and unpauses the machine instead and the debugger commands answer `err`.
--
-- A7800_MCP_REWIND (default 64) is how many states `rewind` keeps: one is saved each time the
-- machine is set going from a stop.
--
-- The socket is opened through emu.file, as MAME's own gdbstub plugin does, and only on the
-- loopback address. MAME's Lua has no other way to listen.

local MACHINE = (type(manager.machine) == "function") and manager:machine() or manager.machine
local PORT = tonumber(os.getenv("A7800_MCP_PORT") or "2461")
local cpu = MACHINE.devices[":maincpu"]
local space = cpu.spaces["program"]
local dbg = MACHINE.debugger
-- MARIA's own address space, on a MAME built with the a7800 patch: its DMA fetches are there and
-- not in the CPU's space. On another MAME they go through the CPU's space and this is nil.
local maria_dev = MACHINE.devices[":maria"]
local mspace = maria_dev and maria_dev.spaces and maria_dev.spaces["dma"] or nil

-- the screen's own frame count: a counter of frame_done calls would also count the redraws the
-- debugger makes while it is stopped, and could not follow a state put back by `rewind`
local screen = nil
for _, sc in pairs(MACHINE.screens) do screen = sc; break end
local function screen_frame() return screen and screen:frame_number() or 0 end

local sock = emu.file("", 7)
sock:open("socket.127.0.0.1:" .. PORT)

local frames = 0
local inbuf = ""
local pending = nil          -- {id=, kind="run"|"step", target=}
local we_stopped = false     -- the stop the debugger is in is ours, not a breakpoint's
local tap, taplog, taplimit = nil, {}, 0

local function esc(s)
  return (tostring(s):gsub("\\", "\\\\"):gsub("\n", "\\n"))
end

local function reply(id, ok, text)
  sock:write(id .. (ok and " ok " or " err ") .. esc(text or "") .. "\n")
end

local function unpct(s)
  return (s:gsub("%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

local function stopped()
  if dbg then return dbg.execution_state == "stop" end
  return MACHINE.paused
end

local function halt()
  if dbg then
    we_stopped = true
    dbg.execution_state = "stop"
  else
    emu.pause()
  end
end

local function go()
  if dbg then
    we_stopped = false
    dbg.execution_state = "run"
  else
    emu.unpause()
  end
end

local function pc()
  return cpu.state["PC"].value
end

local REGS = { "PC", "A", "X", "Y", "P", "SP" }

local function regs()
  local out = {}
  for _, r in ipairs(REGS) do
    local s = cpu.state[r]
    if s then out[#out + 1] = string.format("%s=%X", r, s.value) end
  end
  return table.concat(out, " ")
end

local function status()
  return string.format("frame=%d state=%s %s debugger=%s maria_space=%s", frames,
                       stopped() and "stopped" or "running", regs(), dbg and "yes" or "no",
                       mspace and "yes" or "no")
end

local function num(s)
  if not s then return nil end
  s = s:gsub("^%$", "0x")
  return tonumber(s)
end

local commands = {}

function commands.hello(id)
  reply(id, true, "a7800 mcp-server; " .. status() .. " system=" .. emu.romname())
end

function commands.status(id) reply(id, true, status()) end

-- rewind: a state saved each time the machine is set going from a stop, so it can be put back
-- (MAME's own rewind keeps states only for the debugger's single steps). Up to A7800_MCP_REWIND
-- of them (default 64; a 7800 state is about 140 KB).
local REWIND_MAX = tonumber(os.getenv("A7800_MCP_REWIND") or "64")
local REW = {}

local function checkpoint()
  if REWIND_MAX <= 0 or not stopped() then return end
  local ok, st = pcall(function() return MACHINE:buffer_save() end)
  if ok and st then
    REW[#REW + 1] = { state = st, frame = frames, pc = cpu.state["PC"].value }
    if #REW > REWIND_MAX then table.remove(REW, 1) end
  end
end

function commands.rewind(id, n)
  n = num(n) or 1
  if n < 1 or n > #REW then return reply(id, false, string.format("%d step(s) kept to go back to", #REW)) end
  local cp = REW[#REW - n + 1]
  local ok, e = pcall(function() return MACHINE:buffer_load(cp.state) end)
  if not ok then return reply(id, false, "could not load the state: " .. tostring(e)) end
  for _ = 1, n do table.remove(REW) end                               -- it, and what came after it
  frames = screen_frame()
  reply(id, true, string.format("back %d: ", n) .. status())
end

function commands.run(id, n)
  n = num(n) or 1
  checkpoint()
  pending = { id = id, kind = "run", target = frames + n }
  go()
end

function commands.step(id, n)
  if not dbg then return reply(id, false, "stepping needs MAME started with -debug") end
  n = num(n) or 1
  checkpoint()
  pending = { id = id, kind = "step" }
  we_stopped = true
  dbg:command("step " .. n)
end

function commands.stop(id)
  halt()
  reply(id, true, status())
end

function commands.regs(id) reply(id, true, regs()) end

function commands.setreg(id, name, v)
  local s = cpu.state[name]
  if not s then return reply(id, false, "no register " .. tostring(name)) end
  s.value = num(v)
  reply(id, true, regs())
end

function commands.read(id, a, n)
  a, n = num(a), num(n) or 16
  if not a then return reply(id, false, "read ADDR LEN") end
  local t = {}
  for i = 0, n - 1 do t[#t + 1] = string.format("%02X", space:read_u8((a + i) & 0xFFFF)) end
  reply(id, true, table.concat(t))
end

function commands.write(id, a, hex)
  a = num(a)
  if not a or not hex then return reply(id, false, "write ADDR HEXBYTES") end
  local i = 0
  for b in hex:gmatch("%x%x") do
    space:write_u8((a + i) & 0xFFFF, tonumber(b, 16))
    i = i + 1
  end
  reply(id, true, i .. " bytes")
end

function commands.bp(id, op, a, cond)
  if not dbg then return reply(id, false, "breakpoints need MAME started with -debug") end
  local d = cpu.debug
  if op == "set" then
    local n = d:bpset(num(a), cond and unpct(cond) or nil, nil)
    reply(id, true, "breakpoint " .. n .. " at " .. string.format("%04X", num(a)))
  elseif op == "clear" then
    if a then d:bpclear(num(a)) else d:bpclear() end
    reply(id, true, "cleared")
  else
    local t = {}
    for k, b in pairs(d:bplist()) do
      t[#t + 1] = string.format("%d %04X %s%s", k, b.address, b.enabled and "on" or "off",
                                (b.condition and b.condition ~= "1") and (" if " .. b.condition) or "")
    end
    reply(id, true, table.concat(t, "\n"))
  end
end

function commands.wp(id, op, kind, a, n, cond, which)
  if not dbg then return reply(id, false, "watchpoints need MAME started with -debug") end
  local d = cpu.debug
  if op == "set" then
    local sp = space
    if which == "maria" then
      if not mspace then return reply(id, false, "this MAME has no separate MARIA address space") end
      sp = mspace                                  -- MARIA's fetches; the stop is the CPU's debugger's
    end
    local i = d:wpset(sp, kind or "w", num(a), num(n) or 1, (cond and cond ~= "-") and unpct(cond) or nil)
    reply(id, true, "watchpoint " .. i)
  elseif op == "clear" then
    d:wpclear()
    reply(id, true, "cleared")
  else
    local t = {}
    for k, w in pairs(d:wplist(space)) do
      local ty = w.type
      if type(ty) ~= "string" then ty = (ty == 1 and "r") or (ty == 2 and "w") or "rw" end
      t[#t + 1] = string.format("%d %04X+%X %s", k, w.address, w.length, ty)
    end
    reply(id, true, table.concat(t, "\n"))
  end
end

function commands.trace(id, file)
  if not dbg then return reply(id, false, "tracing needs MAME started with -debug") end
  local was = stopped()
  if file == "off" then
    dbg:command("trace off,maincpu")
  else
    dbg:command("trace " .. unpct(file) .. ",maincpu")
  end
  if was then halt() end               -- a console command lets the machine run on
  reply(id, true, "trace " .. unpct(file))
end

function commands.snap(id)
  local ok = pcall(function() MACHINE.video:snapshot() end)
  if not ok then ok = pcall(function() MACHINE:video():snapshot() end) end
  reply(id, ok, ok and "snapshot written" or "snapshot failed")
end

function commands.inputs(id)
  local t = {}
  for tag, port in pairs(MACHINE.ioport.ports) do
    for name, _ in pairs(port.fields) do t[#t + 1] = tag .. "|" .. name end
  end
  table.sort(t)
  reply(id, true, table.concat(t, "\n"))
end

function commands.input(id, tag, field, v)
  local port = MACHINE.ioport.ports[unpct(tag or "")]
  local f = port and port.fields[unpct(field or "")]
  if not f then return reply(id, false, "no input " .. tostring(tag) .. " " .. tostring(field)) end
  f:set_value(num(v) or 0)
  reply(id, true, "set")
end

-- a save or load happens between timeslices, and scheduling one releases a stopped debugger,
-- so each runs the machine one frame and answers when it has stopped again
function commands.save(id, name)
  MACHINE:save(unpct(name))
  pending = { id = id, kind = "run", target = frames + 1 }
  go()
end

function commands.load(id, name)
  MACHINE:load(unpct(name))
  pending = { id = id, kind = "run", target = frames + 1 }
  go()
end

function commands.tap(id, lo, hi, limit)
  if tap then tap:remove(); tap = nil end
  taplog, taplimit = {}, num(limit) or 4096
  tap = space:install_write_tap(num(lo), num(hi), "mcp", function(offset, data, mask)
    if #taplog < taplimit then
      taplog[#taplog + 1] = string.format("%d %04X %04X %02X", frames, pc(), offset, data & 0xFF)
    end
  end)
  reply(id, true, "tapping writes to " .. lo .. "-" .. hi)
end

function commands.untap(id)
  if tap then tap:remove(); tap = nil end
  reply(id, true, table.concat(taplog, "\n"))
  taplog = {}
end

-- ---------------------------------------------------------------- banks, as exectrace.lua has them
-- The window's bank is the last value stored to the bank-select range, read the way
-- probes/exectrace.lua reads it (cart.py's probe_env gives the numbers), so a trace or a read log
-- here names spaces exactly as that probe, dyn.py and the disassembler do: f<bank> for the fixed
-- part, b<N> for window bank N. `banks` must be sent before `xtrace` or `rtap` mean anything for a
-- banked cartridge; tools/mamemcp.py sends it when it starts MAME.
local B = { sel_lo = 0x8000, sel_hi = 0xBFFF, win_lo = 0x8000, win_hi = 0xBFFF, nbanks = 1,
            first = 0, wbanks = 1, lowbank = nil, bank = 0 }
local X = { on = false, x = {}, j = {}, s = {}, pending_j = nil, file = nil }
local R = { on = false, n = 0, limit = 0, hits = {}, insn = {}, busy = false }
TAPS = TAPS or {}            -- globals: a tap held only in a local is collected (docs/pitfalls.md)

local function key(addr)
  if B.nbanks > 1 and addr >= B.win_lo and addr <= B.win_hi then
    return string.format("b%d:%04X", B.bank, addr)
  end
  if B.lowbank and addr < 0x8000 then return string.format("f%d:%04X", B.lowbank, addr) end
  if B.fixed then return string.format("%s:%04X", B.fixed, addr) end      -- a linear cart: "rom"
  return string.format("f%d:%04X", B.nbanks - 1, addr)
end

local function hexrange(s)
  local lo, hi = s:match("^(%x+)%-(%x+)$")
  return tonumber(lo, 16), tonumber(hi, 16)
end

function commands.banks(id, sel, win, nbanks, first, wbanks, lowbank, fixed)
  B.sel_lo, B.sel_hi = hexrange(sel)
  B.win_lo, B.win_hi = hexrange(win)
  B.nbanks, B.first, B.wbanks = tonumber(nbanks), tonumber(first), tonumber(wbanks)
  B.lowbank = (lowbank and lowbank ~= "-") and tonumber(lowbank) or nil
  B.fixed = (fixed and fixed ~= "-") and fixed or nil
  B.bank = B.first
  if TAPS.banksel then TAPS.banksel:remove() end
  if B.nbanks > 1 then
    TAPS.banksel = space:install_write_tap(B.sel_lo, B.sel_hi, "mcp-banksel", function(offset, data)
      local v = B.first + (data % B.wbanks)
      if X.on then
        local k = string.format("%s %d", key(cpu.state["PC"].value), v)
        X.s[k] = (X.s[k] or 0) + 1
      end
      B.bank = v
      return data
    end)
  end
  reply(id, true, string.format("%d banks, select %04X-%04X, window %04X-%04X", B.nbanks,
                                B.sel_lo, B.sel_hi, B.win_lo, B.win_hi))
end

-- every instruction fetched from the cartridge, with its bank; indirect jumps; bank switches --
-- probes/exectrace.lua's log, for tools/dyn.py
-- One fetch, from the cartridge or from RAM: what static analysis cannot know. Kept for the log:
--   X <space>:<addr>          a cartridge instruction ran here (exectrace.lua's line, for dyn.py)
--   J <from> <to>             a JMP (indirect) went there (both in the cartridge)
--   S <from> <bank> xN        a bank-select store
--   C <from> <to> xN          a JSR: its site and target, cartridge or RAM, with how often
--   G <routine> <to> xN       the same JSR by the routine it was made from (the call graph)
--   I <vector> <to> xN        an interrupt entered its handler (vector $FFFA NMI, $FFFE IRQ/BRK)
--   M ram:<addr> <op>         an instruction ran from RAM, and the opcode it had then
--   JR <from> <to>            a JMP (indirect) to or from RAM
local function rkey(addr) return string.format("ram:%04X", addr) end

local function on_fetch(k, data, in_ram)
  if X.pending_j then
    local line = X.pending_j .. " " .. k
    if in_ram or X.pending_j:sub(1, 4) == "ram:" then X.jr[line] = true else X.j[line] = true end
    X.pending_j = nil
  end
  -- the routine running is the top of a call stack kept from what runs: a JSR pushes its target,
  -- an interrupt its handler, RTS and RTI pop. A game that drops a return address (PLA PLA) or
  -- jumps out of a routine leaves it wrong until the stack unwinds; "?" is code entered before
  -- the trace began.
  local top = X.stack[#X.stack] or "?"
  if X.pending_c then
    local line = X.pending_c .. " " .. k
    X.c[line] = (X.c[line] or 0) + 1
    local g = top .. " " .. k
    X.g[g] = (X.g[g] or 0) + 1
    if #X.stack < 64 then X.stack[#X.stack + 1] = k end
    X.pending_c = nil
  end
  if X.pending_i then
    local line = X.pending_i .. " " .. k
    X.i[line] = (X.i[line] or 0) + 1
    if #X.stack < 64 then X.stack[#X.stack + 1] = k end
    X.pending_i = nil
  end
  if data == 0x6C then X.pending_j = k end                            -- JMP (ind): the next fetch is its target
  if data == 0x20 then X.pending_c = k end                            -- JSR: likewise
  if data == 0x60 or data == 0x40 then X.stack[#X.stack] = nil end    -- RTS, RTI
end

function commands.xtrace(id, op, file)
  if op == "on" then
    X.on, X.x, X.j, X.s, X.c, X.i, X.m, X.jr, X.g, X.stack = true, {}, {}, {}, {}, {}, {}, {}, {}, {}
    X.pending_j, X.pending_c, X.pending_i, X.file = nil, nil, nil, unpct(file or "exectrace.log")
    if not TAPS.xfetch then
      TAPS.xfetch = space:install_read_tap(0x4000, 0xFFFF, "mcp-xtrace", function(offset, data)
        if not X.on or R.busy then return data end
        if cpu.state["PC"].value ~= offset then
          -- an interrupt reads its vector without a fetch: the next fetch is the handler
          if offset == 0xFFFA or offset == 0xFFFE then X.pending_i = string.format("$%04X", offset) end
          return data
        end
        local k = key(offset)
        on_fetch(k, data, false)
        X.x[k] = true
        return data
      end)
      TAPS.xram = space:install_read_tap(0x0040, 0x27FF, "mcp-xtrace-ram", function(offset, data)
        if not X.on or R.busy then return data end
        if cpu.state["PC"].value ~= offset then return data end
        local k = rkey(offset)
        on_fetch(k, data, true)
        if not X.m[k] then X.m[k] = data end
        return data
      end)
    end
    return reply(id, true, "tracing to " .. X.file)
  end
  X.on = false
  local lines, nx, nm = {}, 0, 0
  for k in pairs(X.x) do lines[#lines + 1] = "X " .. k; nx = nx + 1 end
  for k in pairs(X.j) do lines[#lines + 1] = "J " .. k end
  for k in pairs(X.jr) do lines[#lines + 1] = "JR " .. k end
  for k, n in pairs(X.s) do lines[#lines + 1] = string.format("S %s x%d", k, n) end
  for k, n in pairs(X.c) do lines[#lines + 1] = string.format("C %s x%d", k, n) end
  for k, n in pairs(X.i) do lines[#lines + 1] = string.format("I %s x%d", k, n) end
  for k, n in pairs(X.g) do lines[#lines + 1] = string.format("G %s x%d", k, n) end
  for k, op in pairs(X.m) do lines[#lines + 1] = string.format("M %s %02X", k, op); nm = nm + 1 end
  table.sort(lines)
  local f = io.open(X.file, "w")
  if not f then return reply(id, false, "cannot write " .. X.file) end
  for _, l in ipairs(lines) do f:write(l, "\n") end
  f:close()
  reply(id, true, string.format("%d cartridge instructions, %d in RAM, %d lines in %s", nx, nm, #lines, X.file))
end

-- data reads: who reads which bytes (the reading instruction is CURPC, its first three bytes kept so
-- the bridge can tell a CPU read from MARIA's DMA, which goes through the same address space)
function commands.rtap(id, lo, hi, limit)
  if TAPS.rtap then TAPS.rtap:remove(); TAPS.rtap = nil end
  R.hits, R.insn, R.n, R.limit, R.on = {}, {}, 0, num(limit) or 20000, true
  TAPS.rtap = space:install_read_tap(num(lo), num(hi), "mcp-reads", function(offset, data)
    if not R.on or R.busy then return data end
    if cpu.state["PC"].value == offset then return data end           -- a fetch or an operand
    local at = cpu.state["CURPC"].value
    local k = key(offset) .. " " .. string.format("%04X", at)
    local n = R.hits[k]
    if n then R.hits[k] = n + 1
    elseif R.n < R.limit then
      R.hits[k] = 1
      R.n = R.n + 1
      if not R.insn[at] then
        R.busy = true                                                  -- our own reads are not logged
        R.insn[at] = string.format("%02X%02X%02X", space:read_u8(at), space:read_u8((at + 1) & 0xFFFF),
                                   space:read_u8((at + 2) & 0xFFFF))
        R.busy = false
      end
    end
    return data
  end)
  -- MARIA's fetches, when they have their own space: no attribution needed, they are MARIA's
  R.mhits = {}
  if TAPS.mtap then TAPS.mtap:remove(); TAPS.mtap = nil end
  if mspace then
    TAPS.mtap = mspace:install_read_tap(num(lo), num(hi), "mcp-maria-reads", function(offset, data)
      if R.on then
        local k = key(offset)
        R.mhits[k] = (R.mhits[k] or 0) + 1
      end
      return data
    end)
  end
  reply(id, true, "tapping reads of " .. lo .. "-" .. hi .. (mspace and " (CPU and MARIA apart)" or ""))
end

function commands.untap_reads(id)
  R.on = false
  if TAPS.rtap then TAPS.rtap:remove(); TAPS.rtap = nil end
  if TAPS.mtap then TAPS.mtap:remove(); TAPS.mtap = nil end
  local t = {}
  for k, n in pairs(R.hits) do t[#t + 1] = k .. " " .. n end
  for k, n in pairs(R.mhits or {}) do t[#t + 1] = "M " .. k .. " " .. n end
  for at, b in pairs(R.insn) do t[#t + 1] = string.format("I %04X %s", at, b) end
  table.sort(t)
  reply(id, true, table.concat(t, "\n"))
end

-- any debugger console command, with what it printed. One that sets the machine going (over, out,
-- gint, gvblank, go...) is answered when it has stopped again, or after `limit` frames.
local function console_since(n0)
  local log, t = dbg.consolelog, {}
  for i = n0 + 1, #log do t[#t + 1] = log[i] end
  return table.concat(t, "\n")
end

function commands.console(id, cmd, limit)
  if not dbg then return reply(id, false, "the debugger commands need MAME started with -debug") end
  local n0 = #dbg.consolelog
  local was = stopped()
  local c = unpct(cmd)
  if c:match("^%s*g") or c:match("^%s*over") or c:match("^%s*out") or c:match("^%s*o%s") or c:match("^%s*n") then
    checkpoint()                                                       -- a command that sets it going
  end
  we_stopped = true
  dbg:command(c)
  if was and not stopped() then
    pending = { id = id, kind = "cmd", n0 = n0, limit = frames + (num(limit) or 600) }
    return
  end
  reply(id, true, console_since(n0))
end

function commands.quit(id)
  reply(id, true, "bye")
  MACHINE:exit()
end

local function handle(line)
  local words = {}
  for w in line:gmatch("%S+") do words[#words + 1] = w end
  local id, cmd = words[1], words[2]
  if not cmd then return end
  local f = commands[cmd]
  if not f then return reply(id, false, "unknown command " .. cmd) end
  local ok, e = pcall(f, id, table.unpack(words, 3))
  if not ok then reply(id, false, e) end
end

local settled = 0

local function pump()
  -- one finished run or step at a time: answer it once the machine has stopped, and stayed
  -- stopped for a second look (a stop takes effect at the next instruction, not at once)
  if pending and stopped() then settled = settled + 1 else settled = 0 end
  if pending and settled >= 2 then
    settled = 0
    local p = pending
    pending = nil
    local why = ""
    if p.kind == "run" and frames < p.target then
      why = string.format(" (stopped early at %04X: a breakpoint or watchpoint)", pc())
    elseif p.kind == "cmd" then
      local out = console_since(p.n0)
      if p.timed_out then out = out .. (#out > 0 and "\n" or "") .. "(not reached; stopped after the frame limit)" end
      return reply(p.id, true, (#out > 0 and (out .. "\n") or "") .. status())
    end
    reply(p.id, true, status() .. why)
  end
  if pending then return end           -- a request at a time
  local d = sock:read(4096)
  if d and #d > 0 then inbuf = inbuf .. d end
  while not pending do
    local line, rest = inbuf:match("^([^\n]*)\n(.*)$")
    if not line then break end
    inbuf = rest
    handle(line:gsub("\r$", ""))
  end
end


emu.register_frame_done(function()
  frames = screen and screen:frame_number() or (frames + 1)
  if pending and pending.kind == "run" and frames >= pending.target and not stopped() then
    halt()
  end
  if pending and pending.kind == "cmd" and pending.limit and frames >= pending.limit and not stopped() then
    pending.timed_out = true
    halt()
  end
end)

emu.register_periodic(pump)

-- the machine waits for its first `run`, stopped
halt()
