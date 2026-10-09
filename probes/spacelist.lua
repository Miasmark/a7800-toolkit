-- spacelist.lua -- print the CPU's address spaces and exit. Use it first on an
-- unfamiliar MAME build, to see which spaces its Lua exposes.

local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
for name, space in pairs(cpu.spaces) do
    print("space: " .. name)
end
MACHINE:exit()
