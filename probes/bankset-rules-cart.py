"""Build a bank-RAM bankset cartridge that obeys, or breaks, the spec's two rules.

The Bankset page (https://7800.8bitdev.org/index.php/Bankset_Bankswitching) says that on a
cartridge with bank RAM "Your DL must be stored in console ram, rather than bankset cart ram" and
that "execution from Sally's cart-ram isn't supported". This makes one image per case so an
emulator, or the simulator, can be shown each:

    python probes/bankset-rules-cart.py ok   rules-ok.a78     # a white box: nothing is broken
    python probes/bankset-rules-cart.py dl   rules-dl.a78     # display list in the cart RAM
    python probes/bankset-rules-cart.py exec rules-exec.a78   # code run from the cart RAM

All three draw the same box from graphics the CPU wrote through $C000-$FFFF into MARIA's RAM
(allowed: that is what the RAM is for). They differ in one thing:

  ok    the list of lists and display list are in console RAM. A white box on black. No warning.
  dl    they are in MARIA's cart RAM ($4000). MARIA reads them as empty: no box; one warning
        ("MARIA fetched a display list from bankset cart RAM") in the a7800 port of MAME,
        and a "note:" from tools/simprobe.py.
  exec  as `ok`, then the CPU copies a four-byte routine into its own cart RAM and jumps to it;
        the routine turns the background red. Where it is refused (the port of MAME, the
        simulator) the background stays black and there is one warning ("the CPU fetched an
        instruction from bankset cart RAM"); an emulator that allows it shows a red background.

The image is flat bankset with bank RAM (header $6000), 64K. Nothing here is a ROM from anywhere:
it is generated, and not kept in the repository.
"""
import os, sys, struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "tools"))
import asm

GFX = 0xC200        # where the CPU writes the box, which MARIA reads at $4200
ROUTINE = (0xA9, 0x44,          # LDA #$44   (a red-ish colour)
           0x85, 0x20,          # STA BACKGRND
           0x4C, 0x04, 0x40)    # JMP $4004: spin here (the JMP itself)


def build(path, mode):
    if mode not in ("ok", "dl", "exec"):
        raise SystemExit("mode is ok, dl or exec")
    dll, dl = (0x4000, 0x4100) if mode == "dl" else (0x1800, 0x1900)
    wr = lambda a: 0xC000 + (a - 0x4000) if a >= 0x4000 else a      # how the CPU reaches it
    L = []; a = L.append
    a("    .org $8000")
    a("reset:")
    for s in ("SEI", "CLD", "LDX #$FF", "TXS", "LDA #$17", "STA $01", "LDA #$00", "STA $38",
              "STA $01", "LDA #$60", "STA $3C", "LDA #$00", "STA $20", "LDA #$0F", "STA $21",
              "STA $22", "STA $23"):
        a("    " + s)
    # the box: 16 scanlines of 8 white bytes, in MARIA's cart RAM (allowed)
    a("    LDA #$FF")
    for line in range(16):
        for k in range(8):
            a("    STA $%04X" % (GFX + line * 0x100 + k))
    # the list of lists: 12 zones of 16 lines; zone 5 has the box (4-byte entry: low address,
    # palette+width, high address, x), the rest are empty
    for z in range(12):
        d = dl + z * 4
        for i, v in enumerate((0x0F, d >> 8, d & 0xFF)):
            a("    LDA #$%02X" % v)
            a("    STA $%04X" % wr(dll + z * 3 + i))
    a("    LDA #$00")
    for z in range(12):
        if z != 5:
            a("    STA $%04X" % wr(dl + z * 4 + 1))                       # an empty list
    box = dl + 5 * 4
    for i, v in enumerate((GFX & 0xFF, 0x18, 0x42, 0x40)):          # width 8 -> 32-8=24=$18
        a("    LDA #$%02X" % v)
        a("    STA $%04X" % wr(box + i))
    # zone 5's list is at `box`, so point its DLL entry there; every other zone's list is zeros
    a("    LDA #$%02X" % (box >> 8))
    a("    STA $%04X" % wr(dll + 5 * 3 + 1))
    a("    LDA #$%02X" % (box & 0xFF))
    a("    STA $%04X" % wr(dll + 5 * 3 + 2))
    a("    LDA #$%02X" % (dll >> 8)); a("    STA $2C")
    a("    LDA #$%02X" % (dll & 0xFF)); a("    STA $30")
    a("    LDA #$40"); a("    STA $3C")                                  # DMA on
    if mode == "exec":
        for i, v in enumerate(ROUTINE):
            a("    LDA #$%02X" % v)
            a("    STA $%04X" % (0x4000 + i))
        a("    JMP $4000")
    a("spin:")
    a("    JMP spin")
    a("nmi:")
    a("    RTI")
    a("code_end:")
    a("    .res $FFFA-code_end,$00")
    a("    .word nmi"); a("    .word reset"); a("    .word reset")
    data = asm.Assembler().assemble("\n".join(L).splitlines())
    assert len(data) == 0x8000, len(data)
    data += bytes(0x8000)                    # MARIA's half: nothing to read in it
    h = bytearray(128); h[0] = 1; h[1:10] = b"ATARI7800"
    h[17:17 + 16] = ("RULES " + mode).upper().encode().ljust(16)
    h[49:53] = struct.pack(">I", len(data)); h[53:55] = struct.pack(">H", 0x6000)
    h[55] = 1; h[56] = 1
    h[100:128] = b"ACTUAL CART DATA STARTS HERE"
    open(path, "wb").write(bytes(h) + data)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    build(sys.argv[2], sys.argv[1])
    print("wrote %s" % sys.argv[2])
