# Using MAME as an instrument

An emulator is the only place a hypothesis about a ROM becomes a fact. Used
carelessly it is also an excellent way to manufacture confident nonsense.

## Read this before writing a tap

```lua
TAP = mem:install_write_tap(lo, hi, "name", fn)
```

The global assignment is not style. `install_write_tap` returns a tap object,
and if nothing holds a reference to it, Lua's garbage collector reclaims it and
**the tap stops firing without any message**. It typically survives boot, dies a
few hundred frames in, and leaves you with output that looks fine.

This cost an afternoon and produced two published-then-retracted claims. Guard
against it: have every probe count writes to an address you know is busy, and
print that count. A number that stops growing means a dead tap, not a quiet game.

More generally: **treat every negative result from an emulator probe as
unproven until you have shown the probe still works.** "I saw nothing" and "my
instrument was switched off" look identical from the outside.

**A third way to switch the instrument off without noticing: a callback that
errors.** A tap function that indexes `cpu.state` with a register name that
doesn't exist for the CPU core in use throws a Lua runtime error -- and that
error is swallowed the same way a GC'd tap or a written-once-then-abandoned
address is: the script keeps running, prints its normal progress lines, and
just never adds an event. On the m6502 core the stack pointer is
`cpu.state["SP"]`, not `cpu.state["S"]` -- a plausible guess that produces
exactly this failure, discovered only by writing a two-line probe that dumps
`for k,v in pairs(cpu.state) do print(k) end` once at startup and reading the
real key names back. Do that dump the first time any new probe touches a
register beyond `PC`, before trusting a clean-looking run with zero results.

## Headless runs

```
mame a7800  -cart game.a78 -autoboot_script probe.lua -video none -sound none -nothrottle -str 30
mame a7800p -cart game.a78 ...
```

`tools/runprobe.py game.a78 PROBE -o out -e KEY=VALUE` is that command with the machine name, the BIOS, the output folder and the environment worked out for you, and it reports which files the probe wrote (`--list` shows every probe). `tools/workbench.py` exposes the same thing as *Run any probe*.

* `a7800` is NTSC, `a7800p` is PAL. Using the wrong one against a PAL image
  gives you a game that runs but times everything wrong.
* `-str N` exits after N seconds of emulated time; combined with `-nothrottle`
  a thirty-second run takes a couple of seconds.
* `-video none -sound none` for anything scripted. Add `-snapshot_directory` and
  call `manager.machine.video:snapshot()` when you want frames.
* The BIOS must be findable: `-rompath` at the directory holding the 7800 BIOS
  images.

## Driving the game

Nothing reaches a hypothesis if the game is sitting on its title screen.

```lua
local fire = manager.machine.ioport.ports[":buttons"].fields["P1 Button 1"]
local joy  = manager.machine.ioport.ports[":joysticks"]
fire:set_value(1)
joy.fields["P1 Up"]:set_value(1)
```

Pulsing fire (`(F % 40) < 8`) gets through title screens and dialogue reliably.
Scripted movement is much less reliable -- games check collision and timing, and
"hold up for 200 frames" often does not arrive anywhere. When you need a
specific game state, record it by hand instead:

```
mame a7800 -cart game.a78 -record session.inp
mame a7800 -cart game.a78 -playback session.inp -autoboot_script probe.lua
```

A recording is reproducible and can be replayed against a *modified* ROM, which
is how you compare stock and patched behaviour on identical input. That only
holds while your edits do not change timing or input handling -- a patch that
shifts a frame count will desynchronise a recording, and the divergence is
usually obvious (the player walks into a wall).

**`-playback`/`-record` silently resolve through `input_directory`, not your
given path.** MAME's `input_directory` setting (default `inp`, persisted in a
generated `mame.ini` you may not know exists) gets prepended to whatever you
pass `-playback`/`-record` -- including an absolute path, at least on the
version this was checked against. A recording sitting right where you pointed
still fails with `Input file ... not found` for no visible reason. Fix by
passing `-input_directory .` (or wherever the file actually lives) explicitly;
don't waste time re-checking the path itself once you've already confirmed the
file exists on disk.

## Watching the right thing

Three probe shapes cover most needs:

**Count writes to a region** -- which addresses are hot, how many distinct
values, when each was first touched. Good for finding where a variable lives.
`probes/watch.lua`.

**Snapshot memory at a chosen frame** -- then decode it offline with a real
tool rather than in Lua. `probes/dumpdl.lua` does this for display lists, and
prints the exact command to decode what it dumped.

