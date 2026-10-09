-- dlitimes.lua -- on which scanline does each display interrupt arrive, and
-- where are the display list list's zone boundaries?
--
--   mame a7800 -cart game.a78 -autoboot_script probes/dlitimes.lua \
--        -video none -sound none -nothrottle -skip_gameinfo -seconds_to_run 10
--
-- Every NMI on the 7800 is a display interrupt. This notes the beam position
-- (raster line, from the top of MAME's 263-line frame) at the first fetch of the NMI vector's target, and at
-- the frame chosen prints the display list list as zones with their first line,
-- height and DLI bit, so the two can be laid side by side:
--
--     zone  0  line   0  height  8  dli 0
--     zone  1  line   8  height  8  dli 1
--     nmi   frame 100  line 23.27  (23 whole lines, 31 cycles in)
--
-- Changes of MSTAT bit 7 (VBLANK) are logged the same way:
--     mstat frame 100  vblank begins  line 258.04
--
-- Output, to A7800_DT_LOG (default dlitimes.log). `line` counts from the first
-- zone, which MARIA starts drawing at screen line 16 (A7800_DT_TOP, default 16).
-- A7800_DT_FRAME is the frame at which the zones are printed (default 100) and
-- A7800_DT_END the frame to stop at (default 130). The NMIs are printed for every
-- frame from A7800_DT_FROM (default 100) to the end.
--
-- LIMITS. The line is a raster line of MAME's frame, which starts MARIA's zone 0 at
-- A7800_DT_TOP. The beam position is taken at the first fetch of the handler: seven cycles after the interrupt was taken, and after whatever
-- instruction was running finished, so it is a few cycles late, not early.
-- Checked on MAME 0.264. It cannot tell a handler's first fetch from a jump to the
-- same address; the vector is read each frame because the cartridge is not mapped
-- at boot.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local PCs = cpu.state["PC"]
local scr
for _, s in pairs(MACHINE.screens) do scr = s break end
-- MAME 0.264's Lua has no screen:vpos()/hpos(); the time until the beam next
-- reaches line 0 gives the same thing. frame_period and scan_period are seconds.
local function beam()
  local lines = scr.frame_period / scr.scan_period
  local left = scr:time_until_pos(0, 0) / scr.scan_period
  local line = (lines - left) % lines
  return line, (line % 1) * 113.5
end

local OUT = os.getenv("A7800_DT_LOG") or "dlitimes.log"
local TOP = tonumber(os.getenv("A7800_DT_TOP") or "") or 16
local SHOW = tonumber(os.getenv("A7800_DT_FRAME") or "") or 100
local FROM = tonumber(os.getenv("A7800_DT_FROM") or "") or 100
local END = tonumber(os.getenv("A7800_DT_END") or "") or 130

local o = io.open(OUT, "w")
local frame, vec, armed = 0, -1, false
local dpph, dppl = 0, 0

-- held in globals: a dead tap does not announce itself (docs/pitfalls.md)
TAPS = {}
TAPS[1] = mem:install_write_tap(0x0001, 0x0001, "dt-inptctrl", function(offset, data)
  if (data & 1) ~= 0 then armed = true end
  return data
end)
TAPS[2] = mem:install_write_tap(0x20, 0x3F, "dt-maria", function(offset, data)
  if offset == 0x2C then dpph = data elseif offset == 0x30 then dppl = data end
  return data
end)
TAPS[3] = mem:install_read_tap(0x4000, 0xFFFF, "dt-fetch", function(offset, data)
  if armed and offset == vec and offset == PCs.value and frame >= FROM then
    local line, cyc = beam()
    o:write(string.format("nmi   frame %d  line %.2f  (%d whole lines, %.0f cycles in)\n",
                          frame, line, math.floor(line), cyc))
  end
  return data
end)

-- MSTAT bit 7 (VBLANK) as the game sees it: every change between two reads, with
-- the raster line of the read. A game polling it in a tight loop gives the edge to
-- within a few cycles; one that polls rarely gives only the line of its poll.
local last_vb = nil
TAPS[4] = mem:install_read_tap(0x28, 0x28, "dt-mstat", function(offset, data)
  local vb = (data & 0x80) ~= 0
  if armed and frame >= FROM and last_vb ~= nil and vb ~= last_vb then
    local line = beam()
    o:write(string.format("mstat frame %d  vblank %s  line %.2f\n", frame, vb and "begins" or "ends", line))
  end
  last_vb = vb
  return data
end)

local function zones()
  local base = (dpph << 8) | dppl
  local line = 0
  for z = 0, 40 do
    local b0 = mem:read_u8(base + 3 * z)
    local h = (b0 & 0x0F) + 1
    if mem:read_u8(base + 3 * z + 1) == 0 and z > 0 then break end
    o:write(string.format("zone %2d  line %3d  height %2d  dli %d\n", z, line, h, (b0 >> 7) & 1))
    line = line + h
    if line >= 263 then break end
  end
end

local function stop() pcall(function() o:close() end) end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(stop) end
emu.register_frame_done(function()
  frame = frame + 1
  vec = mem:read_u8(0xFFFA) | (mem:read_u8(0xFFFB) << 8)
  if frame == SHOW then
    o:write(string.format("dll   $%04X  top screen line %d\n", (dpph << 8) | dppl, TOP))
    zones()
  end
  if frame >= END then o:flush(); stop(); MACHINE:exit() end
end)
