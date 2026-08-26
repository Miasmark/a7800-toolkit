# Traps

Every one of these produced a confidently wrong answer during a real 7800
disassembly. They are in rough order of how much time they cost.

## MAME write taps are garbage-collected

```lua
mem:install_write_tap(0x20, 0x3F, "pal", function(o, d) ... end)   -- WRONG
TAP = mem:install_write_tap(0x20, 0x3F, "pal", function(o, d) ... end)  -- right
```

`install_write_tap` returns a tap object. Drop the return value and Lua collects
it the next time the collector runs -- a few hundred frames in, typically well
after boot. The tap fires during startup, then silently stops.

What that looks like: plausible output, from a probe that has quietly died. It
produced two false conclusions in a row -- "nothing writes to the palette during
play" and "palette 6 is never animated" -- about a game whose palettes are
rewritten every single frame. Neither error announced itself; both looked like
findings.

**Any negative result from a tap is suspect until you have proved the tap was
still alive.** Have the probe count writes to something you know is busy and
print the count. If that number stops growing, your tap is dead.

## `video:snapshot()` under `-video none` can show a pixel that was never drawn

A screenshot taken with `MACHINE.video:snapshot()` while running `-video none`
showed a small bright dot on screen, at a screen position that appeared to
shift between frames in a way that looked like it was tracking a game object.
It wasn't. Reading the same coordinates directly off the framebuffer with the
screen device's own accessor --

```lua
local scr = MACHINE.screens[":screen"]
scr:pixel(x, y)   -- returns a packed ARGB int; unpack with plain arithmetic
```

-- returned the surrounding background colour at every one of those
coordinates, on every frame the screenshot had shown a dot. The PNG was
rendering something the hardware never actually output.

`-video none` is meant for headless runs that never look at a frame at all.
Asking it for a snapshot anyway still produces a file, with no error, and nothing
about the output looks wrong -- it takes a `scr:pixel()` cross-check (or an
actual `-video` backend) to notice the picture and the framebuffer disagree.

**Any visual claim made from a `video:snapshot()` PNG taken under `-video
none` needs a `scr:pixel()` (or real `-video soft`/`bgfx`) cross-check before
it goes in a finding.** A `-video soft` re-render of the same frame is a valid
independent check but can be far slower against a long recording than just
reading the pixel directly -- reach for `scr:pixel()` first.

## A read/write tap's reported PC can be MARIA's DMA, not the 6502

A PC-tagged tap on a ROM address that came back with real, non-trivial hit
counts -- thousands of them, spread across dozens of instruction addresses --
looked like solid evidence of several genuine readers. Every single one turned
out to be false. The reported PCs resolved to instructions with no reference to
the tapped address at all: `LDA ram_2230,X`, `ROL A`, `CLC` -- register-only or
RAM-indexed operations that cannot have produced a read of a ROM address
elsewhere on the map. One batch of hits was even attributed to a PC that was
itself a RAM address, which cannot be a real 6502 program counter.

MARIA's own graphics DMA shares the same bus the 6502 does, and it runs
constantly during normal play -- scanning display-list entries, including empty
ones, every frame. A tap installed on the shared address space cannot tell a
6502 fetch from a MARIA fetch; it reports whatever the CPU's PC register
happens to hold at that bus cycle, which for a MARIA-driven access is
essentially arbitrary. The signature is a tap with real, sustained hit counts
whose reported PCs, checked one by one, never actually name the tapped address
in their own operand.

