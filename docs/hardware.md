# The 7800, as much as a disassembler needs

Enough to read code with. For exact timings and electrical detail go to the
MARIA specification; this is the working subset, with the parts that mislead
people called out.

## Memory map

| range | what |
|---|---|
| `$0000-$001F` | TIA (audio, and the console-control register) |
| `$0020-$003F` | MARIA |
| `$0040-$00FF` | RAM -- **mirror of `$2040-$20FF`** (the 6502's zero page) |
| `$0100-$011F` | TIA again |
| `$0120-$013F` | MARIA again |
| `$0140-$01FF` | RAM -- **mirror of `$2140-$21FF`** (the 6502's stack) |
| `$0280-$02FF` | RIOT I/O -- joysticks, console switches, timers |
| `$0480-$04FF` | RIOT RAM |
| `$1800-$27FF` | the 4K RAM block |
| `$2800-$3FFF` | mirrors of RAM |
| `$4000-$FFFF` | cartridge |

The two RAM mirrors matter constantly in practice: a game will build a structure
at `$2140` and hand MARIA the address `$0140`, and both are correct.

## MARIA

MARIA draws the screen by walking a two-level list, and it does so by DMA --
stealing cycles from the 6502. How much it steals depends on how much you ask it
to draw, which is why 7800 code is so often written around a scanline budget.

**Display List List (DLL)** -- one entry per zone, three bytes:

```
byte 0:  bit 7    display interrupt (DLI): it fires after DMA on the last line of
                  the PREVIOUS zone, so the handler runs before this zone is
                  drawn and its register writes apply to this zone
         bit 6    holey DMA for 16-line zones (odd 4K blocks read as zero)
         bit 5    holey DMA for 8-line zones (odd 2K blocks read as zero)
         bits 3-0 offset: scanlines in this zone, minus one
byte 1:  display list address, high
byte 2:  display list address, low
```

**Display List (DL)** -- one entry per object drawn in that zone, four bytes:

```
byte 0:  graphics address, low
byte 1:  palette (bits 7-5) | width (bits 4-0, as 32 minus the width)
byte 2:  graphics address, high
byte 3:  horizontal position
```

...or five, if byte 1's low five bits are zero:

```
byte 0:  graphics address, low
byte 1:  bit 7 write mode, bit 6 (set in every real header seen), bit 5 indirect
         (character) mode, bits 4-0 zero
byte 2:  graphics address, high
byte 3:  palette | width          <-- moved
byte 4:  horizontal position
```

A byte 1 of zero ends the list. See `pitfalls.md`: the four/five ambiguity is
the single most reliable way to get a confident wrong answer out of a 7800 ROM.

### Registers

| addr | name | notes |
|---|---|---|
| `$20` | `BACKGRND` | border and background colour |
| `$21-$23`, `$25-$27`, `$29-$2B`, `$2D-$2F`, `$31-$33`, `$35-$37`, `$39-$3B`, `$3D-$3F` | `P0C1`..`P7C3` | eight palettes of three colours (write-only; high nibble hue, low nibble luminance); colour 0 is the background. The gaps are WSYNC, MSTAT, DPPH, DPPL, CHARBASE, OFFSET and CTRL |
| `$24` | `WSYNC` | write to stall until the next scanline |
| `$28` | `MSTAT` | read: bit 7 set during vertical blank |
| `$2C`, `$30` | `DPPH`, `DPPL` | where the DLL is. **Write-only** |
| `$34` | `CHARBASE` | high byte for character (indirect) mode |
| `$38` | `OFFSET` | |
| `$3C` | `CTRL` | write-only: bit 7 colour kill, bits 6-5 DMA (`10` normal, `11` off; `00`/`01` are test modes, do not use), bit 4 two-byte characters, bit 3 border (black or background), bit 2 kangaroo (no transparency), bits 1-0 read mode. Bits 4 and 1-0 and the DMA value `10` were checked against MAME here; the rest are from the Guide |

`DPPH`/`DPPL` being write-only is why finding a running game's display list
needs a write tap rather than a memory read -- see `probes/dumpdl.lua`.

### Palettes

Eight palettes, three colours each, plus one shared background. A display list
entry names which palette its object uses, so the same graphics drawn twice with
different palette numbers costs nothing extra.

A colour byte is hue in the high nibble, luminance in the low. Hue 0 is
greyscale. `tools/palette.py` converts to RGB well enough to read artwork by;
real output depends on the console and the television.

### What MARIA costs

MARIA draws by DMA and halts the 6502 while it does, so the frame budget is a
function of what is on screen. These are **measured**, not quoted: a cartridge
that spins a fixed-cost loop for the whole visible period and publishes the
iteration count, built once per display-list shape with nothing else changed.
`tools/dmabudget.py` implements the model and explains the method; the loop was
calibrated against extra NOPs (14.020 cycles/iteration fitted, 14.016 counted
by hand) and the model was checked by predicting four shapes it had not seen,
all within 0.3%.

NTSC: **263 scanlines/frame, 113.5 CPU cycles each (454 cycles of the 7.16 MHz
clock), 29,850.5 cycles/frame, 59.96 frames/second.** That is what MAME runs: a
cartridge that never turns MARIA on and spins in `JMP *` executes 29,850 cycles a
frame, block after block. (This page used to say 262 lines of 114.00, 29,868, and
`dmabudget.py`'s calibration above says the same; the 17.5-cycle difference is
0.06% and below what that calibration could resolve, but the 263 x 113.5 figure is
also the one the 7800 Software Guide gives, and it is the one that adds up.) Of
those lines, about 21 are VBLANK, where DMA is off and the CPU has everything; the
Guide puts the visible area at rasters 16-258 and the safe area at 41-233.
PAL, from the Guide and not measured here: 313 rasters, 35,525.5 cycles.

| what | CPU cycles | colour clocks |
|---|---|---|
| every scanline inside a zone, even an empty one | 5.63 | 22.5 |
| a zone boundary (the DLL fetch) | 1.68 | 6.7 |
| one 4-byte DL entry, per scanline | 2.08 | 8.3 |
| a 5-byte entry instead, per scanline | +0.48 | +1.9 |
| one graphics byte, per scanline | 0.74 | 3.0 |
| one character in character mode, per scanline | 1.49 or 2.25 | 6 or 9 |

The graphics byte landing on 3.0 colour clocks -- the documented figure -- is
the main reason to trust the rest of the table.

Two more costs, measured later and now in `dmabudget.py`:

| what | CPU cycles |
|---|---|
| one display interrupt (MARIA's signal plus the 6502's entry and exit) | 16.6 |
| a graphics byte in a region **holey DMA** suppresses | 0 |

**Three timings.** The table above is MAME 0.264's. The a7800 fork's MARIA (current MAME has it
once its work is ported, docs/emulation.md) charges slightly differently, and
`tools/dmameasure.py` re-measures it on any emulator you point it at: 45 display shapes, fitted
by least squares, worst residual 0.85%.

| what | CPU cycles, MAME 0.264 | CPU cycles, the fork |
|---|---|---|
| every scanline inside a zone | 5.63 | 5.62 |
| a zone boundary | 1.68 | 1.89 |
| one 4-byte DL entry, per scanline | 2.08 | 1.99 |
| one graphics byte, per scanline | 0.74 | 0.755 |
| one display interrupt | 16.6 | 17.4 |
| a **holey** zone, per scanline | 0 | +0.99, and +0.51 per object |
| **most one scanline can cost** | 108.62 | 108.62 |

The fork charges 3 colour clocks for the first hole in each object's graphics, so under it a
holey zone is no longer exactly the byte cost. Its other changes (a 430-clock DMA limit, 8 clocks of
last-line shutdown) show up as the small differences above. A line of DMA stops at the limit: eight
16-byte objects in every zone cost 20,855 cycles a frame on both machines, where the linear model
says 22,583 -- past the limit the 6502 keeps about 4.9 cycles of the line and the rest of what you
asked MARIA to draw is not drawn. `dmabudget.py` applies the cap per scanline and says which zones
reach it. MAME with the fork's MARIA work and the fork itself agree on all 14 shapes compared,
holey and interrupt shapes included. (`dmabudget.py --timing mame0264` is the first set.)

**The 7800 Software Guide's timing, revised in October 2026** from hardware measurements
(Appendix 3, "DMA Timing"), is the third set and `dmabudget.py`'s default (`--timing guide`).
The patched MAME (Miasmark/mame-Atari8bit7800: `master`, `atari-dev`, `atari-0.289`) follows it,
and `dmameasure.py --fit` measured it there over 49 shapes: every one within 1% but single-wide
holey characters, 3.1%, where MAME rounds each line's DMA to whole CPU cycles. Three things
changed from the fork's:

| what | the fork | the Guide, 2026 |
|---|---|---|
| one display interrupt | 17.4 cycles (17 colour clocks of DMA, then the 6502's entry and exit) | 12.75 (the 6502's entry and exit only: a DLI costs no DMA time) |
| a holey character object, per scanline | the same as a direct one (3 colour clocks) | 6 colour clocks single-wide, 9 double-wide: `HOLE_CHAR`, 0.756 cycles more per byte a character |
| the first DMA of a frame | -- | 8 colour clocks (2 CPU cycles) more, every frame with DMA on |

The 2 cycles a frame are inside the rounding of the per-line constant and are not modelled
separately. The other constants came out within a few hundredths of the fork's (per scanline
5.64, per zone 1.71, per 4-byte entry 2.00, per graphics byte 0.755; a holey zone 0.81 a scanline
and 0.57 per object). Re-fitting the old build the same way gave the fork's set back (worst
residual 0.70%), which is what says the method holds.

