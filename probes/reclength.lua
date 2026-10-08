-- reclength.lua -- a recording's TRUE length in frames, before any other analysis.
--
--   mame a7800 -rompath ../bios -cart game.a78 -skip_gameinfo -video none \
--        -sound none -nothrottle -playback run-01.inp \
--        -autoboot_script probes/reclength.lua
--
-- Never trust an early or short exit threshold as "the recording's length".
-- An analysis that stops at frame 6000 of a 20000-frame recording reports on
-- the first third and says nothing about it. Let MAME's own playback run out
-- against a generous cap instead, and read the length from here.
--
-- Prints "recording length: N frames" when the machine stops (playback ended,
-- or the cap was hit -- the line says which). The cap defaults to 600000
-- frames (two and three-quarter hours of NTSC); A7800_MAXFRAMES overrides it.
-- Every 50000 frames it prints progress so a long run does not look hung.
--
-- MAME's Lua keeps a callback alive only while something references it, so the
-- frame callback below is held in a global (docs/pitfalls.md, the GC trap).
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local CAP = tonumber(os.getenv("A7800_MAXFRAMES") or "") or 600000
local F, capped = 0, false

local function report()
  print(string.format("recording length: %d frames%s", F,
        capped and " (CAP HIT -- the recording is at least this long)" or ""))
end

FRAME_CB = emu.register_frame_done(function()
  F = F + 1
  if F % 50000 == 0 then print("progress frame " .. F) end
  if F >= CAP then capped = true MACHINE:exit() end
end)
if emu.register_stop then emu.register_stop(report)
elseif emu.add_machine_stop_notifier then emu.add_machine_stop_notifier(report) end
