-- wildfetch.lua -- trap the first time the CPU runs code from where there is
-- none: graphics, tables, empty space. A jump through a bad pointer, a
-- return through a corrupted stack or a branch into data usually ends in a
-- crash many instructions later, somewhere unhelpful; this stops at the
-- first instruction fetch in the wrong place and writes the registers and
-- the stack, whose top bytes are the return chain that got there.
--
-- A fetch is a read whose address is the program counter, so operand and
-- data reads from the same range do not trip it -- a table MARIA or the CPU
-- reads as data is fine.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/wildfetch.lua \
--        [-playback run.inp] -nothrottle
--
-- Env:
--   A7800_WF_RANGES  where no code should run, hex ranges, comma-separated
--                    (default 8000-BFFF, the usual graphics area)
--   A7800_WF_WATCH   optional hex addresses to report alongside, e.g. the
--                    game's state byte: "9D,E4"
--   A7800_WF_LOG     output file (default wildfetch.log)
--   A7800_WF_END     stop at this frame if nothing trips (default 36000)
--
-- The log is empty-handed or one hit:
--   f5120 wild fetch at $9C02 A=00 X=04 Y=FF SP=F1 P=24 [$9D=04]
--   stack: 3C D2 ...          (24 bytes above SP: the return chain)
--   recent: 5115:[04] 5116:[04] ...   (the watched bytes, frame by frame)
-- Found in Pole Position II VS, where it guarded every build since
-- checkpoint 80.

local M = (type(manager.machine) == "function") and manager:machine() or manager.machine
local cpu = M.devices[":maincpu"]
local mem = cpu.spaces["program"]

local function hex(s) return tonumber(s, 16) end
local ranges = {}
for lo, hi in string.gmatch(os.getenv("A7800_WF_RANGES") or "8000-BFFF",
                            "(%x+)%s*%-%s*(%x+)") do
  ranges[#ranges + 1] = {hex(lo), hex(hi)}
end
local watch = {}
for a in string.gmatch(os.getenv("A7800_WF_WATCH") or "", "%x+") do
  watch[#watch + 1] = hex(a)
end
local END = tonumber(os.getenv("A7800_WF_END") or "36000")
local log = io.open(os.getenv("A7800_WF_LOG") or "wildfetch.log", "w")

local f, done, ring = 0, false, {}
local function watched()
  if #watch == 0 then return "" end
  local t = {}
  for _, a in ipairs(watch) do t[#t + 1] = string.format("$%02X=%02X", a, mem:read_u8(a)) end
  return " [" .. table.concat(t, " ") .. "]"
end

-- held in a global: a tap that is only a local is garbage-collected and
-- silently stops firing (docs/pitfalls.md)
WILDFETCH_TAPS = {}
for i, r in ipairs(ranges) do
  WILDFETCH_TAPS[i] = mem:install_read_tap(r[1], r[2], "wildfetch" .. i, function(a, d)
    if not done and cpu.state["PC"].value == a then
      done = true
      local sp = cpu.state["SP"].value & 0xFF
      log:write(string.format("f%d wild fetch at $%04X A=%02X X=%02X Y=%02X SP=%02X P=%02X%s\n",
        f, a, cpu.state["A"].value, cpu.state["X"].value, cpu.state["Y"].value,
        sp, cpu.state["P"].value, watched()))
      local t = {}
      for k = 1, 24 do t[#t + 1] = string.format("%02X", mem:read_u8(0x100 + ((sp + k) & 0xFF))) end
      log:write("stack: " .. table.concat(t, " ") .. "\n")
      if #watch > 0 then log:write("recent: " .. table.concat(ring, " ") .. "\n") end
      log:flush()
    end
    return d
  end)
end

emu.register_frame_done(function()
  f = f + 1
  if #watch > 0 then
    ring[#ring + 1] = string.format("%d:%s", f, watched():sub(2))
    if #ring > 6 then table.remove(ring, 1) end
  end
  if done or f >= END then
    if not done then log:write(string.format("no fetch in those ranges by frame %d\n", f)) end
    log:close()
    M:exit()
  end
end)
