-- addrorigin.lua -- which bytes of the cartridge ARE addresses, found by following
-- every value the CPU uses as one back to the byte it was loaded from.
--
--   mame a7800 -cart game.a78 -autoboot_script probes/addrorigin.lua \
--        -video none -sound none -nothrottle -skip_gameinfo -seconds_to_run 60
--   python tools/origins.py addrorigin.log game.a78 -c annotations.json
--
-- Static analysis cannot tell a pointer table from any other table, and a
-- disassembler that cannot tell leaves the table as bytes and every routine it
-- points at unreached. The running machine can: when a (zp),Y pointer is used,
-- the two bytes of the pointer came from somewhere, and where they came from is
-- the table. Following the values through register transfers, stores to RAM and
-- pushes gives, for each use of an address, the origin of its high and low halves:
--   ptr      (zp),Y or (zp,X): the zero-page pair's two bytes
--   jmpind   JMP (vector): the vector's two bytes
--   rts      RTS to an address pushed with PHA rather than by JSR
--   selfmod  an absolute operand that was written at run time
-- Effective addresses are computed from the opcode, its operand bytes and X/Y/SP at
-- the fetch, never taken from observed reads, because MARIA's DMA goes through the
-- same address space and shows up in a read tap in the middle of an instruction.
-- Arithmetic leaves the accumulator's origin as it was, so a pointer plus an
-- offset still traces to the pointer's byte; a value built from two sources is
-- credited to the last one loaded. That is a heuristic, and it is stated here
-- so a computed pointer is read as "comes from this byte, adjusted".
--
-- Output, to A7800_AO_LOG (default addrorigin.log), one fact per line:
--     P <kind> <pcspace>:<pc> <hi> <lo> xN <tmin> <tmax>
--         an address was used N times; its halves came from `hi` and `lo`
--         (<space>:<addr>, spaces named as in exectrace.lua; an origin in RAM that
--         nothing tracked wrote is <space>:<addr> of the RAM byte), and the
--         targets ranged from tmin to tmax
--     I <space>:<addr>   that origin is the operand of an LDA/LDX/LDY #imm
-- tools/origins.py groups them into pointer tables, immediates and computed pointers.
--
--   A7800_AO_LOG      output file (default addrorigin.log)
--   A7800_AO_SELECT   bank-select window, hex "lo-hi" (default 8000-BFFF)
--   A7800_AO_WINDOW   switched window, hex "lo-hi" (default 8000-BFFF)
--   A7800_AO_BANKS    number of banks (default 8)
--   A7800_AO_END      stop and write after this frame (default: at the end of the run)
--
-- LIMITS. Values are followed from the first instruction, so a pointer the game
-- builds in its reset code is traced, but a use is only recorded once the game
-- locks INPTCTRL (as exectrace.lua), so the BIOS's own pointers are left out; a value that passes through a flag, a shift or a computed index loses its
-- origin; only what the run reaches is seen; checked on MAME 0.264 only, and it
-- needs the read tap to see instruction fetches (docs/pitfalls.md). It is slow
-- (every instruction is decoded in Lua): expect a few seconds of run per second.
local MACHINE = (type(manager.machine) == "function")
                and manager:machine() or manager.machine
local cpu = MACHINE.devices[":maincpu"]
local mem = cpu.spaces["program"]
local PCs, Xs, Ys, SPs = cpu.state["PC"], cpu.state["X"], cpu.state["Y"], cpu.state["SP"]

