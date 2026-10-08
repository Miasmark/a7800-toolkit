-- pcwrites.lua -- every RAM write in a frame window, tagged with the true 6502
-- PC that performed it.
--
--   A7800_PW_LO=0x1800 A7800_PW_HI=0x1FFF A7800_PW_FROM=600 A7800_PW_TO=900 \
--   mame a7800 -cart game.a78 -autoboot_script probes/pcwrites.lua
--
-- A write tap on one fixed address cannot catch a computed pointer: an
-- indirect `STA ($02),Y` lands somewhere the listing does not name. This taps
-- a whole range and lets you filter by PC (or by address) afterwards -- "which
-- instruction writes this byte" and "what does this instruction write" are
-- both a grep away.
--
--   A7800_PW_LO / A7800_PW_HI    address range (default $0000-$27FF, all RAM)
--   A7800_PW_FROM / A7800_PW_TO  frame window (default: the whole run)
--   A7800_PW_LOG                 output (default pcwrites.log)
--
-- Output, one write per line:   frame $addr old new $pc
-- Keep the window narrow: a full RAM range over many frames is a very large
-- log (probes/diffwrites.lua is the cheap first look, one line per address).
-- Read-modify-write instructions (INC, DEC, ASL, LSR, ROL, ROR) write twice: the
-- old value back first, then the new one. So `INC counter` appears as two lines,
-- `98 98` then `98 99`, and a byte's write count is double its instruction count.
-- Under MARIA DMA the PC at a tap is the PC of the next instruction fetched
-- (docs/pitfalls.md); read a "writer" with that in mind.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]

local OUT  = os.getenv("A7800_PW_LOG") or "pcwrites.log"
local FROM = tonumber(os.getenv("A7800_PW_FROM") or "0")
local TO   = tonumber(os.getenv("A7800_PW_TO") or "1000000000")
local LO   = tonumber(os.getenv("A7800_PW_LO") or "0x0000")
local HI   = tonumber(os.getenv("A7800_PW_HI") or "0x27FF")

local f = io.open(OUT, "w")
f:write("frame addr old new pc\n")

local F = 0
-- held in globals: a dead tap does not announce itself (the GC trap)
TAPS = { mem:install_write_tap(LO, HI, "pcwrites", function(offset, data)
  if F >= FROM and F <= TO then
    f:write(string.format("%d $%04X %d %d $%04X\n", F, offset,
            mem:read_u8(offset), data, cpu.state["PC"].value))
  end
  return data
end) }
FRAME_CB = emu.register_frame_done(function() F = F + 1 end)

local function dump() f:flush() f:close() print("pcwrites: wrote " .. OUT) end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
