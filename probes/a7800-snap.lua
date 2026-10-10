-- A screenshot at a chosen frame, for the a7800 fork.
--
-- The fork's Lua predates `install_write_tap`, but it can still count frames and take
-- a snapshot, which is enough to see what a cartridge MARIA draws -- including bankset
-- cartridges, which mainline MAME will not run. `tools/forkshot.py` runs this for you;
-- by hand:
--
--   a7800 a7800 -cart game.a78 -video none -sound none -nothrottle -str 30 \
--         -snapshot_directory shots -autoboot_script probes/a7800-snap.lua
--
-- The picture lands in <snapshot_directory>/a7800/0000.png, MARIA's own 320 x 243.
--
-- A7800_SNAP_FRAME   the frame to photograph (default 600)
-- A7800_DRIVE        hold fire on a loop until then, for a game that waits at a title

local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine

local AT = tonumber(os.getenv("A7800_SNAP_FRAME") or "600")
local DRIVE = os.getenv("A7800_DRIVE")

local frame = 0
local fire = nil
if DRIVE then
  local ok, port = pcall(function()
    return MACHINE.ioport.ports[":buttons"].fields["P1 Button 1"]
  end)
  if ok then fire = port end
end

emu.register_frame_done(function()
  frame = frame + 1
  if fire then fire:set_value(((frame % 70) < 10) and 1 or 0) end
  if frame >= AT then
    local ok = pcall(function() MACHINE:video():snapshot() end)
    if not ok then ok = pcall(function() MACHINE.video:snapshot() end) end
    MACHINE:logerror(string.format("A78SNAP %d %s\n", frame, tostring(ok)))
    MACHINE:exit()
  end
end)
