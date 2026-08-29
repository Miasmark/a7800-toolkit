### Where the fault actually is

Diffed instruction by instruction against MAME's debugger trace (with
`noloop` -- see pitfalls.md), from the moment the BIOS hands the cartridge
control. **The first 427,399 instructions are identical**, about 124 frames.
The 6502 core, the mapper, the memory map and the RAM model are all correct
over that span.

The first divergence is a vertical-blank wait, and it is **not** the fault:
it re-synchronises 1,160 instructions later when MAME finishes the same
spin, and sweeping this simulator's VBLANK phase across a whole frame
changes the score by nothing at all (98 matched rows at every offset tried).
Phase is not it.

What follows is 1,086 further re-synchronisations, almost all of the same
shape: MAME executes about 36 instructions that this does not, over and
over. That is an interrupt handler running on hardware and not here.

The chain, as far as it is now understood:

1. Ballblazer spends 94% of its time in a wait at `$FCBC` -- `STA $66 /
   LDA $66 / BNE` -- for a counter its interrupt handler clears.
2. Interrupts do work. Early on the simulation raises 16 a frame and the
   game's other counter, at `$40`, counts down exactly once per frame as
   designed. An earlier reading of this as "the handler never runs" was
   simply wrong.
3. But by frame 400 the display list this simulator is walking contains
   **no zones with the interrupt bit set at all**, where MAME's has 19. No
   interrupts can then be raised, and every wait becomes permanent.

So the question is why the DLL goes flat here and not on hardware. The
untested hypothesis is double buffering: if the game keeps two display
lists and points MARIA at the next one part-way through a frame, then
reading `DPPH`/`DPPL` once at frame start -- which is what `run()` does --
can consistently read the buffer being written rather than the one being
shown. MARIA latches that pointer at a particular point in the frame, and
this does not model when.

