-- snapwhen.lua -- screenshot when a byte says the moment has come, a few
-- times, spaced out. For "what does it look like when X": a car at its
-- nearest size, a boss on screen, a menu open -- whatever a RAM byte marks.
--
-- The condition is checked once per frame: (byte at ADDR & MASK) == VALUE,
-- or != VALUE with A7800_SW_NOT=1. Snapshots go where MAME puts them
-- (-snapshot_directory); the log says which frame each one is.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/snapwhen.lua \
--        -playback run.inp -snapshot_directory shots -nothrottle
--
-- Env:
--   A7800_SW_ADDR   hex address to test (required)
--   A7800_SW_VALUE  hex value (default 00)
--   A7800_SW_MASK   hex mask (default FF)
--   A7800_SW_NOT    1 to snap when it does NOT match
--   A7800_SW_MAX    most snapshots (default 6)
--   A7800_SW_GAP    frames between them at least (default 300)
--   A7800_SW_END    stop at this frame (default 36000)
--   A7800_SW_LOG    log file (default snapwhen.log)

local M = (type(manager.machine) == "function") and manager:machine() or manager.machine
local mem = M.devices[":maincpu"].spaces["program"]

local ADDR = tonumber(os.getenv("A7800_SW_ADDR") or "", 16)
if not ADDR then error("snapwhen.lua: set A7800_SW_ADDR") end
local VALUE = tonumber(os.getenv("A7800_SW_VALUE") or "0", 16)
local MASK = tonumber(os.getenv("A7800_SW_MASK") or "FF", 16)
local NOT = os.getenv("A7800_SW_NOT") == "1"
local MAX = tonumber(os.getenv("A7800_SW_MAX") or "6")
local GAP = tonumber(os.getenv("A7800_SW_GAP") or "300")
local END = tonumber(os.getenv("A7800_SW_END") or "36000")
local log = io.open(os.getenv("A7800_SW_LOG") or "snapwhen.log", "w")

local f, last, shots, frames = 0, -1e9, 0, 0
emu.register_frame_done(function()
  f = f + 1
  local v = mem:read_u8(ADDR)
  local match = ((v & MASK) == VALUE) ~= NOT
  if match then
    frames = frames + 1
    if shots < MAX and f - last >= GAP then
      last, shots = f, shots + 1
      M.video:snapshot()
      log:write(string.format("snap %d at frame %d ($%04X=%02X)\n", shots, f, ADDR, v))
      log:flush()
    end
  end
  if f >= END or shots >= MAX then
    log:write(string.format("condition held on %d of %d frames\n", frames, f))
    log:close()
    M:exit()
  end
end)
