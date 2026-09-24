#!/usr/bin/env python3
"""
Atari 7800 NTSC colour bytes to RGB.

A colour byte is hue in the high nibble and luminance in the low. Hue 0 is
greyscale; hues 1-15 step around the NTSC colour wheel. This uses the widely
published base RGB per hue at high luminance and scales by the luma nibble,
which is close enough to read artwork by -- real output depends on the console,
the television and the decoder.

gfx.py's earlier approximation derived hue from a phase formula and put hue 9
in the olive range when it should be blue; that is visible immediately on the
zombie sprites, whose faces are blue in play.

For colours that match MAME exactly, use mame7800(), or set
A7800_PALETTE=mame (mame-pal for PAL) and ntsc7800() switches to MAME's own
table: see the end of this file.
"""
import os

HUE = [
    (236, 236, 236),   # 0  grey
    (252, 252,  84),   # 1  yellow
    (252, 188,  60),   # 2  orange
    (252, 124,  88),   # 3  red-orange
    (252, 104, 124),   # 4  pink
    (236,  88, 180),   # 5  purple
    (188,  88, 236),   # 6  purple-blue
    (140, 100, 252),   # 7  blue-purple
    ( 92, 120, 252),   # 8  blue
    ( 60, 148, 252),   # 9  light blue
    ( 56, 180, 252),   # A  turquoise
    ( 56, 204, 180),   # B  green-blue
    ( 56, 212, 108),   # C  green
    (100, 220,  60),   # D  yellow-green
    (168, 220,  60),   # E  orange-green
    (216, 212,  60),   # F  light orange
]


def ntsc7800(byte, gamma=0.80, chroma=0.75):
    """Colour byte to RGB.

    Luminance and chrominance are independent in NTSC, so a dark colour is not
    a dimmed bright one -- it keeps its chroma. An earlier version multiplied
    the whole base RGB by (lum/15)**0.85, which is a brightness dimmer: it made
    every low-luma entry both too dark and too grey, and it wrecked hue balance,
    since scaling (252,252,84) keeps R=G where real gold has R>G.

    This sets Y from the luma nibble and adds the hue's offset from its own grey
    level at roughly constant amplitude. The hue table itself is unchanged --
    its angles were verified against play (hue 9 blue on the zombie faces, hue C
    green on the grass, hue 7 the purple pool).

    Chroma is held at full amplitude all the way down, including luma 0, which
    is what the hardware does: a luma-0 colour is not black unless its hue is
    neutral. `$70` is dark blue and `$20` dark red -- both visible on the Skull's
    projectile, whose palette is `$70 $20 $0C`.

    An earlier version faded chroma out over the bottom luma steps to force
    luma 0 black. That was over-correction: the case it was written for, the
    bat's black wings, is handled by the hue being neutral, since a neutral hue
    has no chroma offset to survive. Fading it also erased every dark saturated
    colour in the game.
    """
    mode = os.environ.get("A7800_PALETTE", "")
    if mode in ("mame", "mame-pal"):
        return mame7800(byte, pal=(mode == "mame-pal"))
    hue, lum = (byte >> 4) & 0x0F, byte & 0x0F
    base = HUE[hue]
    grey = 0.299 * base[0] + 0.587 * base[1] + 0.114 * base[2]
    y = 255.0 * ((lum / 15.0) ** gamma)
    k = chroma
    return tuple(max(0, min(255, int(round(y + (c - grey) * k)))) for c in base)



# ---------------------------------------------------------------------------
# Palettes sampled from play, not read from ROM.
#
# MARIA palettes 2, 4 and 6 are only ever loaded from the base block at
# b3:$81C9, and those values do not match what the game shows: palette 2's
# middle entry is $02, a flat grey, yet the pumpkin-head zombie is plainly
# orange on screen. Something rewrites them at a point not found in the
# disassembly -- every candidate an opcode scan turned up was a byte pattern
# inside graphics data.
#
# Where a screenshot exists, the colours are taken from it. Anything here is
# observed, not derived, and is marked as such wherever it is displayed.
# Palettes 2-5 have no per-area source. Exactly two block copies write them in
# the whole ROM -- b3:$81C9 during the intro and b0:$A581 when the Grampa screen
# opens -- and nothing reloads them when an area is entered, so the world keeps
# whichever ran last. The values below are sampled from play and remain useful
# for sprites; item icons now read the blocks directly (see tools/rooms.py).
PLAY_PALETTE = {
    # pumpkin-head zombie: dark rind, orange body, lit orange
    2: [(38, 28, 20), (198, 106, 38), (240, 152, 62)],
    # headless zombie: the neck stub, dark red with pale flecks
    4: [(64, 22, 20), (146, 38, 30), (232, 228, 214)],
}

