-- exectrace.lua -- which code a run actually executes, with the bank it ran in,
-- and what static analysis cannot know: where indirect jumps went and what
-- bank-switch stores selected.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/exectrace.lua \
--        -video none -sound none -nothrottle -skip_gameinfo -seconds_to_run 60
--
-- A disassembler that follows code from the vectors stops at every `JMP (ptr)`
-- through RAM and at every bank switch whose number is not a constant. This
-- watches the real machine take them. tools/dyn.py turns the log into entry
-- points and bank-switch pins for the annotations file (python tools/init.py
-- --dynamic exectrace.log).
--
-- Output, one line per distinct fact (so the log stays small however long the
-- run), sorted by address when written:
--     X <space>:<addr>              an instruction was fetched here
--     J <space>:<from> <space>:<to> a JMP (indirect) at <from> went to <to>
--     S <space>:<from> <value> xN   a store into the bank-select window, and
--                                   what it wrote, N times
-- <space> is `f<bank>` for the fixed upper half and `b<N>` for window bank N,
-- the names every other tool here uses.
--
--   A7800_XT_LOG      output file (default exectrace.log)
--   A7800_XT_SELECT   bank-select window, hex "lo-hi" (default 8000-BFFF, the
--                     SuperGame register). Writes there are logged as bank
--                     switches; the value written, masked, is the bank.
--   A7800_XT_BANKS    number of banks (default 8; the mask is this minus one)
--   A7800_XT_WINDOW   hex "lo-hi" of the switched window (default 8000-BFFF)
--
-- Nothing is logged until the game locks INPTCTRL (a write to $01 with bit 0 set),
-- which is the BIOS handing the machine over, so the BIOS's own code is not
-- mistaken for the game's (the same trigger as romcoverage.lua).
-- LIMITS, so nobody trusts it further than it goes:
--   * Checked on MAME 0.264 only. It relies on a read tap seeing instruction
--     fetches from ROM, which docs/pitfalls.md records as true there and false
--     elsewhere. It counts a fetch only where the tap's address equals the
--     CPU's PC, so data reads in the same range are ignored; if the run
--     reports zero X lines, the tap is not seeing fetches -- do not read that
--     as "nothing ran".
--   * Bank tracking assumes a SuperGame-style register. Other mappers
--     (Activision, Absolute) need their own bank-select rule.
--   * Only what the run reaches. Play, or run a recording, to reach more.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]

local function range(env, default)
  local lo, hi = (os.getenv(env) or default):match("^(%x+)%-(%x+)$")
  return tonumber(lo, 16), tonumber(hi, 16)
end
local OUT = os.getenv("A7800_XT_LOG") or "exectrace.log"
local SEL_LO, SEL_HI = range("A7800_XT_SELECT", "8000-BFFF")
local WIN_LO, WIN_HI = range("A7800_XT_WINDOW", "8000-BFFF")
local NBANKS = tonumber(os.getenv("A7800_XT_BANKS") or "") or 8
local MASK = NBANKS - 1
local FIXED_BANK = NBANKS - 1          -- the fixed half is the last bank

local bank = 0
local armed = false

local seen_x, seen_j, seen_s = {}, {}, {}
local pending_j = nil                  -- "from" of a JMP (ind) awaiting its target

local function key(addr)
  if addr >= WIN_LO and addr <= WIN_HI then return string.format("b%d:%04X", bank, addr) end
  return string.format("f%d:%04X", FIXED_BANK, addr)
end

local function fetch(offset, data)
  if not armed then return data end
  local pc = cpu.state["PC"].value
  if pc ~= offset then return data end         -- a data read, not a fetch
  local k = key(pc)
  if pending_j then
    seen_j[pending_j .. " " .. k] = true
    pending_j = nil
  end
  seen_x[k] = true
  if data == 0x6C then pending_j = k end        -- JMP (ind): the next fetch is its target
  return data
end

-- held in globals: a dead tap does not announce itself (docs/pitfalls.md)
TAPS = {}
TAPS[1] = mem:install_write_tap(0x0001, 0x0001, "inptctrl", function(offset, data)
  if (data & 0x01) ~= 0 then armed = true end
  return data
end)
TAPS[2] = mem:install_read_tap(0x4000, 0xFFFF, "exectrace", fetch)
TAPS[3] = mem:install_write_tap(SEL_LO, SEL_HI, "banksel", function(offset, data)
  local v = data & MASK
  if armed then
    local k = string.format("%s %d", key(cpu.state["PC"].value), v)
    seen_s[k] = (seen_s[k] or 0) + 1
  end
  bank = v
  return data
end)

local function dump()
  local lines = {}
  for k in pairs(seen_x) do lines[#lines + 1] = "X " .. k end
  for k in pairs(seen_j) do lines[#lines + 1] = "J " .. k end
  for k, n in pairs(seen_s) do lines[#lines + 1] = string.format("S %s x%d", k, n) end
  table.sort(lines)
  local f = io.open(OUT, "w")
  if not f then print("exectrace: cannot write " .. OUT) return end
  for _, l in ipairs(lines) do f:write(l, "\n") end
  f:close()
  local nx = 0
  for _ in pairs(seen_x) do nx = nx + 1 end
  print(string.format("exectrace: %d distinct instructions, wrote %s", nx, OUT))
  if nx == 0 then
    print("exectrace: ZERO fetches seen. The tap is not seeing instruction fetches on "
          .. "this MAME, or the reset target was not found; the log means nothing.")
  end
end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