**Assert a model** -- the strongest kind. Compute what your model predicts,
compare against what the machine did, and print only when they differ. A probe
that prints nothing for 1,800 frames and then prints one line has told you far
more than one that prints 1,800 lines.

## Registers you cannot read

`DPPH`/`DPPL` are write-only, so there is no way to ask a running machine where
the screen is being drawn from -- you have to watch the write. The same applies
to most MARIA registers. Where a game keeps a RAM shadow of a register, prefer
reading the shadow; where it does not, tap the write.

## Comparing two builds

Run both, capture the same measurement from each, diff the measurements -- not
the screenshots. Screenshots differ for uninteresting reasons (one frame of
animation phase) and are identical for interesting ones (a colour that only
changes on alternate frames). A number extracted at a known frame is a better
witness than a picture.

When a change *should* be invisible, that is a test: build it, run both, and
show the measurement is unchanged.

## Which emulator to use, and for what

Two are worth having, and they are good at different things.

**MAME** (v0.287 here) is what the probes run on. Its Lua exposes
`install_write_tap`, which is how `probes/audio.lua`, `dumpdl.lua` and
`watch.lua` see anything at all.

**[`a7800`](https://github.com/7800-devtools/a7800)** (v5.2 here) is the
7800-devtools fork of MAME's driver. It is ahead on hardware fidelity --
corrected POKEY poly9 sequence and init state, better two-tone mode, accurate
MARIA DMA hole penalties, mid-scanline register updates -- and ahead on
cartridge formats, with Bankset bankswitching and POKEY@800 for several layouts
that MAME answers `Unsupported mapper` for.

### The audio model agrees with both

Every distortion, at three divider and clock settings, plus poly9 on two
clocks: **1.0000 against a7800 and 1.0000 against MAME**. The fork's poly9
correction does not change the sequence a phase-aligned comparison sees. So
capturing on MAME is sound; the register stream and the chip model both check
out against the more accurate emulator.

### But the probes only run on MAME

`a7800` v5.2 forks an older MAME whose Lua predates `install_write_tap`:

```
[LUA ERROR] attempt to index (get) lua_nil value "install_write_tap"
```

Its Lua also exposes the machine as `manager:machine()` rather than
`manager.machine`. The probes now resolve that difference themselves, so they
load cleanly on both, but on `a7800` they cannot install the taps and there is
no headless substitute -- `-debugscript` watchpoints print to the debugger
console, not stdout.

**So today: capture on MAME, check sound accuracy on a7800.** The split costs
nothing for audio, because the capture records what the *cartridge writes*,
which does not depend on how well the chip is emulated.

### But a7800 can be probed too, without touching its C++

The Lua tap is missing; the debugger is not. `-debugscript` sets watchpoints,
and a watchpoint action can call `logerror`, which `-log` sends to `error.log`:

```
wpset 4000,10,w,1,{logerror "AUD %04X %02X
",wpaddr,wpdata; g}
go
```

```
a7800 a7800 -cart game.a78 -debug -debugscript wp.txt -log       -sound none -video none -nothrottle -str 4
```

That captured **694 POKEY writes** from Ballblazer headlessly. Frame boundaries
come from the other side: a7800's Lua does expose `machine:logerror()`, so an
`-autoboot_script` can stamp `FRAME n` into the same log, giving the same
per-frame structure `probes/audio.lua` produces.

That probe is written. `capture.py` recognises the fork from its executable
name and switches routes by itself:

```
python tools/capture.py game.a78 --mame .../a7800.exe
  machine    a7800 (NTSC)  [a7800 fork: debugger watchpoints]
  song       game.trk -- 719 rows, 172 with a change
```

It writes the watchpoint script for whichever chip the header declares, runs
the fork with `-debug -debugscript ... -autoboot_script probes/a7800-frames.lua
-log`, and folds `error.log` back into the ordinary per-frame format. TIA and
POKEY both work. Two details cost an hour each and are worth knowing:

* **The newline in the watchpoint's format string must reach the debugger as
  backslash-n**, not as a real newline. A real one ends the command, and the
  watchpoint then never fires -- leaving a log full of frame markers and no
  writes, which looks like a game that makes no sound.
* **Lua's `logerror` prefixes its lines with `[luaengine] `** while the
  debugger writes its own bare. Anchoring the parse at the start of the line
  drops every frame marker, and with them every row.

### How closely the two agree

Capturing Ballblazer on both and aligning: **561 of 658 rows identical, 85.3%**.
The residue is not the probe. The fork's MARIA DMA timing differs from MAME's,
so the game's own code runs at a slightly different rate and reaches its music
at slightly different moments. Expect agreement, not equality, and treat a
capture as a record of *that emulator's* run.

The alternative, if the Lua tap is wanted properly, is upstream work: a7800
would need `address_space::install_write_tap` (MAME's `memory_passthrough_handler`
machinery, which post-dates its fork point) plus the Lua binding for it. That is
a core memory-system backport, not a small patch -- which is exactly why the
watchpoint route is worth having.

### Does MAME run the image at all? (`mamecheck.py`)

Every comparison here assumes MAME can run the cartridge. Measured on a stratified 278-image
sample of the 1,309-image library (MAME 0.264, each image run for 360 frames after the BIOS hands
over; "live" is MARIA kept drawing from a display list; the question is also asked of the
simulator, and MAME is checked against the cartridge's own vectors so a BIOS built-in game is
not counted as the cartridge):

| | images |
|---|---|
| both run it | 256 (92.8%) |
| simulator only | 15 (5.4%) |
| MAME only | 2 (0.7%) |
| neither | 3 (1.1%) |
| not compared (could not be laid out: SOUPER, the 512K flat SN Cart Demo) | 2 |

*Simulator only* is MAME's gap, not the simulator's: the 14 bankset images ("Unsupported mapper"
-- the OpenBIOS game runs instead), five Activision images in the usual (AM) block order (MAME
runs the (OM) order and leaves the BIOS running on these), and one image whose header MAME
rejects (SuperCart bit missing). On those MAME cannot be the reference; the `a7800` fork runs
the bankset ones. *MAME only* is the simulator's gap: Bad Apple Demo (not drawn at all) and Turret
Turmoil (a KIL at `$D4A7` stops the program at frame 55; undiagnosed). Display-interrupt
timing disagrees on 15 images, mostly where one side has no live list. Rerun it on a new MAME
before trusting any of this: `python tools/mamecheck.py /path/to/roms --sample 60 --cache c`.

### Running the fork, and what it showed about bankset cartridges

The fork's source is at <https://github.com/7800-devtools/a7800>. It is a MAME tree from
2019, so a current toolchain (GCC 13, Python 3.13, GNU make 4.3) needs a handful of patches
to build; none touches the 7800 code, and the build is about fifteen minutes on four cores
with `make -j4 NOWERROR=1 TOOLS=0 USE_QTDEBUG=0 NO_USE_MIDI=1 NO_USE_PORTAUDIO=1 OPTIMIZE=1
SYMBOLS=0` (it builds only the 7800 driver; the binary is `mame64`):

* `scripts/build/msgfmt.py`: `array.tostring()` was removed in Python 3.9 -- `tobytes()`.
* `src/devices/cpu/*/*make.py` (m6502, m6809, mcs96, tms57002): `open(f, "rU")` is gone in
  Python 3.11 -- `"r"`.
* `3rdparty/sol2/sol/stack_push.hpp`: the three `stack::push<const wchar_t*>(L, str, str + sz)`
  calls (and the `char16_t`, `char32_t` ones) are ambiguous under GCC 13 -- cast `str`.
* `src/osd/modules/render/bgfx/effect.h`: add `#include <string>`.
* the generated `build/projects/sdl/mame/gmake-linux/*.make`: GNU make 4.3 drops the space
  `max_args` relied on, so `ar -qc libemu.a../../obj/...` -- put a space in
  `$1$(_args)`, then build again with `REGENIE=` so the files are not regenerated.

Run it headless with `-video none -sound none -nothrottle -str N`; `probes/a7800-snap.lua`
takes a snapshot at a chosen frame, and `tools/forkshot.py` does both and sets the picture
beside the simulator's. The BIOS is optional (it warns and runs).

What that showed, on 15 bankset images (the ten Bankset Test demos, Attack of the Petscii
Robots, Bubble Bobble, StoneAge and Pit Fighter's two prototypes):
the simulator and the fork draw the same screens on the demos -- the text, the plasma
background, Attack of the Petscii Robots down to its "GAME START TIMER 191" -- except that
the demos' bank counters differ with the frame reached. The checks found and fixed two things
the simulator got wrong: **bank RAM** (the CPU's writes to `$C000-$FFFF` land in MARIA's 16K
at `$4000`; the "BANKRAM 1" line on the 2x128K RAM demo is text written there, and it was
blank before) and **ROM at `$4000` on a flat bankset with POKEY at `$4000`** (the fork routes
only writes to the chip; StoneAge executes `JMP $4000` and the simulator stopped on a KIL).
Still different: Pit Fighter (Alt 1) shows stripes on the fork and the simulator never gets
a display list; Bubble Bobble's title screen on the fork is gameplay on the simulator (it
holds fire; the fork was not driven); and one Pit Fighter frame colours its ground
differently. Those are not diagnosed.

### Neither emulates the second POKEY

Both instantiate one chip for a dual-POKEY cartridge:

```
Starting Atari 7800 ROM Carts w/POKEY @ 0x0450 ':cartslot:a78_p450_t0'
Starting Atari C012294 POKEY ':cartslot:a78_p450_t0:pokey450'
```

a7800 at least recognises the header rather than reporting an unsupported
mapper, but the `$0440` chip is still not there. This is why the toolkit
captures POKEY from the **CPU bus** instead of the device: it records what the
game writes to both chips whether or not anything is listening, and
`tracker.py` renders all eight voices from that. See `docs/audio.md`.

## Running MAME with no Atari BIOS

The probes need a booting 7800, and MAME's `a7800` needs a BIOS the toolkit
cannot ship. 7800OpenBIOS (CC0, `docs/bios.md`) stands in. This is the whole
setup, as run on Ubuntu 24.04 with MAME 0.264 -- older than the 0.287 the rest
of these notes were measured on, and every probe in `probes/` ran on it:

    sudo apt-get install mame dasm
    git clone --depth 1 https://github.com/7800-devtools/7800OpenBIOS
    (cd 7800OpenBIOS && dasm 7800openbios.asm -f3 -v0 -I. -o7800openbios.bin)
    mkdir -p bios/a7800
    cp 7800OpenBIOS/7800openbios.bin bios/a7800/c300558-001a.u7

    export A7800_MAME=/usr/games/mame A7800_ROMPATH=$PWD/bios A7800_BIOS=a7800pr

`A7800_BIOS` is passed to MAME as `-bios`; `capture.py`, `session.py`,
`replay.py` and `regress.py` all honour it. MAME prints `c300558-001a.u7 WRONG
CHECKSUMS` and runs it anyway. With those three variables set, `selftest.py`
adds a check that runs the generic probes under MAME on the synthetic
cartridge, whose answers are known (sprite row at `$D000`, frame counter at
`$81`), and measures a recording it makes itself.

Things this turned up that the notes above did not say:

* **`-exit_after_playback`** is what stops MAME where a recording stops. Without
  it the game carries on under live control after the last recorded input, and
  `reclength.lua` runs on to its safety cap.
* **`emu.register_stop` is deprecated** in current MAME ("use
  emu.add_machine_stop_notifier"); the older a7800 fork may have only the
  former, so probes try the notifier first and fall back.
* **`-video soft` hangs** in a container with no display. Use `-video none`;
  `snapstop.lua` takes its screenshot at machine stop and MAME writes it under
  `~/.mame/snap/a7800/`.
* **`whocalls.lua` needs the debugger**, which a headless run does not have.
* **Read-modify-write instructions write twice** -- see `pcwrites.lua`.

* **The debugger works headless**, which gives instruction traces and what
  `whocalls.lua` needs, if Qt is told not to look for a display:

      QT_QPA_PLATFORM=offscreen mame a7800 ... -video none -debug \
          -debugscript trace.txt -seconds_to_run 12

  with `trace out.log,0,noloop` then `go` in the script (`noloop` keeps a wait
  loop to one line -- `docs/pitfalls.md` explains why a diff needs it). A
  12-second Triple Punch run gives 3.9 million lines. The trace has no bank
  number: a line in `$8000-$BFFF` does not say which bank ran.

## Probe index

Every file in `probes/`. All are parameterised by environment variables (each
file's header lists them); none hard-codes a game's addresses. A probe written
for one game's RAM map belongs in that game's repository, and the pattern it
proved goes here once its addresses have become parameters.

| probe | what it does |
|---|---|
| `watch.lua` | See what a running game does: write taps and logging. |
| `audio.lua` | Log audio register writes for `tracker.py` (`A7800_POKEY=<base>` for cartridge POKEY). |
| `a7800-frames.lua` | Frame markers for the `a7800` fork, alongside a debugger watchpoint log. |
| `a7800-snap.lua` | A screenshot at a chosen frame, for the `a7800` fork (`tools/forkshot.py` runs it). |
| `dumpdl.lua` | Find the display list list and dump RAM so `dlwalk.py` can decode it. |
| `liveslots.lua` | Every ROM address the live display lists reference over a whole run, with the widest object seen -- confirms candidate sprite sheets on evidence. |
| `dumpgfx.lua` | Dump a live game's graphics and MARIA register writes (`dumpgfx_regs.txt`) for `spritedump.py`. |
| `rendersurvey.lua` | MARIA and CPU spend per frame; dumps RAM for `zonebill.py`. |
| `dma-count.lua`, `dma-costcart.py` | Measure CPU cycles that survive DMA. |
| `pokey-polyoracle.py` | Build a cartridge sampling POKEY's RANDOM register at known spacing. |
| `wildfetch.lua` | Stop at the first instruction fetched from where no code should be. |
| `hangsnap.lua` | PC, SP, the stack and chosen bytes at chosen frames, with the interrupt count since the last one: for 'the clock froze'. Compare frames either side of the symptom. |
| `cyclebudget.lua` | Where a frame's CPU cycles go and how many MARIA took: executed cycles (with branch and page-crossing extras), the part inside the NMI, the TIA/RIOT slow-access penalty, `dma` as what is left of the frame, and executed cycles per named PC range. Compare `dma` with `dmabudget.py`'s model of the same display list. Measured first in the Karateka XE port; see its header for what it does not count. |
| `dlitimes.lua` | On which raster line each display interrupt arrives, beside the display list list's zones (start line, height, DLI bit), and the lines at which MSTAT's VBLANK bit rises and falls. How the DLI timing and the 263-line frame were measured; MAME 0.264 has no `screen:vpos()`, so the beam position is worked out from `time_until_pos`. |
| `addrorigin.lua` | Which ROM bytes are addresses: follows each value the CPU uses as a pointer (`(zp),Y`, `JMP (vector)`, RTS-as-jump, self-modified operands) back to the byte it was loaded from, through transfers, stores and pushes. Slow (a few seconds per emulated second). `tools/origins.py` turns the log into address tables and immediates; arithmetic keeps the accumulator's origin, so a pointer plus an offset traces to the pointer's byte. |
| `rates.lua` | How often chosen instructions run, and the gap in frames between reads of a controller port. A game answers no faster than it asks. |
| `pcprof.lua` | A sampling profiler with no timer: the program counter each time MARIA reads a display-list-list entry. `pcmap.py` names the routines. Visible frame only. |
| `forcedl.lua` | Force a display-list entry, graphics and palettes into a running machine and screenshot it: the way to ask MARIA what a mode does. Used by selftest to check `mariapix.py`; needs a cartridge that already builds a display list, such as `tests/carts/synth128.a78`. |
| `exectrace.lua` | Which code a run executes, with its bank, where each `JMP (ptr)` went and what each bank-switch store selected; `dyn.py` turns the log into annotations. Slow (a Lua tap on every ROM read: about 4 s per emulated second here) and checked on MAME 0.264 only. |
| `romcoverage.lua` | Which cartridge bytes a run reads (feeds `modmap.py`). |
| `handover.lua` | State at the moment a cartridge's reset code first runs (see `bios.md`). |
| `threadprof.lua` | Profile a threaded-code (Forth) game while a person plays; read with `forth.py --profile`. Used by `replay.py`. |
| `reclength.lua` | A recording's true length in frames. Run it first on any `.inp`. |
| `ramsnap.lua` | Periodic snapshots of chosen RAM pages: events show as steps in one byte. |
| `diffwrites.lua` | Which RAM addresses are written in a frame window. |
| `pcwrites.lua` | Every RAM write in a window, tagged with the PC that did it -- finds computed-pointer targets. |
| `whocalls.lua` | Log callers of an address via an execution breakpoint. |
| `freeram.lua` | Which candidate bytes a game never writes, with a positive control. |
| `inputreaders.lua` | Which routine reads INPT0-5 / SWCHA / SWCHB, and how often, per bank. |
| `inputtrace.lua` | Log the raw joystick port and decoded stick direction. |
| `peek.lua` | Read fixed addresses at chosen frames. |
| `ramdump.lua` | Dump a RAM range to a file at machine stop. |
| `snap.lua`, `snapat.lua`, `snaprange.lua`, `snapstop.lua`, `snapwhen.lua` | Screenshots at chosen frames, an exact frame, every Nth frame, machine stop, or when a RAM byte says so. |
| `framecounter.lua` | Show the running frame number on screen. |
| `spacelist.lua` | Print the CPU's address spaces and exit. |

Recording sessions for these to replay is `tools/session.py`.
