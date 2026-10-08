#!/usr/bin/env python3
"""A synthetic 128K SuperGame + POKEY cartridge, built from source, with answers.

    python tests/synth.py out.a78            # NTSC
    python tests/synth.py out.a78 --pal

The toolkit's tests need a cartridge whose behaviour is known in advance, that
is banked, and that makes sound -- and no commercial ROM can be shipped. This is
newgame.py's cartridge (display, sprite, joystick) with a layer on top that
does the things a static tracer finds hard, each of them on purpose:

  * a COMPUTED bank switch: the bank number comes from a table (`LDA
    bank_table,X / STA $8000`), so no constant reaches the store and the tracer
    reports the switch unresolved;
  * a `JMP (vector)` through a RAM vector filled in at run time, so the handler
    it reaches (`handler_a`) is on no path the tracer can follow;
  * a POKEY tune played from bank 3, with its frequency and control tables
    there -- an audio table to find, and POKEY writes to hear;
  * text in bank 5 that is never code; banks 0 and 6 that are never touched.

`facts()` says what a correct tool must find. Every bank's routine sits at
$8000, and bank 7 is the fixed $C000 half.

The result is deterministic: the same bytes every time, so a copy committed at
tests/carts/synth128.a78 can be compared against a fresh build.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")
sys.path.insert(0, TOOLS)

import asm        # noqa: E402
import newgame    # noqa: E402

BANK_SIZE = 0x4000
NBANKS = 8
EXECUTED = (1, 2, 3, 4)          # banks the game switches to, in table order
TUNE_F = (0x7B, 0x6E, 0x62, 0x5C, 0x52, 0x49, 0x41, 0x3A)   # AUDF1 per step
TUNE_C = 0xA8                                               # pure tone, volume 8
TEXT = b"SYNTH CART BANK FIVE"


def bank_source(n):
    """Assembly for switched bank n: one entry point at $8000."""
    L = ["    .org $8000", "entry:"]
    if n == 3:
        # the tune: step through a table of AUDF values, one per frame
        L += ["    LDA $81", "    AND #$07", "    TAX",
              "    LDA tune_f,X", "    STA $4000        ; AUDF1",
              "    LDA tune_c,X", "    STA $4001        ; AUDC1", "    RTS",
              "tune_f:", "    .byte " + ",".join("$%02X" % v for v in TUNE_F),
              "tune_c:", "    .byte " + ",".join("$%02X" % TUNE_C for _ in TUNE_F)]
    elif n in EXECUTED:
        L += ["    INC $%02X          ; this bank's hit counter" % (0xB0 + n),
              "    RTS"]
    elif n == 5:
        L += ["    RTS", "text:",
              "    .byte " + ",".join("$%02X" % c for c in TEXT)]
    else:
        L += ["    RTS", "marker:", "    .byte $%02X,$%02X,$%02X,$%02X" % ((n,) * 4)]
    L += ["end_of_bank:", "    .res $C000-end_of_bank,$FF"]
    return "\n".join(L) + "\n"


EXTRA_CODE = """
NMICNT     = $0092
VEC        = $00A0          ; a RAM vector, filled in by synth_init
BANKSEL    = $8000

synth_init:
    LDA #$00
    STA $4008               ; POKEY AUDCTL
    LDA #$03
    STA $400F               ; POKEY SKCTL: out of init mode
    LDA #<handler_a
    STA VEC
    LDA #>handler_a
    STA VEC+1
    RTS

; once a frame, from the main loop
synth_tick:
    LDA FRAME
    AND #$03
    TAX
    LDA bank_table,X        ; the bank number comes from a table ...
computed_switch:
    STA BANKSEL             ; ... so this switch cannot be resolved statically
    JSR $8000               ; every switched bank has its routine here
    JMP (VEC)               ; a jump through a RAM vector: handler_a
handler_a:
    RTS
bank_table:
    .byte %s
"""


def fixed_source(title):
    s = newgame.source(title)
    s = s.replace("    TXS\n", "    TXS\n    JSR synth_init\n", 1)
    s = s.replace("    INC FRAME\n", "    INC FRAME\n    JSR synth_tick\n", 1)
    s = s.replace("nmi:\n    PHA\n", "nmi:\n    INC NMICNT\n    PHA\n", 1)
    extra = EXTRA_CODE % ",".join("$%02X" % b for b in EXECUTED)
    assert "code_end:\n" in s
    s = s.replace("code_end:\n", extra + "\ncode_end:\n", 1)
    return s


def header(size, title, region="ntsc"):
    h = bytearray(128)
    h[0] = 1
    h[1:10] = b"ATARI7800"
    t = title.encode("latin1")[:32]
    h[17:17 + len(t)] = t
    h[49:53] = struct.pack(">I", size)
    h[53:55] = struct.pack(">H", 0x0003)       # POKEY at $4000 + SuperGame
    h[55] = 1                                  # joystick
    h[56] = 1
    h[57] = 1 if region == "pal" else 0
    h[100:128] = b"ACTUAL CART DATA STARTS HERE"
    return bytes(h)


def build(region="ntsc", title="Synth128"):
    """(cartridge bytes with header, facts) for the synthetic cartridge."""
    banks = []
    for n in range(NBANKS - 1):
        a = asm.Assembler()
        data = a.assemble(bank_source(n).splitlines())
        assert len(data) == BANK_SIZE, (n, len(data))
        banks.append((data, a.sym))
    fixed = asm.Assembler()
    fdata = fixed.assemble(fixed_source(title).splitlines())
    assert len(fdata) == BANK_SIZE, len(fdata)
    image = b"".join(b[0] for b in banks) + fdata
    sym = fixed.sym
    facts = {
        "banks": NBANKS,
        "executed_banks": list(EXECUTED),
        "never_executed_banks": [n for n in range(NBANKS - 1) if n not in EXECUTED],
        "window_entry": 0x8000,
        "reset": sym["reset"], "nmi": sym["nmi"],
        "handler_a": sym["handler_a"],          # reached only via JMP (VEC)
        "ram_vector": 0x00A0,
        "computed_switch": sym["computed_switch"],   # the STA BANKSEL
        "bank_table": sym["bank_table"],
        "frame_counter": 0x0081, "nmi_counter": 0x0092,
        "hit_counters": {n: 0xB0 + n for n in EXECUTED if n != 3},
        "sprite_row": 0xD000, "sprite_width": 4,
        "pokey": 0x4000, "tune_f": TUNE_F, "tune_c": TUNE_C,
        "tune_table_bank": 3,
        "text": TEXT.decode(), "text_bank": 5,
        "sym": sym,
    }
    return header(len(image), title, region) + image, facts


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    pal = "--pal" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        return 2
    data, _facts = build("pal" if pal else "ntsc")
    with open(args[0], "wb") as f:
        f.write(data)
    print("wrote %s (%d bytes)" % (args[0], len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
