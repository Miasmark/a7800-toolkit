# Atari 7800 toolkit

Tools and hard-won notes for taking 7800 cartridges apart — and, increasingly,
for putting one together. It started as the byproduct of a complete
byte-identical disassembly of a 128K commercial game, and grew through several
more.

Nothing here is specific to those games. The cartridge model was tested against
**1,309 retail and homebrew images** (Trebor's 7800 ROM PROPack v8_17, the library the rest of these docs count) and lays out all but two of them (it refuses SOUPER, which has its own mapper and extra hardware, and a 512K linear demo), Activision's 8K-granular mapper and bankset cartridges, whose two halves are read separately with `side=`; the
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
python tools/firstlook.py game.a78             # what is this, in one report
python tools/workbench.py game.a78             # open everything at once
```

That is the one command worth remembering: it reads the header, scans for
artwork and music, and each result has a button that opens it in the right
editor. Everything below is the same work done a piece at a time.

```
python tools/survey.py game.a78 --strings      # what am I even looking at
python tools/init.py game.a78 -o annotations.json   # the file the disassembler reads
python tools/disasm.py game.a78 -c annotations.json -o src
python tools/verify.py game.a78 -d src         # must pass, from day one
python tools/build.py game.a78 -d src          # and the image must rebuild identically
```

(`disasm.py -c` on a file that does not exist is an error: `init.py` writes it.
The workbench does all of this from one page, in `<rom>-workbench/`: **Check my
work** runs the disassembly, the annotation checks, the round trip and the rebuild as
one job. Pictures need Pillow, `python -m pip install pillow`; everything else is
plain Python 3.)

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
| `firstlook.py` | **One command, one report, on a cartridge nobody has told the toolkit anything about.** Reads the header and identifies the music player (against `formats/`, or by fingerprint), then runs five short headless runs -- in the simulator by default, so no emulator or BIOS is needed (`--engine mame` for MAME) -- music (to `.log`, `.trk` and `.wav`), screenshots, the live display list with its artwork rendered, every graphics address drawn from, and which code ran in which bank (merged by `dyn.py` into a starter `annotations.json`) -- and writes `report.md`. Static analysis found 2 graphics blocks and 1 audio table in a 128K banked POKEY cartridge; the same cartridge's one-minute first look found 36 sheets of graphics, 30 seconds of music, and 332 executed instructions the tracer had missed. It says plainly what one run cannot show, refuses to render pixel formats it cannot decode, and works without MAME (static half only) or Pillow (no pictures). `--playback` runs every probe over a recording instead. |
| `workbench.py` | **One place to work on a cartridge.** Overview (the header, what a scan finds, a button on each result that launches the right editor with the space, base and format filled in), **Run** (first look, disassemble, observe code, find address tables, census with suggestions you tick into the annotations, start annotations, check annotations, **check my work** -- disassemble, lint, verify and rebuild in one job -- capture music with optional POKEY-to-TIA, sampling profile, cycle budget, display-interrupt timing, and any probe with its settings), **Results** (each job's command lines, live output, and its files -- pictures and audio play in place), **Listing** (the disassembly, searchable by name or address) and **Annotations** (edit `annotations.json` and see the checks as you save). Jobs write to `<rom>-workbench` beside the cartridge; the ones that need MAME are greyed out, with the reason, when it or the BIOS is missing. A launcher, not another tool: each job is the command line you would have typed, shown beside its result. |
| `simprobe.py` | **Watch a cartridge run in the simulator and write what the MAME probes would have:** `exectrace.log` (instructions executed, indirect jumps, computed returns -- an `RTS` whose address was pushed by hand -- and bank switches, in `exectrace.lua`'s format, so `dyn.py` and the workbench read it), `dataread.log` (ROM bytes the CPU read as data), the MARIA registers and RAM (`dumpgfx.lua`'s files) and the sound log. No emulator, no BIOS. See `corpus.py` for how far to trust it. `--explore` also runs with the stick swept and the console switches worked and unions the runs; `--force` adds code only a forced branch reached (`branchforce.py`) as `F` lines; code that ran from RAM is written as `R` lines with the ROM bytes it was copied from; a jump from a RAM trampoline is recorded. `--interrupts FRAME` writes `dlitimes.log` (the zones and the line each display interrupt arrives on, in `probes/dlitimes.lua`'s format); the workbench's Display interrupts job uses it by default. A program that runs a KIL opcode stops there, and the report says so. |
| `census.py` | **An automatic ROM and RAM census, from a simulated run.** Sorts every byte of the cartridge by what the machine did with it -- executed, read as data, fetched by MARIA's DMA (the display lists are walked), code the tracer reaches but the run did not exercise, untouched data block, fill, or **DARK**: nothing ran, read or drew it and the tracer does not reach it, each dark area described by what its bytes look like (text, address table, plausible code, graphics-like). RAM the same way: written, read, read before written, pointers, counters, flags, free ranges, stack depth. `--explore` sweeps the joystick, and unions in a second run that works the console switches (difficulty, Select, Reset, Pause) and second buttons; `--merge` unions runs so coverage only grows. Writes `census.md`, `census.json` and a coverage map per bank. On the synthetic cartridge the dark areas are exactly the text bank and the two untouched banks it was built with. "Unreachable" means "not found by this run or the tracer", and the report says so. |
| `branchforce.py` | **Find code a run never reached by taking the branches the game did not take.** Watches a simulated run, keeps a copy of the machine at every conditional branch whose other side has not run, then restarts each copy down the other side, in a sandbox, with the real registers and RAM. A path that reaches code the run executed (`joined`) or runs its budget without trouble (`ran on`) is kept; one that executes an undefined or illegal opcode, BRK, JAM, an address that is not ROM or RAM, or a byte the run read as data is **dead**, and everything it ran since the fork is thrown away. The static tracer already follows both sides of every branch, so what this adds is concrete values: a hand-pushed `RTS`, a jump table or a bank switch on the untaken side is followed to where it goes. `--truth N` grades the kept code against a longer run; a control starts paths at bytes the run read as data with that one veto switched off, so it can fail: 2-4% of those paths still rejoin real code (on Asteroids and Food Fight that would keep 4-7 instructions against several hundred forced; Galaga's controls keep ~280-550 as `ran on`, which is why only `joined` paths are counted). `census.py --force` marks what it finds as its own class. A forced path may be one the game never reaches -- the registers do not agree with the branch -- so it is reported apart from what executed. |
| `mamecheck.py` | **Which cartridges MAME runs, which the simulator runs, and where they disagree.** Runs each image (a folder, or `--sample N` of it) in MAME with `probes/rendersurvey.lua` and in the simulator, and asks both the same question: after the BIOS hands over, is MARIA kept drawing from a display list? Reports *both*, *sim only* (MAME cannot be the reference for that image: a mapper or flag it does not implement), *mame only* (a gap in the simulator) and *neither*, with the failures grouped by mapper. Cached by file hash, so a run over the whole library can be stopped and resumed. |
| `localserver.py` | Not a command: the request rules the four local web servers (`workbench.py`, `spriteedit.py`, `trackeredit.py`, `explore.py`) share. None has a password and all can write files, so each answers only its own page: a Host that is not this machine's address (DNS rebinding), a foreign Origin (a cross-site form or `fetch`), a POST that is not `application/json`, an oversized body, or a save path outside the cartridge's folder, the working folder and the workbench project are refused. |
| `simorigins.py` | `addrorigin.lua` without MAME: follows every address the CPU uses back to the ROM bytes it was built from, in the simulator, and writes the same log for `origins.py` (address tables, immediates). On Triple Punch it finds the same tables the MAME probe does, and more, because a simulated run is longer. Slow (Python decodes every instruction). |
| `corpus.py` | **Measure the toolkit against a whole library.** Runs the static tracer and the simulator on every cartridge in a folder and grades each against the other: executed instructions the tracer missed (and how execution first reached them: RAM code, indirect jump, `RTS` trick, interrupt, unresolved bank), data the CPU read from inside what was printed as code, and how many cartridges the simulator got going on. Cached by content. The yardstick for changes to the disassembler and the simulator. |
| `runprobe.py` | Run one of the Lua probes on a cartridge, headless, with MAME and the BIOS found for you and its `A7800_*` settings given as `-e KEY=VALUE`; says what files it wrote and when it wrote none. `--list` shows the probes. The typing every recipe in `docs/emulation.md` repeats, in one place. |
| `cart.py` | The `.a78` header and the mappers. Header flags checked against the image library, not against published bit lists — they disagree, and the cartridges win. |
| `library.py` | Search a ROM collection **inside its zip**, without extracting 22MB to find one file. Lays out matches, extracts them, or surveys one. |
| `init.py` | Starts a game: reads the header, takes the vectors as entry points, writes the annotations file and reports what the disassembler reached with it. Refuses to overwrite an existing one. |
| `survey.py` | First look: layout, per-bank entropy, strings, where the vectors point. |
| `disasm.py` | Bank-aware recursive-descent 6502 disassembler. `--cycles` annotates timings, `gfx` blocks draw their bits, `--low`/`--mapper` override a header that understates the mapping. Carries constants through so `LDA #n / STA $8000` resolves by itself; reports every switch it could not resolve. `--gaps` reports byte ranges that are neither code nor a declared data block -- the true unexplained set, not just "not code" (which includes every table and tile sheet you've already annotated) -- sorted largest-first so the next annotation to write is obvious. `--map` renders the same picture as a `coverage-<bank>.png` heatmap, one pixel per byte (green code, blue declared data, red gap) -- a glance instead of a read; needs Pillow, the one dependency this tool has and only if you ask for it. |
| `newgame.py` | **Starts a game.** Writes a project that assembles, boots and puts a moving sprite on screen: the two-level display list, the vblank-synced main loop, and a sprite stored the way MARIA actually reads one -- bottom-up, a page per scanline. Every other tool here reads a cartridge somebody else wrote; this one writes the smallest cartridge that is still a real one. The register values come from shipping 1987 code, and the source is commented to be edited. |
| `dmabudget.py` | **What MARIA leaves you.** MARIA draws by DMA and halts the 6502 while it does, so the cycle budget is a function of what is on screen. Give it a screen and it reports what drawing costs and what is left for game logic. The constants are measured, not quoted -- a cartridge that counts loop iterations per frame, one build per display-list shape -- and the model was validated by predicting shapes it had never seen. |
| `asm.py` | The assembler that closes the loop. `.res` fills, `#<label`/`#>label` and `label+n` make it usable for source written by hand, not just for round-tripping a listing. A name defined twice with different values is refused, not silently resolved to the later one. |
| `verify.py` | Reassembles every listing and compares to the ROM, byte for byte. |
| `build.py` | Rebuilds a complete image from listings. |
| `dlwalk.py` | Decodes MARIA display lists — including the five-byte entries that put the palette in a different byte. `--selftest` demonstrates the failure mode. |
| `zonebill.py` | What a frame's display costs MARIA, zone by zone: walks a RAM dump from `probes/rendersurvey.lua` and bills every zone with `dmabudget.py`'s measured constants, so any game's frame is costed by the same instrument -- three racing games were compared this way. |
| `gfx.py` | Renders character sets and sprite pages, line-planar. `--direct WIDTH` renders one direct-mode display-list object at its real size instead of the 256-wide indirect-mode grid — get WIDTH and `--lines` from the live display list, not a guess, or the render silently pulls in whatever unrelated data shares the object's low byte at other pages. |
| `mksprite.py` | **Artwork in, not just out.** Turns a PNG into a direct-mode sprite laid out the way MARIA reads one -- bottom-first, a page per scanline -- with `--frames N` packing an animation side by side at the stride shipping sprite sheets use. Refuses an image with more colours than the mode has. |
| `spriteedit.py` | Paint a cartridge's artwork in the browser and write it back. Pen, fill, line, rectangle and ellipse, with undo and copy/paste between cells. Renders in greys because the colours are MARIA registers rather than part of the artwork; a picker and the palettes the cartridge's own code writes let you choose. Reads the line-planar layout in either pixel format, opens straight from an `assets.py` manifest, and refuses to save if a byte outside the region you opened would change. |
| `explore.py` | Work out an unknown music format by ear. Reads a stretch of bytes, ranks plausible layouts of them -- serial records or parallel streams -- renders each to audio, and lets you adjust and listen until it sings. The workbench puts an **explore** button on every audio table it finds, so the address carries across. Saving writes a `reader: "direct"` format into `formats/`, keyed on the player fingerprint, and the tracker then opens those notes from the ROM. The ranking uses structure the bytes prove about themselves, so it assumes you are pointed at music and cannot tell a tune from graphics or code. Your ear decides; confirm with `tracker.py capture`. |
| `palette.py` | 7800 colour bytes to RGB: an approximation to read artwork by, and MAME's own table (`mame7800()`, or `A7800_PALETTE=mame` for every tool that draws) when a picture has to match the emulator. |
| `rammap.py` | Every RAM address the traced code touches, and how often. |
| `audiotrace.py` | Finds a cartridge's music *in the ROM*: locates every audio-register write, traces back to the tables feeding it, and reports them. |
| `songfmt.py` | Pulls a game's songs out of the ROM as editable data and pushes edited songs back in place, driven by a JSON description of the player's format. Refuses any write that would grow a pattern or touch a byte the format did not declare. `render` turns a pulled song into a tracker file, and `--verify` checks it against a capture frame by frame. |
| `assets.py` | Finds the artwork and the music *as data*: traces MARIA and audio register writes back to what feeds them, follows a captured display list to the graphics it names, and writes annotation blocks plus a manifest the asset tools consume. Bank-ambiguous finds are reported as candidates, not findings. |
| `sim.py` | **A TIA tool, and it works.** A 6502 core that runs a cartridge's own code and traps its audio writes, so a player is its own authority on its format. It follows MARIA's display interrupts and agrees with MAME instruction for instruction over the first 427,399 instructions after a cartridge takes control. Scored against like-for-like MAME 0.264 captures (the same stretch of play) it reproduces all five TIA cartridges' music -- Ikari Warriors, Midnight Mutants (97.6%), Dark Chambers, Donkey Kong and Choplifter, with the frame clock exact in each; `sim.py`'s header has the per-game figures and what is not yet traced. These need commercial ROMs, so they cannot be re-run from the repository. It is **not** a MAME stand-in everywhere: it does not model cartridge RAM beyond what `Bus` maps, IRQs, lightguns or paddles, and `mamecheck.py` measures where it and MAME disagree. The POKEY path is NOT validated; see the module docstring. |
| `capture.py` | Cartridge to song in one step: reads the header for the sound chip — both of them, on the eighteen images that carry two POKEYs — runs MAME with the probe, converts the log. Recognises the `a7800` fork and switches to debugger watchpoints, which is the only route that works there. |
| `midi.py` | Reads a Standard MIDI File: tracks, names, note ranges, polyphony and timing. Handles running status and tempo changes, which is where naive parsers quietly lose notes. |
| `trackeredit.py` | The tracker itself: a grid in the browser where you type notes, hear them and save. Imports a MIDI track straight into one voice, leaving the rest of the song alone. Backed by the same renderer that exports, so there is only one sound model. |
| `tracker.py` | Sound, for the TIA and for cartridge POKEY: a note table showing what each chip can and cannot play, a text song format, WAV rendering, capture from a running game, MIDI import, and 6502 export with a player. |
| `selftest.py` | Runs the toolkit against itself. Most checks need no cartridge; `--rom`, `--format` and `--log` add the round trips and the frame-by-frame check against hardware. The doc checks are in here too, because what slipped through last time was not a crash but a stale number. |
| `mktone.py` | Builds a cartridge that holds one POKEY setting (or, with `--tia`, one TIA channel's AUDC/AUDF) forever — a controlled single-tone oracle for checking the sound model against a real emulator, since comparing against a game's own audio measures the comparison more than the model. |
| `pokey2tia.py` | Turns a POKEY game's music (a `probes/audio.lua` log) into TIA music for a port: two voices out of up to eight. By default POKEY voices 1+2 feed TIA channel 1 and 3+4 channel 2, and the louder of two voices wanting a channel wins (`--groups` changes the pairing); `--mash arp` keeps every voice by sharing a channel in turns (livelier, messier, `--arp N` sets the speed); `--map loudest` plays each frame's loudest two instead; `--offset`/`--fit` move the tune onto the TIA's sparse pitches; noise goes to AUDC 8. Writes `tia.trk` (editable in the tracker), `tia.asm` (data and player), `tia.wav` beside `orig.wav`, and a report of what was lost. Generalised from the converter in the Karateka XE port. |
| `bps.py` | BPS patches. Build them headerless. |
| `mksite.py` | Packs generated pages into self-contained HTML. |
| `a8dis.py` | Trace an Atari 8-bit cartridge by following its code rather than sweeping it, and reconstruct the RAM it builds. Karateka's XEGS cartridge is a disk that happens to be silicon: a 22-instruction loader copies whole 8K banks into RAM and jumps there, so a trace of the ROM reaches 75 bytes and leaves. `--overlays` shows which banks each scene loads, `--scene N` rebuilds that address space and traces the real game inside it, and `--frame` prints what the game does every vertical blank -- which is the comparison that matters against the 7800 version. |
| `atx.py` | Read an ATX floppy image -- the format protected Atari 8-bit disks circulate in, which keeps each sector's angular position and error flags so copy protection survives. Takes the good copy of each sector, exports a plain ATR other tools read, shows the boot record, and extracts files when the disk has a directory -- saying so plainly when it does not, which for a self-booting game is the usual answer. |
| `forth.py` | Decompile an indirect-threaded Forth image out of a cartridge. Some 7800 games are not 6502 programs -- Karateka is a Forth program with an interpreter underneath, which is why a tracing disassembler reaches 173 instructions in a 48K ROM and stops. This finds the interpreter by shape (the loop every primitive returns to, the routines that save and restore the thread pointer, the word that eats the following cell) and walks the thread: `--at` decompiles a definition, `--callers` says who names a word, `--map` summarises. It recovers structure, not names -- a shipped Forth has no dictionary. |
| `patchset.py` | A bundle of patches you can pick from, checked a section at a time. `lint` checks a bundle's manifest -- every pair of options, not only the ones asked for. A BPS is a delta between two whole files with a CRC of each, which is the wrong shape for "here are nine independent fixes, take the ones you want": nine fixes are 512 combinations, a whole-file CRC refuses a dump whose header differs, and two patches touching the same bytes apply cleanly and silently produce a ROM that is neither. Nothing standard covers this -- VCDIFF's windows are chosen by the compressor, NINJA and BPM bundle patches for several *files*, PPF validates one hardcoded block. So a patch set names **sections** (a byte range plus the CRC32 of its pre-image, so applying checks 168 bytes rather than 49152), **knobs** (two options turning the same one are alternatives and asking for both is refused rather than resolved by file order), and **floats** (code with no fixed home: the bundle says how much room it needs and where to look, the patcher finds a run of free bytes, and every call site learns the address it chose). Headers are handled by identifying the body rather than the file, so headered and bare dumps take the same bundle -- and because sections stand alone, a ROM already patched elsewhere is still a valid target for whatever nobody has touched. `bundle_from_images` builds a bundle from each option's finished cartridge -- sections, shared spans, anchors, growth -- and refuses to write one that does not reproduce them; the same inputs give the same file. |
| `modmap.py` | What a mod is made of, byte by byte: original bytes kept (read in play or not, from `probes/romcoverage.lua` maps), overwritten, new, and still empty, with your own labels for the new parts and an optional PNG map. It aligns bodies at `$FFFF`, so a mod that grew the cartridge lines up. |
| `portkit.py` | Ship a conversion as a recipe rather than as a copy. A BPS patch is a delta between two files, which breaks the moment the output draws on a second source: a 7800 build using Atari 8-bit artwork would carry every one of those bytes inside the "patch". So this ships coordinates instead -- which images are needed (by SHA-256), which extents to take from them (hashed individually), what original work goes with them, and the hash the finished cartridge must have. Everyone supplies their own copies and gets a byte-identical result. It refuses a recipe that carries embedded data, so the guarantee is enforced rather than promised. |
| `portscan.py` | What it would take to move Atari 8-bit code to the 7800, counted rather than guessed. Both machines run a 6502, which is the least useful fact about the job; the work is everything the code says to the hardware. Sorts every hardware access into what carries over (POKEY is POKEY, at a different address), what has an equivalent needing a rewrite (joysticks), and what has none at all (player/missile graphics, hardware collision detection, ANTIC's display lists). |
| `replay.py` | Replay a recorded session and measure what the game did. MAME reproduces a recording exactly -- two replays give byte-identical profiles -- so a before-and-after number means something, which a scripted run cannot deliver: scripted input reaches a title screen and stops. Reports dispatches a frame, how often the controls are read, and which definitions the time went to. `--compare` replays the same session against a second build, honest only where the change does not alter the game's speed. |
| `lualint.py` | Lint MAME Lua probes for the mistakes that fail silently: a tap held only in a chunk-level `local` (collected within a few hundred frames, after which the probe prints plausible numbers from a dead tap), a missing header, an environment variable the header does not name. A regex pass with three rules -- a report means *look here*, silence means only that these traps are absent. Cannot run MAME; it is what reviews a probe on a machine that has none. `selftest.py` runs it over `probes/`. |
| `dyn.py` | Turns what a run *observed* into annotations. `probes/exectrace.lua` watches the real machine -- MAME, any mapper it can run -- and logs which code executed in which bank, where each `JMP (ptr)` went, and what each computed bank-switch store selected. This writes those into the annotations file (jump targets as `entries`, the banks a switch chose as a `banksw` list, which is what makes the tracer explore all of them), re-runs the disassembler, and reports how much more it reached. Everything it adds is marked observed-not-proven under `_dynamic`, nothing already in the file is replaced, and it is idempotent. `init.py --dynamic LOG` does it at the start. With `--dataread` (from `simprobe.py`) it also cuts out bytes the run read as data that the listing prints as instructions -- as `blocks`, only where nothing in them ever executed, nothing retained JSRs/JMPs/branches into them, no neighbouring byte was both read and executed (a copy or checksum loop walking over code reads everything, and BonQ's whole ROM is read that way, so it gets none), and every instruction the listing reached is still reached. ROM that was copied to RAM and run there becomes an entry at its ROM address, not a block. Code only a forced branch reached is listed under `_dynamic` as `forced_proposals` and is NOT made an entry (measured against longer runs, about a quarter of what forcing joins back is real code); `--adopt-forced` makes them entries, listed as `forced_entries`, and the workbench does so when its branch-forcing box is ticked. |
| `origins.py` | Which bytes of a cartridge are **addresses**, from a run of `probes/addrorigin.lua`, which follows every address the CPU uses back to the bytes it was built from. Reports address tables in the ROM (words, or low and high bytes apart, with the entries the run used and where they led), addresses written into code as immediates (`LDA #<routine`: what has to change if it moves), and pointers built in RAM from nothing it could trace; `-c` adds the tables to `annotations.json` as blocks. Observed, not proven: only entries the run used. Idea and first version from the Karateka XE port, where it drove relocating a game's pointers. |
| `mariapix.py` | MARIA's pixel formats as pure functions: bytes in, (palette, colour) pairs out. 160A is four pixels a byte from the entry's own palette (three colours and transparent); 160B is two pixels a byte, each with its own palette bits, so one entry draws from four palettes. Character mode reads one byte per scanline or, with CTRL bit 4 set, two. Measured against MAME rather than taken from a document -- `selftest.py` has MAME draw each format (via `probes/forcedl.lua`) and checks the prediction -- because the write-mode bit had been recorded in the wrong place. No 320 modes. |
| `annotations.py` | Check an annotations file the way `disasm.py` reads it, before it quietly ignores half of it: a typo'd key (`label` for `labels`, with the nearest real one suggested), a key repeated in the JSON, a name given to two places or to a hardware register, a bad location or block, a bank-switch pin that is not a bank. With `--rom`, also entries that sit inside a data block you declared (a block wins, so the code is never traced), locations outside the image, and labels that point into the middle of an instruction. A block that starts inside another is a warning: the disassembler never reaches its start, so its name and note vanish. Checked against the 11 real annotation files in the sibling game repositories: all pass; Ball Blazer's one overlap is real, documented in its own note, and reported as the warning it is. |
| `pcmap.py` | Name the routines a profile spent its time in. `probes/pcprof.lua` samples the program counter each time MARIA starts a zone -- about thirty evenly spaced samples a frame, no timer needed -- and this groups them under the nearest label below each address, using your annotations' names, a disassembly listing's, or a symbols file. A ranking, not a cost: the labels decide where a routine ends, and only the visible frame is sampled. |
| `session.py` | Record and play back a MAME input session for any cartridge (`record`, `play`, `list`), saved beside it in `recordings/` and never overwritten. A recording replays exactly, so two replays give identical profiles; it is what `probes/reclength.lua`, `replay.py` and `regress.py` run over. `Record a session.bat` and `Play a recording.bat` are the drag-and-drop forms. |
| `regress.py` | Asks every build the same questions: a JSON list of MAME probe jobs (probe, recording, environment, expanded over lists of values), each reduced to one verdict line, run in parallel, saved as a baseline and compared against it. Built from Pole Position II's regression set, which it reproduces verdict for verdict. |
| `sign7800.py` | Cartridge signatures. An NTSC 7800 hashes the cartridge and checks a signature over that hash at `$FF80`-`$FFF7`; a cartridge that fails is not refused, it is started in **2600 mode**, which looks like a black screen rather than an error. PAL consoles do not check and no emulator does, so a patched ROM works everywhere it gets tested and nowhere it gets played. Verifies, and signs -- the scheme is Rabin with public exponent 2, so a signature is a square root of the hash mod `n`, found by stepping the hash's one don't-care byte until a root exists. A port of Bruce Tomlin's `sign7800.c`, checked against stock dumps of two different games. Every build path here signs; the patch-set has to do it at apply time, since the signature covers the whole image and every combination of options has a different one. |
| `spritedump.py` | Renders one direct-mode MARIA display-list object -- a real sprite at a known base/width/height/palette, optionally stacked from several zone-sized segments -- rather than a fixed 256-entry character sheet. Reads the palette straight out of a `dumpgfx.lua` register dump so the colours are the ones the game actually used. |

### Shipping a patch, and making it boot

Two things a 7800 patch needs that a diff does not give you.

**A patch set.** `patchset.py` reads and writes `.abp` bundles: many
independent fixes in one file, each option declaring which knob it turns,
so two settings of one knob are refused as a choice not yet made rather
than applied in file order. Sections carry a CRC32 of their pre-image, so
applying checks a byte range instead of the whole file -- a different
header, or another fix already applied elsewhere, still passes. Floats let
new code find its own address at apply time. An option can grow the
cartridge (a 32K game made 48K, say), and the `.a78` header's ROM size
follows; that is format `patchset/3`. An option built on another's result
is recognised from the two patches' checksums, applied after it, and read
back as both applied. See
[docs/patchset-format.md](docs/patchset-format.md). The format also lives
on its own at
[Anchored-Bundle-of-Patches](https://github.com/Miasmark/Anchored-Bundle-of-Patches),
with the console-specific parts removed. That copy reads `patchset/2`, and
refuses a bundle that grows by name.

**A signature.** `sign7800.py` verifies and regenerates the NTSC cartridge
signature at `$FF80`-`$FFF7`. This is easy to skip and expensive to skip:
the console hashes the cartridge and checks that signature, and on a
mismatch it does not refuse -- it starts up in **2600 mode**, which looks
like a black screen rather than an error. PAL consoles do not check and no
emulator verifies it, so a patched cartridge works everywhere you test it
and fails on the hardware it was made for. The scheme is Rabin with public
exponent 2, so verifying is one squaring and signing is a square root of
the hash.

The division of labour is deliberate: the format carries no signature, and
`PatchSet.apply` patches bytes and stops there, because a checksum repair
has to know the exact platform, and wiring one into a patch format would
make the format specific to it. The `patchset.py apply` command then signs,
as a separate step: it signs an NTSC cartridge and leaves a PAL one alone.
PAL consoles never check, and retail PAL dumps carry erased EPROM where a
signature would go. The bundle's `target.region` decides, then the `.a78`
header's TV byte. `sign7800.py` makes the same call from the header, with
`--force` to sign anyway. Anything that patches another way: **apply, then
sign.**

### On Windows

`Record a session.bat` / `Play a recording.bat` — drag a `.a78` onto either to record a MAME session beside the cartridge, or to list and replay one.

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
`tracker.py` can turn a running game's music into an editable song. The Lua
probes among them carry the garbage-collection warning inline, because a dead
tap does not announce itself. Every other probe is listed, one line each, in
[`docs/emulation.md`](docs/emulation.md#probe-index).

`probes/wildfetch.lua` stops at the first instruction fetched from where no
code should be (a bad jump, a bad return) and writes the registers and the
return chain. `probes/romcoverage.lua` maps which cartridge bytes a run
reads, counting from the moment the game locks INPTCTRL so the BIOS's
signature pass is not mistaken for use. `probes/snapwhen.lua` takes
screenshots when a RAM byte says the moment has come.
`probes/rendersurvey.lua` counts DLIs and WSYNCs per frame and dumps RAM for
`zonebill.py`. All four come from the Pole Position II VS mod, where the
wild-fetch trap ran against every build. `probes/handover.lua` records the moment a
cartridge's reset code first runs (frame, registers, flags, INPTCTRL, RAM),
which is how the two BIOSes in `docs/bios.md` were compared.

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
| [`bios.md`](docs/bios.md) | What Atari's NTSC BIOS does before a cartridge runs (self-test, signature, the state it hands over), and how 7800OpenBIOS differs. |
| [`audio.md`](docs/audio.md) | The TIA's two voices, POKEY's four, why one chip is out of tune and the other is not, the tracker, and pulling songs out of a ROM and pushing them back. |

`a7800.py`, `m6502.py` and `addr.py` are libraries, not commands: the machine's constants
and the 6502 opcode and cycle tables. Everything else runs from the shell.

### Templates

`templates/annotations.json` — the annotation file, with every key explained.
All human judgement goes here; generated listings stay disposable.

`templates/format.json` — a player-format description for `songfmt.py`, with
every key explained: where a game keeps its songs, what the bits of a note mean,
and which envelope engine to run. `formats/` holds the filled-in descriptions: `mm-tia.json` (53 images) and
`aa-pokey.json` (58 images across 27 titles), both verified at 100% against
hardware, and four single-title descriptions of the same Atari in-house engine --
`commando-pokey.json`, `fatal-run-tia.json`, `meltdown-tia.json` and
`missing-in-action-tia.json`. `rmt.json` identifies the 84 cartridges carrying a
Raster Music Tracker module without pretending it can play one. The three
shared descriptions cover 23% of the cartridges that have a recognisable player
or module (195 of 841; see `docs/audio.md`).

## Tests, and the cartridge they use

`python tools/selftest.py` needs no ROM: `tests/synth.py` builds a 128K
SuperGame + POKEY cartridge from source, and `tests/carts/synth128.a78` is a
committed copy (selftest fails if the two differ). It is made to be hard for
a static tracer on purpose -- a bank switch whose number comes from a table, a
`JMP` through a RAM vector, a tune played from a switched bank -- and
`facts()` says what a correct tool must find. With MAME and a BIOS configured
([`docs/emulation.md`](docs/emulation.md#running-mame-with-no-atari-bios)),
selftest also runs the probes against it and checks what they observe.

## Addresses, on the command line

Every option that takes an address reads `$C000`, `0xC000` and `C000` the same --
hexadecimal, because that is how addresses on this machine are written (`tools/addr.py`).
Before this, `--base 8000` in `gfx.py`, `assets.py`, `dlwalk.py` and the sprite tools
meant *decimal* 8000 and `$8000` was refused, while `a8dis.py` and `modmap.py`
refused `0x8000`. Bank numbers, counts, frames and lines are not addresses and stay
decimal.

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
for NTSC, `a7800p` for PAL). With no Atari BIOS, 7800OpenBIOS works in its place:
[`docs/emulation.md`](docs/emulation.md#running-mame-with-no-atari-bios) has the
setup, and `A7800_BIOS=a7800pr` points the tools at it.

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

Activision banking and Bankset are laid out (see
[`docs/cartridges.md`](docs/cartridges.md)). Bankset images disassemble with MARIA's half as data, rebuild exactly, and the census and the screen rebuild read each half as the chip that uses it; which of MARIA's banks is in view is assumed, not yet confirmed (that doc says how). SOUPER and the 512K flat layout are
recognised and refused with an explanation rather than laid out wrongly.

## Examples

`examples/exo-annotations.json` — a real annotation file worked out with these
tools, for a 512K homebrew whose inter-bank calls go through a trampoline that
`RTS`es into the destination. It shows what the format looks like when the
tracer needs help, and why.