# Palette 6 is not an identity and has no fixed value: f6:$4C70 rewrites all
# three of its registers every frame from the random generator --
#
#     JSR Random : STA P6C1 : EOR #$F0 : STA P6C2 : ADC #$10 : STA P6C3
#
# so the entries are c, c^$F0 and (c^$F0)+$10: always hue-contrasted at matched
# luminance rather than three unrelated colours, which is what makes it read as
# a strobe instead of noise. sub_4329 forces any part into palette 6 while its
# flash flag is set, and the bulb-head zombie is authored to use it permanently.
UNSOURCED = {}


def flash_palette(seed):
    """One frame of palette 6, reproducing f6:$4C70 from a random byte."""
    c1 = seed & 0xFF
    c2 = c1 ^ 0xF0
    c3 = (c2 + 0x10) & 0xFF
    return [ntsc7800(c) for c in (c1, c2, c3)]

# A note on orange: on NTSC hardware of this era orange sits very close to brown,
# and the two are separated as much by chroma artifacting on a composite signal
# as by the palette entry itself. A flat RGB conversion will always read a shade
# duller than a CRT does, so the sampled values above are closer to the intent
# than an arithmetic conversion of the colour byte would be.


def palette_rgb(index, rom_bytes):
    """RGB triple for a MARIA palette: sampled where known, else converted."""
    if index in PLAY_PALETTE:
        return PLAY_PALETTE[index], "observed"
    return [ntsc7800(b) for b in rom_bytes], "rom"


# ---------------------------------------------------------------------------
# MAME's palette, for pictures that should match the emulator exactly.
#
# ntsc7800() above is an approximation built to read artwork by. MAME uses a
# fixed table instead (the "25.7 phase shift" palette in its a7800 driver,
# after Robert Tuccitto's measurements), and a preview meant to match a MAME
# screenshot needs that table: choosing player 2's highlight colours in Pole
# Position II, $17 came out olive (153,153,27) here and gold (145,126,9) in
# MAME. Every colour in four MAME screenshots of that game is in the table
# below.
#
# mame7800(byte) reads it directly; set A7800_PALETTE=mame (or mame-pal) and
# ntsc7800() -- so every tool that draws with it -- uses it too.
#
# The two tables are data from MAME's src/mame/atari/a7800.cpp (a7800_colors
# and a7800p_colors), under its licence:
#
#   Copyright Dan Boris, Fabio Priuli, Mike Saarna, Robert Tuccitto
#
#   Redistribution and use in source and binary forms, with or without
#   modification, are permitted provided that the following conditions are
#   met:
#
#   1. Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#   2. Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#   3. Neither the name of the copyright holder nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
#
#   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
#   IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
#   THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
#   PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
#   CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
#   EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
#   PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
#   PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
#   LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
#   NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#   SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

