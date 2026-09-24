-- romcoverage.lua -- which cartridge bytes a run actually reads, by the CPU
-- or by MARIA's DMA, from the moment the game has the machine.
--
-- The BIOS reads the whole cartridge to check its signature, so counting
-- from power-on would call every byte used. Counting starts at the first
-- write to INPTCTRL ($0001) with the lock bit set: the cartridge's own code
-- does that just after the BIOS hands over (Pole Position II at $D207,
-- Karateka at $4061, Midnight Mutants at $FF02), and nothing reads for the
-- signature after it.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/romcoverage.lua \
--        -playback run.inp -nothrottle
--
-- Env:
--   A7800_COV_FROM, A7800_COV_TO  the range to map, hex (default 4000-FFFF)
--   A7800_COV_LOG                 output file (default romcoverage.txt)
--   A7800_COV_END                 frame to write it and stop (default 36000)
--
-- The output is one character per byte of the range, "1" read and "0" not,
-- so maps from several runs combine by OR. tools/modmap.py takes them. What
-- no run reached shows as unread, so the map is a lower bound on what the
-- game uses.

local M = (type(manager.machine) == "function") and manager:machine() or manager.machine
local mem = M.devices[":maincpu"].spaces["program"]

local FROM = tonumber(os.getenv("A7800_COV_FROM") or "4000", 16)
local TO = tonumber(os.getenv("A7800_COV_TO") or "FFFF", 16)
local END = tonumber(os.getenv("A7800_COV_END") or "36000")
local armed, hit, f = false, {}, 0

-- globals: a tap held only in a local is garbage-collected (docs/pitfalls.md)
ROMCOVERAGE_TAPS = {
  mem:install_write_tap(0x0001, 0x0001, "romcov-lock", function(a, d)
    if (d & 0x01) ~= 0 then armed = true end
    return d
  end),
  mem:install_read_tap(FROM, TO, "romcov", function(a, d)
    if armed then hit[a] = true end
    return d
  end),
}

emu.register_frame_done(function()
  f = f + 1
  if f >= END then
    local t = {}
    for a = FROM, TO do t[#t + 1] = hit[a] and "1" or "0" end
    local o = io.open(os.getenv("A7800_COV_LOG") or "romcoverage.txt", "wb")
    o:write(table.concat(t))
    o:close()
    if not armed then print("romcoverage: the cartridge never locked INPTCTRL; nothing counted") end
    M:exit()
  end
end)
