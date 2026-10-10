"""Build a cartridge that counts POKEY timer interrupts.

POKEY's timers 1, 2 and 4 can interrupt the 6502, and several 7800 music engines run on that
instead of the frame. This cart programs one timer and counts the interrupts in zero page
(`$80` low, `$81` high), so an emulator -- or the simulator -- can be asked how fast it fires:

    python probes/pokey-irqcart.py out.a78 --audctl 00 --audf 20 --irqen 01

  --audctl   AUDCTL: bit 0 selects the 15 kHz clock for the slow channels, bit 6 clocks channel 1
             at 1.79 MHz, bit 5 channel 3, bit 4 joins 1+2 (16 bit), bit 3 joins 3+4
  --audf     AUDF1..AUDF4 as hex bytes, comma separated (default: the same byte for all four)
  --irqen    IRQEN: 01 timer 1, 02 timer 2, 04 timer 4
  --poll     do not rely on the interrupt (MAME and the a7800 fork never deliver POKEY's to the
             CPU): enable the timer in IRQEN and count how often IRQST says it fired
  --pokey    where POKEY is: 4000 (default) or 0450 or 0800 (the header bit that goes with it)

DMA is off, so nothing steals cycles, and the handler acknowledges by clearing and re-enabling
IRQEN. `probes/pokey-irqcount.lua` is not needed: the count is read from RAM at two frames.
"""
import argparse, os, struct, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "tools"))
import asm

HEADER_BIT = {0x4000: 0x0001, 0x0450: 0x0002, 0x0800: 0x8000}


def build(path, audctl=0, audf=(0x20,) * 4, irqen=0x01, base=0x4000, skctl=0x03, poll=False):
    L = []; a = L.append
    P = lambda r: "$%04X" % (base + r)
    a("    .org $C000")
    a("reset:")
    for s in ("SEI", "CLD", "LDX #$FF", "TXS", "LDA #$17", "STA $01", "LDA #$00", "STA $38",
              "STA $01", "LDA #$60", "STA $3C", "LDA #$00", "STA $80", "STA $81"):
        a("    " + s)
    a("    LDA #$%02X" % skctl); a("    STA %s" % P(0x0F))           # out of reset
    a("    LDA #$%02X" % audctl); a("    STA %s" % P(0x08))
    for i, v in enumerate(audf):
        a("    LDA #$%02X" % v); a("    STA %s" % P(2 * i))          # AUDF1..4
        a("    LDA #$00"); a("    STA %s" % P(2 * i + 1))             # AUDC: silent
    a("    LDA #$%02X" % irqen); a("    STA %s" % P(0x0E))
    a("    STA %s" % P(0x09))                                          # STIMER
    if poll:
        a("poll:")
        a("    LDA %s" % P(0x0E)); a("    AND #$%02X" % irqen)
        a("    CMP #$%02X" % irqen); a("    BEQ poll")             # IRQST reads active low
        a("    INC $80"); a("    BNE phi"); a("    INC $81"); a("phi:")
        a("    LDA #$00"); a("    STA %s" % P(0x0E))
        a("    LDA #$%02X" % irqen); a("    STA %s" % P(0x0E))
        a("    JMP poll")
    else:
        a("    CLI")
    a("spin:")
    a("    JMP spin")
    a("irq:")
    a("    PHA")
    a("    INC $80")
    a("    BNE nohi")
    a("    INC $81")
    a("nohi:")
    a("    LDA #$00"); a("    STA %s" % P(0x0E))                      # acknowledge ...
    a("    LDA #$%02X" % irqen); a("    STA %s" % P(0x0E))            # ... and keep enabled
    a("    PLA")
    a("    RTI")
    a("nmi:")
    a("    RTI")
    a("code_end:")
    a("    .res $FFFA-code_end,$00")
    a("    .word nmi"); a("    .word reset"); a("    .word irq")
    data = asm.Assembler().assemble("\n".join(L).splitlines())
    assert len(data) == 0x4000, len(data)
    h = bytearray(128); h[0] = 1; h[1:10] = b"ATARI7800"
    h[17:30] = b"POKEY IRQ CNT"; h[49:53] = struct.pack(">I", len(data))
    h[53:55] = struct.pack(">H", HEADER_BIT[base]); h[55] = 1; h[56] = 1
    h[100:128] = b"ACTUAL CART DATA STARTS HERE"
    open(path, "wb").write(bytes(h) + data)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--audctl", default="00")
    ap.add_argument("--audf", default="20")
    ap.add_argument("--irqen", default="01")
    ap.add_argument("--pokey", default="4000")
    ap.add_argument("--skctl", default="03")
    ap.add_argument("--poll", action="store_true")
    a = ap.parse_args()
    f = [int(x, 16) for x in a.audf.split(",")]
    f = (f * 4)[:4] if len(f) == 1 else (f + [0] * 4)[:4]
    build(a.out, int(a.audctl, 16), tuple(f), int(a.irqen, 16), int(a.pokey, 16), int(a.skctl, 16), a.poll)
    print("wrote", a.out)