**On hardware.** The counting cartridge reports through RAM, which only an emulator's Lua can
read. `tools/dmacart.py build` makes the same shapes as cartridges that draw the count on screen,
signed for an NTSC console: a 16-line zone under the measured area holds the count now, the
lowest and the highest seen. The digits are 2-byte objects into a fixed font, and the cartridge
only changes which glyph each points at, so the zone costs the same whatever it shows: 46 loop
passes in every shape, checked against the plain cartridge (character mode, holey and DLI shapes
included), so costs against the empty baseline are the plain cartridge's to within a pass. Read
the numbers off the screen (a photo will do), put them in a file, and `dmacart.py compare`
prints them beside MAME's (`dmacart.py mame`).

**Measured on a real NTSC 7800 (October 2026),** started through Atari's BIOS from a flash
cartridge, against the openFPGA 7800 core on an Analogue Pocket and MAME before and after the
two fixes these numbers led to. The count is the one on screen, or its lowest and highest where
they differ; the cost is the baseline's count less the shape's, times 14.0156:

| # | shape | 7800 | Pocket | MAME before | MAME after | cost on the 7800 |
|---|---|---|---|---|---|---|
| 00 | empty zones (baseline) | 1837 | 1836 | 1836 | 1835-1836 | -- |
| 01 | 2 x 8b | 1618 | 1617 | 1615-1616 | 1616-1617 | 3,069 |
| 02 | 2 x 1b | 1756 | 1755 | 1753-1754 | 1754 | 1,135 |
| 03 | 2 x 16b | 1453-1454 | 1452-1453 | 1450-1451 | 1452 | 5,375 |
| 04 | 2 x 8b, 5-byte | 1604 | 1603 | 1602 | 1603 | 3,266 |
| 05 | 2 x 4 chars, 1 b/char | 1604 | 1603 | 1602 | 1603 | 3,266 |
| 06 | 2 x 4 chars, 2 b/char | 1522 | 1521 | 1519-1520 | 1521 | 4,415 |
| 07 | 2 x 20b, 8-line zones | 1369-1370 | 1368-1369 | 1366 | 1368 | 6,552 |
| 08 | 2 x 8b, 8-line zones | 1616 | 1615 | 1614 | 1615 | 3,097 |
| 09 | 2 x 20b holey | 1755 | 1754 | 1752 | 1752 | 1,149 |
| 10 | 2 x 8b holey | 1755 | 1754 | 1752 | 1752 | 1,149 |
| 11 | 2 x 20b + DLI | 1347-1348 | 1346 | 1343-1344 | 1346 | 6,861 |
| 12 | 2 x 8b + DLI | 1594 | 1593 | 1591-1592 | 1592-1593 | 3,406 |
| 13 | 6 x 16b (hits the limit) | 686-687 | 685-686 | 679 | 685 | 16,125 |
| 14 | 8 x 16b (over the limit) | 426-427 | 418-419 | 426-427 | 426-427 | 19,769 |