**Before trusting a tap's PC as a genuine reader, open the listing at that
address and confirm the instruction actually references the tapped range.**
"Fires a lot" is not evidence by itself when the bus is shared -- only "fires,
and the instruction at that PC reads or writes this exact address" is. This
caught two false leads in one project (a set of allegedly-live RAM cells that
turned out to be permanently dead, and a ROM data gap that looked read but
wasn't) using the identical check both times.

## Five-byte display list entries put the palette somewhere else

A MARIA display list entry is four bytes, unless it is five. You tell from
byte 1: if its low five bits are zero -- and the byte is not zero, which would
end the list -- the entry is extended, five bytes long, and **the palette and
width are in byte 3**.

Read an extended entry as a direct one and every field you extract is wrong but
valid-looking. A real case: an entry with byte 1 = `$60` was read as palette 3.
Byte 1's low five bits are clear, so it was extended, and the actual palette
came from byte 3 -- palette 0. The wrong answer was consistent with everything
else visible and survived until a hardware probe contradicted it.

`tools/dlwalk.py` handles this, and its `--selftest` demonstrates the failure
mode on a hand-built pair of entries.

## The zone offset counts down, and sprites are line-planar

A DLL entry's offset field is the number of scanlines minus one, and MARIA
decrements it as it draws. Graphics for successive scanlines therefore come from
successive *pages*, not successive bytes: scanline n of a sprite lives at
(high byte + n) << 8 | low byte.

So a sprite is not a contiguous block. Dump it as one and you get stripes from
eight unrelated objects. Every graphics tool here reads line-planar by default
for this reason.

## RAM is mirrored into the first two pages

The 6502 needs a zero page and a stack, so the 7800 mirrors `$2040-$20FF` down
to `$0040-$00FF` and `$2140-$21FF` down to `$0140-$01FF`.

Games use both views freely, often building a display list through the low
addresses and pointing MARIA at it there. A dump of the RAM block will then be
asked for an address it does not appear to contain. `dlwalk.unmirror()` folds
them; do the same anywhere you map addresses to a dump.

## The header's mapper bits are not what the published lists say

Several widely-copied bit tables put Activision banking at `$0200` and Absolute
at `$0400`. Checked against 1,309 real images, the cartridges disagree:
Double Dragon and Rampage (Activision) read `$0100`, and F-18 Hornet (an
Absolute-mapper game) reads `$0200`.

Worse, some bits are not mappers at all. `$0800` looks like a mapper flag and is
set on 45 images -- every single one of which names YM2151 in its filename, and
is otherwise an ordinary 48K or 128K cart. Treating it as a mapper meant
refusing 45 perfectly readable ROMs.

The lesson generalises: when a spec and a corpus disagree, the corpus is the
thing that actually has to run. `tools/cart.py` documents which bits were
confirmed against which known games, and marks the rest as unconfirmed rather
than guessing.

## A data block's start address can be a literal operand elsewhere in the code

A `blocks` entry's `loc` looked like it overlapped the table before it by one
byte -- easy fix, move `loc` forward by one. The round trip failed immediately,
diverging at an unrelated instruction several hundred bytes away. The real
cause: some other routine loads that exact address as a two-byte immediate
constant (`LDA #$FF / STA ptr_lo`, `LDA #$96 / STA ptr_hi`, forming a pointer),
and the emitter had resolved that literal to the block's own symbolic name.
Move the block and the literal now points at a name that means a different
address -- the assembled bytes change, silently, because both spellings are
individually valid 6502.

The overlap suspicion was reasonable (a table's declared length looked short by
exactly one byte) and still turned out to be wrong: the byte in question was
real data belonging to the table, coincidentally zero, not evidence of a
boundary error. A block's `end` is usually safe to extend when it stops short
of real code with no gap in between -- that was tested and confirmed repeatedly
on the same project with no breakage. Its `loc` is the one that can be
load-bearing elsewhere in the ROM. **Verify with a full round-trip after moving
a block's start address, every time, even when the change looks obviously
correct** -- especially then, since an obviously-correct change is the one you
are least likely to check.

## A `gfx`/data block silently overrides `entries`, even at a confirmed vector

A block's `end` was extended past a game's actual IRQ entry point, on live
display-list evidence that looked continuous right up to a known-real routine
further along. The IRQ vector (`$FFFE/$FFFF`, unambiguous -- it's read
straight from the cartridge, not guessed) pointed inside that "confirmed"
block. The listing rendered the labeled entry point as `.byte $40` instead of
`RTI`: the disassembler had accepted the `entries` seed, then discarded it,
because `forced_data` (what a `blocks` entry populates) is checked *before*
the opcode is even read in the trace loop -- `if loc in self.forced_data:
break`. `entries` never overrides a block; a block always wins. This can hide
an entire subtree of real code with nothing louder than a label sitting on
top of a byte dump, easy to skim past if you don't notice the mnemonic column
is empty.

The lesson generalizes past this one case: **any address in `entries` (or
reachable from one) that ends up inside a `blocks` range is not being traced,
full stop** -- regardless of how strong the evidence for that block's extent
looked. After declaring or widening a `gfx`/data block, grep the regenerated
listing for `J(SR|MP)\s+dat_<block's address range>` (a real call landing on
a `dat_`-prefixed label is direct proof of misclassified code -- this is how
the case above was actually caught, a sound-effect routine called from
thirteen places had been sitting under the block, silently rendered as
`.byte` the whole time) and check every declared `entries`/label address
against the block's span by hand. Two rounds of "shrink the block until the
round trip and the evidence agree" were needed before this was fully
resolved -- the first fix (an end address short enough to stop overlapping
the *known* real code after it) still overlapped the vector, because the
vector was in the *middle* of the block, not at either edge.

## A header-inclusive patch is refused by an identical ROM

Two dumps of the same PAL cartridge held byte-identical cartridge data and
differed only in the declared cart type in the header. A BPS patch built across
the header therefore refused one of them, for a difference that has no effect on
anything.

Build patches against the cartridge data only and keep whatever header the
target image came with.

## The tracer's silence is not evidence

Anything a recursive-descent tracer cannot reach comes out as `.byte`, and the
listing still round-trips perfectly. Byte-identity says you have every byte; it
says nothing about whether you have understood them.

Two specific ways this bites:

* **A table read past its end.** A countdown used as an index into a table one
  entry shorter than the countdown allows will read whatever follows -- usually
  the first opcode of the next routine -- and use it as data. It looks
  deliberate in a listing. Two of these were sitting in one commercial game,
  each drawing a garbage frame at the start of a death animation.
* **Unreferenced data that is not unreferenced.** Before declaring a block
  unused, check every way it could be addressed, including as the second half
  of a 16-bit pointer and as a page number assembled at run time. A block
  "proven" unreferenced twice turned out to be reached through a page register
  loaded from a different table entirely.

## Live churn confirms activity, not what a byte means

Two RAM arrays lit up on nearly every one-second snapshot across a whole
recording, twelve bytes each, right where a twelve-segment centipede would
put its position. Concluded: row array, column array. Both got named and
committed that way, live evidence and all.

They were a status-bits byte and a direction/graphics-offset byte for an
18-slot general object table (every enemy type plus the player's own shot),
not coordinates at all -- caught later, independently, by finding the exact
instruction sequence for a poison-mushroom collision check operating on one
of them with `AND`/`ORA` against bit-pattern constants (`$40`, `$E7`), not
`ADC`/`SBC` against anything coordinate-shaped. Live data proves a byte is
*doing something every frame*; it says nothing about *what kind of thing* --
a status/flags byte that toggles state on every collision check churns just
as continuously as a position that updates every frame, and from a
snapshot-diff alone the two are indistinguishable.

The check that would have caught it directly, and the reason it wasn't run:
**a 6502 idiom for testing or setting bits almost never puts a bitwise
mnemonic on the address itself.** `AND`/`ORA`/`EOR` do have direct memory
addressing modes, but real code overwhelmingly loads the value into A first
(`LDA addr,X`) and masks with an immediate afterward (`AND #$40`) -- so
grepping the generated listing for which mnemonics reference an address
directly (the `most-referenced RAM addresses` report's own address column,
or any `xrefs:` line) will show mostly `LDA`/`STA`/`CMP` regardless of
whether the byte is a coordinate or a bitfield, because the bitwise op that
would give it away is one line down, working on the accumulator, with no
operand naming the address at all. (A version of this was tried as an
automatic classifier in `disasm.py` itself -- tally bitwise vs. arithmetic
mnemonics referencing each address, print a verdict. It mis-classified the
exact motivating byte, because the same byte also had a 2-bit sub-counter
packed into it and `DEC`'d directly, which is memory-direct and *does* show
up this way -- a packed byte can legitimately be mixed. Reverted rather than
shipped, since a wrong automatic verdict here is worse than no verdict: it
would have produced the same false confidence as the original mistake, just
with a tool's name on it instead of a human's.)

The manual check that does work: for a byte suspected of being a coordinate
or counter, grep the listing for every `LDA <name>` (or the indexed form)
and read the one or two lines immediately after each hit. Bit-pattern
constants there (especially ones that aren't round numbers -- `$40`, `$E7`,
not `$05` or `$0A`) mean bitfield, regardless of how the byte behaves live.

## A small-range index doesn't have to mean "player slot"

Four evenly-spaced fields, seven bytes apart, each three bytes long, each
touched by `LDA/STA field,X` with X ranging 0-2. The obvious read: three
players' worth of some four-part value. Four *separate* three-byte BCD
numbers -- player one's score, player two's, a team score for co-op mode,
and a spare block belonging to something else entirely -- with X selecting
a byte *within* one number, not a player.

Both readings explain the exact same access pattern equally well from the
code alone; nothing about `LDA field,X` distinguishes "X indexes a
digit-place shared by three players" from "X indexes a byte within this one
independent field, and the four fields are unrelated to each other except
by being the same shape." The tell, when there is one, is usually external
to the indexed instructions themselves: what other code treats the *whole
field* as a single unit (a 3-byte BCD add with carry chained across exactly
those three bytes and no further, for instance, is a strong sign of "one
number," and if there are four such adds at four different base addresses
rather than one parameterized loop over three player slots, that is itself
the answer). Don't let "a loop variable that happens to range over a small
number matches the number of players" stand in for actually checking what
happens at each value of that variable.

## Cross-bank references need a bank, not just an address

In a banked cart the same address means different bytes depending on which bank
is in the window. A reference to `$8123` is only meaningful together with the
bank that was selected when it executed. Track the bank alongside the address
everywhere -- the "space" idea in `cart.py` exists for exactly this -- or you
will eventually chase a routine that is not there.

## Reference resolution has to come from the mapper too

The tracer was taught to ask the mapper where a bank switch lives and what a
written value means. The *emitter* was not, and kept its own hardcoded copy of
SuperGame's map: `$4000-$7FFF` is f6, `$C000+` is f7, the middle is the window.

On any other layout that is wrong. An Absolute cartridge puts its **window** at
`$4000-$7FFF`, so every reference there asked for a space called "f6", found no
label, and quietly printed a bare address instead. The listing still assembled
-- a literal `$5039` is perfectly valid -- so the round trip passed and nothing
complained. F-18 Hornet came out with **1,110 bare operands and five labelled
ones**: a jump table at `$5000` whose targets had no names at all.

The lesson is not "check the emitter" but that *one* component knowing the
memory map is not enough. Anything that turns an address into a name needs the
same source of truth, and a round-trip test will not catch the difference,
because both spellings assemble.

## A branch cannot leave its bank

Resolving a control transfer by asking `space_of(target, bank)` returns nothing
when the tracer does not know which bank is in the window -- so the transfer was
filed as unresolved and no cross-reference was kept. For transfers *inside* the
window that is needlessly pessimistic: a branch is relative and physically
cannot leave its bank, and a JMP or JSR within the same window region runs
before any switch could take effect.

The cost was invisible in the same way. In Midnight Mutants, `b5:$B96E` is
branched to from three bytes earlier, and printed as a bare `$B96E` with no
label and no xref, because that stretch of bank 5 had been reached without the
tracer knowing the bank. Fixing it added twenty instructions of coverage and
turned the last thirteen bare operands into labels.

Both of these were found by reading DiStella's source and asking what it does
that this does not.