MAME_NTSC = [
    (  0,   0,   0), ( 17,  17,  17), ( 34,  34,  34), ( 51,  51,  51),   # $00
    ( 68,  68,  68), ( 85,  85,  85), (102, 102, 102), (119, 119, 119),   # $04
    (136, 136, 136), (153, 153, 153), (170, 170, 170), (187, 187, 187),   # $08
    (204, 204, 204), (221, 221, 221), (238, 238, 238), (255, 255, 255),   # $0C
    ( 26,   7,   0), ( 43,  24,   0), ( 60,  41,   0), ( 77,  58,   0),   # $10
    ( 94,  75,   0), (111,  92,   0), (128, 109,   0), (145, 126,   9),   # $14
    (162, 143,  26), (179, 160,  43), (196, 177,  60), (213, 194,  77),   # $18
    (230, 211,  94), (247, 228, 111), (255, 245, 131), (255, 247, 151),   # $1C
    ( 49,   0,   0), ( 66,   6,   0), ( 83,  23,   0), (100,  40,   0),   # $20
    (117,  57,   0), (134,  74,   0), (151,  91,  10), (168, 108,  27),   # $24
    (185, 125,  44), (202, 142,  61), (219, 159,  78), (236, 176,  95),   # $28
    (253, 193, 112), (255, 210, 133), (255, 227, 156), (255, 244, 178),   # $2C
    ( 62,   0,   0), ( 79,   0,   0), ( 96,   8,   0), (113,  25,   0),   # $30
    (130,  42,  13), (147,  59,  30), (164,  76,  47), (181,  93,  64),   # $34
    (198, 110,  81), (215, 127,  98), (232, 144, 115), (249, 161, 131),   # $38
    (255, 178, 152), (255, 195, 174), (255, 212, 196), (255, 229, 218),   # $3C
    ( 63,   0,   3), ( 80,   0,  15), ( 97,   0,  27), (114,  15,  43),   # $40
    (131,  32,  60), (148,  49,  77), (165,  66,  94), (182,  83, 111),   # $44
    (199, 100, 128), (216, 117, 145), (233, 134, 162), (250, 151, 179),   # $48
    (255, 168, 200), (255, 185, 222), (255, 202, 239), (255, 219, 244),   # $4C
    ( 51,   0,  53), ( 68,   0,  65), ( 85,   0,  76), (102,  12,  92),   # $50
    (119,  29, 109), (136,  46, 126), (153,  63, 143), (170,  80, 160),   # $54
    (187,  97, 177), (204, 114, 194), (221, 131, 211), (238, 148, 228),   # $58
    (255, 165, 228), (255, 182, 233), (255, 199, 238), (255, 216, 243),   # $5C
    ( 29,   0,  92), ( 46,   0, 104), ( 64,   0, 116), ( 81,  16, 132),   # $60
    ( 98,  33, 149), (115,  50, 166), (132,  67, 183), (149,  84, 200),   # $64
    (166, 101, 217), (183, 118, 234), (200, 135, 235), (217, 152, 235),   # $68
    (233, 169, 236), (251, 186, 235), (255, 203, 239), (255, 220, 244),   # $6C
    (  2,   0, 113), ( 19,   0, 125), ( 36,  11, 140), ( 53,  28, 157),   # $70
    ( 70,  45, 174), ( 87,  62, 191), (104,  79, 208), (121,  96, 225),   # $74
    (138, 113, 242), (155, 130, 247), (172, 147, 247), (189, 164, 247),   # $78
    (206, 181, 247), (223, 198, 247), (240, 215, 247), (255, 232, 248),   # $7C
    (  0,   0, 104), (  0,  10, 124), (  8,  27, 144), ( 25,  44, 161),   # $80
    ( 42,  61, 178), ( 59,  78, 195), ( 76,  95, 212), ( 93, 112, 229),   # $84
    (110, 129, 246), (127, 146, 255), (144, 163, 255), (161, 180, 255),   # $88
    (178, 197, 255), (195, 214, 255), (212, 231, 255), (229, 248, 255),   # $8C
    (  0,  10,  77), (  0,  27,  99), (  0,  44, 121), (  2,  61, 143),   # $90
    ( 19,  78, 160), ( 36,  95, 177), ( 53, 112, 194), ( 70, 129, 211),   # $94
    ( 87, 146, 228), (104, 163, 245), (121, 180, 255), (138, 197, 255),   # $98
    (155, 214, 255), (172, 231, 255), (189, 248, 255), (206, 255, 255),   # $9C
    (  0,  26,  38), (  0,  43,  60), (  0,  60,  82), (  0,  77, 104),   # $A0
    (  6,  94, 124), ( 23, 111, 141), ( 40, 128, 158), ( 57, 145, 175),   # $A4
    ( 74, 162, 192), ( 91, 179, 209), (108, 196, 226), (125, 213, 243),   # $A8
    (142, 230, 255), (159, 247, 255), (176, 255, 255), (193, 255, 255),   # $AC
    (  0,  36,  11), (  0,  53,  16), (  0,  70,  34), (  0,  87,  56),   # $B0
    (  5, 104,  77), ( 22, 121,  94), ( 39, 138, 111), ( 56, 155, 128),   # $B4
    ( 73, 172, 145), ( 90, 189, 162), (107, 206, 179), (124, 223, 196),   # $B8
    (141, 240, 213), (158, 255, 229), (175, 255, 241), (192, 255, 253),   # $BC
    (  0,  39,  12), (  0,  56,  17), (  0,  73,  22), (  0,  90,  27),   # $C0
    ( 16, 107,  27), ( 33, 124,  44), ( 50, 141,  61), ( 67, 158,  78),   # $C4
    ( 84, 175,  95), (101, 192, 112), (118, 209, 129), (135, 226, 146),   # $C8
    (152, 243, 163), (169, 255, 179), (186, 255, 191), (203, 255, 203),   # $CC
    (  0,  35,  10), (  0,  52,  16), (  4,  69,  19), ( 21,  86,  19),   # $D0
    ( 38, 103,  19), ( 55, 120,  19), ( 72, 137,  20), ( 89, 154,  37),   # $D4
    (106, 171,  54), (123, 188,  71), (140, 205,  88), (157, 222, 105),   # $D8
    (174, 239, 122), (191, 255, 139), (208, 255, 151), (225, 255, 163),   # $DC
    (  0,  23,   7), ( 14,  40,   8), ( 31,  57,   8), ( 48,  74,   8),   # $E0
    ( 65,  91,   8), ( 82, 108,   8), ( 99, 125,   8), (116, 142,  13),   # $E4
    (133, 159,  30), (150, 176,  47), (167, 193,  64), (184, 210,  81),   # $E8
    (201, 227,  98), (218, 244, 115), (235, 255, 130), (252, 255, 142),   # $EC
    ( 25,   7,   0), ( 42,  24,   0), ( 59,  41,   0), ( 76,  58,   0),   # $F0
    ( 93,  75,   0), (110,  92,   0), (127, 109,   0), (144, 126,   9),   # $F4
    (161, 143,  26), (178, 160,  43), (195, 177,  60), (212, 194,  77),   # $F8
    (229, 211,  94), (246, 228, 111), (255, 245, 130), (255, 255, 150),   # $FC
]

