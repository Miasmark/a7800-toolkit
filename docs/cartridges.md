# Cartridges: the header, the mappers, and what to distrust

## The `.a78` header

128 bytes on the front of the image. Not part of the cartridge -- strip it
before doing anything that cares about ROM contents, including building patches.

| offset | meaning |
|---|---|
| 0 | header version |
| 1-9 | `ATARI7800` |
| 17-48 | title |
| 49-52 | ROM size, big-endian, excluding the header |
| 53-54 | cart type flags, big-endian |
| 55-56 | controller 1, controller 2 |
| 57 | region: bit 0 set means PAL |
| 58 | save device: bit 0 high score cart, bit 1 SaveKey |
| 100-127 | footer, usually `ACTUAL CART DATA STARTS HERE` |

A headerless dump is perfectly usable; you lose the mapper declaration and have
to infer it from size, which is reliable for anything 48K or smaller.

## Mapper flags, as the cartridges actually use them

These assignments were checked against 1,309 retail and homebrew images by
picking out cartridges whose mapper is independently known:

| bit | meaning | confirmed by |
|---|---|---|
| `$0001` | POKEY at `$4000` | Ballblazer, Commando |
| `$0002` | SuperGame bank switching | Midnight Mutants, Xenophobe |
| `$0004` | on-cart RAM at `$4000` | Impossible Mission |
| `$0008` | an extra ROM bank at `$4000` | Crossbow, Alien Brigade (144K) |
| `$0010` | bank 6 at `$4000` | Midnight Mutants, Xenophobe |
| `$0040` | POKEY at `$0450` | images that name it |
| `$0100` | Activision banking | Double Dragon, Rampage |
| `$0200` | Absolute banking | F-18 Hornet |
| `$0400` | a **second** POKEY at `$0440` | nine images, every one named "POKEY 440" |
| `$0800` | YM2151 audio -- **not a mapper** | 45 images, all YM demos |
| `$2000` | Bankset | the Bankset test series |
| `$8000` | POKEY at `$0800` | images that name it |

Bits 5, 7 and 12 were not pinned down; `cart.py` labels them unconfirmed
rather than inventing a meaning. Bit 10 was: every image that sets it names
a POKEY at `$0440` in its own title, and seven of the nine are explicitly
"Dual POKEY 440 450" demos, so it is a second POKEY alongside the `$0450`
one rather than a mapper bit.

**Do not trust the widely-copied bit lists above bit 6.** Several put Activision
at `$0200` and Absolute at `$0400`, which the images contradict. And `$0800`
being an audio flag rather than a mapper matters: treat it as a mapper and you
refuse 45 ordinary cartridges.

## The mappers

### Linear

No banking. The image sits at the top of the address space: 48K reaches down to
`$4000`, 32K to `$8000`, 16K to `$C000`, and so on. **48K is the ceiling** --
without a mapper the 7800 cannot see more, so an unbanked image larger than that
is a mis-declared header, not a 96K linear cart.

This is much the most common arrangement: over a thousand of the 1,309 images
surveyed are linear.

### SuperGame

The standard banked layout. 16K banks; `$8000-$BFFF` is a window showing any of
them, and a write *anywhere* in `$8000-$FFFF` selects which. The last bank is
fixed at `$C000-$FFFF`.

**The switch is as wide as the bank count, not a fixed three bits.** A 128K cart
has eight banks and the familiar `data & 7`; a 512K cart has thirty-two and uses
`data & 31`. Confirmed on a 512K homebrew by poking values and reading the
window back: 32 and 64 both select bank 0, 63 and 255 both select bank 31. So
the rule is `value % banks`, and hardcoding `& 7` silently mis-resolves every
bank above 7 on a large cart.

Because the bank is chosen by the value written, a disassembler has to carry
constants through the code to resolve `LDA #3 / STA $8000` -- and every site
where the value was not a tracked constant is a hole in the trace that has to be
pinned by hand.

Three things can sit at `$4000-$7FFF`, and the header says which: nothing,
on-cart RAM (`$0004`), the second-to-last bank (`$0010`), or an extra ROM bank
(`$0008`, the 144K arrangement and its 272K and 528K relatives). Getting this wrong is
quiet -- the code still disassembles, it just disassembles the wrong bank.

The `$0008` layout is **measured**, in MAME 0.264, on Alien Brigade (144K), Lunar
Patrol (272K), Kinetescape and Drone Patrol (528K): `$4000` shows **file bank 0**, `$C000`
shows the last bank, and a write of value *v* to the window shows **file bank *v* + 1**,
so the window covers file banks 1 up to the last (`b1`..`b(n-1)`; `f0` and `f(n-1)` are
the fixed ones, and `b(n-1)` is the same bytes as `f(n-1)`). The fork's `a78_rom_sg9_device` says the same: `$4000` is file bank 0, the window is
`(value & (n-2)) + 1` -- eight values for nine banks, *n*-1 of them in general -- and the last bank
is at `$C000`, so the top value reaches the last bank rather than wrapping. An earlier version of this layout put bank *n*-2 at `$4000` and
numbered the window from 0; none of those images ran in the simulator, and all of them do
now.

