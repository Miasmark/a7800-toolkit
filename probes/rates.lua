-- rates.lua -- how often does the game do things? Instruction runs and controller polls.
--
--   A7800_RATE_SITES="C080,D259" A7800_RATE_PORTS="0280,000C" \
--   mame a7800 -cart game.a78 -autoboot_script probes/rates.lua \
--        -video none -sound none -nothrottle -seconds_to_run 60
--
-- What a game can answer is set by how often it gets round to asking, not by how
-- cheap the asking is: a game that reads the stick every fourteenth frame cannot
-- respond in one, however tight the reading code. Looking at the code never shows
-- that. This measures it.
--
--   A7800_RATE_SITES  hex addresses of instructions: how many times each is fetched
--                     (a read tap on the opcode's own address, PC-checked, so MARIA
--                     DMA and data reads of the same byte are not counted) and so
--                     once every how many frames. A main-loop pass, a routine's
--                     entry, an interpreter's inner loop.
--   A7800_RATE_PORTS  hex addresses read as controls (default 0280, SWCHA): in how
--                     many frames the game read it, and the gap, in frames, between
--                     frames that did -- mean, smallest, largest.
--   A7800_RATE_FROM / A7800_RATE_TO   the frame window (default: all)
--   A7800_RATE_OUT    output (default rates.log)
--
-- A cartridge sits in attract mode until a console switch is pressed, and an
-- attract screen polls nothing; measuring there reports a game that never reads
-- its controls. Use a recording (-playback) that starts the game, or a window after
-- it has started. This probe touches nothing.
--
-- Checked on MAME 0.264, where a read tap sees opcode fetches from ROM
-- (docs/pitfalls.md says it did not elsewhere): a site that reports zero runs
-- may be a tap that does not see fetches, not code that never runs.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local OUT = os.getenv("A7800_RATE_OUT") or "rates.log"
local FROM = tonumber(os.getenv("A7800_RATE_FROM") or "") or 0
local TO = tonumber(os.getenv("A7800_RATE_TO") or "") or math.huge

local function hexlist(env, default)
  local out = {}
  for a in (os.getenv(env) or default):gmatch("[^,%s]+") do out[#out + 1] = tonumber(a, 16) end
  return out
end
local SITES, PORTS = hexlist("A7800_RATE_SITES", ""), hexlist("A7800_RATE_PORTS", "0280")

local F = 0
local runs, port = {}, {}
TAPS = {}                          -- global: see docs/pitfalls.md
for i, a in ipairs(SITES) do
  runs[i] = 0
  TAPS[#TAPS + 1] = mem:install_read_tap(a, a, "site", function(o, d)
    if F >= FROM and F <= TO and cpu.state["PC"].value == a then runs[i] = runs[i] + 1 end
    return d
  end)
end
for i, a in ipairs(PORTS) do
  port[i] = { frames = 0, last = nil, gaps = {}, seen = -1 }
  TAPS[#TAPS + 1] = mem:install_read_tap(a, a, "port", function(o, d)
    local p = port[i]
    if F >= FROM and F <= TO and p.seen ~= F then          -- one count per frame
      p.seen = F
      p.frames = p.frames + 1
      if p.last then p.gaps[#p.gaps + 1] = F - p.last end
      p.last = F
    end
    return d
  end)
end

FRAME_CB = emu.register_frame_done(function() F = F + 1 end)

local function dump()
  local frames = math.max(0, math.min(F, TO) - FROM + 1)
  local f = io.open(OUT, "w")
  if not f then print("rates: cannot write " .. OUT) return end
  f:write(string.format("frames %d (%d-%d)\n", frames, FROM, math.min(F, TO)))
  for i, a in ipairs(SITES) do
    f:write(string.format("site %04X: %d runs = once every %.2f frames\n", a, runs[i],
            frames / math.max(runs[i], 1)))
  end
  for i, a in ipairs(PORTS) do
    local p = port[i]
    local sum, lo, hi = 0, math.huge, 0
    for _, g in ipairs(p.gaps) do sum = sum + g lo = math.min(lo, g) hi = math.max(hi, g) end
    if #p.gaps > 0 then
      f:write(string.format("port %04X: read in %d of %d frames; gap between reading frames "
              .. "mean %.2f min %d max %d\n", a, p.frames, frames, sum / #p.gaps, lo, hi))
    else
      f:write(string.format("port %04X: read in %d of %d frames; too few reads for a gap\n",
              a, p.frames, frames))
    end
  end
  f:close()
  print("rates: wrote " .. OUT)
end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