#!/usr/bin/env python3
"""
Run a cartridge's own code, and listen to what it writes to the sound chip.

    python tools/sim.py game.a78 --seconds 20 -o game.log

`capture.py` gets the same log by driving an emulator. This gets it by
executing the 6502 directly: RAM, the cartridge mapper, enough of MARIA to keep
a game's timing loop happy, and a trap on every write to TIA or POKEY.

The reason to have it is not to replace the emulator, which is better at being
an emulator. It is that **a player written for a format nobody has described is
still the authority on that format.** Reimplementing RMT's replayer means
reading 803 instructions and hoping; running it means the answer is correct by
construction. The same goes for every other player in the library.

## State: still unfinished, but the gap is no longer MARIA

The 6502 core works. It boots cartridges, runs their startup code, switches
banks, takes interrupts and reaches their main loops -- checked against real
disassemblies instruction by instruction. MARIA's display interrupts are now
implemented: the DLL the game builds in RAM is walked every frame and an NMI
raised at the end of each zone whose entry has bit 7 set. That walk is verified
against MAME, which reports the same display list byte for byte.

**It is still not trustworthy, and `--compare` is how you find that out.** Point
it at a capture from `capture.py` and it reports how much of a known-good log
the simulation reproduces:

    Ballblazer          98 of 1544 reference rows   (6.3%)
    Midnight Mutants     5 of  818 reference rows   (0.6%)

Ballblazer is the encouraging one: of the 98 rows it produces, 98 are correct,
at a constant frame offset. It plays the right notes at the right times and
then stops. Both games now run indefinitely without crashing and simply cease
to advance -- Ballblazer spinning at $FCBC, Midnight Mutants at $809E. Those
are the leads.

### What the earlier version of this file got wrong

It said the blocker was that "Midnight Mutants runs, but its music routine is
never called: its timing comes from DLIs, not from the frame." Measured against
hardware, **Midnight Mutants raises no display interrupts at all** -- twenty
zones, not one with bit 7 set. Whatever stops its music, that was not it. The
lesson is the ordinary one: the explanation was plausible, was never measured,
and survived in a docstring long enough to look established.

### What has been ruled out

* **The RIOT timer.** Instrumenting every hardware read shows neither game ever
  touches `INTIM`, so a missing countdown timer is not the cause.
* **RAM mirroring.** The 7800 mirrors `$2040-$20FF` into the zero page and
  `$2140-$21FF` into the stack, and this simulator did not. It does now (see
  `fold`), which is a real fix and made no difference to either game.
* **A crash on a null interrupt vector.** DLIs were being raised from frame
  zero, before a game had installed the RAM vector its handler dispatches
  through, so the jump went to `$0000`. Ballblazer spent 31.5% of its run
  executing address zero. Gating interrupts on DMA actually being enabled --
  which is what the hardware does -- fixed it: 0.0% now. It did not improve
  the audio, so the stall is a separate fault.

### Where the fault actually is

Diffed instruction by instruction against MAME's debugger trace, from the
moment the BIOS hands the cartridge control. **The first 427,399 instructions
are identical** -- about 124 frames. The 6502 core, the mapper, the memory
map and the RAM model are all correct over that span; whatever is wrong is
narrow.

The first divergence is a vertical-blank wait:

    $FC47  BIT $28        ; MSTAT
    $FC49  BPL $FC47      ; spin until bit 7 says VBLANK

MAME stays in the loop. This simulator's VBLANK flag is already set, so it
falls straight through. The game runs its initialisation for those 124 frames
without ever waiting on the video, and the very first time it does, the two
disagree.

The likely reason is phase, not rate. MAME's video clock has been running
since power-on and the cartridge is handed control part-way through a frame;
this simulator starts its own frame zero at the reset vector, so its VBLANK
edge sits at an arbitrary offset from the hardware's. That is unfixable
without emulating from power-on -- and it should also be harmless, since a
constant offset is exactly what `compare()` aligns away. So it is the first
divergence without necessarily being the fault, and the next step is to find
out which, by re-running the diff with a re-sync window wide enough to cover
a whole frame of spinning (8,000 instructions, not the 400 used here -- that
window was too small and made a transient difference look permanent).

### What the earlier version of this file got wrong

It said the blocker was that "Midnight Mutants runs, but its music routine is
never called: its timing comes from DLIs, not from the frame." Measured
against hardware, **Midnight Mutants raises no display interrupts at all** --
twenty zones, not one with bit 7 set. Whatever stops its music, that was not
it.

### What has been ruled out

* **The BIOS.** The suspicion was that entering at the reset vector with RAM
  zeroed, rather than after the console's own startup, left a game reading
  state it never initialised. Disproven: execution matches MAME exactly for
  427,399 instructions after handover. Had inherited state mattered, they
  would have parted company immediately.
* **VBLANK phase.** Sweeping the flag's position across a whole frame moves
  the score by nothing, and the one divergence it causes re-synchronises.
* **The mapper and the memory map.** The two agree byte for byte on the
  vectors and on the code at the reset address, and now for most of half a
  million instructions of execution.
* **The RIOT timer.** Neither game ever reads `INTIM`.
* **POKEY reads.** The `RANDOM` register returns zero here where hardware
  returns live state, which is a real gap -- but Ballblazer never reads it,
  so it is not this.
* **RAM mirroring**, and **a crash on a null interrupt vector**: both were
  real faults, both are fixed, neither changed the score.

Until `--compare` reports a high number, `capture.py` is the way to hear a
cartridge and this remains groundwork. See `docs/emulation.md`.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cart as cart_module
import m6502

# Flags
C, Z, I, D, B, U, V, N = 1, 2, 4, 8, 16, 32, 64, 128

# `m6502.BRANCHES` is a set of names; the core needs to know which flag each
# one tests and which way round.
BRANCH_ON = {"BCC": (C, False), "BCS": (C, True),
             "BNE": (Z, False), "BEQ": (Z, True),
             "BPL": (N, False), "BMI": (N, True),
             "BVC": (V, False), "BVS": (V, True)}


def fold(a):
    """The 7800 mirrors RAM into the first two pages -- see docs/pitfalls.md.

    The 6502 needs a zero page and a stack, so $2040-$20FF appears at
    $0040-$00FF and $2140-$21FF at $0140-$01FF. They are the SAME bytes, and
    games use both views freely: state written through the high view and read
    back through the low one, or the other way round.

    A simulator with a flat 64K array silently gives you two separate stores
    instead. Nothing crashes; the game simply reads back zero where it wrote a
    value, and the symptom is a player that starts correctly and then stops
    advancing -- which is exactly how this was found.
    """
    if 0x0040 <= a <= 0x00FF or 0x0140 <= a <= 0x01FF:
        return a + 0x2000
    return a


class Bus(object):
    """The 7800's memory map, as much of it as sound needs.

    RAM, the cartridge through its mapper, and traps on the audio registers.
    MARIA is present only as far as a timing loop can tell: `MSTAT` bit 7 says
    whether the machine is in vertical blank, which is what a game waits on.
    """

    def __init__(self, cart, drive=False):
        self.cart = cart
        self.ram = bytearray(0x10000)
        self.bank = cart.nbanks - 1 if cart.nbanks > 1 else 0
        self.audio = {}                 # address -> last value written
        self.writes = []                # (frame, address, value)
        self.frame = 0
        self.vblank = False
        self.dpph = None                # DPPH/DPPL are WRITE-ONLY on real
        self.dppl = None                # hardware; the sim keeps its own copy
        self.ctrl = 0x00                # MARIA CTRL: DMA is OFF until a game
                                        # turns it on, and so are its interrupts
        self.drive = drive
        self.pokeys = set()
        for base in cart.pokeys():
            for r in range(16):
                self.pokeys.add(base + r)

    # -- reads
    def read(self, a):
        a &= 0xFFFF
        if a >= 0x4000:
            sp = self.cart.space_of(a, self.bank)
            if sp is not None:
                try:
                    return self.cart.byte(sp, a)
                except Exception:                            # noqa: BLE001
                    return 0xFF
            return self.ram[a]
        if (a & 0xFF) == 0x28 and 0x20 <= (a & 0xFF) <= 0x3F:
            # MSTAT: bit 7 set during vertical blank
            return 0x80 if self.vblank else 0x00
        if a in (0x0008, 0x0009, 0x000A, 0x000B):
            return 0x00                  # INPT0-3: no paddles
        if a in (0x000C, 0x000D):
            # INPT4/5: fire buttons, active low
            return 0x00 if self.drive else 0x80
        if a == 0x0280:
            # SWCHA: joystick directions, also active low. All ones is centred.
            return 0xFF
        if a == 0x0282:
            # SWCHB: the console switches, and they are **active low** -- a set
            # bit means "not pressed". Returning zeros here reads as reset and
            # select both held down, and a game that waits for them to be
            # released waits forever. Midnight Mutants spins on exactly that.
            #   bit 0 reset, bit 1 select, bit 3 colour/BW
            return 0x0B
        return self.ram[fold(a)]

    # -- writes
    def write(self, a, v):
        a &= 0xFFFF
        v &= 0xFF
        if a >= 0x8000 and self.cart.nbanks > 1:
            b = self.cart.map.bank_from_write(a, v)
            if b is not None:
                self.bank = b
                return
        if a >= 0x4000:
            if a in self.pokeys:
                self.audio[a] = v
                self.writes.append((self.frame, a, v))
                return
            return                       # ROM: writes go nowhere
        low = a & 0xFF
        if a < 0x0400 and (a & 0x300) in (0, 0x100, 0x200):
            # MARIA's display-list pointer. Write-only on hardware, so nothing
            # can read it back -- the sim has to catch it on the way past or
            # it never learns where the display list is.
            if low == 0x2C:
                self.dpph = v
            elif low == 0x30:
                self.dppl = v
            elif low == 0x3C:
                self.ctrl = v
        if a < 0x0400 and 0x15 <= low <= 0x1A and (a & 0x300) in (0, 0x100, 0x200):
            self.audio[0x0000 + low] = v
            self.writes.append((self.frame, low, v))
            return
        if a in self.pokeys:
            self.audio[a] = v
            self.writes.append((self.frame, a, v))
            return
        self.ram[fold(a)] = v


    MAX_ZONES = 32
    MAX_LINES = 250

    def zones(self):
        """Walk the DLL the game built in RAM -> [(line_after_zone, dli), ...].

        Three bytes per zone: byte 0 is flags and the offset (scanlines minus
        one) in bits 3-0, then the display list address high and low. Bit 7 is
        the display interrupt, which is the only interrupt MARIA raises -- and
        it fires at the END of its zone, which is what makes the line count
        matter rather than just the flag.

        Returns [] until the game has actually pointed MARIA somewhere, and
        gives up on a list that runs past the screen or past MAX_ZONES: during
        boot the pointer is often mid-write and the bytes are garbage.
        """
        if self.dpph is None or self.dppl is None:
            return []
        # MARIA raises nothing while DMA is off, and DMA is off out of reset.
        # Firing zone interrupts before a game enables DMA delivers an NMI
        # before it has installed its handler vector -- and games dispatch
        # through a RAM vector, so the jump goes to $0000 and the machine is
        # gone. CTRL bits 6-5: 10 is on, 11 is off (measured; see a7800.py).
        if (self.ctrl & 0x60) != 0x40:
            return []
        addr = ((self.dpph << 8) | self.dppl) & 0xFFFF
        if addr < 0x1800 or addr > 0x27FF:      # 7800 RAM; anything else is
            return []                           # a half-written pointer
        out, line = [], 0
        for z in range(self.MAX_ZONES):
            b0 = self.ram[(addr + z * 3) & 0xFFFF]
            line += (b0 & 0x0F) + 1
            out.append((line, bool(b0 & 0x80)))
            if line >= self.MAX_LINES:
                break
        return out


class CPU(object):
    """A 6502, written against the tables in `m6502.py`.

    Plain and unhurried rather than fast: correctness here is the whole point,
    since a subtly wrong core produces a plausible log. It is checked against a
    real emulator's capture of a real game -- see `--verify`.
    """

    def __init__(self, bus):
        self.bus = bus
        self.a = self.x = self.y = 0
        self.s = 0xFD
        self.p = U | I
        self.pc = self.word(0xFFFC)
        self.cycles = 0

    def word(self, a):
        return self.bus.read(a) | (self.bus.read(a + 1) << 8)

    def push(self, v):
        self.bus.write(0x0100 + self.s, v & 0xFF)
        self.s = (self.s - 1) & 0xFF

    def pop(self):
        self.s = (self.s + 1) & 0xFF
        return self.bus.read(0x0100 + self.s)

    def nmi(self):
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        self.push((self.p | U) & ~B & 0xFF)
        self.p |= I
        self.pc = self.word(0xFFFA)
        self.cycles += 7

    def setzn(self, v):
        v &= 0xFF
        self.p = (self.p & ~(Z | N)) | (Z if v == 0 else 0) | (v & N)
        return v

    def addr(self, mode):
        """Resolve an operand address. Returns (address, extra_cycles)."""
        b = self.bus
        pc = self.pc
        if mode == "imm":
            self.pc += 1
            return pc, 0
        if mode == "zp":
            self.pc += 1
            return b.read(pc), 0
        if mode == "zpx":
            self.pc += 1
            return (b.read(pc) + self.x) & 0xFF, 0
        if mode == "zpy":
            self.pc += 1
            return (b.read(pc) + self.y) & 0xFF, 0
        if mode == "abs":
            self.pc += 2
            return b.read(pc) | (b.read(pc + 1) << 8), 0
        if mode == "abx":
            self.pc += 2
            base = b.read(pc) | (b.read(pc + 1) << 8)
            a = (base + self.x) & 0xFFFF
            return a, 1 if (a ^ base) & 0xFF00 else 0
        if mode == "aby":
            self.pc += 2
            base = b.read(pc) | (b.read(pc + 1) << 8)
            a = (base + self.y) & 0xFFFF
            return a, 1 if (a ^ base) & 0xFF00 else 0
        if mode == "izx":
            self.pc += 1
            z = (b.read(pc) + self.x) & 0xFF
            return b.read(z) | (b.read((z + 1) & 0xFF) << 8), 0
        if mode == "izy":
            self.pc += 1
            z = b.read(pc)
            base = b.read(z) | (b.read((z + 1) & 0xFF) << 8)
            a = (base + self.y) & 0xFFFF
            return a, 1 if (a ^ base) & 0xFF00 else 0
        if mode == "ind":
            self.pc += 2
            p = b.read(pc) | (b.read(pc + 1) << 8)
            # the page-wrap bug is real hardware and games rely on it
            lo = b.read(p)
            hi = b.read((p & 0xFF00) | ((p + 1) & 0xFF))
            return lo | (hi << 8), 0
        if mode == "rel":
            self.pc += 1
            off = b.read(pc)
            return (self.pc + (off - 256 if off & 0x80 else off)) & 0xFFFF, 0
        return 0, 0                      # imp, acc

    def compare(self, reg, v):
        d = (reg - v) & 0x1FF
        self.p = (self.p & ~C) | (1 if reg >= v else 0)
        self.setzn(d & 0xFF)

    def adc(self, v):
        if self.p & D:
            # BCD. Rare in players but not unheard of, and silently wrong
            # arithmetic is exactly the kind of bug that hides.
            lo = (self.a & 0x0F) + (v & 0x0F) + (self.p & C)
            hi = (self.a >> 4) + (v >> 4) + (1 if lo > 9 else 0)
            if lo > 9:
                lo += 6
            r = ((hi << 4) | (lo & 0x0F)) & 0xFF
            if hi > 9:
                hi += 6
                r = ((hi << 4) | (lo & 0x0F)) & 0xFF
            self.p = (self.p & ~C) | (1 if hi > 15 else 0)
            self.setzn(r)
            self.a = r
            return
        t = self.a + v + (self.p & C)
        self.p = (self.p & ~(C | V)) | (1 if t > 0xFF else 0)
        if (~(self.a ^ v) & (self.a ^ t)) & 0x80:
            self.p |= V
        self.a = self.setzn(t)

    def sbc(self, v):
        if self.p & D:
            lo = (self.a & 0x0F) - (v & 0x0F) - (1 - (self.p & C))
            hi = (self.a >> 4) - (v >> 4)
            if lo & 0x10:
                lo -= 6
                hi -= 1
            if hi & 0x10:
                hi -= 6
            t = self.a - v - (1 - (self.p & C))
            self.p = (self.p & ~C) | (0 if t & 0x100 else 1)
            self.a = self.setzn(((hi & 0x0F) << 4) | (lo & 0x0F))
            return
        self.adc(v ^ 0xFF)

    def step(self):
        b = self.bus
        op = b.read(self.pc)
        entry = m6502.OPCODES.get(op)
        self.pc = (self.pc + 1) & 0xFFFF
        if entry is None:
            self.cycles += 2
            return
        mn, mode = entry[0], entry[1]
        cyc = m6502.CYCLES[op]
        a, extra = self.addr(mode)

        def rd():
            return b.read(a)

        if mn == "LDA":
            self.a = self.setzn(rd())
        elif mn == "LDX":
            self.x = self.setzn(rd())
        elif mn == "LDY":
            self.y = self.setzn(rd())
        elif mn == "STA":
            b.write(a, self.a)
        elif mn == "STX":
            b.write(a, self.x)
        elif mn == "STY":
            b.write(a, self.y)
        elif mn == "TAX":
            self.x = self.setzn(self.a)
        elif mn == "TAY":
            self.y = self.setzn(self.a)
        elif mn == "TXA":
            self.a = self.setzn(self.x)
        elif mn == "TYA":
            self.a = self.setzn(self.y)
        elif mn == "TSX":
            self.x = self.setzn(self.s)
        elif mn == "TXS":
            self.s = self.x
        elif mn == "PHA":
            self.push(self.a)
        elif mn == "PLA":
            self.a = self.setzn(self.pop())
        elif mn == "PHP":
            self.push(self.p | U | B)
        elif mn == "PLP":
            self.p = (self.pop() | U) & ~B
        elif mn == "AND":
            self.a = self.setzn(self.a & rd())
        elif mn == "ORA":
            self.a = self.setzn(self.a | rd())
        elif mn == "EOR":
            self.a = self.setzn(self.a ^ rd())
        elif mn == "ADC":
            self.adc(rd())
        elif mn == "SBC":
            self.sbc(rd())
        elif mn == "CMP":
            self.compare(self.a, rd())
        elif mn == "CPX":
            self.compare(self.x, rd())
        elif mn == "CPY":
            self.compare(self.y, rd())
        elif mn == "INC":
            b.write(a, self.setzn(rd() + 1))
        elif mn == "DEC":
            b.write(a, self.setzn(rd() - 1))
        elif mn == "INX":
            self.x = self.setzn(self.x + 1)
        elif mn == "INY":
            self.y = self.setzn(self.y + 1)
        elif mn == "DEX":
            self.x = self.setzn(self.x - 1)
        elif mn == "DEY":
            self.y = self.setzn(self.y - 1)
        elif mn in ("ASL", "LSR", "ROL", "ROR"):
            v = self.a if mode == "acc" else rd()
            if mn == "ASL":
                c, v = (v >> 7) & 1, (v << 1) & 0xFF
            elif mn == "LSR":
                c, v = v & 1, v >> 1
            elif mn == "ROL":
                c, v = (v >> 7) & 1, ((v << 1) | (self.p & C)) & 0xFF
            else:
                c, v = v & 1, ((v >> 1) | ((self.p & C) << 7)) & 0xFF
            self.p = (self.p & ~C) | c
            v = self.setzn(v)
            if mode == "acc":
                self.a = v
            else:
                b.write(a, v)
        elif mn == "BIT":
            v = rd()
            self.p = (self.p & ~(Z | N | V)) | (Z if not (self.a & v) else 0) \
                | (v & (N | V))
        elif mn == "JMP":
            self.pc = a
        elif mn == "JSR":
            r = (self.pc - 1) & 0xFFFF
            self.push((r >> 8) & 0xFF)
            self.push(r & 0xFF)
            self.pc = a
        elif mn == "RTS":
            self.pc = (self.pop() | (self.pop() << 8)) + 1 & 0xFFFF
        elif mn == "RTI":
            self.p = (self.pop() | U) & ~B
            self.pc = self.pop() | (self.pop() << 8)
        elif mn in BRANCH_ON:
            bit, want = BRANCH_ON[mn]
            if bool(self.p & bit) == want:
                cyc += 1 + (1 if (a ^ self.pc) & 0xFF00 else 0)
                self.pc = a
        elif mn == "CLC":
            self.p &= ~C
        elif mn == "SEC":
            self.p |= C
        elif mn == "CLI":
            self.p &= ~I
        elif mn == "SEI":
            self.p |= I
        elif mn == "CLV":
            self.p &= ~V
        elif mn == "CLD":
            self.p &= ~D
        elif mn == "SED":
            self.p |= D
        elif mn == "BRK":
            self.pc = (self.pc + 1) & 0xFFFF
            self.push((self.pc >> 8) & 0xFF)
            self.push(self.pc & 0xFF)
            self.push(self.p | U | B)
            self.p |= I
            self.pc = self.word(0xFFFE)
        elif mn == "NOP":
            pass
        else:
            pass                          # undocumented: treated as a NOP
        if extra and mn in ("LDA", "LDX", "LDY", "ADC", "SBC", "AND", "ORA",
                            "EOR", "CMP"):
            cyc += extra
        self.cycles += cyc


# Measured, not quoted: a counting cartridge run under MAME put the NTSC
# scanline at exactly 114.00 CPU cycles and the non-VBLANK window at exactly
# 241.0 of the 262 lines. See docs/hardware.md, "What MARIA costs", and
# probes/dma-costcart.py for the instrument. PAL is derived the same way from
# its own clock and line count, and has NOT been measured.
CYCLES_PER_LINE = 114.0
LINES = {"ntsc": 262, "pal": 312}
VBLANK_LINES = {"ntsc": 21, "pal": 21}


def run(cart, frames, region="ntsc", drive=False, nmi=True, quiet=False,
        frame_nmi=False):
    """Execute the cartridge for `frames` frames, collecting audio writes.

    Interrupts follow the hardware: on the 7800 the ONLY thing that raises NMI
    is MARIA's display interrupt, fired at the end of any zone whose DLL entry
    has bit 7 set. There is no separate vertical-blank interrupt. A game that
    wants one puts a DLI on its last zone -- Asteroids does exactly that, with
    one DLI in seventeen zones -- while a game doing per-zone work raises many
    (Ms. Pac-Man: thirty).

    This is what an earlier version got wrong. It raised one NMI a frame at the
    end of the visible screen, which is right for a game like Asteroids by
    accident and wrong for everything that hangs work off zone boundaries.
    `frame_nmi=True` restores that behaviour for comparison.
    """
    bus = Bus(cart, drive=drive)
    cpu = CPU(bus)
    lines = LINES[region]
    vb_line = lines - VBLANK_LINES[region]
    per = int(lines * CYCLES_PER_LINE)

    for f in range(frames):
        bus.frame = f + 1
        base = cpu.cycles
        bus.vblank = False

        # The display list is rebuilt in RAM every frame by most games, so the
        # zone layout is read fresh rather than cached.
        events = []
        if nmi:
            for line_end, dli in bus.zones():
                if dli and line_end <= vb_line:
                    events.append((base + int(line_end * CYCLES_PER_LINE), "dli"))
        events.append((base + int(vb_line * CYCLES_PER_LINE), "vblank"))
        if frame_nmi and nmi:
            events.append((base + int(vb_line * CYCLES_PER_LINE), "dli"))
        events.sort(key=lambda e: e[0])

        target = base + per
        i = 0
        while cpu.cycles < target:
            while i < len(events) and cpu.cycles >= events[i][0]:
                if events[i][1] == "vblank":
                    bus.vblank = True
                else:
                    cpu.nmi()
                i += 1
            cpu.step()
    return bus


def read_log(path):
    """Parse a capture log -> [(frame, (values...))]."""
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        try:
            rows.append((int(parts[0]), tuple(parts[1:])))
        except ValueError:
            continue
    return rows


def compare(sim_rows, ref_rows, window=400):
    """How well does the simulation reproduce a known-good capture?

    Absolute frame numbers cannot match: MAME boots through the BIOS before
    the cartridge gets control, so its clock starts a hundred-odd frames
    earlier. What must match is the SEQUENCE of register states and the gaps
    between them, so this searches for the offset that aligns them best and
    reports the agreement at that offset.

    Reported as (offset, matched, total): how many of the reference's rows
    appear in the simulation at the same relative frame, with the same values.
    """
    if not sim_rows or not ref_rows:
        return (0, 0, len(ref_rows))
    sim_by = {}
    for f, v in sim_rows:
        sim_by.setdefault(f, []).append(v)
    best = (0, -1)
    lo = ref_rows[0][0] - sim_rows[0][0]
    for off in range(lo - window, lo + window + 1):
        hit = 0
        for f, v in ref_rows:
            if v in sim_by.get(f - off, ()):
                hit += 1
        if hit > best[1]:
            best = (off, hit)
    return (best[0], best[1], len(ref_rows))


def write_log(bus, cart, path, region="ntsc"):
    """The same per-frame format probes/audio.lua produces."""
    bases = cart.pokeys()
    nvals = (9 * len(bases)) if bases else 6
    slot = {}
    if bases:
        for i, base in enumerate(bases):
            for r in range(9):
                slot[base + r] = i * 9 + r
    else:
        for off, k in ((0x15, 0), (0x17, 1), (0x19, 2),
                       (0x16, 3), (0x18, 4), (0x1A, 5)):
            slot[off] = k
    cur = [0] * nvals
    last = [-1] * nvals
    rows = []
    byframe = {}
    for f, a, v in bus.writes:
        byframe.setdefault(f, []).append((a, v))
    for f in sorted(byframe):
        for a, v in byframe[f]:
            k = slot.get(a)
            if k is not None:
                cur[k] = v
        if cur != last:
            rows.append((f, list(cur)))
            last = list(cur)
    with open(path, "w", encoding="utf-8") as fh:
        if bases:
            names = ",".join("$%04X" % b for b in bases)
            fh.write("# chip %s  base %s\n"
                     % ("pokey2" if len(bases) > 1 else "pokey", names))
            heads = []
            for i in range(len(bases)):
                n = i * 4
                heads.append("f%d c%d  f%d c%d  f%d c%d  f%d c%d  ctl"
                             % (n+1, n+1, n+2, n+2, n+3, n+3, n+4, n+4))
            fh.write("# frame  " + "   ".join(heads) + "   (hex)\n")
        else:
            fh.write("# chip tia\n")
            fh.write("# frame  c0 f0 v0  c1 f1 v1   (hex)\n")
        for f, vals in rows:
            fh.write("%d %s\n" % (f, " ".join("%02X" % v for v in vals)))
    return len(rows)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-o", "--out")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--frames", type=int)
    ap.add_argument("--drive", action="store_true",
                    help="hold fire, for a game that waits at a title screen")
    ap.add_argument("--compare", metavar="REF.log",
                    help="compare the simulation against a known-good capture "
                         "(from capture.py or probes/audio.lua) and report how "
                         "much of it is reproduced. This is the only thing "
                         "that makes the simulation trustworthy.")
    ap.add_argument("--frame-nmi", action="store_true",
                    help="raise one NMI a frame at end of visible instead of "
                         "following the display list. The old behaviour, kept "
                         "for comparison.")
    ap.add_argument("--no-nmi", action="store_true",
                    help="do not call the NMI handler each frame")
    ap.add_argument("--verify", metavar="LOG",
                    help="an emulator capture of the same game, to check "
                         "this against")
    args = ap.parse_args()

    try:
        cart = cart_module.Cart(args.rom)
    except (cart_module.UnknownMapper, cart_module.UnknownSpace, IOError) as e:
        sys.stderr.write("%s\n" % e)
        return 2
    region = ((cart.info or {}).get("region", "NTSC")).lower()
    frames = args.frames or int(args.seconds * (50 if region == "pal" else 60))

    bus = run(cart, frames, region, drive=args.drive, nmi=not args.no_nmi,
              frame_nmi=args.frame_nmi)
    out = args.out or (os.path.splitext(args.rom)[0] + "-sim.log")
    n = write_log(bus, cart, out, region)
    print("%s -- %d frames simulated, %d audio writes, %d changed rows"
          % (os.path.basename(out), frames, len(bus.writes), n))

    if args.compare:
        off, hit, total = compare(read_log(out), read_log(args.compare))
        pct = 100.0 * hit / total if total else 0.0
        print("compared with %s" % os.path.basename(args.compare))
        print("  best alignment: reference frame = simulated frame + %d" % off)
        print("  %d of %d reference rows reproduced exactly (%.1f%%)"
              % (hit, total, pct))
        if pct < 50:
            print("  NOT trustworthy for this cartridge. Something the "
                  "simulation does not model is changing what its player does.")
    if not bus.writes:
        print("  Nothing was written to the sound chip. The game may need "
              "input (--drive),")
        print("  or may depend on hardware this does not provide. capture.py "
              "runs a real")
        print("  emulator and does not care.")

    if args.verify:
        import tracker
        a = list(tracker.read_capture(out, region).states())
        b = list(tracker.read_capture(args.verify, region).states())
        n2 = min(len(a), len(b))
        best = (0, -1)
        for off in range(0, min(400, n2 - 60)):
            same = sum(1 for i in range(n2 - off) if a[i + off] == b[i])
            if same > best[1]:
                best = (off, same)
            same = sum(1 for i in range(n2 - off) if b[i + off] == a[i])
            if same > best[1]:
                best = (-off, same)
        off, same = best
        total = n2 - abs(off)
        print("  verify: %d of %d rows identical (%.1f%%) at offset %d"
              % (same, total, 100.0 * same / max(total, 1), off))
    return 0


if __name__ == "__main__":
    sys.exit(main())
