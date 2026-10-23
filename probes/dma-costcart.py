"""Build a 7800 cartridge that measures how many CPU cycles survive DMA.

Each frame it zeroes a counter, spins a fixed-cost loop for the whole visible
period, and publishes the count when VBLANK starts. More drawing -> fewer
iterations. Everything about the ROM is identical between runs except the
display lists, so the difference IS the DMA cost.

This is the instrument behind the cost table in docs/hardware.md and the model
in tools/dmabudget.py. It is kept so the numbers can be re-derived rather than
taken on trust -- and re-derived on other emulators, or on hardware.

    python probes/dma-costcart.py                 # writes t.a78, 1 object of 8 bytes
    mame a7800 -cart t.a78 -autoboot_script probes/dma-count.lua \
         -window -seconds_to_run 12 -nothrottle -sound none -video soft

dma-count.lua prints the per-frame iteration count. Multiply by the loop cost
(14.0156 cycles, calibrated with the `nops` parameter) to get cycles, and
compare against a run with dma_on=False for the ceiling.

build() takes the display-list shape: objects per zone, object width, how many
zones carry them, zone height, 4- or 5-byte entries, and character mode with
one or two bytes per character.

With show=True the cartridge also shows its count on the screen, for consoles
and FPGA cores that have no Lua to read it, and is signed so an NTSC console
starts it in 7800 mode. Below the measured zones a 16-line zone holds twelve
digits: the count (current), then the lowest and the highest since tracking
started two seconds in. Each digit is a 2-byte object pointing into a fixed
font; the cartridge changes only which glyph (the object's low address byte,
in a display list in RAM), so the zone costs the same DMA whatever it shows
and every build carries the same constant. Compare counts between show=True
builds, not with show=False ones. One number is converted a frame (current,
lowest, highest in turn), during VBLANK; $92 counts frames where that work ran
past VBLANK (it should stay 0).
"""
import os, sys, struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "tools"))
import asm

ZONE_LINES = 16
ZONES      = 12          # 12 * 16 = 192 visible scanlines
GFX        = 0xD000
DLL        = 0xE000
DLS        = 0xE100
DL_STRIDE  = 64

