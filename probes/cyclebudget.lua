-- cyclebudget.lua -- where a frame's CPU cycles go, and how many MARIA took.
--
--   A7800_CB_RANGES="main=C000-DFFF,nmi=E000-EFFF" \
--   mame a7800 -cart game.a78 -autoboot_script probes/cyclebudget.lua \
--        -video none -sound none -nothrottle -seconds_to_run 60
--
-- Per block of frames, averaged per frame:
--   executed  6502 cycles actually run: the opcode's base count, +1 for a taken
--             branch, +1 for a page crossing on an indexed read, and 7 for each
--             NMI entry. (IRQ and BRK entries are not seen, so a game that uses
--             them reads a little low.)
--   nmi       the part of executed that ran between the NMI vector and its RTI
--   slow      +0.5 cycle for every access to TIA ($00-$1F) and RIOT
--             ($280-$2FF), which run at 1.19 MHz (docs/hardware.md)
--   dma       frame - executed - slow: what MARIA took. The CPU is halted while
--             MARIA reads, so what the CPU did not spend, MARIA did. A game that
--             idles in a loop gives an exact answer; one that ends a frame early
--             and spins still counts the spin as executed, which is right.
--   ranges    executed cycles per named PC range (A7800_CB_RANGES), for "how
--             much of the frame is the sound driver / the drawing routine".
-- Compare dma with tools/dmabudget.py's model of the same display list.
--
-- Output, one line per block, to A7800_CB_OUT (default cyclebudget.log):
--   f<frame> executed N nmi N slow N dma N [name N ...]
--
--   A7800_CB_FROM    first frame counted (default 0)
--   A7800_CB_END     stop after this frame (default 600)
--   A7800_CB_BLOCK   frames per output line (default 100)
--   A7800_CB_FRAME   cycles in a frame (default 29868 = 262 lines x 114, NTSC;
--                    PAL is 35568 = 312 x 114)
--   A7800_CB_RANGES  name=lo-hi,... hex PC ranges, no $
--   A7800_CB_OUT     output file
--
-- LIMITS. Page-crossing extras are counted for indexed reads and (zp),Y reads
-- only, not for stores (which always take the longer count in the table).
-- Cycles the 6502 loses to a RAM refresh or a wait state are not modelled.
-- The measurement is a difference, so any error in `executed` lands in `dma`.
-- It has not been checked against an independent count of MARIA's cycles.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local PCs, SPs, Xs, Ys = cpu.state["PC"], cpu.state["SP"], cpu.state["X"], cpu.state["Y"]
local CYC = {7,6,0,8,3,3,5,5,3,2,2,2,4,4,6,6,2,5,0,8,4,4,6,6,2,4,2,7,4,4,7,7,6,6,0,8,3,3,5,5,4,2,2,2,4,4,6,6,2,5,0,8,4,4,6,6,2,4,2,7,4,4,7,7,6,6,0,8,3,3,5,5,3,2,2,2,3,4,6,6,2,5,0,8,4,4,6,6,2,4,2,7,4,4,7,7,6,6,0,8,3,3,5,5,4,2,2,2,5,4,6,6,2,5,0,8,4,4,6,6,2,4,2,7,4,4,7,7,2,6,2,6,3,3,3,3,2,2,2,2,4,4,4,4,2,6,0,6,4,4,4,4,2,5,2,5,5,5,5,5,2,6,2,6,3,3,3,3,2,2,2,2,4,4,4,4,2,5,0,5,4,4,4,4,2,4,2,4,4,4,4,4,2,6,2,8,3,3,5,5,2,2,2,2,4,4,6,6,2,5,0,8,4,4,6,6,2,4,2,7,4,4,7,7,2,6,2,8,3,3,5,5,2,2,2,2,4,4,6,6,2,5,0,8,4,4,6,6,2,4,2,7,4,4,7,7}
local MODE = {"imp","izx","imp","izx","zp","zp","zp","zp","imp","imm","acc","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpx","zpx","imp","aby","imp","aby","abx","abx","abx","abx","abs","izx","imp","izx","zp","zp","zp","zp","imp","imm","acc","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpx","zpx","imp","aby","imp","aby","abx","abx","abx","abx","imp","izx","imp","izx","zp","zp","zp","zp","imp","imm","acc","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpx","zpx","imp","aby","imp","aby","abx","abx","abx","abx","imp","izx","imp","izx","zp","zp","zp","zp","imp","imm","acc","imm","ind","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpx","zpx","imp","aby","imp","aby","abx","abx","abx","abx","imm","izx","imm","izx","zp","zp","zp","zp","imp","imm","imp","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpy","zpy","imp","aby","imp","aby","abx","abx","aby","aby","imm","izx","imm","izx","zp","zp","zp","zp","imp","imm","imp","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpy","zpy","imp","aby","imp","aby","abx","abx","aby","aby","imm","izx","imm","izx","zp","zp","zp","zp","imp","imm","imp","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpx","zpx","imp","aby","imp","aby","abx","abx","abx","abx","imm","izx","imm","izx","zp","zp","zp","zp","imp","imm","imp","imm","abs","abs","abs","abs","rel","izy","imp","izy","zpx","zpx","zpx","zpx","imp","aby","imp","aby","abx","abx","abx","abx"}
local PEN = {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,1,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,1,0,0,0,0,0,1,0,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,1,1,0,0}
local FROM = tonumber(os.getenv("A7800_CB_FROM") or "") or 0
local END = tonumber(os.getenv("A7800_CB_END") or "") or 600
local BLOCK = tonumber(os.getenv("A7800_CB_BLOCK") or "") or 100
local FRAME = tonumber(os.getenv("A7800_CB_FRAME") or "") or 29868
local OUT = os.getenv("A7800_CB_OUT") or "cyclebudget.log"
local RANGES = {}
for name, lo, hi in (os.getenv("A7800_CB_RANGES") or ""):gmatch("([%w_]+)=(%x+)%-(%x+)") do
  RANGES[#RANGES + 1] = {name = name, lo = tonumber(lo, 16), hi = tonumber(hi, 16), n = 0}
end

local function r(a) return mem:read_u8(a & 0xFFFF) end
local nmi_vec = -1          -- read each frame: the cartridge is not mapped at boot
local executed, innmi, slow, nframes, f = 0, 0, 0, 0, 0
local lastpc, lastop = -1, 0
local in_nmi, nmi_sp = false, 0
local reading = false

-- held in globals: a dead tap does not announce itself (docs/pitfalls.md)
TAPS = {}
TAPS[1] = mem:install_read_tap(0x0000, 0xFFFF, "cb-fetch", function(a, d)
  if reading or a ~= PCs.value or f < FROM then return d end
  local cost = 0
  -- the previous instruction's branch outcome
  if lastpc >= 0 and MODE[lastop + 1] == "rel" and a ~= ((lastpc + 2) & 0xFFFF) then
    cost = cost + 1
    if (a & 0xFF00) ~= ((lastpc + 2) & 0xFF00) then cost = cost + 1 end
  end
  if a == nmi_vec and not in_nmi and lastpc >= 0 and a ~= lastpc then
    in_nmi, nmi_sp = true, SPs.value & 0xFF
    cost = cost + 7
  end
  cost = cost + CYC[d + 1]
  if PEN[d + 1] == 1 then
    reading = true
    local m, base, idx = MODE[d + 1], 0, 0
    if m == "abx" or m == "aby" then
      base = r(a + 1) | (r(a + 2) << 8)
      idx = (m == "abx") and Xs.value or Ys.value
    elseif m == "izy" then
      local z = r(a + 1)
      base = r(z) | (r((z + 1) & 0xFF) << 8)
      idx = Ys.value
    end
    reading = false
    if ((base + idx) & 0xFF00) ~= (base & 0xFF00) then cost = cost + 1 end
  end
  executed = executed + cost
  if in_nmi then
    innmi = innmi + cost
    if d == 0x40 and (SPs.value & 0xFF) == nmi_sp then in_nmi = false end
  end
  for _, rg in ipairs(RANGES) do
    if a >= rg.lo and a <= rg.hi then rg.n = rg.n + cost end
  end
  lastpc, lastop = a, d
  return d
end)
local function slowtap(a, d) if f >= FROM then slow = slow + 0.5 end return d end
TAPS[2] = mem:install_read_tap(0x0000, 0x001F, "cb-slow1", slowtap)
TAPS[3] = mem:install_write_tap(0x0000, 0x001F, "cb-slow2", slowtap)
TAPS[4] = mem:install_read_tap(0x0280, 0x02FF, "cb-slow3", slowtap)
TAPS[5] = mem:install_write_tap(0x0280, 0x02FF, "cb-slow4", slowtap)

local o = io.open(OUT, "w")
local function stop() o:close(); MACHINE:exit() end
emu.add_machine_stop_notifier(function() pcall(function() o:close() end) end)
emu.register_frame_done(function()
  f = f + 1
  nmi_vec = r(0xFFFA) | (r(0xFFFB) << 8)
  if f <= FROM then executed, innmi, slow = 0, 0, 0; return end
  nframes = nframes + 1
  if nframes == BLOCK then
    local ex, sl = executed / BLOCK, slow / BLOCK
    local line = string.format("f%d executed %.0f nmi %.0f slow %.0f dma %.0f",
                               f, ex, innmi / BLOCK, sl, FRAME - ex - sl)
    for _, rg in ipairs(RANGES) do
      line = line .. string.format(" %s %.0f", rg.name, rg.n / BLOCK)
      rg.n = 0
    end
    o:write(line .. "\n"); o:flush()
    executed, innmi, slow, nframes = 0, 0, 0, 0
  end
  if f >= END then stop() end
end)