MAME_PAL = [
    (  0,   0,   0), ( 17,  17,  17), ( 34,  34,  34), ( 51,  51,  51),   # $00
    ( 68,  68,  68), ( 85,  85,  85), (102, 102, 102), (119, 119, 119),   # $04
    (136, 136, 136), (153, 153, 153), (170, 170, 170), (187, 187, 187),   # $08
    (204, 204, 204), (221, 221, 221), (238, 238, 238), (255, 255, 255),   # $0C
    (  0,  23,   7), ( 14,  40,   8), ( 31,  57,   8), ( 48,  74,   8),   # $10
    ( 65,  91,   8), ( 82, 108,   8), ( 99, 125,   8), (116, 142,  13),   # $14
    (133, 159,  30), (150, 176,  47), (167, 193,  64), (184, 210,  81),   # $18
    (201, 227,  98), (218, 244, 115), (235, 255, 130), (252, 255, 142),   # $1C
    ( 26,   7,   0), ( 43,  24,   0), ( 60,  41,   0), ( 77,  58,   0),   # $20
    ( 94,  75,   0), (111,  92,   0), (128, 109,   0), (145, 126,   9),   # $24
    (162, 143,  26), (179, 160,  43), (196, 177,  60), (213, 194,  77),   # $28
    (230, 211,  94), (247, 228, 111), (255, 245, 131), (255, 247, 151),   # $2C
    ( 49,   0,   0), ( 66,   6,   0), ( 83,  23,   0), (100,  40,   0),   # $30
    (117,  57,   0), (134,  74,   0), (151,  91,  10), (168, 108,  27),   # $34
    (185, 125,  44), (202, 142,  61), (219, 159,  78), (236, 176,  95),   # $38
    (253, 193, 112), (255, 210, 133), (255, 227, 156), (255, 244, 178),   # $3C
    ( 62,   0,   0), ( 79,   0,   0), ( 96,   8,   0), (113,  25,   0),   # $40
    (130,  42,  13), (147,  59,  30), (164,  76,  47), (181,  93,  64),   # $44
    (198, 110,  81), (215, 127,  98), (232, 144, 115), (249, 161, 131),   # $48
    (255, 178, 152), (255, 195, 174), (255, 212, 196), (255, 229, 218),   # $4C
    ( 63,   0,   3), ( 80,   0,  15), ( 97,   0,  27), (114,  15,  43),   # $50
    (131,  32,  60), (148,  49,  77), (165,  66,  94), (182,  83, 111),   # $54
    (199, 100, 128), (216, 117, 145), (233, 134, 162), (250, 151, 179),   # $58
    (255, 168, 200), (255, 185, 222), (255, 202, 239), (255, 219, 244),   # $5C
    ( 51,   0,  53), ( 68,   0,  65), ( 85,   0,  76), (102,  12,  92),   # $60
    (119,  29, 109), (136,  46, 126), (153,  63, 143), (170,  80, 160),   # $64
    (187,  97, 177), (204, 114, 194), (221, 131, 211), (238, 148, 228),   # $68
    (255, 165, 228), (255, 182, 233), (255, 199, 238), (255, 216, 243),   # $6C
    ( 29,   0,  92), ( 46,   0, 104), ( 64,   0, 116), ( 81,  16, 132),   # $70
    ( 98,  33, 149), (115,  50, 166), (132,  67, 183), (149,  84, 200),   # $74
    (166, 101, 217), (183, 118, 234), (200, 135, 235), (217, 152, 235),   # $78
    (233, 169, 236), (251, 186, 235), (255, 203, 239), (255, 220, 244),   # $7C
    (  2,   0, 113), ( 19,   0, 125), ( 36,  11, 140), ( 53,  28, 157),   # $80
    ( 70,  45, 174), ( 87,  62, 191), (104,  79, 208), (121,  96, 225),   # $84
    (138, 113, 242), (155, 130, 247), (172, 147, 247), (189, 164, 247),   # $88
    (206, 181, 247), (223, 198, 247), (240, 215, 247), (255, 232, 248),   # $8C
    (  0,   0, 104), (  0,  10, 124), (  8,  27, 144), ( 25,  44, 161),   # $90
    ( 42,  61, 178), ( 59,  78, 195), ( 76,  95, 212), ( 93, 112, 229),   # $94
    (110, 129, 246), (127, 146, 255), (144, 163, 255), (161, 180, 255),   # $98
    (178, 197, 255), (195, 214, 255), (212, 231, 255), (229, 248, 255),   # $9C
    (  0,  10,  77), (  0,  27,  99), (  0,  44, 121), (  2,  61, 143),   # $A0
    ( 19,  78, 160), ( 36,  95, 177), ( 53, 112, 194), ( 70, 129, 211),   # $A4
    ( 87, 146, 228), (104, 163, 245), (121, 180, 255), (138, 197, 255),   # $A8
    (155, 214, 255), (172, 231, 255), (189, 248, 255), (206, 255, 255),   # $AC
    (  0,  26,  38), (  0,  43,  60), (  0,  60,  82), (  0,  77, 104),   # $B0
    (  6,  94, 124), ( 23, 111, 141), ( 40, 128, 158), ( 57, 145, 175),   # $B4
    ( 74, 162, 192), ( 91, 179, 209), (108, 196, 226), (125, 213, 243),   # $B8
    (142, 230, 255), (159, 247, 255), (176, 255, 255), (193, 255, 255),   # $BC
    (  0,  36,  11), (  0,  53,  16), (  0,  70,  34), (  0,  87,  56),   # $C0
    (  5, 104,  77), ( 22, 121,  94), ( 39, 138, 111), ( 56, 155, 128),   # $C4
    ( 73, 172, 145), ( 90, 189, 162), (107, 206, 179), (124, 223, 196),   # $C8
    (141, 240, 213), (158, 255, 229), (175, 255, 241), (192, 255, 253),   # $CC
    (  0,  39,  12), (  0,  56,  17), (  0,  73,  22), (  0,  90,  27),   # $D0
    ( 16, 107,  27), ( 33, 124,  44), ( 50, 141,  61), ( 67, 158,  78),   # $D4
    ( 84, 175,  95), (101, 192, 112), (118, 209, 129), (135, 226, 146),   # $D8
    (152, 243, 163), (169, 255, 179), (186, 255, 191), (203, 255, 203),   # $DC
    (  0,  35,  10), (  0,  52,  16), (  4,  69,  19), ( 21,  86,  19),   # $E0
    ( 38, 103,  19), ( 55, 120,  19), ( 72, 137,  20), ( 89, 154,  37),   # $E4
    (106, 171,  54), (123, 188,  71), (140, 205,  88), (157, 222, 105),   # $E8
    (174, 239, 122), (191, 255, 139), (208, 255, 151), (225, 255, 163),   # $EC
    (  0,  23,   7), ( 14,  40,   8), ( 31,  57,   8), ( 48,  74,   8),   # $F0
    ( 65,  91,   8), ( 82, 108,   8), ( 99, 125,   8), (116, 142,  13),   # $F4
    (133, 159,  30), (150, 176,  47), (167, 193,  64), (184, 210,  81),   # $F8
    (201, 227,  98), (218, 244, 115), (235, 255, 130), (252, 255, 142),   # $FC
]


def mame7800(byte, pal=False):
    """Colour byte to RGB exactly as MAME draws it (NTSC, or PAL with pal=True)."""
    return (MAME_PAL if pal else MAME_NTSC)[byte & 0xFF]


if __name__ == "__main__":
    for b in (0x00, 0x94, 0x0C, 0x11, 0x26, 0x20, 0xB0, 0xB4, 0xB6):
        print("$%02X -> %s" % (b, ntsc7800(b)))
