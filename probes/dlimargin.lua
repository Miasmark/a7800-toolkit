-- dlimargin.lua -- how close does each display interrupt's handler come to the next one?
--
--   mame a7800 -cart game.a78 -autoboot_script probes/dlimargin.lua \
--        -video none -sound none -nothrottle -skip_gameinfo
--
-- dlitimes.lua says on which line each NMI arrives. This says when its handler is done
-- (the RTI: the next fetch at the address the interrupt pushed, with the stack back where
-- it was) and how many lines are left before the next NMI arrives: the margin. A handler
-- that runs on past the next NMI is interrupted by it (the 7800's NMI can't be masked), or,
-- if the game holds the next one off in software, makes it late. Either way, whatever that
-- next handler does on its line (a colour change, a CHARBASE, a sky) lands lines late in
-- the frames where the first runs long: a flicker that comes and goes with the work (a
-- music tick is the usual culprit), and that a slower machine shows where MAME doesn't.
-- Measured this way on Karateka's MARIA Edition: a music DLI's handler ended within a line
-- of the status bar's colour DLI in MAME, and the arrows flickered on an FPGA core.
--
-- An NMI is named by the line it arrives on (a DLI comes on a fixed line for its zone,
-- give or take the instruction it interrupted; arrivals within A7800_DM_JOIN lines, default
-- 3, are one NMI). For each, over frames A7800_DM_FROM to A7800_DM_END:
--
--     nmi at line 185  arrives 185.1-185.8  ends 187.5-213.6  next at 214  margin 0.9-26.8  (274)  <- tight
--     nmi at line 153  arrives 153.2-156.3  ends 154.3-194.9  next at 175  margin -14.0-42.0  (1500)  <- OVERRUN
--         the next NMI arrived while this one ran, in 8 frames: 4302 4310 4318 ...
--
-- `ends` is in lines from the top of the frame the NMI arrived in, so a handler that runs
-- into the next frame ends past the last line (263 on NTSC). `margin` is the next NMI's
-- arrival minus this handler's end, in lines; its smallest value is the one to watch.
-- Under A7800_DM_TIGHT lines (default 2) is marked "tight"; negative, "OVERRUN", with the
-- frames listed. Lines are MAME's raster lines, as dlitimes.lua counts them
-- (screen:time_until_pos: MAME 0.264-0.287's Lua has no vpos()).
--
-- Environment: A7800_DM_LOG (default dlimargin.log), A7800_DM_FROM (default 100),
-- A7800_DM_END (default 400: the run stops there), A7800_DM_EVERY=1 also logs every NMI,
-- A7800_DM_STOP=0 to let another script end the run.
--
-- LIMITS. The arrival is the first fetch of the vector's target: seven cycles after the
-- interrupt was taken, after the instruction it interrupted. The end is the fetch at the
-- return address, just after the RTI. A handler that leaves another way (resets the stack,
-- jumps into the main loop) never ends, and is counted as such. While a handler is open,
-- every read of the program space goes through this probe, so the run is slower than plain
-- MAME. A fetch is told from MARIA's DMA reads (which go through the CPU's space in stock
-- MAME) by the PC and the stack pointer both matching.
local MACHINE = (type(manager.machine) == "function") and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local PCs, SPs = cpu.state["PC"], cpu.state["SP"]
local scr
for _, s in pairs(MACHINE.screens) do scr = s break end
local function now() return MACHINE.time:as_double() end
local function beam()
  local lines = scr.frame_period / scr.scan_period
  local left = scr:time_until_pos(0, 0) / scr.scan_period
  return (lines - left) % lines
end

local OUT = os.getenv("A7800_DM_LOG") or "dlimargin.log"
local FROM = tonumber(os.getenv("A7800_DM_FROM") or "") or 100
local END = tonumber(os.getenv("A7800_DM_END") or "") or 400
local EVERY = os.getenv("A7800_DM_EVERY") == "1"
local TIGHT = tonumber(os.getenv("A7800_DM_TIGHT") or "") or 2
local JOIN = tonumber(os.getenv("A7800_DM_JOIN") or "") or 3
local STOPRUN = os.getenv("A7800_DM_STOP") ~= "0"
local o = io.open(OUT, "w")

local frame, vec, armed = 0, -1, false
local open = {}          -- handlers running, innermost last: {key, line, t, ret, sp, next_t}
local seq = {}           -- this frame's events, for A7800_DM_EVERY
local stats = {}         -- by key (an arrival line): {arr, fin, gap, count, overrun, nexts}
local keys = {}          -- the keys seen, to join arrivals a line or two apart
local last = nil         -- the handler that ended last, until the next arrival: {key, t}
local lost = 0

local function range(t, k, v)
  local e = t[k]
  if not e then t[k] = {v, v} else
    if v < e[1] then e[1] = v end
    if v > e[2] then e[2] = v end
  end