local OPT = {[0]={"BRK","imp"},[1]={"ORA","izx"},[2]={"JAM","imp"},[3]={"SLO","izx"},[4]={"NOP","zp"},[5]={"ORA","zp"},[6]={"ASL","zp"},[7]={"SLO","zp"},[8]={"PHP","imp"},[9]={"ORA","imm"},[10]={"ASL","acc"},[11]={"ANC","imm"},[12]={"NOP","abs"},[13]={"ORA","abs"},[14]={"ASL","abs"},[15]={"SLO","abs"},[16]={"BPL","rel"},[17]={"ORA","izy"},[18]={"JAM","imp"},[19]={"SLO","izy"},[20]={"NOP","zpx"},[21]={"ORA","zpx"},[22]={"ASL","zpx"},[23]={"SLO","zpx"},[24]={"CLC","imp"},[25]={"ORA","aby"},[26]={"NOP","imp"},[27]={"SLO","aby"},[28]={"NOP","abx"},[29]={"ORA","abx"},[30]={"ASL","abx"},[31]={"SLO","abx"},[32]={"JSR","abs"},[33]={"AND","izx"},[34]={"JAM","imp"},[35]={"RLA","izx"},[36]={"BIT","zp"},[37]={"AND","zp"},[38]={"ROL","zp"},[39]={"RLA","zp"},[40]={"PLP","imp"},[41]={"AND","imm"},[42]={"ROL","acc"},[43]={"ANC","imm"},[44]={"BIT","abs"},[45]={"AND","abs"},[46]={"ROL","abs"},[47]={"RLA","abs"},[48]={"BMI","rel"},[49]={"AND","izy"},[50]={"JAM","imp"},[51]={"RLA","izy"},[52]={"NOP","zpx"},[53]={"AND","zpx"},[54]={"ROL","zpx"},[55]={"RLA","zpx"},[56]={"SEC","imp"},[57]={"AND","aby"},[58]={"NOP","imp"},[59]={"RLA","aby"},[60]={"NOP","abx"},[61]={"AND","abx"},[62]={"ROL","abx"},[63]={"RLA","abx"},[64]={"RTI","imp"},[65]={"EOR","izx"},[66]={"JAM","imp"},[67]={"SRE","izx"},[68]={"NOP","zp"},[69]={"EOR","zp"},[70]={"LSR","zp"},[71]={"SRE","zp"},[72]={"PHA","imp"},[73]={"EOR","imm"},[74]={"LSR","acc"},[75]={"ALR","imm"},[76]={"JMP","abs"},[77]={"EOR","abs"},[78]={"LSR","abs"},[79]={"SRE","abs"},[80]={"BVC","rel"},[81]={"EOR","izy"},[82]={"JAM","imp"},[83]={"SRE","izy"},[84]={"NOP","zpx"},[85]={"EOR","zpx"},[86]={"LSR","zpx"},[87]={"SRE","zpx"},[88]={"CLI","imp"},[89]={"EOR","aby"},[90]={"NOP","imp"},[91]={"SRE","aby"},[92]={"NOP","abx"},[93]={"EOR","abx"},[94]={"LSR","abx"},[95]={"SRE","abx"},[96]={"RTS","imp"},[97]={"ADC","izx"},[98]={"JAM","imp"},[99]={"RRA","izx"},[100]={"NOP","zp"},[101]={"ADC","zp"},[102]={"ROR","zp"},[103]={"RRA","zp"},[104]={"PLA","imp"},[105]={"ADC","imm"},[106]={"ROR","acc"},[107]={"ARR","imm"},[108]={"JMP","ind"},[109]={"ADC","abs"},[110]={"ROR","abs"},[111]={"RRA","abs"},[112]={"BVS","rel"},[113]={"ADC","izy"},[114]={"JAM","imp"},[115]={"RRA","izy"},[116]={"NOP","zpx"},[117]={"ADC","zpx"},[118]={"ROR","zpx"},[119]={"RRA","zpx"},[120]={"SEI","imp"},[121]={"ADC","aby"},[122]={"NOP","imp"},[123]={"RRA","aby"},[124]={"NOP","abx"},[125]={"ADC","abx"},[126]={"ROR","abx"},[127]={"RRA","abx"},[128]={"NOP","imm"},[129]={"STA","izx"},[130]={"NOP","imm"},[131]={"SAX","izx"},[132]={"STY","zp"},[133]={"STA","zp"},[134]={"STX","zp"},[135]={"SAX","zp"},[136]={"DEY","imp"},[137]={"NOP","imm"},[138]={"TXA","imp"},[139]={"ANE","imm"},[140]={"STY","abs"},[141]={"STA","abs"},[142]={"STX","abs"},[143]={"SAX","abs"},[144]={"BCC","rel"},[145]={"STA","izy"},[146]={"JAM","imp"},[147]={"SHA","izy"},[148]={"STY","zpx"},[149]={"STA","zpx"},[150]={"STX","zpy"},[151]={"SAX","zpy"},[152]={"TYA","imp"},[153]={"STA","aby"},[154]={"TXS","imp"},[155]={"TAS","aby"},[156]={"SHY","abx"},[157]={"STA","abx"},[158]={"SHX","aby"},[159]={"SHA","aby"},[160]={"LDY","imm"},[161]={"LDA","izx"},[162]={"LDX","imm"},[163]={"LAX","izx"},[164]={"LDY","zp"},[165]={"LDA","zp"},[166]={"LDX","zp"},[167]={"LAX","zp"},[168]={"TAY","imp"},[169]={"LDA","imm"},[170]={"TAX","imp"},[171]={"LXA","imm"},[172]={"LDY","abs"},[173]={"LDA","abs"},[174]={"LDX","abs"},[175]={"LAX","abs"},[176]={"BCS","rel"},[177]={"LDA","izy"},[178]={"JAM","imp"},[179]={"LAX","izy"},[180]={"LDY","zpx"},[181]={"LDA","zpx"},[182]={"LDX","zpy"},[183]={"LAX","zpy"},[184]={"CLV","imp"},[185]={"LDA","aby"},[186]={"TSX","imp"},[187]={"LAS","aby"},[188]={"LDY","abx"},[189]={"LDA","abx"},[190]={"LDX","aby"},[191]={"LAX","aby"},[192]={"CPY","imm"},[193]={"CMP","izx"},[194]={"NOP","imm"},[195]={"DCP","izx"},[196]={"CPY","zp"},[197]={"CMP","zp"},[198]={"DEC","zp"},[199]={"DCP","zp"},[200]={"INY","imp"},[201]={"CMP","imm"},[202]={"DEX","imp"},[203]={"SBX","imm"},[204]={"CPY","abs"},[205]={"CMP","abs"},[206]={"DEC","abs"},[207]={"DCP","abs"},[208]={"BNE","rel"},[209]={"CMP","izy"},[210]={"JAM","imp"},[211]={"DCP","izy"},[212]={"NOP","zpx"},[213]={"CMP","zpx"},[214]={"DEC","zpx"},[215]={"DCP","zpx"},[216]={"CLD","imp"},[217]={"CMP","aby"},[218]={"NOP","imp"},[219]={"DCP","aby"},[220]={"NOP","abx"},[221]={"CMP","abx"},[222]={"DEC","abx"},[223]={"DCP","abx"},[224]={"CPX","imm"},[225]={"SBC","izx"},[226]={"NOP","imm"},[227]={"ISC","izx"},[228]={"CPX","zp"},[229]={"SBC","zp"},[230]={"INC","zp"},[231]={"ISC","zp"},[232]={"INX","imp"},[233]={"SBC","imm"},[234]={"NOP","imp"},[235]={"SBC","imm"},[236]={"CPX","abs"},[237]={"SBC","abs"},[238]={"INC","abs"},[239]={"ISC","abs"},[240]={"BEQ","rel"},[241]={"SBC","izy"},[242]={"JAM","imp"},[243]={"ISC","izy"},[244]={"NOP","zpx"},[245]={"SBC","zpx"},[246]={"INC","zpx"},[247]={"ISC","zpx"},[248]={"SED","imp"},[249]={"SBC","aby"},[250]={"NOP","imp"},[251]={"ISC","aby"},[252]={"NOP","abx"},[253]={"SBC","abx"},[254]={"INC","abx"},[255]={"ISC","abx"}}

