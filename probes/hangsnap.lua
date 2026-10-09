-- hangsnap.lua -- where is a stuck machine? CPU state at chosen frames.
--
--   A7800_HANG_AT="300 600 900" A7800_HANG_PEEK="80,81,E5" \
--   mame a7800 -cart game.a78 -autoboot_script probes/hangsnap.lua \
--        -video none -sound none -nothrottle -seconds_to_run 30
--
-- For "the clock froze", "it stops responding after the second level": run it over
-- the recording at frames before and after the symptom and compare. Each line:
--     f<frame> pc=<PC> sp=<SP> stack=<ten bytes above SP> nmis=<since last line>
--     peek: $80=<v> $81=<v> ...
-- A frozen game sits at the same PC every time with the same stack; a game in an
-- endless loop of its own shows the same PC but a stack that is growing or not; one
-- that jumped into data is wildfetch.lua's job, not this one's.
--
--   A7800_HANG_AT    space-separated frame numbers (required)
--   A7800_HANG_PEEK  comma-separated hex addresses to read at each frame (optional)
--   A7800_HANG_OUT   output file (default hangsnap.log)
--
-- `nmis` counts the interrupt vector reads since the previous line (display-list
-- interrupts included): zero for a game whose interrupts have stopped. The PC is read
-- at the end of the frame, wherever the CPU happens to be, so one snapshot is a
-- sample, not a diagnosis; take several.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local OUT = os.getenv("A7800_HANG_OUT") or "hangsnap.log"

local AT, last = {}, 0
for w in (os.getenv("A7800_HANG_AT") or ""):gmatch("%d+") do
  AT[tonumber(w)] = true
  if tonumber(w) > last then last = tonumber(w) end
end
local PEEK = {}
for a in (os.getenv("A7800_HANG_PEEK") or ""):gmatch("[^,%s]+") do PEEK[#PEEK + 1] = tonumber(a, 16) end

local f = io.open(OUT, "w")
local nmi, F = 0, 0
-- held in globals: a dead tap does not announce itself (docs/pitfalls.md)
TAPS = { mem:install_read_tap(0xFFFA, 0xFFFA, "nmi", function(o, d) nmi = nmi + 1 return d end) }
FRAME_CB = emu.register_frame_done(function()
  F = F + 1
  if AT[F] then
    local sp = cpu.state["SP"].value & 0xFF           -- MAME reports $1xx
    local st = {}
    for i = 1, 10 do st[#st + 1] = string.format("%02X", mem:read_u8(0x100 + ((sp + i) & 0xFF))) end
    f:write(string.format("f%d pc=%04X sp=%02X stack=%s nmis=%d\n", F,
            cpu.state["PC"].value, sp, table.concat(st, " "), nmi))
    nmi = 0
    if #PEEK > 0 then
      local v = {}
      for _, a in ipairs(PEEK) do v[#v + 1] = string.format("$%04X=%02X", a, mem:read_u8(a)) end
      f:write("peek: " .. table.concat(v, " ") .. "\n")
    end
    f:flush()
  end
  if F >= last and last > 0 then f:close() MACHINE:exit() end
end)