end
local function key_of(line)
  for _, k in ipairs(keys) do
    if math.abs(k - line) <= JOIN then return k end
  end
  local k = math.floor(line + 0.5)
  keys[#keys + 1] = k
  stats[k] = {count = 0, overrun = {}, nexts = {}}
  return k
end
local function on() return armed and frame >= FROM and frame <= END end
local function lines_since(t0) return (now() - t0) / scr.scan_period end

-- held in globals: a tap that is garbage-collected stops without a word (docs/pitfalls.md)
DM_TAPS = {}
DM_TAPS[1] = mem:install_write_tap(0x0001, 0x0001, "dm-inptctrl", function(offset, data)
  if (data & 1) ~= 0 then armed = true end          -- the BIOS handed the cartridge over
  return data
end)
DM_TAPS[2] = mem:install_read_tap(0x0000, 0xFFFF, "dm-fetch", function(offset, data)
  if not on() or offset ~= PCs.value then return data end
  local sp = SPs.value & 0xFF
  -- the end of the innermost handler: back at the address the interrupt pushed, stack restored
  local h = open[#open]
  if h and offset == h.ret and sp == h.sp then
    open[#open] = nil
    local s = stats[h.key]
    local fin = h.line + lines_since(h.t)
    range(s, "fin", fin)
    if EVERY then seq[#seq + 1] = string.format("end%d@%.1f", h.key, fin) end
    if h.next_t then
      range(s, "gap", (h.next_t - now()) / scr.scan_period)   -- negative: it overran
      s.overrun[#s.overrun + 1] = frame
      last = nil
    else
      last = {key = h.key, t = now()}
    end
    return data
  end
  -- an arrival: the first fetch of the vector's target
  if offset == vec then
    local line = beam()
    local k = key_of(line)
    local s = stats[k]
    s.count = s.count + 1
    range(s, "arr", line)
    if EVERY then seq[#seq + 1] = string.format("nmi%d@%.1f", k, line) end
    local prev = open[#open]
    if prev then
      if not prev.next_t then prev.next_t = now(); stats[prev.key].nexts[k] = true end
    elseif last then
      range(stats[last.key], "gap", lines_since(last.t))
      stats[last.key].nexts[k] = true
    end
    last = nil
    -- the interrupt pushed PC (high, low) then P: the return address is just above P
    local ret = mem:read_u8(0x100 + ((sp + 2) & 0xFF)) | (mem:read_u8(0x100 + ((sp + 3) & 0xFF)) << 8)
    open[#open + 1] = {key = k, line = line, t = now(), ret = ret, sp = (sp + 3) & 0xFF}
  end
  return data
end)

local function report()
  o:write(string.format("dlimargin: frames %d-%d; lines are MAME raster lines from the top of the frame\n",
    FROM, math.min(frame, END)))
  table.sort(keys)
  local function r(e) return e and string.format("%5.1f-%5.1f", e[1], e[2]) or "     -     " end
  for _, k in ipairs(keys) do
    local s = stats[k]
    local nx = {}
    for n in pairs(s.nexts) do nx[#nx + 1] = n end
    table.sort(nx)
    local nxs = #nx > 0 and table.concat(nx, "/") or "-"
    local mark = ""
    if s.gap and s.gap[1] < 0 then mark = "  <- OVERRUN"
    elseif s.gap and s.gap[1] < TIGHT then mark = "  <- tight" end
    o:write(string.format("nmi at line %3d  arrives %s  ends %s  next at %s  margin %s  (%d)%s\n",
      k, r(s.arr), r(s.fin), nxs, r(s.gap), s.count, mark))
    if #s.overrun > 0 then
      local fs = {}
      for i = 1, math.min(#s.overrun, 12) do fs[#fs + 1] = tostring(s.overrun[i]) end
      o:write(string.format("    the next NMI arrived while this one ran, in %d frames: %s%s\n",
        #s.overrun, table.concat(fs, " "), #s.overrun > 12 and " ..." or ""))
    end
  end
  if lost > 0 then o:write(string.format("%d handlers never returned to where they were interrupted\n", lost)) end
end

local done = false
local function stop()
  if done then return end
  done = true
  pcall(function() report(); o:close() end)
end
if emu.add_machine_stop_notifier then DM_STOP = emu.add_machine_stop_notifier(stop) end
emu.register_frame_done(function()
  if on() and EVERY and #seq > 0 then o:write(string.format("f%d %s\n", frame, table.concat(seq, " "))) end
  seq = {}
  frame = frame + 1
  if #open > 4 then lost = lost + #open; open = {} end      -- left another way: give them up
  vec = mem:read_u8(0xFFFA) | (mem:read_u8(0xFFFB) << 8)
  if frame == END + 1 then
    stop()
    if STOPRUN then MACHINE:exit() end
  end
end)