local function range(env, default)
  local lo, hi = (os.getenv(env) or default):match("^(%x+)%-(%x+)$")
  return tonumber(lo, 16), tonumber(hi, 16)
end
local OUT = os.getenv("A7800_AO_LOG") or "addrorigin.log"
local SEL_LO, SEL_HI = range("A7800_AO_SELECT", "8000-BFFF")
local WIN_LO, WIN_HI = range("A7800_AO_WINDOW", "8000-BFFF")
local NBANKS = tonumber(os.getenv("A7800_AO_BANKS") or "") or 8
local MASK = NBANKS - 1
local FIXED = NBANKS - 1
local END = tonumber(os.getenv("A7800_AO_END") or "")

local bank, armed, frame = 0, false, 0
local JSRM, INTM = "jsr", "int"      -- a return address pushed by JSR / an interrupt
local memsrc = {}                    -- RAM address -> origin of the byte last stored there
local srcA, srcX, srcY = nil, nil, nil
local cur = nil                      -- the instruction being executed
local uses, imms = {}, {}
local INNER = false

local function key(a)
  if a >= WIN_LO and a <= WIN_HI then return string.format("b%d:%04X", bank, a) end
  if a >= 0x4000 then return string.format("f%d:%04X", FIXED, a) end
  return string.format("r:%04X", a)
