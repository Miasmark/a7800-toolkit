-- forcedl.lua -- put a display-list entry, graphics and palette into a running
-- machine, and take a screenshot, to see what MARIA does with them.
--
--   A7800_FD_WRITES="1930=00 C0 20 3C 28 00; 2000*16/256=1B E4 6C C6; 21=46 86 C6" \
--   A7800_FD_CTRL=40 \
--   mame a7800 -cart tests/carts/synth128.a78 -autoboot_script probes/forcedl.lua \
--        -video none -sound none -nothrottle -seconds_to_run 2 \
--        -snapshot_directory shots
--
-- The way to learn what a MARIA mode does is to ask the hardware, and MAME's
-- MARIA is the nearest thing to hand. This is how docs/graphics.md's pixel
-- formats were checked (and how a wrong bit position in dlwalk.py was found).
-- It needs a cartridge that already runs and builds a display list in RAM, so
-- there is a screen to draw on: tests/carts/synth128.a78 is one, with zone 3's
-- list at $1930 free to overwrite.
--
--   A7800_FD_WRITES   ';'-separated writes, applied in order:
--                       ADDR=bytes                 hex bytes at ADDR
--                       ADDR*N/STRIDE=bytes        the same bytes N times,
--                                                  STRIDE (hex) apart -- for
--                                                  line-planar data, one copy
--                                                  per scanline page
--                     MARIA's registers are memory-mapped at $20-$3F, so a
--                     palette is just "21=46 86 C6" (P0C1-P0C3).
--   A7800_FD_CTRL     hex value for CTRL ($3C), written after the others
--   A7800_FD_AT       frame at which to write (default 30)
--   A7800_FD_REPEAT   rewrite every this many frames (default 15), so the game
--                     cannot quietly undo it
--
-- The game rewrites palette 0 and its own display list every frame; the zone
-- and palettes 1-7 you are not using are left alone. Palette 0 is not yours.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local mem = MACHINE.devices[":maincpu"].spaces["program"]

local AT = tonumber(os.getenv("A7800_FD_AT") or "") or 30
local EVERY = tonumber(os.getenv("A7800_FD_REPEAT") or "") or 15
local CTRL = tonumber(os.getenv("A7800_FD_CTRL") or "", 16)

local writes = {}
for item in (os.getenv("A7800_FD_WRITES") or ""):gmatch("[^;]+") do
  local lhs, rhs = item:match("^%s*([^=]+)=(.*)$")
  if lhs then
    local addr, count, stride = lhs:match("^%s*(%x+)%*(%d+)/(%x+)%s*$")
    if not addr then addr = lhs:match("^%s*(%x+)%s*$") count, stride = 1, "0" end
    local bytes = {}
    for b in rhs:gmatch("%x+") do bytes[#bytes + 1] = tonumber(b, 16) end
    writes[#writes + 1] = { tonumber(addr, 16), tonumber(count), tonumber(stride, 16), bytes }
  end
end

local F = 0
local function apply()
  for _, w in ipairs(writes) do
    for i = 0, w[2] - 1 do
      for j, v in ipairs(w[4]) do mem:write_u8(w[1] + i * w[3] + j - 1, v) end
    end
  end
  if CTRL then mem:write_u8(0x3C, CTRL) end
end

-- held in a global: a dead callback does not announce itself (docs/pitfalls.md)
FRAME_CB = emu.register_frame_done(function()
  F = F + 1
  if F >= AT and (F - AT) % EVERY == 0 then apply() end
end)

-- the snapshot is taken at machine stop, not mid-stream (see snapstop.lua)
local function snap() pcall(function() MACHINE.video:snapshot() end) end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(snap)
else emu.register_stop(snap) end
