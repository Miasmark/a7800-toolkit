#!/usr/bin/env python3
"""MARIA's pixel formats, as measured on MAME 0.264.

No dependencies, no pictures: bytes in, (palette, colour) pairs out. Used by
firstlook.py to rebuild a screen from a live display list, and checked against
what MAME actually draws by `selftest.py` (via probes/forcedl.lua), because the
documentation this was first written from had the write-mode bit in the wrong
place.

A 160-wide screen has two ways to read a byte:

  160A   four pixels per byte, two bits each, most significant first. The pixel
         value is a colour index into the entry's own palette: 0 is transparent,
         1-3 are that palette's three colours ("3 + 1").

  160B   two pixels per byte, and each pixel carries its own palette bits:

             bit  7 6   5 4   3 2   1 0
                  c0    c1    p0    p1

         pixel 0 has colour c0 and palette-select p0; pixel 1 has c1 and p1.
         The palette actually used is (entry palette & 4) | pselect, so one entry
         can draw from four palettes -- the group the entry's palette bit 2
         picks. Colour 0 is transparent whatever the palette.

Which one applies: CTRL's read mode (bits 1-0) must be 0 (the 160 modes), and
bit 7 of a five-byte display-list header's second byte is the write mode: clear
is 160A, set is 160B. (docs/pitfalls.md has how this was found: dlwalk.py had
taken bit 6, and an entry with $40 draws as plain 160A, $C0 as 160B.)

Character mode adds one thing: CTRL bit 4 clear makes each list entry select one
byte of graphics per scanline (four 160A pixels), set makes it two consecutive
bytes -- the code's and the next -- eight pixels wide.

  320A   CTRL read mode 3 with write mode 0: eight pixels per byte, one bit each,
         most significant first, twice the horizontal resolution. A set bit is
         the entry's palette colour 2 (not 1); a clear bit is transparent. Used
         for HUD text on real cartridges (Triple Punch's score row switches CTRL
         to read mode 3 from its display-list interrupt).

The other 320 modes (read mode 2, and read mode 3 with write mode 1) pair bits
across the byte; they were seen to draw something other than 1 bit per pixel and
are not decoded here. `pixel_format` returns None for them, and callers must say
so rather than guess. A display-list entry's horizontal position is in 160-pixel
units whatever the mode, so a 320 pixel is half a position.
"""

A160, B160, A320 = "160A", "160B", "320A"


def pixel_format(read_mode, write_mode):
    """'160A', '160B', or None for a mode this module does not decode."""
    if read_mode == 0:
        return B160 if write_mode else A160
    if read_mode == 3 and not write_mode:
        return A320
    return None


def width(fmt):
    """Screen pixels (on a 320-wide framebuffer) one pixel of `fmt` covers."""
    return 1 if fmt == A320 else 2


def decode(fmt, byte):
    """One byte as a list of (palette_select, colour); palette_select is None
    for 160A (the entry's own palette applies as it is)."""
    if fmt == A160:
        return [(None, (byte >> (6 - 2 * i)) & 3) for i in range(4)]
    if fmt == B160:
        return [((byte >> 2) & 3, (byte >> 6) & 3),
                (byte & 3, (byte >> 4) & 3)]
    if fmt == A320:
        return [(None, 2 if (byte >> (7 - i)) & 1 else 0) for i in range(8)]
    raise ValueError("not a decoded format: %r" % (fmt,))


def palette_used(entry_palette, select):
    """The palette a pixel is drawn from."""
    return entry_palette if select is None else (entry_palette & 4) | select


def row_pixels(fmt, data, entry_palette):
    """A scanline's bytes as [(palette, colour)], colour 0 meaning transparent."""
    out = []
    for b in data:
        for sel, colour in decode(fmt, b):
            out.append((palette_used(entry_palette, sel), colour))
    return out