end
local function rd(a) INNER = true; local v = mem:read_u8(a & 0xFFFF); INNER = false; return v end
local function orig(a) local m = memsrc[a]; if m ~= nil then return m end return key(a) end

local function use(kind, pc, hi, lo, target)
  if not armed or hi == nil or lo == nil or target == nil then return end
  if hi == JSRM or lo == JSRM or hi == INTM or lo == INTM then return end
  local k = string.format("%s %s %s %s", kind, key(pc), hi, lo)
  local u = uses[k]
  if not u then u = {n = 0, lo = target, hi = target}; uses[k] = u end
  u.n = u.n + 1
  if target < u.lo then u.lo = target end
  if target > u.hi then u.hi = target end
end

local function start(pc, op)
  local mn, mode = OPT[op][1], OPT[op][2]
  local c = {op = op, mn = mn, mode = mode, pc = pc, sp = SPs.value & 0xFF}
  local x, y = Xs.value & 0xFF, Ys.value & 0xFF
  if mode == "imm" then c.ea = pc + 1; c.immkey = key(pc + 1)
  elseif mode == "zp" then c.ea = rd(pc + 1)
  elseif mode == "zpx" then c.ea = (rd(pc + 1) + x) & 0xFF
  elseif mode == "zpy" then c.ea = (rd(pc + 1) + y) & 0xFF
  elseif mode == "abs" then c.ea = rd(pc + 1) + 256 * rd(pc + 2)
  elseif mode == "abx" then c.ea = (rd(pc + 1) + 256 * rd(pc + 2) + x) & 0xFFFF
  elseif mode == "aby" then c.ea = (rd(pc + 1) + 256 * rd(pc + 2) + y) & 0xFFFF
  elseif mode == "izy" then
    local z = rd(pc + 1); c.zp = z
    c.ea = (rd(z) + 256 * rd((z + 1) & 0xFF) + y) & 0xFFFF
  elseif mode == "izx" then
    local z = (rd(pc + 1) + x) & 0xFF; c.zp = z
    c.ea = rd(z) + 256 * rd((z + 1) & 0xFF)
  elseif mode == "ind" then c.vec = rd(pc + 1) + 256 * rd(pc + 2)
  end
  return c
end

local function loaded(c)             -- the origin of the byte a load just took
  if c.mode == "imm" then
    local k = c.immkey
    imms[k] = true
    return k
  end
  return orig(c.ea)
end

