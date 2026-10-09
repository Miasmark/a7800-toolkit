-- liveslots.lua -- every ROM address the live display lists reference, over a
-- whole run, with the widest object seen at each.
--
--   mame a7800 -rompath ../bios -cart game.a78 -skip_gameinfo -video none \
--        -sound none -nothrottle -playback run-01.inp \
--        -autoboot_script probes/liveslots.lua -str 600
--
-- A static scan finds graphics the code could draw; this finds what a real
-- session did draw. Use it to confirm candidate sprite or character sheets
-- (from assets.py or gfx.py) on evidence, and to leave the unconfirmed ones
-- marked as unconfirmed.
--
-- Each frame it follows the display list list (DPPH/DPPL, written to $2C/$30)
-- to each zone's display list, and from there to each object's graphics
-- address. Four- and five-byte entries are told apart by the second byte, as
-- dlwalk.py does (docs/hardware.md).
--
--   A7800_ROMLO   lowest address counted as ROM (default 4000, where cartridge
--                 space starts); objects pointing into RAM are ignored
--   A7800_LINES   scanlines of display to walk (default 242; use 192 for a
--                 shorter picture)
--   A7800_SETTLE  frames to ignore at boot (default 200; see below)
--   A7800_OUT     output file (default liveslots-out.json)
--
-- Output: {"frames":N,"refs":[{"addr":32768,"width":4},...]}, checkpointed every
-- 300 frames so a long run still leaves output if killed.
--
-- Boot: DPPH/DPPL hold the BIOS's own list for a long time (Dig Dug: frame 16 to
-- 165), so nothing is recorded before A7800_SETTLE frames. Past that, a few
-- references can still be torn reads -- the callback races the CPU -- and are
-- discarded when they fall below A7800_ROMLO. docs/pitfalls.md has the story.
--
-- Caveat: this reads the lists as they stand at the end of each frame. A game
-- that rebuilds its lists mid-frame, or switches them in a display interrupt,
-- will show only what is left at frame end. romcoverage.lua is the check that
-- counts reads, whatever the list looked like when.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local mem = MACHINE.devices[":maincpu"].spaces["program"]

local ROMLO = tonumber(os.getenv("A7800_ROMLO") or "4000", 16)
local OUT   = os.getenv("A7800_OUT") or "liveslots-out.json"
local SCREEN = tonumber(os.getenv("A7800_LINES") or "") or 242
local SETTLE = tonumber(os.getenv("A7800_SETTLE") or "") or 200

local F, dpph, dppl = 0, nil, nil
local refs = {}          -- addr -> max width seen

-- held in a global: a dead tap does not announce itself (the GC trap)
TAP_MARIA = mem:install_write_tap(0x20, 0x3F, "maria regs", function(offset, data)
  if offset == 0x2C then dpph = data end
  if offset == 0x30 then dppl = data end
  return data
end)

local function byte(a) return mem:read_u8(a) end

local function walk_dl(addr)
  for _ = 1, 48 do
    local b0, b1 = byte(addr), byte(addr + 1)
    if b1 == 0 then return end                    -- terminator
    local gfx, width, len
    if (b1 & 0x1F) == 0 then                      -- five-byte extended entry
      local b2, b3 = byte(addr + 2), byte(addr + 3)
      gfx, width, len = b0 | (b2 << 8), (~b3 & 0x1F) + 1, 5
    else                                          -- four-byte direct entry
      local b2 = byte(addr + 2)
      gfx, width, len = b0 | (b2 << 8), (~b1 & 0x1F) + 1, 4
    end
    if gfx >= ROMLO and gfx < 0x10000 then
      if not refs[gfx] or width > refs[gfx] then refs[gfx] = width end
    end
    addr = addr + len
  end
end

local function walk_dll(base)
  -- No last-entry flag exists: the list runs until the screen is full. Each
  -- zone is (offset & 15) + 1 scanlines tall.
  local lines = 0
  for z = 0, 63 do
    local a = base + 3 * z
    local b0, b1, b2 = byte(a), byte(a + 1), byte(a + 2)
    local dl = (b1 << 8) | b2
    if dl == 0 then break end
    walk_dl(dl)
    lines = lines + (b0 & 0x0F) + 1
    if lines >= SCREEN then break end
  end
end

local function dump()
  local parts, n = {}, 0
  for a, w in pairs(refs) do
    parts[#parts + 1] = string.format('{"addr":%d,"width":%d}', a, w)
    n = n + 1
  end
  local h = io.open(OUT, "w")
  if h then
    h:write(string.format('{"frames":%d,"refs":[%s]}\n', F, table.concat(parts, ",")))
    h:close()
  end
  print(string.format("wrote %s: frames=%d rom refs=%d", OUT, F, n))
end

FRAME_CB = emu.register_frame_done(function()
  F = F + 1
  if F > SETTLE and dpph and dppl then walk_dll((dpph << 8) | dppl) end
  if F % 300 == 0 then dump() end
end)
-- held in a global; register_stop is the older spelling (deprecated in new MAME)
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
