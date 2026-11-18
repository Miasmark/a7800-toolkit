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
                  the PREVIOUS zone (see "When a display interrupt fires" below)
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

### When a display interrupt fires

Bit 7 of zone N's DLL entry raises the NMI when MARIA finishes its DMA for the
**last line of zone N-1**: it is at that point that she fetches zone N's entry,
and the Guide puts the interrupt one 7.16 MHz clock after DMA shuts down
(Appendix 3; the DLL section says the same, "Interrupt after DMA on last line of
previous zone"). For the first zone the fetch, and so the interrupt, comes at
the end of vertical blank (raster 16). MAME with the fork's MARIA work
(Miasmark/mame-Atari8bit7800, `atari-0.289`: `src/mame/atari/maria.cpp`,
`startdma` and `vblank_edge`) does exactly this.

Line RAM is double-buffered: the DMA at the start of one raster builds the line
shown on the next. So, in raster terms, if zone N-1's last line is on screen at
raster R:

- the DMA for that line runs at the start of raster R-1, and the NMI arrives
  right after it, still in raster R-1;
- zone N's first line is built in raster R and shown in raster R+1.

What the handler writes then lands at different places:

- **Colours** apply the moment they are written, mid-line included. Written at
  once, or after one `WSYNC`, they still reach zone N-1's last line. After two
  `WSYNC`s they start on zone N's first. This was measured in MAME with the
  fork's MARIA timing (`pitfalls.md`, "A DLI's handler that runs into the next DLI").
- **What DMA reads** (`CHARBASE`, `CTRL`'s character width, and the like) has to
  be in place before zone N's first DMA. That DMA starts 8 cycles into raster R
  (the Guide says 7). A write made at once does that, and so does a write right
  after one `WSYNC`. This comes from that MAME's model; it has not been measured.

*Corrected 2026-11-08:* this table used to say the handler "runs before this
zone is drawn and its register writes apply to this zone". `making-a-game.md`
said the interrupt comes "at the end of that zone". Both are now replaced by the
account above.

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

**Two timings.** The table above is MAME 0.264's. The a7800 fork's MARIA (current MAME has it
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

Holey DMA is a large saving, not a rounding error -- two 20-byte objects over
192 scanlines went from 1,412 iterations a frame to 1,825 -- and it is exactly
the byte cost that disappears: the objects still pay for their display-list
entries. Which addresses are suppressed was measured, not looked up: with the
16K bit set a fetch is dropped when **address bit 12 is set** (`$D000` and
`$F000` free, `$C000` and `$E000` charged in full). The 8K bit made no
measurable difference anywhere in `$C000-$FFFF`.

*Corrected 2026-11-08:* that last sentence does not hold, at least in the
emulators the measurement ran on. MARIA's source in MAME 0.264 and in MAME with
the fork's MARIA work (`is_holey`, the same in both) decides each graphics fetch
from its address:

- with bit 6 (H16) set, a fetch is dropped when A15 and A12 are both set;
- with bit 5 (H8) set, a fetch is dropped when A15 and A11 are both set;
- with both bits set, either rule drops it.

The address checked is the one fetched, the graphics address plus the zone's
offset times 256. The Guide gives the same rules ("A12 high => data=0",
"A11 high => data=0"). So H8 drops `$C800-$CFFF`, `$D800-$DFFF`, `$E800-$EFFF`
and `$F800-$FFFF`. Centipede counts on that: its 8-line zones all set H8 (in
its DLL template at `$C198` they are `$27` headers, `$A7` with a DLI). The
measuring cartridge (`probes/dma-costcart.py`) keeps its graphics at
`$D000`. An 8-line zone there fetches only from `$D000-$D7FF`, where A11 is clear, which would explain why
nothing changed. H8 has not been re-measured since.

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

**After the lock, `$01` is the TIA's `VBLANK`.** INPTCTRL decodes the whole
TIA range `$00-$1F` until bit 0 is set (the Guide; MAME's `tia_w` does the
same), and writes go on to the TIA as well. Once it is locked, a write to `$01`
reaches only the TIA, where it is `VBLANK`: bit 7 grounds the paddle inputs
`INPT0-3`, bit 6 latches `INPT4/5`. So the zero most games write to `$01` right
after locking (`LDA #$17 / STA $01 / ... / LDA #0 / STA $01`), and Centipede's
display interrupts writing `$80` and `$00` there, set VBLANK; they do not
rewrite INPTCTRL. `disasm.py` names a store to `$01` `INPTCTRL` only in the
reset code, up to and including the lock, and `VBLANK` everywhere else.
(*Corrected 2026-11-08:* it used to name every one `INPTCTRL`.)

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
