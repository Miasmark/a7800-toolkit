-- inputtrace.lua -- log the raw joystick port, and any bytes you name, every
-- frame in a window -- to settle whether a character's lack of movement
-- reflects no input or a broken response to input.
--
-- Env: A7800_IT_FROM and A7800_IT_TO (frame window, default the whole run);
--      A7800_IT_LOG (default inputtrace.log);
--      A7800_IT_WATCH (comma-separated hex addresses to log beside SWCHA, e.g.
--      "A2,A3" for a game's velocity bytes; default none).

local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local mem = MACHINE.devices[":maincpu"].spaces["program"]

local FROM = tonumber(os.getenv("A7800_IT_FROM") or "0")
local TO   = tonumber(os.getenv("A7800_IT_TO") or "1000000000")
local OUT  = os.getenv("A7800_IT_LOG") or "inputtrace.log"
local WATCH = {}
for tok in string.gmatch(os.getenv("A7800_IT_WATCH") or "", "[^,%s]+") do
  WATCH[#WATCH + 1] = tonumber(tok, 16)
end

local f = io.open(OUT, "w")
local cols = {}
for _, a in ipairs(WATCH) do cols[#cols + 1] = string.format("%04X", a) end
f:write("frame swcha " .. table.concat(cols, " ") .. "\n")

local F = 0
emu.register_frame_done(function()
  F = F + 1
  if F >= FROM and F <= TO then
    local swcha = mem:read_u8(0x0280)
    local vals = {}
    for _, a in ipairs(WATCH) do vals[#vals + 1] = tostring(mem:read_u8(a)) end
    f:write(string.format("%d $%02X %s\n", F, swcha, table.concat(vals, " ")))
  end
end)

local function dump()
  f:flush(); f:close()
  print("inputtrace: wrote " .. OUT)
end
if emu.add_machine_stop_notifier then
  STOPPER = emu.add_machine_stop_notifier(dump)
else
  emu.register_stop(dump)
end
