# Atari 7800 toolkit

Tools and hard-won notes for taking 7800 cartridges apart — and, increasingly,
for putting one together. It started as the byproduct of a complete
byte-identical disassembly of a 128K commercial game, and grew through several
more.

Nothing here is specific to those games. The cartridge model was tested against
**2,664 retail and homebrew images** and lays out all but four of them — including Activision's 8K-granular mapper and bankset cartridges, whose two halves are read separately with `side=`; the
disassembler reproduces the hand-verified 128K disassembly byte for byte while
also handling unbanked 4K-48K ROMs.

The authoring side is newer and smaller: `newgame.py` writes a cartridge that
boots, takes input and splits the screen with an interrupt; `mksprite.py` gets
artwork into it; `dmabudget.py` says what MARIA's DMA leaves you to compute
with. Those numbers were measured rather than quoted, and the measurement
corrected a register bit this toolkit had recorded backwards — see
[`docs/making-a-game.md`](docs/making-a-game.md).

## Start here

```
python tools/workbench.py game.a78             # open everything at once
```

That is the one command worth remembering: it reads the header, scans for
artwork and music, and each result has a button that opens it in the right
editor. Everything below is the same work done a piece at a time.

```
python tools/survey.py game.a78 --strings      # what am I even looking at
python tools/disasm.py game.a78 -c annotations.json -o src
python tools/verify.py game.a78 -d src         # must pass, from day one
```

To hear a cartridge's music instead of reading its code:

```
python tools/capture.py game.a78 --render      # .a78 -> .log -> .trk -> .wav
```

It works out TIA or POKEY from the header, picks the right machine for the
region, records in MAME and renders. To edit what comes out:

```
python tools/trackeredit.py game.trk           a grid you can type notes into
```

`audio.md` breaks the capture into its four steps for when one comes out wrong;
on Windows both jobs are a drag onto `Render dropped file.bat` or
`Open in tracker.bat`.

Then read [`docs/method.md`](docs/method.md) — the working order — and
[`docs/pitfalls.md`](docs/pitfalls.md), which is a list of things that produced
confidently wrong answers in real work. The garbage-collection trap in the
emulator section is worth reading before you write any probe.

## What's here

### Tools