# show=True: the digit zone's display list in RAM, and the font: digit d is the 2 bytes at
# $F0xx + 2d of every page, the top row on page $FF (MARIA counts its line offset down)
DIGIT_DL   = 0x2200
FONT       = 0xF0
DIGIT_X    = [20 + 44 * g + 8 * i for g in range(3) for i in range(4)]
GLYPHS = {
    0: ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    1: ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    2: ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    3: ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    4: ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    5: ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    6: ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    7: ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    8: ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    9: ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def font_page(row):
    """The 20 bytes of font row `row` (0 is the top, 15 the bottom) of every digit, each glyph
    row shown on two lines. 160A: 4 pixels a byte, the leftmost in bits 7-6; lit is colour 3."""
    out = []
    for d in range(10):
        bits = GLYPHS[d][row // 2] + "000" if row // 2 < 7 else "00000000"
        v = 0
        for c in bits:
            v = (v << 2) | (3 if c == "1" else 0)
        out += [(v >> 8) & 0xFF, v & 0xFF]
    return out


def show_code(a):
    """The VBLANK routine of a show=True build: track the lowest and highest count, convert one
    of the three numbers to decimal digits, and say if that ran past VBLANK."""
    a("show:")
    a("    LDA frames")
    a("    CMP #120")
    a("    BEQ track")
    a("    INC frames")                           # for two seconds the lowest and highest
    a("    LDA res_lo"); a("    STA min_lo"); a("    STA max_lo")   # follow the count
    a("    LDA res_hi"); a("    STA min_hi"); a("    STA max_hi")
    a("    JMP disp")
    a("track:")
    a("    LDA res_lo"); a("    CMP min_lo")       # res < min ?
    a("    LDA res_hi"); a("    SBC min_hi")
    a("    BCS no_min")
    a("    LDA res_lo"); a("    STA min_lo"); a("    LDA res_hi"); a("    STA min_hi")
    a("no_min:")
    a("    LDA max_lo"); a("    CMP res_lo")       # max < res ?
    a("    LDA max_hi"); a("    SBC res_hi")
    a("    BCS disp")
    a("    LDA res_lo"); a("    STA max_lo"); a("    LDA res_hi"); a("    STA max_hi")
    a("disp:")
    a("    LDA phase")
    a("    BNE ph1")
    a("    LDA res_lo"); a("    STA t_lo"); a("    LDA res_hi"); a("    STA t_hi")
    a("    LDX #0"); a("    JMP conv")
    a("ph1:")
    a("    CMP #1")
    a("    BNE ph2")
    a("    LDA min_lo"); a("    STA t_lo"); a("    LDA min_hi"); a("    STA t_hi")
    a("    LDX #16"); a("    JMP conv")
    a("ph2:")
    a("    LDA max_lo"); a("    STA t_lo"); a("    LDA max_hi"); a("    STA t_hi")
    a("    LDX #32")
    a("conv:")                                     # 4 decimal digits of t into the DL at X
    a("    LDY #0")
    a("place:")
    a("    LDA #0"); a("    STA dig")
    a("sub:")
    a("    LDA t_lo"); a("    SEC"); a("    SBC p_lo,Y"); a("    STA tmp")
    a("    LDA t_hi"); a("    SBC p_hi,Y")
    a("    BCC placed")
    a("    STA t_hi"); a("    LDA tmp"); a("    STA t_lo")
    a("    INC dig")
    a("    JMP sub")
    a("placed:")
    a("    LDA dig"); a("    ASL A"); a("    STA $%04X,X" % DIGIT_DL)
    a("    INX"); a("    INX"); a("    INX"); a("    INX")
    a("    INY"); a("    CPY #3"); a("    BNE place")
    a("    LDA t_lo"); a("    ASL A"); a("    STA $%04X,X" % DIGIT_DL)
    a("    INC phase"); a("    LDA phase"); a("    CMP #3"); a("    BNE ph_ok")
    a("    LDA #0"); a("    STA phase")
    a("ph_ok:")
    a("    LDA MSTAT")                            # still in VBLANK?
    a("    BMI in_vb")
    a("    INC overrun")
    a("in_vb:")
    a("    RTS")
    a("p_lo:"); a("    .byte <1000,<100,<10")
    a("p_hi:"); a("    .byte >1000,>100,>10")
    a("digit_dl:")                                 # copied to DIGIT_DL at reset
    for x in DIGIT_X:
        a("    .byte $00,$1E,$%02X,$%02X" % (FONT, x))   # glyph 0, palette 0, 2 bytes wide
    a("    .byte $00,$00")
    a("digit_dl_end:")


def build(path, nobj, width, zones_used=ZONES, dma_on=True, five_byte=False, nops=0, zlines=ZONE_LINES, nzones=ZONES, indirect=False, charwidth1=False, holey=False, dli=False, show=False, title=None):
    L=[]; a=L.append
    a("MSTAT = $28"); a("DPPH = $2C"); a("DPPL = $30"); a("CTRL = $3C")
    a("OFFSET = $38"); a("BACKGRND = $20"); a("P0C1 = $21"); a("P0C2 = $22")
    a("P0C3 = $23"); a("INPTCTRL = $01")
    a("cnt_lo = $80"); a("cnt_hi = $81"); a("res_lo = $90"); a("res_hi = $91")
    if show:
        a("overrun = $92"); a("frames = $93"); a("min_lo = $94"); a("min_hi = $95")
        a("max_lo = $96"); a("max_hi = $97"); a("t_lo = $98"); a("t_hi = $99")
        a("dig = $9A"); a("tmp = $9B"); a("phase = $9C")
    a("    .org $C000")
    a("reset:")
    a("    SEI"); a("    CLD"); a("    LDX #$FF"); a("    TXS")
    a("    LDA #$17"); a("    STA INPTCTRL")
    a("    LDA #$00"); a("    STA OFFSET"); a("    STA INPTCTRL")
    a("    LDA #$60"); a("    STA CTRL")          # DMA off while we set up
    a("    LDA #$00"); a("    STA BACKGRND")
    a("    LDA #$0F"); a("    STA P0C1")
    a("    STA P0C2"); a("    STA P0C3")
    a("    LDA #$00"); a("    STA res_lo"); a("    STA res_hi")
    if show:
        a("    STA overrun"); a("    STA frames"); a("    STA phase")
        a("    LDX #digit_dl_end-digit_dl")
        a("copy_dl:")
        a("    LDA digit_dl-1,X"); a("    STA $%04X,X" % (DIGIT_DL - 1))
        a("    DEX"); a("    BNE copy_dl")
    a("    LDA #>dll"); a("    STA DPPH")
    a("    LDA #<dll"); a("    STA DPPL")
    a("    LDA #$D0"); a("    STA $34")   # CHARBASE
    a("    LDA #$%02X" % ((0x40|(0x10 if charwidth1 else 0)) if dma_on else 0x60)); a("    STA CTRL")
    a("main:")
    a("wait_vb:")                                  # wait until NOT in vblank
    a("    LDA MSTAT"); a("    BMI wait_vb")
    a("    LDA #$00"); a("    STA cnt_lo"); a("    STA cnt_hi")
    a("count:")                                    # 14 cycles/iteration
    a("    INC cnt_lo")
    a("    BNE no_carry")
    a("    INC cnt_hi")
    a("no_carry:")
    for _ in range(nops):
        a("    NOP")
    a("    LDA MSTAT")
    a("    BPL count")                             # spin for the visible period
    a("    LDA cnt_lo"); a("    STA res_lo")
    a("    LDA cnt_hi"); a("    STA res_hi")
    if show:
        a("    JSR show")
    a("    JMP main")
    a("nmi:")                                      # the display-interrupt handler: do nothing
    a("    RTI")
    if show:
        show_code(a)
    a("code_end:")
    a("    .res $D000-code_end,$00")
    a("gfx:")
    a("    .res $1000,$FF")                        # $D000-$DFFF, all pixels lit
    a("dll:")
    for z in range(nzones):
        dl = DLS + z*DL_STRIDE
        # bit 7 raises a display interrupt, bit 6 makes MARIA's fetches from
        # $D000 (address bit 12 set) "holey" -- suppressed
        flags = (zlines-1) | (0x40 if holey else 0) | (0x80 if dli else 0)
        a("    .byte $%02X,$%02X,$%02X" % (flags, dl>>8, dl&0xFF))
    if show:
        a("    .byte $0F,$%02X,$%02X" % (DIGIT_DL >> 8, DIGIT_DL & 0xFF))   # 16 lines of digits
    a("dll_end:")
    a("    .res $E100-dll_end,$00")
    a("dls:")
    for z in range(nzones):
        row=[]
        if z < zones_used:
            for i in range(nobj):
                if indirect:
                    # 5-byte with bit 5 set: the address names a CHARACTER LIST,
                    # and each character is fetched from CHARBASE separately.
                    row += [0x00, 0x60, 0xEF, ((32-width)&0x1F), (i*width)%160]
                elif five_byte:
                    # 5-byte: byte1 has low 5 bits zero (write mode / indirect)
                    row += [0x00, 0x40, GFX>>8, ((32-width)&0x1F), (i*width)%160]
                else:
                    row += [0x00, ((32-width)&0x1F), GFX>>8, (i*width)%160]
        row += [0x00,0x00]
        row += [0x00]*(DL_STRIDE-len(row))
        a("    .byte " + ",".join("$%02X"%b for b in row))
    a("dls_pad:")
    a("    .res $EF00-dls_pad,$00")
    a("charlist:")
    a("    .res $100,$41")
    a("dls_end:")
    if show:
        for page in range(16):                     # the font: page $F0 is its bottom row
            a("    .byte " + ",".join("$%02X" % b for b in font_page(15 - page)))
            a("    .res %d,$00" % (0x100 - 20 if page < 15 else 0x80 - 20))
        a("    .res $78,$FF")                      # the signature ($FF80-$FFF7), made below
        a("    .byte $FF")                         # $FFF8
        a("    .byte $C7")                         # $FFF9: hashed from $C000, no rainbow
    else:
        a("    .res $FFFA-dls_end,$00")
    a("    .word nmi"); a("    .word reset"); a("    .word reset")
    src="\n".join(L)+"\n"
    asm_=asm.Assembler()
    data=asm_.assemble(src.splitlines())
    assert len(data)==0x4000, len(data)
    if show:
        # the count loop's cost is calibrated: its branches must not cross a page
        assert asm_.sym["count"] >> 8 == asm_.sym["main"] >> 8 == (asm_.sym["nmi"] - 1) >> 8
        assert asm_.sym["code_end"] <= 0xD000 and asm_.sym["dls_end"] == 0xF000
        import sign7800
        data = sign7800.signed(bytes(data))
        assert sign7800.verify(data)
    h=bytearray(128); h[0]=1; h[1:10]=b"ATARI7800"
    h[17:26]=b"DMA TEST "
    if title:
        t = title.encode("latin1")[:32]; h[17:49] = t + bytes(32 - len(t))
    h[49:53]=struct.pack(">I",len(data))
    h[53:55]=struct.pack(">H",0); h[55]=1; h[56]=1; h[57]=0
    h[100:128]=b"ACTUAL CART DATA STARTS HERE"
    open(path,"wb").write(bytes(h)+data)

if __name__=="__main__":
    build("t.a78", 1, 8)
    print("ok")
