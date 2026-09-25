-- rendersurvey.lua -- how a game spends MARIA and the CPU, frame by frame,
-- measured the same way for any cartridge. Built for Pole Position II and
-- used on Motor Psycho and Fatal Run, so three racing games' display
-- budgets could be compared by one instrument.
--
-- Per frame it counts:
--   DLIs    reads of the NMI vector at $FFFA (the CPU fetches it on every NMI)
--   WSYNC   writes to $24 (a beam-racing kernel shows up as hundreds a frame)
--   DPPH/L  and CTRL as last written (MARIA's registers are write-only, so
--           this is the only way to know where the display list list is)
-- and at chosen frames saves RAM $1800-$27FF with those three values beside
-- it, plus a screenshot, so the display lists can be walked and costed
-- offline: tools/zonebill.py.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/rendersurvey.lua \
--        [-playback run.inp] -snapshot_directory shots -nothrottle
--
-- Env:
--   A7800_RS_OUT     file prefix (default survey): <prefix>-frames.csv,
--                    <prefix>-ram-NNNNN.bin and .txt
--   A7800_RS_DUMP    frames to dump RAM at, comma-separated
--   A7800_RS_SHOTS   also a screenshot every N frames (default 0, none)
--   A7800_RS_FRAMES  stop after this many frames (default 3600)

local M = (type(manager.machine) == "function") and manager:machine() or manager.machine
local mem = M.devices[":maincpu"].spaces["program"]
local out = os.getenv("A7800_RS_OUT") or "survey"
local every = tonumber(os.getenv("A7800_RS_SHOTS") or "0")
local stop = tonumber(os.getenv("A7800_RS_FRAMES") or "3600")
local dumps = {}
for n in string.gmatch(os.getenv("A7800_RS_DUMP") or "", "%d+") do dumps[tonumber(n)] = true end

local f, dli, wsync = 0, 0, 0
local dpph, dppl, ctrl = 0, 0, 0
-- globals: a tap held only in a local is garbage-collected (docs/pitfalls.md)
RENDERSURVEY_TAPS = {
  mem:install_read_tap(0xFFFA, 0xFFFA, "rs-nmi", function(o, d) dli = dli + 1; return d end),
  mem:install_write_tap(0x24, 0x24, "rs-wsync", function(o, d) wsync = wsync + 1; return d end),
  mem:install_write_tap(0x2C, 0x2C, "rs-dpph", function(o, d) dpph = d; return d end),
  mem:install_write_tap(0x30, 0x30, "rs-dppl", function(o, d) dppl = d; return d end),
  mem:install_write_tap(0x3C, 0x3C, "rs-ctrl", function(o, d) ctrl = d; return d end),
}

local log = io.open(out .. "-frames.csv", "w")
log:write("frame,dli,wsync,dpph,dppl,ctrl\n")

emu.register_frame_done(function()
  f = f + 1
  log:write(string.format("%d,%d,%d,%d,%d,%d\n", f, dli, wsync, dpph, dppl, ctrl))
  dli, wsync = 0, 0
  if every > 0 and f % every == 0 then M.video:snapshot() end
  if dumps[f] then
    local b = io.open(string.format("%s-ram-%05d.bin", out, f), "wb")
    local t = {}
    for a = 0x1800, 0x27FF do t[#t + 1] = string.char(mem:read_u8(a)) end
    b:write(table.concat(t)); b:close()
    local m = io.open(string.format("%s-ram-%05d.txt", out, f), "w")
    m:write(string.format("dpph=%02X dppl=%02X ctrl=%02X\n", dpph, dppl, ctrl)); m:close()
    M.video:snapshot()
  end
  if f >= stop then log:close(); M:exit() end
end)
