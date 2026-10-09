-- freeram.lua -- which candidate RAM bytes does a game never write?
--
--   A7800_CANDIDATES=D0-FF A7800_CONTROL=E8 \
--   mame a7800 -cart game.a78 -autoboot_script probes/freeram.lua
--   (or add -playback run-01.inp -video none -sound none -nothrottle)
--
-- Play a full session -- every mode, die, reach the next screen -- then close
-- MAME. Prints a verdict and writes the table to A7800_FREERAM_LOG.
--
--   A7800_CANDIDATES  addresses to test, hex, comma-separated, ranges allowed:
--                     "D7,DC,EF,F9" or "D0-FF" (default 00-FF)
--   A7800_CONTROL     a byte you KNOW is written constantly: a frame counter, an
--                     interpreter's thread pointer (required for a verdict)
--   A7800_FREERAM_LOG output file (default freeram.log)
--
-- ## Why this is needed at all
--
-- A patch that must remember something between calls needs a byte nobody else
-- uses, and reading cannot settle that. Indexed zero-page access wraps: `LDA
-- $55,X` names one address in the listing and touches a different one for
-- every value of X, and a stack-indexed base reaches different bytes at
-- different depths. Which one it is, is a runtime fact. So a static scan can
-- say "no byte is provably free" and nothing more; this asks the machine.
--
-- ## The positive control
--
-- The output is a list of things that did not happen, and a tap reporting zero
-- is the easiest measurement to get wrong. The control byte must be written
-- constantly; if it reports no writes the tap is not working and none of the
-- other zeroes mean anything. This probe says so and withholds its verdict.
--
-- ## Reading the result
--
-- Zero writes across a real session is evidence, not proof -- a path nobody
-- reached could still use the byte. Prefer a byte with no plausible indexed
-- base near it, and play widely rather than long.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]

local OUT = os.getenv("A7800_FREERAM_LOG") or "freeram.log"

local WATCH = {}
for part in (os.getenv("A7800_CANDIDATES") or "00-FF"):gmatch("[^,%s]+") do
  local lo, hi = part:match("^(%x+)%-(%x+)$")
  if not lo then lo = part hi = part end
  for a = tonumber(lo, 16), tonumber(hi, 16) do WATCH[#WATCH + 1] = a end
end
local CONTROL = tonumber(os.getenv("A7800_CONTROL") or "", 16)
local isw = {}
for _, a in ipairs(WATCH) do isw[a] = true end
if CONTROL and not isw[CONTROL] then WATCH[#WATCH + 1] = CONTROL end

local writes, firstpc, frames = {}, {}, 0
for _, a in ipairs(WATCH) do writes[a] = 0 end

-- held in a global: a dead tap does not announce itself (the GC trap)
TAPS = {}
for _, addr in ipairs(WATCH) do
  local a = addr
  TAPS[#TAPS + 1] = mem:install_write_tap(a, a, "freeram", function(offset, data)
    writes[a] = writes[a] + 1
    if not firstpc[a] then firstpc[a] = cpu.state["PC"].value end
    return data
  end)
end

FRAME_CB = emu.register_frame_done(function() frames = frames + 1 end)

local function dump()
  local f = io.open(OUT, "w")
  if not f then print("freeram: could not write " .. OUT) return end
  f:write(string.format("# frames %d\n# addr writes first_pc\n", frames))
  for _, a in ipairs(WATCH) do
    f:write(string.format("%02X %d %04X\n", a, writes[a], firstpc[a] or 0))
  end
  f:close()

  if not CONTROL then
    print("freeram: no A7800_CONTROL given, so a zero below proves nothing. "
          .. "Re-run with a byte that is written every frame.")
  elseif writes[CONTROL] == 0 then
    print(string.format("freeram: THE CONTROL FAILED. $%02X reported no "
          .. "writes, so the taps are not working. Ignore this run.", CONTROL))
    return
  else
    print(string.format("freeram: %d frames; control $%02X saw %d writes, so "
          .. "the taps worked.", frames, CONTROL, writes[CONTROL]))
  end
  local free = {}
  for _, a in ipairs(WATCH) do
    if a ~= CONTROL and writes[a] == 0 then free[#free + 1] = string.format("%02X", a) end
  end
  print(string.format("freeram: %d of %d candidates never written -> %s  "
        .. "(evidence, not proof: play widely before relying on it)",
        #free, #WATCH - (CONTROL and 1 or 0), table.concat(free, " ")))
end

-- held in a global: the notifier is collected if its return value is dropped
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
