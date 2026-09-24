-- handover.lua -- the moment a cartridge's reset code first runs: which frame,
-- the CPU registers and flags the BIOS left, the INPTCTRL writes that led
-- there, and RAM as the BIOS left it.
--
-- For comparing BIOSes (docs/bios.md), for telling a 7800 hand-over from a
-- 2600-mode one (an unsigned cartridge on the NTSC BIOS reaches its reset
-- code through the 2600 bootstrap at $0480, which MAME then runs as a 7800
-- game anyway), and for checking what a game assumes about its start state.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/handover.lua -nothrottle
--
-- Env:
--   A7800_HO_RESET  the cartridge's reset vector, hex (required: the word at
--                   $FFFC-$FFFD of the cartridge)
--   A7800_HO_LOG    output file (default handover.log); RAM goes to
--                   <log>.ram: $40-$FF, $100-$1FF, $1800-$27FF
--   A7800_HO_END    stop at this frame (default 900)
--   A7800_HO_SNAP   a screenshot at this frame (default none)

local M = (type(manager.machine) == "function") and manager:machine() or manager.machine
local cpu = M.devices[":maincpu"]
local mem = cpu.spaces["program"]
local RESET = tonumber(os.getenv("A7800_HO_RESET") or "", 16)
if not RESET then error("handover.lua: set A7800_HO_RESET to the cartridge's reset vector") end
local LOG = os.getenv("A7800_HO_LOG") or "handover.log"
local END = tonumber(os.getenv("A7800_HO_END") or "900")
local SNAP = tonumber(os.getenv("A7800_HO_SNAP") or "0")
local o = io.open(LOG, "w")
local f, done, inpt = 0, false, {}

-- globals: a tap held only in a local is garbage-collected (docs/pitfalls.md)
HANDOVER_TAPS = {
  mem:install_write_tap(0x0001, 0x0001, "ho-inpt", function(a, d)
    if not done then
      inpt[#inpt + 1] = string.format("f%d:$%02X@%04X", f, d, cpu.state["PC"].value)
    end
    return d
  end),
  mem:install_read_tap(RESET, RESET, "ho-reset", function(a, d)
    if not done and cpu.state["PC"].value == RESET then
      done = true
      local p = cpu.state["P"].value
      o:write(string.format("handover at frame %d: A=%02X X=%02X Y=%02X SP=%02X P=%02X (D=%d I=%d)\n",
        f, cpu.state["A"].value, cpu.state["X"].value, cpu.state["Y"].value,
        cpu.state["SP"].value & 0xFF, p, (p >> 3) & 1, (p >> 2) & 1))
      local n, last = #inpt, {}
      for k = math.max(1, n - 3), n do last[#last + 1] = inpt[k] end
      o:write("INPTCTRL writes before it (last 4): " .. table.concat(last, " ") .. "\n")
      local t = {}
      for a2 = 0x0040, 0x01FF do t[#t + 1] = string.char(mem:read_u8(a2)) end
      for a2 = 0x1800, 0x27FF do t[#t + 1] = string.char(mem:read_u8(a2)) end
      local b = io.open(LOG .. ".ram", "wb"); b:write(table.concat(t)); b:close()
      o:flush()
    end
    return d
  end),
}

emu.register_frame_done(function()
  f = f + 1
  if SNAP > 0 and f == SNAP then M.video:snapshot() end
  if f >= END then
    if not done then o:write(string.format("no handover by frame %d\n", f)) end
    o:close()
    M:exit()
  end
end)