The rule that the *last* bank is the one at `$C000` is not an assumption: across
207 SuperGame images where the reset vector could be matched to real startup
code, 199 put the last bank there, and the eight exceptions look like misfires
of the heuristic. `Cart.probe_fixed_high()` runs that check on demand -- worth
doing on any new image, since it also catches overdumps, where the image has
been padded and the real last bank is not the last one in the file.

### Absolute

F-18 Hornet. Two selectable 16K banks low, 32K fixed high, selected by a write
to `$8000`. **Verified against the NTSC image running under MAME**, three ways:

* Tapping the whole cartridge address space during play shows writes to exactly
  one address, `$8000`, carrying exactly two values, `$01` and `$02`.
* Poking values and reading `$4000` back shows `$01` selecting bank 0 and `$02`
  selecting bank 1. Out-of-range values follow the low bit -- odd picks bank 0,
  even picks bank 1 -- rather than being ignored, though the cartridge itself
  never writes anything else, so only 1 and 2 are established behaviour.
* Reading `$8000` and `$C000` back shows banks 2 and 3, confirming the fixed
  high region is the last 32K.

The disassembler then reproduces the image byte-for-byte from the resulting
listings, with both low banks reached and every switch site resolved.

### Not implemented

* **Activision banking** (Double Dragon, Rampage) -- 8K granularity and a switch
  at `$FF80`, unlike everything else.
* **Bankset** -- two parallel sets of banks, one seen by the CPU and one by
  MARIA, so a single address holds two different bytes depending on who is
  asking. This breaks the assumption that an address names a byte, which most
  disassembly tooling is built on.
* **SOUPER** (Rikki & Vikki) -- its own mapper plus extra hardware.

`cart.py` recognises all three and refuses them with an explanation rather than
producing a plausible, wrong layout.

## POKEY

`$0001` puts it at `$4000`, `$0040` at `$0450`, `$8000` at `$0800`. Only the
`$4000` placement is inside cartridge address space, and it does not actually
collide with ROM in any ordinary image -- see `hardware.md` for why, and for the
register map, whose read and write sides differ.

Two prototypes in the library declare a POKEY at `$4000` *and* ROM there.
`cart.py` lays them out and warns, rather than choosing one silently.

## Coverage

*Historical figures.* Over the 1,309-image library, `cart.py` first laid out
1,284 and refused 25 (Bankset, Activision, SOUPER, and two images whose declared
size cannot be mapped at all), with no crashes. Activision, Bankset and 52K are
now supported (next section), so only the "Still unsupported" list at the end of
this file remains refused: four images in all. The larger library the README
quotes has not been re-tallied here. Three layouts were actively disputed by the
reset probe -- two files whose names say "Overdump" and one prototype, which is
the probe doing its job.

# Activision, Bankset, and 52K

Three formats that took the library's unsupported count from 50 images to 4.

## Activision (Double Dragon, Rampage) — 16 images

The odd one out. Every other 7800 mapper thinks in 16K banks; this one scatters
four fixed **8K** blocks around a 16K window, and the blocks are not in address
order in the file:

| CPU | file | 8K block |
|---|---|---|
| `$4000-$5FFF` | `$1A000` | 13 |
| `$6000-$7FFF` | `$18000` | 12 |
| `$8000-$9FFF` | `$1E000` | 15 |
| `$A000-$DFFF` | the window, eight 16K banks | |
| `$E000-$FFFF` | `$1C000` | 14 |

`$E000` is block 14 and `$8000` is block 15 — reversed against what address
order suggests. Verified by reading memory out of a running a7800 and matching
the bytes back to file offsets; every region agreed, and the vectors read
`NMI $4000 / RESET $FF74 / IRQ $4000`.

A write to **`$FF80-$FF8F`** selects the bank from the low three bits of the
*address*, not the value: `STA $FF83` selects bank 3 whatever is in A. Unlike
every other mapper here, where the value chooses.

**The "(OM)" dumps have blocks 14 and 15 swapped**, which puts the vectors in a
block full of zeros. That is the image being wrong, not the layout — a7800
cannot boot them either. `cart.py` says so rather than laying them out quietly.

## Bankset — 28 images

A bankset cartridge carries **two parallel sets of banks at the same
addresses**: one the CPU sees, one MARIA sees. A single address holds two
different bytes depending on which chip is asking, so no single address map can
describe it. Open the side you want:

