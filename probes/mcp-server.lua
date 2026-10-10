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
-- The socket is opened through emu.file, as MAME's own gdbstub plugin does, and only on the
-- loopback address. MAME's Lua has no other way to listen.

local MACHINE = (type(manager.machine) == "function") and manager:machine() or manager.machine
local PORT = tonumber(os.getenv("A7800_MCP_PORT") or "2461")
local cpu = MACHINE.devices[":maincpu"]
local space = cpu.spaces["program"]
local dbg = MACHINE.debugger

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
  return string.format("frame=%d state=%s %s debugger=%s", frames,
                       stopped() and "stopped" or "running", regs(), dbg and "yes" or "no")
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

function commands.run(id, n)
  n = num(n) or 1
  pending = { id = id, kind = "run", target = frames + n }
  go()
end

function commands.step(id, n)
  if not dbg then return reply(id, false, "stepping needs MAME started with -debug") end
  n = num(n) or 1
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

function commands.wp(id, op, kind, a, n)
  if not dbg then return reply(id, false, "watchpoints need MAME started with -debug") end
  local d = cpu.debug
  if op == "set" then
    local i = d:wpset(space, kind or "w", num(a), num(n) or 1)
    reply(id, true, "watchpoint " .. i)
  elseif op == "clear" then
    d:wpclear()
    reply(id, true, "cleared")
  else
    local t = {}
    for k, w in pairs(d:wplist(space)) do
      t[#t + 1] = string.format("%d %04X+%X %s", k, w.address, w.length, w.type == 1 and "r"
                                or (w.type == 2 and "w" or "rw"))
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

-- the screen's own frame count: a counter of frame_done calls would also count the redraws the
-- debugger makes while it is stopped
local screen = nil
for _, s in pairs(MACHINE.screens) do screen = s; break end

emu.register_frame_done(function()
  frames = screen and screen:frame_number() or (frames + 1)
  if pending and pending.kind == "run" and frames >= pending.target and not stopped() then
    halt()
  end
end)

emu.register_periodic(pump)

-- the machine waits for its first `run`, stopped
halt()
