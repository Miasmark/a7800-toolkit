-- dumpgfx.lua -- capture RAM and every MARIA register at a moment you choose.
--
--   A7800_GFX_WHEN=<hex addr of a started-playing flag> A7800_GFX_SELECT=1 \
--   mame a7800 -cart game.a78 -autoboot_script probes/dumpgfx.lua \
--        -video none -sound none -nothrottle -skip_gameinfo
--
-- dumpdl.lua answers "what is MARIA drawing right now" for whatever frame you
-- pick. This asks the same question at a frame that means something. Say what
-- "something" is in one of two ways:
--
--   A7800_GFX_WHEN=<hex addr>   wait until that RAM byte becomes non-zero -- a
--                               "game has started", "in a fight", "level
--                               number" flag -- so a title screen or an
--                               attract demo cannot be mistaken for play. Find
--                               one with ramsnap.lua / diffwrites.lua.
--   A7800_GFX_AT=<frames>       otherwise, dump at this frame (default 600).
--
-- It also captures every MARIA register, not just DPPH/DPPL: CHARBASE and
-- CTRL are what decide whether a display-list entry's graphics pointer is a
-- bitmap address or a character-list address, and what pixel format the bytes
-- at that address are in. Guessing either produces a confident wrong picture.
--
--   A7800_GFX_SELECT=1   press Select every second until the trigger fires, for
--                        a game that waits at a title screen. (Under
--                        -playback, leave it off: the recording does the
--                        pressing, and an extra press desyncs it.)
--   A7800_GFX_SETTLE     ignore the trigger before this frame (default 200).
--                        DPPH/DPPL hold the BIOS's own list for a long time
--                        at boot (docs/pitfalls.md).
--   A7800_GFX_REACH      give up if the trigger never fires by this frame
--                        (default 5400).
--   A7800_GFX_DELAY      frames to wait after the trigger, so the dump is not
--                        the very first frame of the new state (default 60).
--   A7800_GFX_BANKSEL    "lo-hi" hex: a bank-select window (SuperGame: 8000-BFFF).
--                        Writes there are tracked, and the bank in force at the
--                        dump is written to the regs file as "bank N", so a
--                        graphics address in a switched window can be attributed.
--   A7800_GFX_BANKS      number of banks (default 8; the bank is the value & N-1).
--                        The regs file also lists, for the dump frame, the registers
--                        at its start (`s $xx = $vv`) and every MARIA register write
--                        in it (`w <NMIs so far> $xx = $vv`), so a reader can give
--                        each zone the palettes and CHARBASE it was drawn with.
--   A7800_GFX_RAM        output, $1800-$27FF, the 7800's whole RAM
--                        (default dumpgfx_ram.bin)
--   A7800_GFX_REGS       output, every MARIA register write seen and the frame
--                        it dumped on (default dumpgfx_regs.txt)
--
-- Then:
--   python tools/dlwalk.py --raw dumpgfx_ram.bin --at 0x1800 --dll <DPPH DPPL> --follow
--   python tools/gfx.py <rom> --base <CHARBASE address> --ascending ...
--       (only if a zone turns out to be indirect; direct-mode zones point
--       straight at ROM or RAM bitmap data with dlwalk's own --gfx addr)
--   python tools/spritedump.py ... --palette-regs dumpgfx_regs.txt

local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local mem = MACHINE.devices[":maincpu"].spaces["program"]

local WHEN   = tonumber(os.getenv("A7800_GFX_WHEN") or "", 16)  -- nil: by frame
local AT     = tonumber(os.getenv("A7800_GFX_AT") or "") or 600
local SELECT = os.getenv("A7800_GFX_SELECT") == "1"
local SETTLE = tonumber(os.getenv("A7800_GFX_SETTLE") or "") or 200
local REACH  = tonumber(os.getenv("A7800_GFX_REACH") or "5400")
local DELAY  = tonumber(os.getenv("A7800_GFX_DELAY") or "60")
local RAM_OUT  = os.getenv("A7800_GFX_RAM") or "dumpgfx_ram.bin"
local REG_OUT  = os.getenv("A7800_GFX_REGS") or "dumpgfx_regs.txt"

local cons = SELECT and MACHINE.ioport.ports[":console_buttons"] or nil

-- Registers per zone. A game repaints palettes and switches CHARBASE or CTRL from
-- its display-list interrupt, so the values at the end of a frame say little
-- about the zones above. Every write in the frame is logged with the number of
-- NMIs (display-list interrupts) that had fired before it; the writes before
-- the first NMI plus the state at the frame's start are what zone 0 was drawn
-- with, and each NMI hands the next zone the registers written since.
local regs = {}
local nmi, log, start = 0, {}, {}
local last_log, last_start = {}, {}
TAPS = {}
TAPS[1] = mem:install_write_tap(0x20, 0x3F, "maria", function(offset, data)
  regs[offset] = data
  log[#log + 1] = { nmi, offset, data }
  return data
end)
-- an NMI shows as a read of its vector
TAPS[3] = mem:install_read_tap(0xFFFA, 0xFFFA, "nmi", function(offset, data)
  nmi = nmi + 1
  return data
end)

local bank = nil
local SEL_LO, SEL_HI = (os.getenv("A7800_GFX_BANKSEL") or ""):match("^(%x+)%-(%x+)$")
if SEL_LO then
  local mask = (tonumber(os.getenv("A7800_GFX_BANKS") or "") or 8) - 1
  TAPS[2] = mem:install_write_tap(tonumber(SEL_LO, 16), tonumber(SEL_HI, 16),
                                  "banksel", function(offset, data)
    bank = data & mask
    return data
  end)
end

local frame, phase, reach_frame = 0, "boot", 0

local function dump()
  local f = io.open(RAM_OUT, "wb")
  for a = 0x1800, 0x27FF do f:write(string.char(mem:read_u8(a))) end
  f:close()

  local g = io.open(REG_OUT, "w")
  g:write(string.format("frame %d\n", frame))
  if bank then g:write(string.format("bank %d\n", bank)) end
  if WHEN then g:write(string.format("trigger $%04X = %d\n", WHEN, mem:read_u8(WHEN))) end
  g:write(string.format("DPPH=$%02X DPPL=$%02X\n", regs[0x2C] or 0, regs[0x30] or 0))
  g:write(string.format("CHARBASE=$%02X OFFSET=$%02X CTRL=$%02X BACKGRND=$%02X\n",
    regs[0x34] or 0, regs[0x38] or 0, regs[0x3C] or 0, regs[0x20] or 0))
  -- registers as they stood when this frame began, then every write in it
  for a = 0x20, 0x3F do
    if last_start[a] then g:write(string.format("s $%02X = $%02X\n", a, last_start[a])) end
  end
  for _, w in ipairs(last_log) do
    g:write(string.format("w %d $%02X = $%02X\n", w[1], w[2], w[3]))
  end
  g:write("palettes (P0C1-P7C3), 21-3F:\n")
  for a = 0x21, 0x3F do
    if a % 8 ~= 4 and a % 4 ~= 0 then   -- skip WSYNC(24)/MSTAT(28) slots roughly
      g:write(string.format("  $%02X = $%02X\n", a, regs[a] or 0))
    end
  end
  g:close()

  print(string.format(
    "dumpgfx: frame %d. DPPH=$%02X DPPL=$%02X CHARBASE=$%02X CTRL=$%02X",
    frame, regs[0x2C] or 0, regs[0x30] or 0,
    regs[0x34] or 0, regs[0x3C] or 0))
  print("dumpgfx: wrote " .. RAM_OUT .. " and " .. REG_OUT)
  print(string.format(
    "dumpgfx: python tools/dlwalk.py --raw %s --at 0x1800 --dll 0x%02X%02X --follow",
    RAM_OUT, regs[0x2C] or 0, regs[0x30] or 0))
end

-- held in a global: a dead callback does not announce itself
FRAME_CB = emu.register_frame_done(function()
  frame = frame + 1
  -- the interval that just ended is what dump() describes; start the next one
  last_log, last_start = log, start
  log, start, nmi = {}, {}, 0
  for k, v in pairs(regs) do start[k] = v end

  if phase == "boot" then
    if SELECT and cons then
      cons.fields["Select"]:set_value((frame % 60 < 8) and 1 or 0)
    end
    local fired
    if WHEN then fired = frame > SETTLE and mem:read_u8(WHEN) ~= 0
    else fired = frame >= AT end
    if fired then
      if SELECT and cons then cons.fields["Select"]:set_value(0) end
      phase, reach_frame = "wait", frame
    elseif frame > REACH then
      phase = "never"
    end
    return
  end

  if phase == "wait" then
    if frame - reach_frame >= DELAY then
      dump()
      MACHINE:exit()
    end
    return
  end

  if phase == "never" then
    print("dumpgfx: the trigger never fired in " .. REACH .. " frames; "
          .. "nothing captured. Check A7800_GFX_WHEN, or raise A7800_GFX_REACH.")
    MACHINE:exit()
  end
end)
