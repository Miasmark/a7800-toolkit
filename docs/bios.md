# The 7800 BIOS: Atari's NTSC BIOS and 7800OpenBIOS

What happens between power-on and a cartridge's first instruction, and what
[7800OpenBIOS](https://github.com/7800-devtools/7800OpenBIOS) (Mike Saarna, Bob
DeCrescenzo; CC0 source, commit `b8bc745`) does differently from Atari's NTSC
BIOS. That is MAME's default `7800.u7`, 4K at `$F000`, SHA-1
`d9d134bb6b36907c615a594cc7688f7bfcef5b43`.

The Atari side was read from a disassembly of `7800.u7`. It was traced from
its vectors, and the code it copies to RAM was traced at the RAM addresses it
runs from. Both sides were then measured in MAME 0.287 with
`probes/handover.lua`, which records the frame, registers, flags, INPTCTRL
writes and RAM at the cartridge's first instruction. The cartridge was Pole
Position II VS (48K), signed and unsigned. No BIOS bytes are reproduced here.

## The differences

| | Atari NTSC BIOS | 7800OpenBIOS |
|---|---|---|
| Power-on self-test | CPU instruction and flag test, then a RAM test of every page with six patterns; an error jumps to `$F880` with a code in Y | none |
| Cartridge signature | hashed and checked (Rabin, `$FF80-$FFF7`); a mismatch runs the cartridge in **2600 mode** | **not checked**: any cartridge that passes the header tests runs as a 7800 game |
| Header tests | `$FE00`/`$FD80` compare, reset vector not `$0000`/`$FFFF`, `$FFF8` region byte, `$FFF9` start page (`$2306-$235A`, run from RAM) | the same tests in the same order, plus two it needs without a signature: a 4K-mirror test at `$1BEA` and 2600 vector mirrors at `$DFFA`/`$BFFA`/... |
| No cartridge | 2600 mode | its built-in game (KiloParsec) |
| Boot screen | small Fuji over striped ATARI, shown while the hash runs | large colour-cycling Fuji and "ATARI 7800" (320A), shown for a fixed time; holding B/W skips it |
| Hand-over frame (PP2 VS, 48K) | 204 signed; unsigned, 209 via the 2600 path | 322, signed or not (about 5.3 s, whatever the cartridge) |
| INPTCTRL at hand-over | `$16` (`$26BB`), not locked | `$06`, not locked |
| Stack pointer | **`$16`** (the BIOS does `LDX #$16 / STX $01 / TXS`) | `$FF` |
| Decimal flag | **set** (`SED` at `$26BE`) | clear (`CLD`) |
| X at hand-over | `$16` | `$FF` |
| RAM left behind | hash and signature workspace, the BIOS's own code at `$2300-$27FF`, display data at `$1984-$1F84`; `$40-$48` hold hash bytes (`87 47 E2 5E 33 2C DD 18 64` for PP2 VS) | `$2000-$27FF` cleared, then `$2000-$203F` and `$2100-$213F` set to `$FF`; `$40-$48` set to `$FF`; its own code and lists at `$2300`/`$2600` |
| MARIA at hand-over | `CTRL = $60` set during the check | `CTRL = $60`, and it waits for vblank before jumping |

Measured RAM difference at the first cartridge instruction (PP2 VS, signed):

- zero page `$40-$FF`: 71 of 192 bytes differ;
- stack page: 70 of 256;
- `$1800-$27FF`: 3,522 of 4,096. OpenBIOS leaves 3,435 zeros there,
  Atari 582.

OpenBIOS's source says it sets "the 6502 state similar to when leaving the
NTSC BIOS" with `SP = $FF`, `CLD` and `$40-$48 = $FF`. The NTSC BIOS as
measured leaves `SP = $16`, decimal set and hash bytes in `$40-$48`.

## What it means

- **Unsigned cartridges.** On OpenBIOS they boot as 7800 games, homebrew and
  test builds included. On an NTSC console with Atari's BIOS they run in 2600
  mode, which looks like a black screen (`sign7800.py`).
- **MAME hides the difference.** MAME's driver does not emulate 2600 mode
  (a TODO in its source), so an unsigned cartridge on the Atari BIOS still
  plays, through the 2600 bootstrap (INPTCTRL `$02` from `$26C4`, then `$00`
  from `$0483`). In MAME an unsigned build therefore starts from the 2600
  path's state (SP `$FF`, decimal clear, frame 209). A signed build starts
  from the 7800 path's (SP `$16`, decimal set, frame 204). See pitfalls,
  "Re-signing a cartridge moves the start of the game".
- **Recordings do not cross BIOSes.** The hand-over frame differs by more
  than a hundred frames, so an `.inp` recorded under one BIOS is a different
  run under the other.
- **Games that trust the BIOS's leftovers.** A game that never sets its
  stack pointer, never clears decimal mode, or reads RAM before writing it
  will behave differently under the two. Pole Position II does not: its
  first instructions are `LDA #$07 / STA INPTCTRL / SEI / CLD`, then
  `LDX #$FF / TXS`. Check a game's reset code before assuming either way.
- **2600 cartridges.** Both hand them to the same kind of bootstrap at `$0480`,
  and OpenBIOS's copy follows Atari's. OpenBIOS has to recognise them
  without the signature's help, which is what its extra mirror tests are
  for.

## Running OpenBIOS in MAME

It builds with DASM (`dasm 7800openbios.asm -f3 -o7800openbios.bin`) to a
16K image, `$C000-$FFFF`, which includes the built-in game. MAME's `a7800`
has a second NTSC BIOS slot of that size, `a7800pr`. Keep a separate ROM
folder, so the real set stays intact:

    mkdir -p rp/a7800
    cp 7800.u7 rp/a7800/                      # the Atari BIOS, as it is
    cp 7800openbios.bin rp/a7800/c300558-001a.u7
    mame a7800 -bios a7800pr -rompath rp -cart game.a78

MAME reports `c300558-001a.u7 WRONG CHECKSUMS` and runs it. `-bios a7800`
from the same folder is Atari's.
