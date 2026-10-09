-- pcprof.lua -- where does the CPU spend its time? A sampling profiler with no
-- timer: it reads the program counter each time MARIA starts a zone.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/pcprof.lua \
--        -video none -sound none -nothrottle -seconds_to_run 60
--   python tools/pcmap.py pcprof.log -c annotations.json
--
-- MAME's Lua has no periodic timer to sample from, but the machine has one built
-- in: MARIA reads one three-byte display-list-list entry at the start of every
-- zone, and a read tap on the list sees it. That is about thirty evenly spaced
-- samples a frame (one per zone), and the PC at that moment is where the 6502
-- was when MARIA took the bus. Over a minute that is ~100,000 samples, plenty for
-- a ranking of routines.
--
-- Output, one line per distinct place, sorted:
--     <space>:<pc> <count>
-- `space` is f<N> for the fixed upper half and b<N> for window bank N, as in
-- exectrace.lua; tools/pcmap.py turns it into routine names.
--
--   A7800_PC_OUT       output file (default pcprof.log)
--   A7800_PC_FROM      first frame to count (default 0), A7800_PC_TO last (default end)
--   A7800_PC_BANKSEL   "lo-hi" hex bank-select window (default 8000-BFFF)
--   A7800_PC_BANKS     number of banks (default 8; 1 for an unbanked cartridge)
--
-- LIMITS. Only the visible frame is sampled: code that runs in vertical blank is
-- not in the profile at all, and a zone that holds MARIA's DMA for long is
-- sampled at its start. The PC read during DMA is the instruction the CPU was
-- halted before (docs/pitfalls.md), which is what is wanted here. A CPU data
-- read of an address inside the list is counted too; there are few. The DLL's
-- address is learned from DPPH/DPPL writes, so nothing is counted before the game
-- sets it. A sample is where the CPU was, not what it cost: a routine that runs
-- a long time shows up more, but a long routine called rarely shows up less than
-- its time deserves if it happens to be between zone starts. Treat it as a ranking.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]

local OUT = os.getenv("A7800_PC_OUT") or "pcprof.log"
local FROM = tonumber(os.getenv("A7800_PC_FROM") or "") or 0
local TO = tonumber(os.getenv("A7800_PC_TO") or "") or math.huge
local NBANKS = tonumber(os.getenv("A7800_PC_BANKS") or "") or 8
local SEL_LO, SEL_HI = (os.getenv("A7800_PC_BANKSEL") or "8000-BFFF"):match("^(%x+)%-(%x+)$")
SEL_LO, SEL_HI = tonumber(SEL_LO, 16), tonumber(SEL_HI, 16)
local LOWBANK = tonumber(os.getenv("A7800_PC_LOWBANK") or "")        -- the file bank at $4000, if fixed
local FIRST = tonumber(os.getenv("A7800_PC_FIRST") or "") or 0      -- window value 0 -> file bank FIRST
local WBANKS = tonumber(os.getenv("A7800_PC_WBANKS") or "") or NBANKS  -- (EXROM: 1 and NBANKS - 1)

local dpph, dppl, bank, F = nil, nil, 0, 0
local counts = {}

-- held in globals: a dead tap does not announce itself (docs/pitfalls.md)
TAPS = {}
TAPS[1] = mem:install_write_tap(0x20, 0x3F, "maria", function(offset, data)
  if offset == 0x2C then dpph = data end
  if offset == 0x30 then dppl = data end
  return data
end)
TAPS[2] = mem:install_write_tap(SEL_LO, SEL_HI, "banksel", function(offset, data)
  bank = FIRST + (data % WBANKS)
  return data
end)
TAPS[3] = mem:install_read_tap(0x1800, 0x27FF, "dll", function(offset, data)
  if dpph and dppl and F >= FROM and F <= TO then
    local dll = dpph * 256 + dppl
    local d = offset - dll
    if d >= 0 and d < 120 and d % 3 == 0 then
      local pc = cpu.state["PC"].value
      local space = (pc >= SEL_LO and pc <= SEL_HI) and ("b" .. bank) or ("f" .. ((LOWBANK and pc < 0x8000) and LOWBANK or (NBANKS - 1)))
      local key = string.format("%s:%04X", space, pc)
      counts[key] = (counts[key] or 0) + 1
    end
  end
  return data
end)

FRAME_CB = emu.register_frame_done(function() F = F + 1 end)

local function dump()
  local keys, total = {}, 0
  for k, n in pairs(counts) do keys[#keys + 1] = k total = total + n end
  table.sort(keys)
  local f = io.open(OUT, "w")
  if not f then print("pcprof: cannot write " .. OUT) return end
  for _, k in ipairs(keys) do f:write(string.format("%s %d\n", k, counts[k])) end
  f:close()
  print(string.format("pcprof: %d samples at %d places over %d frames, wrote %s",
                      total, #keys, F, OUT))
  if total == 0 then
    print("pcprof: ZERO samples. The display list list was never read through the "
          .. "tap (DPPH/DPPL never written, or the list is not in RAM $1800-$27FF).")
  end
end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