local function finalize(c, nextpc)
  local mn, mode = c.mn, c.mode
  if mn == "LDA" or mn == "LAX" then srcA = loaded(c); if mn == "LAX" then srcX = srcA end
  elseif mn == "LDX" then srcX = loaded(c)
  elseif mn == "LDY" then srcY = loaded(c)
  elseif mn == "PLA" then srcA = orig(0x100 + ((c.sp + 1) & 0xFF))
  elseif mn == "TAX" then srcX = srcA elseif mn == "TAY" then srcY = srcA
  elseif mn == "TXA" then srcA = srcX elseif mn == "TYA" then srcA = srcY end
  if mode == "izy" or mode == "izx" then
    use("ptr", c.pc, orig((c.zp + 1) & 0xFF), orig(c.zp), c.ea)
  elseif mode == "ind" then
    use("jmpind", c.pc, orig(c.vec + 1), orig(c.vec), nextpc)
  elseif mn == "RTS" then
    use("rts", c.pc, orig(0x100 + ((c.sp + 2) & 0xFF)), orig(0x100 + ((c.sp + 1) & 0xFF)), nextpc)
  elseif mode == "abs" or mode == "abx" or mode == "aby" then
    if memsrc[c.pc + 1] ~= nil or memsrc[c.pc + 2] ~= nil then
      local target = (mn == "JMP" or mn == "JSR") and nextpc or c.ea
      use("selfmod", c.pc, orig(c.pc + 2), orig(c.pc + 1), target)
    end
  end
end

-- held in globals: a dead tap does not announce itself (docs/pitfalls.md)
TAPS = {}
TAPS[1] = mem:install_write_tap(0x0001, 0x0001, "inptctrl", function(offset, data)
  if (data & 0x01) ~= 0 then armed = true end
  return data
end)
TAPS[2] = mem:install_read_tap(0x0000, 0xFFFF, "ao-fetch", function(offset, data)
  if INNER or offset ~= PCs.value then return data end
  if cur then finalize(cur, offset) end
  cur = start(offset, data)
  return data
end)
TAPS[3] = mem:install_write_tap(0x0000, 0xFFFF, "ao-write", function(offset, data)
  if not cur then return data end
  if offset >= SEL_LO and offset <= SEL_HI then bank = data & MASK end
  local mn = cur.mn
  if offset >= 0x100 and offset < 0x200 and offset ~= cur.ea then
    if mn == "PHA" then memsrc[offset] = srcA
    elseif mn == "JSR" then memsrc[offset] = JSRM
    elseif mn == "PHP" then memsrc[offset] = nil
    else memsrc[offset] = INTM end              -- an interrupt's pushes
  elseif mn == "STA" or mn == "PHA" then memsrc[offset] = srcA
  elseif mn == "STX" then memsrc[offset] = srcX
  elseif mn == "STY" then memsrc[offset] = srcY
  end
  return data
end)

local function dump()
  local lines = {}
  local seen = {}
  for k, u in pairs(uses) do
    lines[#lines + 1] = string.format("P %s x%d %04X %04X", k, u.n, u.lo, u.hi)
    for origin in k:gmatch("[bfr]%d*:%x%x%x%x") do seen[origin] = true end
  end
  for k in pairs(imms) do
    if seen[k] then lines[#lines + 1] = "I " .. k end
  end
  table.sort(lines)
  local f = io.open(OUT, "w")
  if not f then print("addrorigin: cannot write " .. OUT) return end
  for _, l in ipairs(lines) do f:write(l, "\n") end
  f:close()
  print(string.format("addrorigin: %d distinct uses of an address, wrote %s", #lines, OUT))
end
if emu.add_machine_stop_notifier then STOP_CB = emu.add_machine_stop_notifier(dump)
else emu.register_stop(dump) end
if END then
  emu.register_frame_done(function()
    frame = frame + 1
    if frame >= END then MACHINE:exit() end
  end)
end
