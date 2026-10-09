#!/usr/bin/env python3
"""
Run a cartridge's own code, and listen to what it writes to the sound chip.

    python tools/sim.py game.a78 --seconds 20 -o game.log
    python tools/sim.py game.a78 -o out.log --compare known-good.log

`capture.py` gets the same log by driving an emulator. This gets it by
executing the 6502 directly: RAM, the cartridge mapper, enough of MARIA to keep
a game's timing loop happy, and a trap on every write to TIA or POKEY.

The reason to have it is not to replace the emulator, which is better at being
an emulator. It is that **a player written for a format nobody has described is
still the authority on that format.** Reimplementing RMT's replayer means
reading 803 instructions and hoping; running it means the answer is correct by
construction. The same goes for every other player in the library.

## State: a TIA tool, and it works

`--compare` scores the simulation against a capture from `capture.py`. Run
like for like -- same emulator, no input, same length -- across five TIA
cartridges that play music on their own:

    Ikari Warriors    agreement 99.4%   progress 100.0%   timing 1.00x
    Midnight Mutants            97.6%             100.0%         1.00x
    Donkey Kong                 99.2%             100.0%         1.00x
    Dark Chambers               98.3%             100.0%         1.00x
    Choplifter                  97.8%              96.7%         1.00x

Measured against the Trebor's PROPack v8_17 ROMs and MAME 0.264 with the
7800OpenBIOS, **over the same stretch of play**: 882 frames of the simulator, which
is what a 1,200-frame MAME capture covers, because the simulator counts from the
cartridge's reset and MAME from power-on, 322 frames earlier with OpenBIOS. Scoring
a longer simulator run against the capture counts everything it plays after the
capture ended as disagreement: the same simulator scored Midnight Mutants 43.9%
over 1,200 frames and 94.3% over 882 (97.6% now that MARIA's DMA is charged).

Reference states reproduced, in order: Ikari 521 of 529, Midnight Mutants 83 of 83
(the two extra states are a note that starts on the capture's last frame), Dark
Chambers 173 of 173, Donkey Kong 119 of 120, Choplifter 174 of 180. The differences
left in Ikari are one-frame envelope steps; Choplifter's six, and the two states
it stops short of, have not been traced.
(The earlier figures for the middle three, 99.0, 89.0 and 78.4 per cent, came from
runs whose lengths were not recorded and could not be reproduced.)

All five reproduce a commercial game's music from its own code, with the frame
clock exact in every one. That is what this was built to do.

**Treat this as a TIA tool.** The POKEY path is not validated: Ballblazer
generates its music from POKEY's random register and cannot be scored by log
comparison at all, and Commando -- the only other retail POKEY cartridge --
emits 11 states against a capture's 424 for reasons not yet understood.

### The bug that made Donkey Kong silent

Worth recording because of how badly the symptom described the cause. Donkey
Kong emitted a single state and looked like a cartridge this could not run.

Its title music is driven by one display interrupt, on the LAST visible zone:
DLL byte `$C7`, ending at scanline 248. MAME takes 457 of them in ten
seconds; the handler at `$F7B1` runs `dec $74` and the main loop at `$E24A`
spins on `$74` waiting for it.

This simulator took none. When display interrupts were added here they were
fired only for zones ending before the VBLANK line, which was an invention --
MARIA walks the display list and does not consult anybody's idea of where
vertical blank starts. That filter silently dropped Donkey Kong's only
interrupt, and the game span for ever. One bit, in one DLL entry, presenting
as "makes no sound".

### Choplifter, traced -- and the real cause

Its channel 0 matched the capture value for value, while `AUDC1` and `AUDF1`
were never written at all. This was once put down to its attract demo taking a
different course. **That was wrong.** Diffing every write to `$1920-$1928`
against MAME's showed the two identical until the player at `$B29E` -- which is
voice-generic -- ran `LDA ($95),Y` for voice 1 and read a zero where MAME read
`$02`. The pointer's high byte lives at `$1928`, and `Bus.read` took any address
whose low byte is `$28` for MSTAT, so it came back as the vertical-blank flag.
The voice ended at once, every time.

The same bug hid the last stretch of Ikari Warriors: its second tune read a
table across an address ending in `$28`, the simulator wrote values no AUDC can
hold (`$1F`) and then fell silent for the rest of the run. With MSTAT answering
only at `$28` and `$128`, both play through.

**Treat this as a TIA tool.** The POKEY path is not validated: Ballblazer
generates its music from POKEY's random register and cannot be scored by log
comparison at all, and Commando -- the only other retail POKEY cartridge --
emits 11 states against a capture's 424 for reasons not yet understood.

### Comparing like for like, which is most of the difficulty

Two things have to match between the capture and the simulation or the numbers
mean nothing, and both caught me out:

  * **Driving.** `capture.py` taps fire BY DEFAULT (its flag is `--no-drive`).
    `sim.py --drive` now taps on the same duty cycle rather than holding the
    button, but the two still count frames from different origins -- the
    emulator from machine boot, this from the cartridge's reset, about 133
    frames apart -- so the presses land at different points in a title
    sequence and the runs diverge before the music starts. Every driven
    comparison attempted here scored badly for that reason and none of them
    should be read as a measurement of the simulator.
  * **Length.** Midnight Mutants leaves its attract loop at frame 1170. Every
    run of it was 14 seconds for a long time, and it read 7.7%.

## What is known to work

The 6502 core, the mapper and the memory map are right, and this is not an
opinion: diffed instruction by instruction against MAME's debugger trace (with
`noloop` -- see pitfalls.md), from the moment the BIOS hands over the
cartridge, **the first 427,399 instructions are identical**. That is about 124
frames.

MARIA's display interrupts are implemented -- the DLL the game builds in RAM
is walked each frame and an NMI raised at the start of every zone whose entry has
bit 7 set -- and the walk agrees with MAME byte for byte. They demonstrably
fire: early in Ballblazer this raises 16 a frame, and the game's counter at
`$40` counts down exactly once per frame, as its author intended.

## What stopped it: POKEY's random number generator

Traced to a root cause and fixed.

Ballblazer does not play a score, it **generates** one, and it asks POKEY for
the entropy. At `$B333`:

    CMP $400A        ; POKEY's RANDOM register
    BCS $B34A        ; skip this note if the comparison fails
    LDA $B936,Y
    STA $2123        ; otherwise emit it

This simulator returned zero for every POKEY read. `A >= 0` is always true, so
the branch always skipped, no note was ever emitted, and the four voice cells
the player reads stayed at zero. The player then did exactly what it was told
and played silence -- which is why the failure looked like a player that
stops, then like a missing interrupt, then like a display-list problem, and
was none of those.

`Bus.random` now implements the 17-bit polynomial counter, clocked at the CPU
rate. The music plays: 428 of 429 logged states carry voice four, against one
of 164 before, and it keeps playing to the end of the run instead of dying
around frame 400.

### And the score went DOWN

Agreement fell from 60% to 24% while the simulation became fundamentally more
correct, because a generated soundtrack driven by a different random stream is
different music -- valid, in the same style, not the same notes. **Ballblazer
cannot be scored by log comparison at all** unless the polynomial counter is
bit-exact and cycle-aligned with the hardware, which is a far higher bar than
"plays the right music".

That is a limit of the gate, not a defect in the fix, and it is worth stating
plainly because the number moving the wrong way is exactly what an unwary
reading would call a regression. For cartridges that play a fixed score the
comparison means what it says; for one that improvises, it cannot.

## MARIA's DMA, and what the score is worth


The simulation gave the game more CPU than hardware does, because it never
paid for MARIA's cycle stealing. `--dma-steal` charges it, using the cost
model in `dmabudget.py` -- including holey DMA, which matters enormously
here: Ballblazer's interrupt-bearing zones set the holey bit and keep their
graphics at `$1C00`, which bit 12 suppresses, so almost all of their apparent
cost is not paid at all. Modelling that took the charge from 12% of a frame
to 8%, and took `--dma-steal` from scoring 0.1% to scoring the same 6.3% as
without it.

**And then a more faithful version scored 0.1% again.** MARIA steals a
scanline at a time, not a zone at a time; spreading the identical total across
each zone's scanlines instead of charging it in one lump at the zone boundary
is unambiguously closer to the hardware, and it moves the score by a factor of
sixty. The two distributions differ by 17 cycles a frame out of 2,350.

That is the useful finding, and it is about the instrument rather than the
simulator: **the score is chaotic with respect to timing.** 6.3% does not mean
six per cent of the way there. It means a particular arrangement of a broken
simulation happens to keep 98 rows in step before drifting, and a small timing
change moves which rows those are. `--compare` can be trusted to say "not
trustworthy". It cannot be trusted to rank two near-misses, and it must not be
used to tune.

**It is now on by default**, because that finding was about Ballblazer, whose
score cannot be read at all, and on the games whose score can be read it is the
better setting. Measured over the same 882 frames against MAME (Trebor's PROPack
ROMs, OpenBIOS), without and with the charge:

    Choplifter         79.6%  ->  97.8%    (its rhythm passage runs 37 beats, not 5)
    Midnight Mutants   94.3%  ->  97.6%
    Ikari Warriors     99.4%  ->  99.4%
    Donkey Kong        99.2%  ->  99.2%
    Dark Chambers      98.3%  ->  98.3%

None gets worse. Choplifter's passage is counted in loop iterations, not frames:
with the 6502 given every cycle, the loop spins about a third more often per frame
than on hardware (a third more RAM writes over the same frames) and ends early.
`--no-dma-steal` restores the old behaviour. (That `--compare` cannot rank two
near-misses still stands; these five are not near-misses.)

## What has been ruled out

Each of these was a confident diagnosis at some point, and each was wrong.

* **Display interrupts as the original blocker.** The first version of this
  file said Midnight Mutants' "timing comes from DLIs, not from the frame".
  Measured against hardware, that game raises **no display interrupts at
  all** -- twenty zones, not one with bit 7 set.
* **The BIOS.** The suspicion was that entering at the reset vector with RAM
  zeroed left a game reading state it never initialised. Disproven by the
  427,399 identical instructions: had inherited state mattered, the two would
  have parted company at once.
* **The mapper and the memory map.** Both agree with MAME on the vectors, on
  the code at the reset address, and now across most of half a million
  instructions of execution.
* **VBLANK phase.** The first divergence in the trace is a VBLANK wait, but it
  re-synchronises 1,160 instructions later, and sweeping the flag's phase
  across a whole frame changes the score by nothing.
* **The RIOT timer.** Neither of those two games ever read `INTIM`. The simulator now
  models the interval timer ($0284-$0287 reads, $0294-$0297 writes), WSYNC (the CPU waits
  for the next scanline), RAM on the cartridge, the 52K image's low ROM and NMOS decimal-mode
  flags -- each checked against a synthetic cartridge (`selftest.py: t_sim_machine`),
  not against MAME.
* ~~**POKEY reads.**~~ Recorded here as ruled out, on the grounds that
  "Ballblazer never reads it". That was wrong: it was measured over 300
  frames, and the music engine that reads `$400A` does not start until frame
  400. It was the bug. Left in place as a reminder that a negative result is
  only as good as the window it was measured over.
* **Double buffering of the display list.** The hypothesis was that the game
  keeps two lists and repoints MARIA mid-frame, so reading `DPPH`/`DPPL` once
  at frame start would read the wrong one. It does not: the pointer is
  written twice at frame 46 and once more at frame 404, and never within a
  frame.
* **A different display list.** Read out of MAME's memory at a matched frame,
  the game's list is byte-for-byte what this simulator has. An earlier claim
  that MAME ran 19 zone interrupts to this simulator's 16 is **withdrawn**:
  that reading came from tap-captured pointers, and the taps had died before
  the cartridge started, so it described the BIOS's display list.
* **Holey DMA.** Set in these zones, and measured to make drawing cheaper,
  not dearer -- so it cannot explain a cost that is too high.
* **RAM mirroring**, and **a crash through a null interrupt vector**: both
  were real faults, both are fixed, neither moved the score.

### A retraction

An earlier version of this file offered, as evidence that the two diverge
during initialisation, that MAME's display list sat at `$1F84` where this
simulator built one at `$26EE`. **That comparison was invalid.** `$1F84` is
written at MAME frame 16, and the cartridge does not get control until about
frame 133 -- so it is the BIOS's own display list, and the comparison was
against the logo screen rather than the game. The instruction-level diff,
which came later and is trustworthy, shows the two agreeing for 427,399
instructions instead.

A method note worth more than any of them: the useful signal was not the
"first divergence", which has now been misleading three times, but the SHAPE
of the 1,086 re-synchronisations that follow it -- almost all of them MAME
executing the same ~36 instructions this does not. That is a missing interrupt
handler, and it says so without needing a story.

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


def explore_switches(frame):
    """SWCHB while exploring with `drive="switches"` (the joystick sweep of
    `drive="explore"` plus these): the console switches move, so code behind them runs.

    Active low, as on the console. The two difficulty switches (bits 7 and 6) step
    through all four combinations, a new one every 300 frames. Select (bit 1) is held
    for ten frames out of every 240 and Reset/Start (bit 0) for ten out of every 480,
    at different phases so each is seen alone and neither starts the run. Pause
    (bit 3) is pressed twice, thirty frames apart, once every 600 frames: most games
    toggle on the press, so the pair pauses and then resumes, and a game that
    pauses is not left stopped for the rest of the run."""
    v = 0x0B | (((frame // 300) & 3) << 6)
    if 100 <= frame % 240 < 110:
        v &= ~0x02
    if 200 <= frame % 480 < 210:
        v &= ~0x01
    t = frame % 600
    if 400 <= t < 404 or 430 <= t < 434:
        v &= ~0x08
    return v & 0xFF


def explore_second_button(frame, which):
    """INPT0-3 while exploring: bit 7 set (pressed) for eight frames in every ninety,
    each input at its own phase. 0 otherwise."""
    return 0x80 if (frame + 23 * which) % 90 < 8 else 0x00


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
        # MAME shows the window's FIRST bank at power-on (a SuperGame's reset code can live
        # there); the last bank is the fixed one at $C000
        mapper = getattr(cart, "map", None)
        if getattr(mapper, "name", "") == "supergame":
            self.bank = getattr(mapper, "first_window", 0)
        self.audio = {}                 # address -> last value written
        self.writes = []                # (frame, address, value)
        self.frame = 0
        self.vblank = False
        self.dpph = None                # DPPH/DPPL are WRITE-ONLY on real
        self.dppl = None                # hardware; the sim keeps its own copy
        self.ctrl = 0x00                # MARIA CTRL: DMA is OFF until a game
                                        # turns it on, and so are its interrupts
        self.poly = 0x1FFFF             # POKEY's 17-bit polynomial counter
        self.poly_at = 0                # ... and the cycle it was last advanced
        self.drive = drive
        self.obs = None                 # an observer, or None
        self.maria = {}                 # MARIA register -> last value written
        self.cpu_cycles = lambda: 0     # set by CPU.__init__
        # where cartridge ROM starts (a 52K image reaches down to $3000) and which ranges are
        # RAM on the cartridge: reads and writes there are memory, not ROM
        regions = getattr(cart, "_region", [])
        starts = [r[0] for r in regions if r[2] != "ram"]
        self.rom_low = min([0x4000] + starts)
        self.cart_ram = [(r[0], r[1]) for r in regions if r[2] == "ram"]
        self.wsync = False              # a write to WSYNC is waiting to stall the CPU
        self.jammed = None              # set to the address of a KIL the program ran
        self.timer = None               # the RIOT interval timer: (set at cycle, value, interval)
        self.timer_flag_cleared = False
        self.pokeys = set()
        for base in cart.pokeys():
            for r in range(16):
                self.pokeys.add(base + r)

    RIOT_BIAS = 1       # cycles from the write that starts the timer to the first count (fitted)

    def riot(self, a):
        """INTIM ($0284/$0286) and TIMINT ($0285/$0287) as the 6532 answers them (the numbers
        are MAME's, checked against tests/golden/riot-mame.bin):

        the timer reads N - ceil(e / interval) until it reaches zero, expiring at e = N *
        interval; then it counts down once per CYCLE from $FF and TIMINT bit 7 is set. Reading
        INTIM clears TIMINT, and if the timer had expired it goes back to the programmed rate
        from the value it had just reached."""
        if self.timer is None:
            return 0x00
        t0, val, interval, bias = self.timer
        e = int(self.cpu_cycles() - t0) + bias
        expired = e >= val * interval
        if not expired:
            value = val - -(-e // interval)
        else:
            value = (0xFF - (e - val * interval) + (1 if interval == 1 else 0)) & 0xFF
        if a in (0x0285, 0x0287):
            return 0x80 if (expired and not self.timer_flag_cleared) else 0x00
        # an INTIM read: clears the flag if it is set, and an expired timer goes back to
        # counting at the programmed rate from where it has got to
        if expired:
            self.timer = (self.cpu_cycles(), value, interval, bias)
            self.timer_flag_cleared = True
        return value

    def random(self, cycles):
        """POKEY's RANDOM register: the top bits of a 17-bit LFSR.

        This is not a detail. Ballblazer generates its music rather than
        playing a score, and the generator asks POKEY for entropy --
        `CMP $400A / BCS` at $B333, which skips the note when the comparison
        fails. Return a constant zero, as this simulator did, and the
        comparison always skips: the engine runs, emits nothing, and the game
        plays silence through a player that is working perfectly.

        The polynomial is x^17 + x^12 + 1, clocked at the CPU rate, so it is
        advanced by however many cycles have passed since it was last asked.
        """
        # the cycle count is fractional once MARIA's DMA has taken its share of a line
        step = int(cycles - self.poly_at)
        self.poly_at += step
        p = self.poly
        for _ in range(min(step, 4096)):
            p = ((p >> 1) | (((p ^ (p >> 5)) & 1) << 16)) & 0x1FFFF
        self.poly = p
        return (p >> 9) & 0xFF

    # -- reads
    def read(self, a):
        a &= 0xFFFF
        if a in self.pokeys:
            reg = a & 0x0F
            if reg == 0x0A:                       # RANDOM
                return self.random(self.cpu_cycles())
            return 0xFF
        if a >= self.rom_low:
            sp = self.cart.space_of(a, self.bank)
            if sp is not None:
                try:
                    return self.cart.byte(sp, a)
                except Exception:                            # noqa: BLE001
                    return 0xFF
            return self.ram[a]
        if (a & 0xFF) == 0x28 and a < 0x0400 and (a & 0x300) in (0, 0x100, 0x200):
            # MSTAT: bit 7 set during vertical blank. (This used to test only the low
            # byte, so RAM at $1928, $1A28, $2028 ... read back as MSTAT: Choplifter keeps
            # the high byte of voice 1's note pointer at $1928, read it as zero, and never
            # played its second voice.)
            return 0x80 if self.vblank else 0x00
        if a in (0x0008, 0x0009, 0x000A, 0x000B):
            # INPT0-3: no paddles -- or, exploring, the second buttons of two-button
            # sticks, pressed (bit 7 set) in short turns so a game that has them uses them.
            if self.drive == "switches":
                return explore_second_button(self.frame, a - 0x0008)
            return 0x00
        if a in (0x000C, 0x000D):
            # INPT4/5: fire buttons, active low.
            #
            # --drive taps fire rather than holding it, on the same duty cycle
            # probes/audio.lua uses: ten frames in every seventy. Holding it
            # down is not the same thing -- a title screen that waits for a
            # press AND a release never gets the release, and the game sits
            # there while the capture it is being compared against has long
            # since started. The two have to agree or the comparison is
            # between different sessions.
            if not self.drive:
                return 0x80
            return 0x00 if (self.frame % 70) < 10 else 0x80
        if a == 0x0280:
            # SWCHA: joystick directions, also active low. All ones is centred --
            # unless exploring: then the stick sweeps right, left, down, up and rests,
            # twenty frames each, so a game that needs a push to do anything does it.
            if self.drive in ("explore", "switches"):
                step = (self.frame // 20) % 5
                return (0xFF, 0x7F, 0xBF, 0xDF, 0xEF)[step] if step else 0xFF
            return 0xFF
        if 0x0280 <= a < 0x0300 and (a & 0x04):
            # RIOT INTIM and TIMINT. The timer counts down once per interval from the value
            # written; when it passes zero it flags (TIMINT bit 7) and counts down every
            # cycle. A game that waits on it with BIT $0285 / BPL used to wait for ever.
            return self.riot(0x0285 if (a & 1) else 0x0284)    # INTIM / TIMINT, mirrored
        if a == 0x0282:
            # SWCHB: the console switches, and they are **active low** -- a set
            # bit means "not pressed". Returning zeros here reads as reset and
            # select both held down, and a game that waits for them to be
            # released waits forever. Midnight Mutants spins on exactly that.
            #   bit 0 reset, bit 1 select, bit 3 pause, bits 6/7 difficulty
            if self.drive == "switches":
                return explore_switches(self.frame)
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
                if self.obs is not None:
                    self.obs.bank_switch(a, v, b)
                return
        if a >= self.rom_low:
            if a in self.pokeys:
                self.audio[a] = v
                self.writes.append((self.frame, a, v))
                return
            for lo, hi in self.cart_ram:
                if lo <= a < hi:
                    self.ram[a] = v      # RAM on the cartridge
                    return
            return                       # ROM: writes go nowhere
        if 0x0280 <= a < 0x0300 and (a & 0x14) == 0x14:
            # the RIOT's interval timer: TIM1T / TIM8T / TIM64T / T1024T, mirrored through
            # the RIOT's whole range ($029C-$029F are the interrupt-enabled forms)
            interval = (1, 8, 64, 1024)[a & 3]
            self.timer = (self.cpu_cycles(), v, interval, 2 if interval == 1 else self.RIOT_BIAS)
            self.timer_flag_cleared = False
            return
        low = a & 0xFF
        if low == 0x24 and a < 0x0400 and (a & 0x300) in (0, 0x100, 0x200):
            self.wsync = True            # the CPU waits for the end of the scanline
        if a < 0x0400 and (a & 0x300) in (0, 0x100, 0x200):
            # MARIA's display-list pointer. Write-only on hardware, so nothing
            # can read it back -- the sim has to catch it on the way past or
            # it never learns where the display list is.
            if 0x20 <= low <= 0x3F:
                self.maria[low] = v
                if self.obs is not None:
                    self.obs.maria_write(low, v)
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
        if self.obs is not None:
            self.obs.data_write(a, v)
        self.ram[fold(a)] = v


    MAX_ZONES = 32
    MAX_LINES = 250

    # Measured DMA costs, in CPU cycles. Same numbers as tools/dmabudget.py
    # and docs/hardware.md; see probes/dma-costcart.py for how they were got.
    DMA_LINE, DMA_ZONE = 5.633, 1.678
    DMA_OBJ, DMA_BYTE, DMA_FIVE = 2.081, 0.744, 0.483
    DMA_DLI = 16.6

    def zone_cost(self, dl, lines, flags=0):
        """CPU cycles MARIA steals drawing one zone.

        MARIA draws by DMA and halts the 6502 while it does. A simulator that
        ignores that hands the game two to three times the CPU the hardware
        gives it, which does not look like a timing bug -- everything still
        runs, just with the balance between the main loop and the interrupt
        handlers completely wrong.
        """
        holey16 = bool(flags & 0x40)
        per_line = self.DMA_LINE
        i = 0
        for _ in range(32):
            b1 = self.mem((dl + i + 1) & 0xFFFF)
            if b1 == 0:
                break
            lo = self.mem((dl + i) & 0xFFFF)
            if (b1 & 0x1F) == 0:                      # five-byte entry
                hi = self.mem((dl + i + 2) & 0xFFFF)
                w = 32 - (self.mem((dl + i + 3) & 0xFFFF) & 0x1F)
                five, chars = True, bool(b1 & 0x20)
                i += 5
            else:
                hi = self.mem((dl + i + 2) & 0xFFFF)
                w = 32 - (b1 & 0x1F)
                five, chars = False, False
                i += 4
            # Holey DMA drops the graphics fetch when address bit 12 is set,
            # measured; the entry is still read, so the object costs its
            # header and no pixels. See docs/hardware.md.
            if holey16 and (((hi << 8) | lo) & 0x1000):
                w = 0
            if chars:
                bpc = 2 if (self.ctrl & 0x10) else 1
                per_line += (self.DMA_OBJ + self.DMA_FIVE
                             + w * (1 + bpc) * self.DMA_BYTE)
            else:
                per_line += (self.DMA_OBJ + w * self.DMA_BYTE
                             + (self.DMA_FIVE if five else 0))
        return (lines * per_line + self.DMA_ZONE
                + (self.DMA_DLI if (flags & 0x80) else 0))

    def mem(self, a):
        """A byte as MARIA reads it: RAM (through its mirrors), or the cartridge."""
        f = fold(a & 0xFFFF)
        if 0x1800 <= f <= 0x27FF:
            return self.ram[f]
        return self.read(a)

    def zones(self):
        """Walk the DLL the game built in RAM -> [(line_after_zone, dli, cost, height), ...].

        Three bytes per zone: byte 0 is flags and the offset (scanlines minus
        one) in bits 3-0, then the display list address high and low. Bit 7 is
        the display interrupt, which is the only interrupt MARIA raises. It is taken
        on the last line of the zone BEFORE the flagged one, so that its handler
        runs ahead of the flagged zone's first line (docs/hardware.md); the line
        count matters, not just the flag.

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
        # the list list is in RAM (or its zero-page and stack views) or in the cartridge's
        # ROM -- several games keep a fixed screen there; anything else is a half-written pointer
        if not (0x1800 <= fold(addr) <= 0x27FF or addr >= self.rom_low):
            return []
        out, line = [], 0
        for z in range(self.MAX_ZONES):
            b0 = self.mem((addr + z * 3) & 0xFFFF)
            hi = self.mem((addr + z * 3 + 1) & 0xFFFF)
            lo = self.mem((addr + z * 3 + 2) & 0xFFFF)
            n = (b0 & 0x0F) + 1
            line += n
            out.append((line, bool(b0 & 0x80),
                        self.zone_cost((hi << 8) | lo, n, b0), n))
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
        bus.cpu_cycles = lambda: self.cycles     # POKEY's LFSR runs on these
        self.a = self.x = self.y = 0
        self.s = 0xFD
        self.p = U | I
        self.pc = self.word(0xFFFC)
        self.cycles = 0
        self.jammed = None              # the address of a KIL/JAM opcode the program ran
        self.obs = None                 # an observer (see Observer), or None

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
            if self.obs is not None:
                self.obs.pointer(z)
            return b.read(z) | (b.read((z + 1) & 0xFF) << 8), 0
        if mode == "izy":
            self.pc += 1
            z = b.read(pc)
            if self.obs is not None:
                self.obs.pointer(z)
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
            # NMOS decimal ADC (the 6502 as it is documented in "6502.org: Decimal Mode"): the
            # low nibble is adjusted first; N and V come from that intermediate sum, Z from
            # the plain binary sum, and only the carry and the result from the final adjust.
            a, c_in = self.a, self.p & C
            lo = (a & 0x0F) + (v & 0x0F) + c_in
            if lo >= 0x0A:
                lo = ((lo + 0x06) & 0x0F) + 0x10
            mid = (a & 0xF0) + (v & 0xF0) + lo
            self.p &= ~(N | V | Z | C)
            if mid & 0x80:
                self.p |= N
            if (~(a ^ v) & (a ^ mid)) & 0x80:
                self.p |= V
            if ((a + v + c_in) & 0xFF) == 0:
                self.p |= Z
            if mid >= 0xA0:
                mid += 0x60
            if mid >= 0x100:
                self.p |= C
            self.a = mid & 0xFF
            return
        t = self.a + v + (self.p & C)
        self.p = (self.p & ~(C | V)) | (1 if t > 0xFF else 0)
        if (~(self.a ^ v) & (self.a ^ t)) & 0x80:
            self.p |= V
        self.a = self.setzn(t)

    def sbc(self, v):
        if self.p & D:
            a0 = self.a
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
            # NMOS: N, V and Z of a decimal subtract are the binary subtract's
            self.p = (self.p & ~(N | V | Z)) | (N if t & 0x80 else 0) \
                | (Z if (t & 0xFF) == 0 else 0) \
                | (V if ((a0 ^ v) & (a0 ^ t) & 0x80) else 0)
            return
        self.adc(v ^ 0xFF)

    def step(self):
        b = self.bus
        obs = self.obs
        pc0 = self.pc
        op = b.read(pc0)
        entry = m6502.OPCODES.get(op)
        self.pc = (self.pc + 1) & 0xFFFF
        if obs is not None:
            obs.fetch(pc0, op)
        if entry is None:
            self.cycles += 2
            return
        mn, mode = entry[0], entry[1]
        if mn == "JAM":
            # KIL halts a real 6502 for good (only a reset frees it). Games plant them as
            # error traps; running on past one invents behaviour the machine never had.
            self.jammed = pc0
            self.pc = pc0
            self.cycles += 2
            return
        cyc = m6502.CYCLES[op]
        a, extra = self.addr(mode)

        if obs is None or mode == "imm":
            def rd():
                return b.read(a)
        else:
            def rd():
                obs.data_read(a)
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
            if obs is not None and mode == "ind":
                obs.jump_indirect(pc0, a)
            self.pc = a
        elif mn == "JSR":
            r = (self.pc - 1) & 0xFFFF
            self.push((r >> 8) & 0xFF)
            self.push(r & 0xFF)
            self.pc = a
            if obs is not None:
                obs.call(pc0, a, r + 1, self.s)
        elif mn == "RTS":
            sp = self.s
            self.pc = (self.pop() | (self.pop() << 8)) + 1 & 0xFFFF
            if obs is not None:
                obs.ret(pc0, self.pc, sp)
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


# Measured on MAME 0.264 (probes/dlitimes.lua, probes/cyclebudget.lua), and the
# 7800 Software Guide says the same: the NTSC frame is 263 lines of 113.5 CPU cycles
# (454 cycles of the 7.16 MHz clock), 29,850.5 cycles. MSTAT's VBLANK bit rises at
# raster 258 and falls at raster 16, 21 lines, and MARIA's zone 0 starts at raster 16.
# This simulator's frame starts where VBLANK falls, so a zone that starts L lines into
# the display starts L lines into the frame, and VBLANK rises 242 lines in.
# (It used to have 262 lines of 114.00, from an early count that could not tell the
# two apart, and fired each display interrupt at the END of its zone; MAME fires it
# at the START of the flagged zone -- see docs/hardware.md. PAL is from the Guide,
# 313 lines, and has NOT been measured.)
CYCLES_PER_LINE = 113.5
LINES = {"ntsc": 263, "pal": 313}
VBLANK_LINES = {"ntsc": 21, "pal": 21}
# The handler's first instruction runs 22-26 cycles into the line of the flagged
# zone (measured); 7 of those are the interrupt sequence, so the interrupt is
# taken about this far into the line.
DLI_LAG = 15


class Observer(object):
    """What a run can be watched with. Subclass and override what you need.

    The hooks are called from inside the CPU loop, so they should be cheap. Addresses
    are CPU addresses; the bank that was mapped at the time is `self.bus.bank`.
    """

    def attach(self, bus, cpu):
        self.bus, self.cpu = bus, cpu

    def frame_start(self, frame):
        pass

    def fetch(self, pc, opcode):
        """An instruction is about to run at `pc`."""

    def data_read(self, addr):
        """An instruction read `addr` as data (not an operand byte, not a fetch)."""

    def jump_indirect(self, pc, target):
        """`JMP (vector)` at `pc` went to `target`."""

    def call(self, pc, target, ret, sp):
        """`JSR` at `pc` to `target`; it will return to `ret` (stack pointer now `sp`)."""

    def ret(self, pc, target, sp):
        """`RTS` at `pc` went to `target`; `sp` was the stack pointer before the pops."""

    def bank_switch(self, addr, value, bank):
        """A store of `value` to `addr` selected `bank`."""

    def nmi(self):
        """A display-list interrupt is being taken."""

    def maria_write(self, reg, value):
        """A write to MARIA register `reg` ($20-$3F)."""

    def data_write(self, addr, value):
        """A store to RAM (including the stack) at `addr`."""

    def pointer(self, zp):
        """A `(zp),Y` or `(zp,X)` operand is about to read its pointer from `zp`."""


def load_handover(path):
    """The machine as a BIOS left it, from probes/handover.lua's log and its .ram.

    Returns {"regs": {a, x, y, s, p}, "ram": {address: byte}} for the cartridge's
    first instruction. Starting from this instead of from zeroed RAM and a bare
    CPU is what the real BIOS hands a game: OpenBIOS clears $2000-$27FF but
    leaves $FF in $40-$48 and parts of the stack and zero page, and a game that
    reads before it writes (Forth systems do) can tell.
    """
    import re
    text = open(path).read()
    m = re.search(r"A=([0-9A-F]{2}) X=([0-9A-F]{2}) Y=([0-9A-F]{2}) SP=([0-9A-F]{2}) P=([0-9A-F]{2})", text)
    if not m:
        raise ValueError("%s is not a handover.lua log" % path)
    a, x, y, sp, pp = [int(v, 16) for v in m.groups()]
    data = open(path + ".ram", "rb").read()
    if len(data) != 0x1C0 + 0x1000:
        raise ValueError("%s.ram should be $40-$1FF and $1800-$27FF, %d bytes; "
                         "this one is %d" % (path, 0x1C0 + 0x1000, len(data)))
    ram = {}
    for i, b in enumerate(data[:0x1C0]):
        ram[0x40 + i] = b
    for i, b in enumerate(data[0x1C0:]):
        ram[0x1800 + i] = b
    return {"regs": {"a": a, "x": x, "y": y, "s": sp, "p": pp}, "ram": ram}


def run(cart, frames, region="ntsc", drive=False, nmi=True, quiet=False,
        frame_nmi=False, steal=True, log=None, start_state=None, observer=None):
    """Execute the cartridge for `frames` frames, collecting audio writes.

    Interrupts follow the hardware: on the 7800 the ONLY thing that raises NMI
    is MARIA's display interrupt, taken as the flagged zone begins (the end of the
    zone before it) for every zone whose DLL entry
    has bit 7 set. There is no separate vertical-blank interrupt. A game that
    wants one puts a DLI on its last zone -- Asteroids does exactly that, with
    one DLI in seventeen zones -- while a game doing per-zone work raises many
    (Ms. Pac-Man: thirty).

    This is what an earlier version got wrong. It raised one NMI a frame at the
    end of the visible screen, which is right for a game like Asteroids by
    accident and wrong for everything that hangs work off zone boundaries.
    `frame_nmi=True` restores that behaviour for comparison.

    `observer`, if given, is told what the CPU does (see Observer): every fetch, data
    read, indirect jump, JSR/RTS, bank switch, interrupt and frame -- what the MAME
    probes in probes/ record, without MAME.

    `log`, if a list, receives ("nmi" or "vblank", frame, cycles into the
    frame) for every display interrupt and vertical-blank start, which is what
    the timing is checked against MAME with (probes/dlitimes.lua).
    """
    bus = Bus(cart, drive=drive)
    cpu = CPU(bus)
    if observer is not None:
        cpu.obs = bus.obs = observer
        observer.attach(bus, cpu)
    if start_state:
        for addr, b in start_state["ram"].items():
            bus.ram[fold(addr)] = b
        r = start_state["regs"]
        cpu.a, cpu.x, cpu.y, cpu.s = r["a"], r["x"], r["y"], r["s"]
        cpu.p = (r["p"] | U) & ~B & 0xFF
    lines = LINES[region]
    vb_line = lines - VBLANK_LINES[region]
    frame_cycles = lines * CYCLES_PER_LINE
    start = cpu.cycles

    for f in range(frames):
        bus.frame = f + 1
        if observer is not None:
            observer.frame_start(f + 1)
        # frames are scheduled from the start, not from wherever the last one
        # happened to end: an instruction that straddles the boundary does not
        # lengthen the next frame
        base = start + f * frame_cycles
        bus.vblank = False

        # The display list is rebuilt in RAM every frame by most games, so the
        # zone layout is read fresh rather than cached.
        events = []
        zones = bus.zones()
        if nmi:
            for line_end, dli, _cost, height in zones:
                # Every zone with the bit set raises its interrupt. An earlier
                # version fired only zones ending before the VBLANK line, which
                # was an invention -- MARIA walks the list and does not consult
                # anybody's notion of where vertical blank starts. Donkey Kong
                # puts its ONLY display interrupt on the last visible zone,
                # ending at line 248, and that filter silently dropped it: no
                # interrupt, a counter at $74 never decremented, and the game
                # spinning at $E24A for ever while looking like a cartridge
                # that simply makes no sound.
                first = line_end - height          # the flagged zone's first line
                if dli and first < lines:
                    events.append((base + first * CYCLES_PER_LINE + DLI_LAG, "dli"))
        # MARIA halts the 6502 while it draws, and it does so a scanline at a
        # time. Charging a whole zone's worth in one lump at the zone boundary
        # is not the same thing: it hands the CPU a burst of uninterrupted time
        # and then takes a large block away, which is enough to make a game
        # miss its own deadlines and write a garbage display-list pointer. So
        # the cost is spread across the zone's scanlines, where it belongs.
        if steal:
            top = 0
            for line_end, _dli, cost, _height in zones:
                n = max(1, line_end - top)
                line_cost = cost / float(n)
                for ln in range(top + 1, line_end + 1):
                    if ln <= vb_line:
                        events.append((base + ln * CYCLES_PER_LINE,
                                       ("steal", line_cost)))
                top = line_end
        events.append((base + vb_line * CYCLES_PER_LINE, "vblank"))
        if frame_nmi and nmi:
            events.append((base + vb_line * CYCLES_PER_LINE, "dli"))
        events.sort(key=lambda e: e[0])

        target = base + frame_cycles
        i = 0
        while cpu.cycles < target:
            while i < len(events) and cpu.cycles >= events[i][0]:
                kind = events[i][1]
                if kind == "vblank":
                    bus.vblank = True
                    if log is not None:
                        log.append(("vblank", f + 1, cpu.cycles - base))
                elif isinstance(kind, tuple):
                    cpu.cycles += kind[1]          # MARIA takes these
                else:
                    if log is not None:
                        log.append(("nmi", f + 1, cpu.cycles - base))
                    if observer is not None:
                        observer.nmi()
                    cpu.nmi()
                i += 1
            cpu.step()
            if cpu.jammed is not None:
                bus.jammed = cpu.jammed          # the program has stopped itself
            if bus.wsync:
                # WSYNC: the CPU is halted until the start of the next scanline
                bus.wsync = False
                n = int((cpu.cycles - base) // CYCLES_PER_LINE) + 1
                cpu.cycles = max(cpu.cycles, base + n * CYCLES_PER_LINE)
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


def states(rows):
    """A log as the ORDERED SEQUENCE OF REGISTER STATES it passes through.

    Consecutive rows carrying the same values are one state, not several: a
    note held for twenty frames is one event in the music and should count
    once, or a simulation that merely stalls on a correct value scores for
    every frame it fails to advance.
    """
    out = []
    for f, v in rows:
        if not out or out[-1][1] != v:
            out.append((f, v))
    return out


def compare(sim_rows, ref_rows):
    """How much of a known-good capture does the simulation reproduce?

    The obvious measure -- count rows that land on the same frame with the
    same values -- was the first one here and it is nearly useless. It
    conflates two independent questions and is chaotic besides: two versions
    of this simulator differing by 17 cycles a frame out of 2,350 scored 6.3%
    and 0.1%, which says nothing about either.

    The two questions are separated here.

      agreement  Of the states the simulation actually produced, how many are
                 the reference's, in the reference's order? This is "does the
                 player play the right notes".
      progress   How far into the reference did it get before it stopped or
                 wandered off? This is "does it keep playing".

    A simulation can be perfect on the first and hopeless on the second --
    Ballblazer is exactly that -- and the single number hid it.

    Matching is by longest common subsequence over the state values, so
    nothing depends on frame numbers, and timing is reported separately as
    the ratio of the gaps between matched events. Returns a dict.
    """
    import difflib
    sim, ref = states(sim_rows), states(ref_rows)
    if not sim or not ref:
        return {"agreement": 0.0, "progress": 0.0, "timing": None,
                "matched": 0, "sim_states": len(sim), "ref_states": len(ref)}
    sm = difflib.SequenceMatcher(a=[v for _, v in ref], b=[v for _, v in sim],
                                 autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    matched = sum(b.size for b in blocks)
    reach = max((b.a + b.size) for b in blocks) if blocks else 0

    # Timing, from the gaps between consecutive matched events in each log.
    pairs = []
    for b in blocks:
        for k in range(b.size):
            pairs.append((ref[b.a + k][0], sim[b.b + k][0]))
    ratios = []
    for i in range(1, len(pairs)):
        dr, ds = pairs[i][0] - pairs[i - 1][0], pairs[i][1] - pairs[i - 1][1]
        if dr > 0 and ds > 0:
            ratios.append(float(ds) / dr)
    ratios.sort()
    timing = ratios[len(ratios) // 2] if ratios else None
    return {"agreement": float(matched) / len(sim),
            "progress": float(reach) / len(ref),
            "timing": timing, "matched": matched,
            "sim_states": len(sim), "ref_states": len(ref)}


def window_hint(ref_rows, sim_rows, frames, slack=30):
    """How many frames of simulation cover what the reference does, if it ran longer.

    The simulator counts frames from the cartridge's reset and an emulator from
    power-on, so the same moment has a larger frame number in the capture; the
    difference is read off the first thing each played. A simulation that runs on
    past the end of the capture has its extra states scored as disagreement: the
    same run scored Midnight Mutants 43.9% over 1,200 frames and 94.3% over 882.
    Returns None when the two already span the same stretch (within `slack`).
    """
    if len(ref_rows) < 2 or len(sim_rows) < 2:
        return None
    offset = ref_rows[1][0] - sim_rows[1][0]
    covered = ref_rows[-1][0] - offset
    if frames > covered + slack and covered > 0:
        return covered + slack
    return None


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
                    help="tap fire now and then, for a game that waits at a title screen")
    ap.add_argument("--compare", metavar="REF.log",
                    help="compare against a known-good capture from "
                         "capture.py or probes/audio.lua. Compare LIKE FOR "
                         "LIKE: the capture must be a no-input run of the "
                         "same length, or you are marking the simulation "
                         "against music it was never given time to reach, or "
                         "against someone playing.")
    ap.add_argument("--dma-steal", dest="dma_steal", action="store_true",
                    default=True,
                    help="charge the CPU for MARIA's DMA, per scanline, using "
                         "the measured cost model including holey DMA (the "
                         "default; see the module docstring)")
    ap.add_argument("--no-dma-steal", dest="dma_steal", action="store_false",
                    help="give the CPU every cycle, as before: it runs loops "
                         "faster than hardware does")
    ap.add_argument("--frame-nmi", action="store_true",
                    help="raise one NMI a frame at end of visible instead of "
                         "following the display list. The old behaviour, kept "
                         "for comparison.")
    ap.add_argument("--no-nmi", action="store_true",
                    help="do not call the NMI handler each frame")
    ap.add_argument("--handover", metavar="LOG",
                    help="start from the machine as the BIOS left it: a log "
                         "from probes/handover.lua (with its .ram beside it), "
                         "instead of zeroed RAM and a bare CPU")
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

    start_state = None
    if args.handover:
        try:
            start_state = load_handover(args.handover)
        except (IOError, ValueError) as e:
            sys.stderr.write("sim: %s\n" % e)
            return 2
    bus = run(cart, frames, region, drive=args.drive, nmi=not args.no_nmi,
              frame_nmi=args.frame_nmi, steal=args.dma_steal,
              start_state=start_state)
    out = args.out or (os.path.splitext(args.rom)[0] + "-sim.log")
    n = write_log(bus, cart, out, region)
    print("%s -- %d frames simulated, %d audio writes, %d changed rows"
          % (os.path.basename(out), frames, len(bus.writes), n))

    if args.compare:
        sim_rows, ref_rows = read_log(out), read_log(args.compare)
        r = compare(sim_rows, ref_rows)
        print("compared with %s" % os.path.basename(args.compare))
        cover = window_hint(ref_rows, sim_rows, frames)
        if cover:
            print("  NOTE: the simulation ran %d frames but the capture ends about "
                  "%d frames in\n  (the capture counts from power-on, this from "
                  "reset): what it plays after that\n  is scored as disagreement. "
                  "Try --frames %d." % (frames, cover - 30, cover))
        print("  reference %d states, simulated %d"
              % (r["ref_states"], r["sim_states"]))
        print("  agreement  %5.1f%%  of what it played is the reference's, "
              "in order" % (100 * r["agreement"]))
        print("  progress   %5.1f%%  of the way through the reference before "
              "it stopped" % (100 * r["progress"]))
        if r["timing"] is not None:
            print("  timing     %5.2fx  gaps between matched events, against "
                  "the reference" % r["timing"])
        if r["sim_states"] < 10:
            print("  Too little output to judge: %d state%s. Whatever the "
                  "agreement figure says above, it is measuring almost "
                  "nothing." % (r["sim_states"],
                                "" if r["sim_states"] == 1 else "s"))
        elif r["agreement"] >= 0.9 and r["progress"] < 0.5:
            print("  Plays correctly and does not keep going: look for what "
                  "stops it, not for what it plays wrong.")
        elif r["agreement"] < 0.5:
            print("  NOT trustworthy for this cartridge. What it plays is not "
                  "what the game plays.")
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
