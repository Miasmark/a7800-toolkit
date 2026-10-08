-- inputreaders.lua -- which routine reads the controller hardware, and how often.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/inputreaders.lua
--
-- Play, then close MAME. Taps the hardware registers themselves -- INPT0-5
-- (the fire buttons / paddles / lightgun lines) and SWCHA/SWCHB (the stick
-- and console switches) -- and records which PC reads each, and how many
-- times. Whichever routine is actually driving the player is at the top.
--
-- Why not watch the RAM the game copies the stick into? A game may mirror the
-- hardware into RAM in one routine that only runs on the title screen; the
-- mirror then barely changes in play and points you at the wrong code. The
-- registers cannot lie about who touched them.
--
-- A cartridge's $8000-$BFFF may be a switched window, so the same PC means a
-- different routine in each bank. A write tap over the window records the bank
-- number from the value written (SuperGame-style mappers); the log prints
-- `bN:PC` for reads from the window and plain `PC` for everything else. Set
-- A7800_IR_NOBANK=1 for a cartridge with no switching, where it only adds noise.
--
--   A7800_IR_LOG  output (default inputreaders.log)
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local OUT = os.getenv("A7800_IR_LOG") or "inputreaders.log"
local NOBANK = os.getenv("A7800_IR_NOBANK")

local bank = -1
-- held in a global: a dead tap does not announce itself (the GC trap)
TAPS = {}
if not NOBANK then
  TAPS[1] = mem:install_write_tap(0x8000, 0xBFFF, "banksel", function(off, data)
    bank = data
    return data
  end)
end

local seen = {}
local function watch(addr, name)
  TAPS[#TAPS + 1] = mem:install_read_tap(addr, addr, name, function(off, data)
    local pc = cpu.state["PC"].value
    local where = (not NOBANK and pc >= 0x8000 and pc < 0xC000)
                  and string.format("b%d:%04X", bank, pc) or string.format("%04X", pc)
    local k = name .. " <- " .. where
    seen[k] = (seen[k] or 0) + 1
    return data
  end)
end
for i = 0, 5 do watch(0x08 + i, "INPT" .. i) end
watch(0x0280, "SWCHA")
watch(0x0282, "SWCHB")

local function dump()
  local f = io.open(OUT, "w")
  local ks = {}
  for k in pairs(seen) do ks[#ks + 1] = k end
  table.sort(ks, function(a, b) return seen[a] > seen[b] end)
  for _, k in ipairs(ks) do f:write(string.format("%-28s x%d\n", k, seen[k])) end
  f:close()
  print("inputreaders: wrote " .. OUT)
end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
