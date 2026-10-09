-- ramsnap.lua -- periodic snapshots of chosen RAM pages, to spot events as
-- step changes in one byte's value over time.
--
--   A7800_PAGES=0000,0100,1800,1900,2000,2100,2200,2300,2400,2500,2600,2700 \
--   mame a7800 -rompath ../bios -cart game.a78 -skip_gameinfo -video none \
--        -sound none -nothrottle -playback run-01.inp \
--        -autoboot_script probes/ramsnap.lua -str 600
--
-- Per-write logging of a whole run makes an unworkable log (millions of
-- entries, tens of megabytes, and a recording that never finishes). A snapshot
-- every N frames is small, and an event -- a life lost, a difficulty step, a
-- level change -- shows up as a step in the byte that holds it.
--
--   A7800_PAGES      comma-separated page bases in hex (default: zero page,
--                    stack page and the 6K of RAM at $1800-$2FFF's start)
--   A7800_EVERY      frames between snapshots (default 60)
--   A7800_MAXFRAMES  stop after this many frames (default: run to the end)
--   A7800_OUT        output file (default ramsnap-out.json)
--
-- Mirrors: $2040-$20FF and $2140-$21FF alias $0040-$00FF and $0140-$01FF on
-- this hardware (docs/pitfalls.md), so a game that writes through the low
-- form is invisible to a tap on the high one. Snapshot both when you do not
-- know which the game uses -- a snapshot reads values, so it sees either.
--
-- Output: {"frames":[60,120,...],"pages":{"1800":{"6160":[v,v,...],...}}}
-- Only bytes that ever changed are kept (constants are dropped to keep the
-- file small); each row is aligned to `frames`. Checkpointed every 100
-- snapshots, so a killed run still leaves output.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local mem = MACHINE.devices[":maincpu"].spaces["program"]

local PAGES = {}
for h in (os.getenv("A7800_PAGES") or "0000,0100,1800,1900"):gmatch("[^,%s]+") do
  PAGES[#PAGES + 1] = tonumber(h, 16)
end
local EVERY = tonumber(os.getenv("A7800_EVERY") or "") or 60
local CAP   = tonumber(os.getenv("A7800_MAXFRAMES") or "")
local OUT   = os.getenv("A7800_OUT") or "ramsnap-out.json"

local F, frames, series = 0, {}, {}
for _, p in ipairs(PAGES) do series[p] = {} end

local function snapshot()
  frames[#frames + 1] = F
  for _, p in ipairs(PAGES) do
    local s = series[p]
    for a = p, p + 255 do
      local rec = s[a]
      if not rec then rec = {} s[a] = rec end
      rec[#frames] = mem:read_u8(a)
    end
  end
end

local function dump()
  local parts = {}
  for _, p in ipairs(PAGES) do
    local rows = {}
    for a, rec in pairs(series[p]) do
      local first, changed = rec[1], false
      for i = 1, #frames do
        if rec[i] ~= first then changed = true break end
      end
      if changed then
        local vs = {}
        for i = 1, #frames do vs[i] = tostring(rec[i] or 0) end
        rows[#rows + 1] = string.format('"%d":[%s]', a, table.concat(vs, ","))
      end
    end
    parts[#parts + 1] = string.format('"%04X":{%s}', p, table.concat(rows, ","))
  end
  local h = io.open(OUT, "w")
  if h then
    h:write(string.format('{"frames":[%s],"pages":{%s}}',
      table.concat(frames, ","), table.concat(parts, ",")))
    h:close()
    print(string.format("wrote %s: frame=%d snapshots=%d", OUT, F, #frames))
  end
end

-- held in a global: a dead callback does not announce itself (the GC trap)
FRAME_CB = emu.register_frame_done(function()
  F = F + 1
  if F % EVERY == 0 then
    snapshot()
    if #frames % 100 == 0 then dump() end
  end
  if CAP and F >= CAP then dump() MACHINE:exit() end
end)
-- held in a global; register_stop is the older spelling (deprecated in new MAME)
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