| | |
|---|---|
| `workbench.py` | One place to open a cartridge: what the header says, what a scan finds, and a button on each result that launches the right editor with the space, base and format already filled in. A launcher, not another tool. |
| `cart.py` | The `.a78` header and the mappers. Header flags checked against the image library, not against published bit lists — they disagree, and the cartridges win. |
| `library.py` | Search a ROM collection **inside its zip**, without extracting 22MB to find one file. Lays out matches, extracts them, or surveys one. |
| `init.py` | Starts a game: reads the header, takes the vectors as entry points, writes the annotations file and reports what the disassembler reached with it. Refuses to overwrite an existing one. |
| `survey.py` | First look: layout, per-bank entropy, strings, where the vectors point. |
| `disasm.py` | Bank-aware recursive-descent 6502 disassembler. `--cycles` annotates timings, `gfx` blocks draw their bits, `--low`/`--mapper` override a header that understates the mapping. Carries constants through so `LDA #n / STA $8000` resolves by itself; reports every switch it could not resolve. `--gaps` reports byte ranges that are neither code nor a declared data block -- the true unexplained set, not just "not code" (which includes every table and tile sheet you've already annotated) -- sorted largest-first so the next annotation to write is obvious. `--map` renders the same picture as a `coverage-<bank>.png` heatmap, one pixel per byte (green code, blue declared data, red gap) -- a glance instead of a read; needs Pillow, the one dependency this tool has and only if you ask for it. |
| `newgame.py` | **Starts a game.** Writes a project that assembles, boots and puts a moving sprite on screen: the two-level display list, the vblank-synced main loop, and a sprite stored the way MARIA actually reads one -- bottom-up, a page per scanline. Every other tool here reads a cartridge somebody else wrote; this one writes the smallest cartridge that is still a real one. The register values come from shipping 1987 code, and the source is commented to be edited. |
| `dmabudget.py` | **What MARIA leaves you.** MARIA draws by DMA and halts the 6502 while it does, so the cycle budget is a function of what is on screen. Give it a screen and it reports what drawing costs and what is left for game logic. The constants are measured, not quoted -- a cartridge that counts loop iterations per frame, one build per display-list shape -- and the model was validated by predicting shapes it had never seen. |
| `asm.py` | The assembler that closes the loop. `.res` fills, `#<label`/`#>label` and `label+n` make it usable for source written by hand, not just for round-tripping a listing. |
| `verify.py` | Reassembles every listing and compares to the ROM, byte for byte. |
| `build.py` | Rebuilds a complete image from listings. |
| `dlwalk.py` | Decodes MARIA display lists — including the five-byte entries that put the palette in a different byte. `--selftest` demonstrates the failure mode. |
| `gfx.py` | Renders character sets and sprite pages, line-planar. `--direct WIDTH` renders one direct-mode display-list object at its real size instead of the 256-wide indirect-mode grid — get WIDTH and `--lines` from the live display list, not a guess, or the render silently pulls in whatever unrelated data shares the object's low byte at other pages. |
| `mksprite.py` | **Artwork in, not just out.** Turns a PNG into a direct-mode sprite laid out the way MARIA reads one -- bottom-first, a page per scanline -- with `--frames N` packing an animation side by side at the stride shipping sprite sheets use. Refuses an image with more colours than the mode has. |
| `spriteedit.py` | Paint a cartridge's artwork in the browser and write it back. Pen, fill, line, rectangle and ellipse, with undo and copy/paste between cells. Renders in greys because the colours are MARIA registers rather than part of the artwork; a picker and the palettes the cartridge's own code writes let you choose. Reads the line-planar layout in either pixel format, opens straight from an `assets.py` manifest, and refuses to save if a byte outside the region you opened would change. |
| `explore.py` | Work out an unknown music format by ear. Reads a stretch of bytes, ranks plausible layouts of them -- serial records or parallel streams -- renders each to audio, and lets you adjust and listen until it sings. The workbench puts an **explore** button on every audio table it finds, so the address carries across. Saving writes a `reader: "direct"` format into `formats/`, keyed on the player fingerprint, and the tracker then opens those notes from the ROM. The ranking uses structure the bytes prove about themselves, so it assumes you are pointed at music and cannot tell a tune from graphics or code. Your ear decides; confirm with `tracker.py capture`. |
| `palette.py` | 7800 colour bytes to RGB. |
| `rammap.py` | Every RAM address the traced code touches, and how often. |
| `audiotrace.py` | Finds a cartridge's music *in the ROM*: locates every audio-register write, traces back to the tables feeding it, and reports them. |
| `songfmt.py` | Pulls a game's songs out of the ROM as editable data and pushes edited songs back in place, driven by a JSON description of the player's format. Refuses any write that would grow a pattern or touch a byte the format did not declare. `render` turns a pulled song into a tracker file, and `--verify` checks it against a capture frame by frame. |
| `assets.py` | Finds the artwork and the music *as data*: traces MARIA and audio register writes back to what feeds them, follows a captured display list to the graphics it names, and writes annotation blocks plus a manifest the asset tools consume. Bank-ambiguous finds are reported as candidates, not findings. |
| `sim.py` | **Unfinished, and measurably so.** A 6502 core that runs a cartridge's own code and traps its audio writes, so a player is its own authority on its format. It follows MARIA's display-list interrupts and charges for its DMA, and it agrees with MAME instruction for instruction over the first 427,399 instructions after a cartridge takes control. `--compare` scores it against a known-good capture, separating whether it plays the right notes (Ballblazer 60%) from whether it keeps playing (6%) -- a distinction the first version of that score hid. Groundwork, not a tool yet. |

| `capture.py` | Cartridge to song in one step: reads the header for the sound chip — both of them, on the eighteen images that carry two POKEYs — runs MAME with the probe, converts the log. Recognises the `a7800` fork and switches to debugger watchpoints, which is the only route that works there. |
| `midi.py` | Reads a Standard MIDI File: tracks, names, note ranges, polyphony and timing. Handles running status and tempo changes, which is where naive parsers quietly lose notes. |
| `trackeredit.py` | The tracker itself: a grid in the browser where you type notes, hear them and save. Imports a MIDI track straight into one voice, leaving the rest of the song alone. Backed by the same renderer that exports, so there is only one sound model. |
| `tracker.py` | Sound, for the TIA and for cartridge POKEY: a note table showing what each chip can and cannot play, a text song format, WAV rendering, capture from a running game, MIDI import, and 6502 export with a player. |
| `selftest.py` | Runs the toolkit against itself. Most checks need no cartridge; `--rom`, `--format` and `--log` add the round trips and the frame-by-frame check against hardware. The doc checks are in here too, because what slipped through last time was not a crash but a stale number. |
| `mktone.py` | Builds a cartridge that holds one POKEY setting forever — a controlled single-tone oracle for checking the sound model against a real emulator, since comparing against a game's own audio measures the comparison more than the model. |
| `bps.py` | BPS patches. Build them headerless. |
| `mksite.py` | Packs generated pages into self-contained HTML. |

### On Windows

`Open workbench.bat` — drag a cartridge onto it to open the workbench: the
header, the mapper, a scan for artwork and music, and a button on each result
that opens it in the right editor. Start here with something unfamiliar.

`Open in tracker.bat` — drag a `.a78`, `.log` or `.trk` onto it to open the
song in the tracker grid and edit it.

`Render dropped file.bat` — drag a `.a78` cartridge, a `.log` capture or a
`.trk` song onto it. A cartridge is recorded in MAME first; all three end as a
WAV beside the file, which it then plays. Several at once is fine, an existing
file is kept as `.bak` rather than overwritten, and the sound chip comes from
the cartridge header so nothing needs choosing.

### Probes

`probes/watch.lua`, `probes/dumpdl.lua`, `probes/audio.lua`,
`probes/dma-count.lua` and `probes/dma-costcart.py` — MAME scripts
for watching writes, capturing a live display list, and logging every audio
register write (TIA, or cartridge POKEY via `A7800_POKEY=<base>`) so
`tracker.py` can turn a running game's music into an editable song. All three
carry the garbage-collection warning inline, because a dead tap does not
announce itself.

### Docs

| | |
|---|---|
| [`method.md`](docs/method.md) | The order of work, and why byte-identity is the discipline everything rests on. |
| [`making-a-game.md`](docs/making-a-game.md) | The other direction: bringing a screen up from nothing, in the order MARIA needs it, and why a sprite is not a bitmap. |
| [`pitfalls.md`](docs/pitfalls.md) | Traps that each produced a wrong answer in real work. |
| [`hardware.md`](docs/hardware.md) | Memory map, MARIA, display lists, TIA, RIOT, PAL vs NTSC. |
| [`cartridges.md`](docs/cartridges.md) | Header format, mapper flags with the evidence for each, mapper layouts. |
| [`graphics.md`](docs/graphics.md) | Line-planar layout, pixel formats, character mode, finding artwork. |
| [`emulation.md`](docs/emulation.md) | MAME as an instrument, and how to avoid measuring nothing. |
| [`audio.md`](docs/audio.md) | The TIA's two voices, POKEY's four, why one chip is out of tune and the other is not, the tracker, and pulling songs out of a ROM and pushing them back. |

`a7800.py` and `m6502.py` are libraries, not commands: the machine's constants
and the 6502 opcode and cycle tables. Everything else runs from the shell.

### Templates

`templates/annotations.json` — the annotation file, with every key explained.
All human judgement goes here; generated listings stay disposable.

`templates/format.json` — a player-format description for `songfmt.py`, with
every key explained: where a game keeps its songs, what the bits of a note mean,
and which envelope engine to run. `formats/` holds two filled in for real
engines and verified at 100% against hardware: `mm-tia.json` (53 images) and
`aa-pokey.json` (58 images across 27 titles), plus `rmt.json`, which
identifies the 84 cartridges carrying a Raster Music Tracker module without
pretending it can play one. Between them, 23% of every cartridge in the library
with a recognisable player or module.

## The one rule

**The rebuild must stay byte-identical.** Assemble every listing straight back
and compare it to the ROM, from the first hour rather than the last. It costs
seconds, and it is what makes everything else — renaming, re-marking data,
regenerating — free rather than frightening.

It proves you have every byte. It does not prove you understand them: anything
the tracer could not reach comes out as `.byte` and still round-trips perfectly.
Watch the coverage figure too, and treat a bank stuck low as an open question.

## Requirements

Python 3, no dependencies. MAME with 7800 BIOS images for the probes (`a7800`
for NTSC, `a7800p` for PAL).

## Status

The mapper layer, disassembler, assembler, round-trip verifier and display-list
decoder are exercised against real images and the results are reproducible.

Verified against running hardware (MAME): the SuperGame layout at 128K and at
512K including the width of its bank switch, and the Absolute mapper on F-18
Hornet. The TIA sound model matches a renderer validated by ear on a real game,
sample for sample across all sixteen waveforms, and captures from running
cartridges — TIA and POKEY alike — replay to the exact register state on every
logged frame. The POKEY model covers four channels, all eight distortions, the
clock selects, both 16-bit pairs, both high-pass filters, both polynomial
lengths and volume-only mode — all of it measured against MAME with
purpose-built single-tone cartridges rather than taken from a datasheet. The
16-bit dividers agree to 0.00 cents across all four pairing paths, the filters
reproduce its spectrum peak for peak, and all eight distortion modes reproduce
its output bit for bit across three different divider and clock settings each.
The polynomial voices took three attempts to get right: the first two were
checked at a single setting, which cannot tell a correct model from a decimated
one. `docs/audio.md` records how that went wrong, because noise generated the
wrong way sounds exactly like noise generated the right way.

Measured against **MAME v0.287 and `a7800` v5.2** — the 7800-devtools fork,
which corrects POKEY's poly9 sequence and init state. Both agree with the model
at 1.0000 on every case. Capture runs on MAME (the fork's Lua predates
`install_write_tap`); accuracy is checked on the fork. `docs/emulation.md` has
the split. The display-list decoder was
checked against a live list pulled out of a running game, not only against its
own self-test.

Activision banking, Bankset and SOUPER are recognised and refused with an
explanation rather than laid out wrongly.

## Examples

`examples/exo-annotations.json` — a real annotation file worked out with these
tools, for a 512K homebrew whose inter-bank calls go through a trampoline that
`RTS`es into the destination. It shows what the format looks like when the
tracer needs help, and why.