What they show:

- **The cost table is right.** Under the limit, the console's cost is the Guide's per-line
  colour clocks rounded up to whole CPU cycles on each line (cart 01: 3,072 predicted, 3,069
  read; 07: 6,552 and 6,552; 13: 16,128 and 16,125). Rounding down instead is worse where a
  line's DMA is not a whole number of CPU cycles (carts 02, 09 and 10).
- **MAME drew the first zone a line too tall:** it stepped the line counter only from the line
  after reading the display list list, so the first line of the first zone was drawn twice. That
  was one line of the shape's DMA a frame too many (14 to 88 cycles), and the picture a line low.
- **Past the limit the 6502 waits 107 cycles from the start of DMA,** half a cycle into the next
  line. MAME let it go when the next line started (112 cycles a frame cheap); the Pocket charges
  about 100 a frame too much here, and matches the console everywhere else.
- **One pass a frame is unexplained:** the console counts one more than the Pocket and both
  MAMEs on every shape, the empty baseline too, so it is a fixed 7 to 21 cycles a frame (where
  the console moves the VBLANK flag within the line, or a small cost once a frame) that these
  carts cannot place. Costs, which subtract the baseline, are unaffected.

With both fixes MAME's costs are the console's to within 7 cycles a frame on 12 of the 15 shapes
and 21 on the other three (the two holey ones and the one over the limit). `dmabudget.py`'s
constants were fitted to MAME before these fixes, not to these numbers.