```python
Cart("game.a78")                  # what the CPU executes
Cart("game.a78", side="maria")    # what MARIA fetches -- the artwork
```

The file is two halves, and the names say so: `Bankset Test - 2x32K`,
`2x128K`. **The first half is the CPU's**, confirmed against a7800 — CPU
`$8000` reads file `$0000` and CPU `$9000` reads `$1000`, and the reset vector
`$F524` read out of the running machine matches what this lays out.

Each half is then an ordinary image: flat if the header has no SuperGame bit,
SuperGame if it does. So `2x32K` is two linear 32K ROMs and `2x128K` is two
SuperGame 128K ones, and every other flag — POKEY at `$4000` or `$0800`, banked
RAM — applies per side as usual.

This is worth knowing when hunting for artwork: on a bankset cartridge the
graphics are not hiding in an unreached corner of the CPU's ROM. They are in
the other half, at addresses the CPU never reads.

### What the tools do with a bankset cartridge

* **`disasm.py` and `build.py`** list the CPU's half as always and MARIA's half as
  data, `m<space>.asm` beside the others (`mrom.asm`, `mb3.asm`, `mf7.asm`), so the
  image rebuilds byte for byte. Nothing is traced in MARIA's half: the CPU never runs it.
* **`sim.py`** runs the CPU's half, which is what executes. MARIA's reads of display
  lists in ROM are not modelled (the sim only walks lists in RAM), bankset or not.
* **`census.py`** reports both halves: MARIA's spaces are `m` + the CPU name. What MARIA
  was seen fetching is marked in MARIA's half, not the CPU's, and no annotation
  suggestions are made for MARIA's half.
* **`firstlook.py`** rebuilds the screen from MARIA's half (`Cart.for_maria()`).
* `gfx.py`, `spriteedit.py` and the workbench's sprite editor take `--side`.

**Established from the `a7800` fork's source, not from a run.** Which bank of MARIA's half
is in view for a given bank register write is the same number as the CPU's. The fork's
`src/devices/bus/a7800/bankset.cpp` says so in code: every `read_40xx` takes the one `m_bank`
the CPU wrote and, when MARIA is the one reading (`m_dmaactive`), adds the size of the first
half to the file offset. The flat forms (2x32K, 2x48K, 2x52K) are the same address map on
each half, the 52K ones starting at `$3000`. `t_bankset_fork_model` in `tools/selftest.py`
transcribes those reads and compares them with `Cart` for the CPU's and MARIA's half in
every window bank on synthetic images of five forms (3,834 reads, no disagreement), and the
same comparison on the library's 14 bankset images found none. Three details come from that
code and are modelled: with bank RAM (`$4000` header bit) the 16K at `$4000` is RAM, one for
each chip; with POKEY at `$800` and no RAM it reads `$FF`; otherwise the second-last bank is
fixed there, whatever the SuperGame low-memory bits say.

**Not modelled:** the fork gives each chip its own 16K of bank RAM at `$4000`, and the CPU's
writes to `$C000-$FFFF` land in *MARIA's* RAM. The simulator does not do this, so a bankset
game that builds its artwork in MARIA's RAM (Bubble Bobble's header, `$E002`, has bank RAM)
draws from RAM the simulator never fills. **Not run:** nothing here has watched a bankset
image execute on the fork -- it was not built or installed for this check -- so this is the
fork's code, read, not the fork's behaviour, observed. **Mainline MAME 0.264 cannot be
asked:** it answers `Unsupported mapper, please contact MAMEdevs` for `$2000`-flagged images
(the header values `$2000`, `$2812`, `$E002`, `$2012` all do), and the BIOS is left running
its own built-in game. That game is KILOPARSEC -- the title screen seen "running" for
`Bubble Bobble (v75a)` in an earlier check was OpenBIOS's, not the cartridge's, which is also why
reads tapped from `$C000` matched no half of the file. `tools/mamecheck.py` tells these apart
(it compares the vectors MAME is executing with the cartridge's own) and prints MAME's
message. Treat every statement about a bankset image that rests on mainline MAME 0.264 as
void. `capture.py` recognises the fork and uses debugger watchpoints; `mamecheck.py` does not
yet drive it.

## 52K

52K is exactly `$3000-$FFFF` — four kilobytes below where a 48K image starts,
overlaying what is normally a RAM mirror. The arithmetic is exact
(53,248 = `$10000 - $3000`) and the vectors come out plausible on all six
images, but **no image in the library boots far enough under emulation to
confirm it by reading memory back**, so this one is inferred rather than
established. `describe()` says so on the mapper line.

## Still unsupported

* **SOUPER** (Rikki and Vikki, 2 images) — its own mapper plus extra hardware.
* **512K flat** (SN Cart demos, 2 images) — declares no mapper bits at a size
  that cannot be linear.