**Artifact colour (November 2026).** A 320 pixel is half a cycle of the colour carrier, so through composite
or RF a 320-mode pattern shows colour no palette entry has (Tower Toppler's tower). `tools/artifacttest.py` builds
two test cartridges. On a console, `artifacttest.a78`'s alternate-pixel `$AA` in white is purple and `$55` green:
MARIA's pixels sit at artifact phase 1 against the carrier, MiSTer's core's and MAME's default. `artifacthues.a78`
shows every hue's `$AA` and `$55` at luminance 4, 8 and C. Patterns in a colour do not decode as MiSTer's composite
model has them: at luminance 4 they turn the other way from it (`$AA` in blue `$84` nearly the solid blue, `$55`
magenta, where the model gives dark violet and dark teal), and they agree more as the luminance rises. A level on
the COLOR pin while a colour is selected, 0.08 V above none, fits that: a solid colour's chroma resonator removes
it, but switched every pixel it is a carrier-rate step. Fitted to two photos, it brings the patterns' hues
(measured against each row's solid block, so the camera's colour cast cancels) from a median 23 degrees off to
9 at luminance 4 and from 30 to 14 at luminance 8. Luminance C could not be judged: the photos wash its solid
colours out. MAME's Composite Video has it as Composite Colour Pin Offset, on by default.

**Where in the line things happen (November 2026).** The openFPGA 7800 core's test carts
(`sim/mstat/` in that repository), which read the same on a console, place the line's events
against WSYNC. Counted in MARIA clocks (454 a line, 4 a CPU cycle) from the line reset, where
WSYNC lets go: MSTAT's vblank bit changes 42 clocks later, on the frame's line 16 and line 258;
the end of vblank fetches the first zone as 16 clocks of DMA of its own (the Guide's "first DMA
of a frame"; it is 8 more there); and a line's DMA starts at the start of horizontal blank, 28
clocks after the reset, the CPU stopping a cycle later. The first line's DMA is the line after the
zone fetch. A line over the limit holds the CPU 107 cycles, 108 on a zone's last line. And a CPU
read of a colour register in a visible line puts that register's colour out at the beam for 2
clocks (a write repeats the previous pixel for 1), so a loop that reads one leaves a trail of
dots. The patched MAME follows all of it since November 2026: `mstat_test.a78` reads 1960 1960
/ 21 105 / 242 105 there as on the console, `dma_diff.a78`'s cost of each of the 263 lines is the
core's on every line, and the series above reads as in the "MAME after" column but for 00 (1836),
03 (1452-1453), 07 (1368-1369) and 14 (426).

Holey DMA is a large saving, not a rounding error -- two 20-byte objects over
192 scanlines went from 1,412 iterations a frame to 1,825 -- and it is exactly
the byte cost that disappears: the objects still pay for their display-list
entries. Which addresses are suppressed was measured, not looked up: with the
16K bit set a fetch is dropped when **address bit 12 is set** (`$D000` and
`$F000` free, `$C000` and `$E000` charged in full). The 8K bit made no
measurable difference anywhere in `$C000-$FFFF`.

Two things that cost nothing extra, also measured, because both looked like
plausible suspects for a discrepancy that turned out not to exist: **zone
height** (8-scanline and 16-scanline zones with the same objects agree to
0.2%) and **where the graphics live** (RAM and ROM measure identically).

A character costs one fetch for the character list plus its own data bytes, so
it is two fetches with `CTRL` bit 4 clear and three with it set. **Bit 4 set
means TWO bytes per character, not one.** This file and `a7800.py` both had it
the other way round until it was measured; the giveaway was that setting the
bit made drawing more expensive. Both shipping games in this series clear it.

For scale, Asteroids mid-play -- rocks, ship, score -- spends about 10% of its
frame on DMA. A full screen of 16-byte-wide objects can pass 60%.

### Holey DMA

With holey DMA on, graphics reads from the odd 4K blocks (16-line zones, bit 6) or
odd 2K blocks (8-line zones, bit 5) return zero instead of fetching, and it only
works above `$8000`. Games use it so one display list entry can span a region where most of the
graphics are absent, without paying DMA for the empty parts. If graphics vanish
in a region that looks correct in the ROM, check the zone's holey bits.

## Character (indirect) mode

Set the indirect bit in a five-byte entry and the "graphics address" points at a
list of *character numbers* instead of pixels. MARIA forms each character's
address as:

```
((CHARBASE + line) << 8) | character_number
```

So a character set is stored line-planar, exactly like sprites: page
`CHARBASE+0` holds line 0 of all 256 characters, page `CHARBASE+1` holds line 1,
and so on. This is what makes text cheap -- one byte per character on screen --
and it is why a game's alphabet is often nowhere near ASCII. Render the pages
with `tools/gfx.py` and read the mapping off the image.

## TIA

On the 7800 the TIA is used almost entirely for audio: `AUDC0/1`, `AUDF0/1`,
`AUDV0/1` (control, frequency, volume). Two channels, and cartridges that wanted
more shipped a POKEY -- the header flags say where it is mapped.

`INPTCTRL` at `$0001` is the console-control register. Games write it during
startup to switch out of 2600 mode and enable MARIA; you will see it in the
first few instructions after RESET, which is how the reset probe in
`cart.py` recognises real startup code. Write-only; per the 7800 Software Guide
(not measured here):

| bit | name | meaning |
|---|---|---|
| 0 | lock | once set, the register cannot be changed until power-off |
| 1 | MARIA enable | enables MARIA and the system RAM |
| 2 | EXT | 0 = the BIOS is mapped at `$8000-$FFFF`, 1 = the cartridge is |
| 3 | TIA-EN | 1 = TIA video and two-button mode off, 0 = MARIA video |

The tools use bit 0 as the sign that the BIOS has handed over (`exectrace.lua`,
`romcoverage.lua`). Accesses to the TIA (`$00-$1F`) and the RIOT (`$280-$2FF`) slow
the CPU to 1.19 MHz for that cycle -- about half a cycle each, which
`cyclebudget.lua` charges as its "slow" figure.

## POKEY, when a cartridge has one

The console has no POKEY. It is an extra chip *on the cartridge*, fitted by
games that wanted more than the TIA's two channels, and the `.a78` header says
whether one is present and where it answers.

Three placements occur, and **only one of them touches the address map**:

| flag | base | in the way of anything? |
|---|---|---|
| `$0001` | `$4000` | inside the cartridge address space |
| `$0040` | `$0450` | no -- unused space below the RIOT RAM |
| `$8000` | `$0800` | no -- unused space below the RAM block |

For the `$4000` case the chip claims `$4000-$400F`, which is cartridge address
space. In practice it never collides, and the library says why: every ordinary
cartridge declaring a POKEY at `$4000` is either **16K or 32K linear** -- so its
ROM starts at `$C000` or `$8000` and never reaches down that far -- or a
**SuperGame with nothing else mapped at `$4000`**, which is exactly the slot the
header's other flags would have filled with RAM or a bank. Of 1,309 images, two
declare a POKEY at `$4000` *and* ROM there; both are prototypes with confused
headers, and `cart.py` warns rather than silently reporting a layout it cannot
justify.

### Registers

Sixteen of them, and **reading one is a different register from writing it**:

| offset | write | read |
|---|---|---|
| `$0`-`$7` | `AUDF1`, `AUDC1` ... `AUDF4`, `AUDC4` | `POT0`-`POT7` |
| `$8` | `AUDCTL` | `ALLPOT` |
| `$9` | `STIMER` | `KBCODE` |
| `$A` | `SKRES` | `RANDOM` |
| `$D` | `SEROUT` | `SERIN` |
| `$E` | `IRQEN` | `IRQST` |
| `$F` | `SKCTL` | `SKSTAT` |

The audio pairs alternate -- frequency at even offsets, control/volume at odd --
which is how POKEY usage announces itself in a write trace: heavy traffic on
`$x001/$x003/$x005/$x007` against lighter traffic on the even ones.

The read/write split is not a curiosity. Ballblazer reads `$400A` a couple of
hundred times a second and never writes it: that is `RANDOM`, the hardware
random number generator, and the same address on write would be `SKRES`. A
disassembler knows which side an instruction is on, so it can pick the right
name -- `disasm.py` does, and `LDA RANDOM` next to `STA SKCTL` is a much better
listing than two references to `$400A`.

## RIOT

Joysticks and console switches at `$0280-$0283`, timers at `$0284`/`$0294-$0297`.
Reading `SWCHA` gives both joystick directions; the fire buttons come from the
TIA `INPT` registers, which catches people out.

## PAL and NTSC

PAL machines run 50 fields per second against NTSC's 60, and have more
scanlines. Any game that measures time in frames therefore runs slower on PAL
unless it was retimed, and plenty of European releases were not: a music track
whose durations are frame counts plays at 83.3% speed.

Use `a7800p` in MAME for PAL. Expect PAL and NTSC images of the same game to
differ substantially -- not just a flag, but relocated code -- so treat them as
two targets that share a design rather than one ROM with a switch.
