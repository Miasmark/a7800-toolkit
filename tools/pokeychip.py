"""One POKEY chip, as far as a program can see it: what the 6502 reads and writes.

`sim.py` used to answer every read of POKEY with $FF except RANDOM, which it took from a plain
XOR shift register that no emulator has. This is the chip as MAME (0.289) and the a7800 fork
(POKEY 4.9) have it, from their source and checked against both by measurement:

  SKCTL      bits 1-0 clear hold the chip in reset: the polynomial counters stay at zero, the
             timers do not run, IRQEN is cleared. It powers up in reset. A write that releases
             it starts the counters (this is "default boot state is in-reset" in the fork).
  RANDOM     ($0A) the polynomial counter's top byte: `poly17[p] >> 8`, or with AUDCTL bit 7
             `poly9[p] & $FF`, where p counts the chip's clocks (the CPU's, one per cycle) since
             SKCTL released it. The tables are the chip's own, XNOR-fed and built the way
             `pokey.cpp` builds them. Measured: on the fork a 14-cycle sampling loop follows the
             table from position 0; on MAME it starts at position 6.
  timers     1, 2 and 4 count AUDF+1 ticks of their clock and set their IRQST bit when IRQEN
             has it enabled. On the 1.79 MHz clock (AUDCTL bits 6 and 5) the period is AUDF+4
             clocks, and AUDF16+7 when two channels are joined; on the 64 kHz or 15 kHz clock it is
             (AUDF+1) * 28 or 114. Measured on MAME and the fork against that, to 0.1%.
  IRQST      ($0E read) active low, bit 3 set at power-up (SEROC). A write to IRQEN clears the
             bits it does not enable, which is how a program acknowledges.

Not modelled: the interrupt itself. Neither MAME nor the a7800 fork wires POKEY's IRQ pin to
the 6502, so a game that waits for it hangs there and this does not pretend otherwise; polling
IRQST works. Not modelled either: the keyboard, serial port and paddles, and a change of AUDF
between one underflow and the next (a timer uses the AUDF it had at STIMER).
"""

_TABLES = {}


def poly9():
    """The chip's 9-bit table (511 entries), as `pokey_device::poly_init_9_17` builds it."""
    return _poly(9)


def poly17():
    """The chip's 17-bit table (131071 entries)."""
    return _poly(17)


def _poly(size):
    if size not in _TABLES:
        mask = (1 << size) - 1
        lfsr = mask
        out = []
        for _ in range(mask):
            if size == 17:
                in8 = ((lfsr >> 8) & 1) ^ ((lfsr >> 13) & 1)
                in0 = lfsr & 1
                lfsr >>= 1
                lfsr = (lfsr & 0xFF7F) | (in8 << 7)
                lfsr = (in0 << 16) | lfsr
            else:
                bit = (lfsr & 1) ^ ((lfsr >> 5) & 1)
                lfsr >>= 1
                lfsr = (bit << 8) | lfsr
            out.append(lfsr)
        _TABLES[size] = out
    return _TABLES[size]


AUDF1, AUDF2, AUDF3, AUDF4 = 0x00, 0x02, 0x04, 0x06
AUDCTL, STIMER, RANDOM, IRQEN, SKCTL = 0x08, 0x09, 0x0A, 0x0E, 0x0F
IRQST = 0x0E
SEROC = 0x08
TIMERS = ((0x01, 0), (0x02, 1), (0x04, 3))      # IRQ bit, AUDF index of the timer's own channel


class PokeyChip(object):
    def __init__(self):
        self.skctl = 0                  # in reset until the program says otherwise
        self.audctl = 0
        self.audf = [0, 0, 0, 0]
        self.irqen = 0
        self.irqst = SEROC
        self.release = None             # the cycle SKCTL took the chip out of reset
        self.stimer = None              # (step, audctl, audf) at the last STIMER
        self.seen = [0, 0, 0]           # underflows already folded into IRQST
        # what the program did that this does not follow (see `notes`)
        self.two_tone = False
        self.irq_enabled = False
        self.irqst_reads = 0

    # -- time: the chip's clocks since it left reset
    def step(self, now):
        if self.release is None:
            return None
        return max(0, int(now) - self.release)

    # -- timers
    def _timer(self, mask, audctl, audf):
        """(first underflow, period) in clocks after STIMER, for the timer with IRQ bit `mask`."""
        div = 114 if audctl & 0x01 else 28
        if mask == 0x01:                                    # channel 1
            n = audf[0] + 1
            if audctl & 0x40:
                return n, n + 3, True
            return n, n * div, False
        if mask == 0x02:                                    # channel 2, alone or joined to 1
            if audctl & 0x10:
                n = audf[1] * 256 + audf[0] + 1
                if audctl & 0x40:
                    return n, n + 6, True
                return n, n * div, False
            return audf[1] + 1, (audf[1] + 1) * div, False
        if audctl & 0x08:                                   # channel 4 joined to 3
            n = audf[3] * 256 + audf[2] + 1
            if audctl & 0x20:
                return n, n + 6, True
            return n, n * div, False
        return audf[3] + 1, (audf[3] + 1) * div, False

    def _wraps(self, mask, now):
        if self.stimer is None:
            return 0
        s0, audctl, audf = self.stimer
        step = self.step(now)
        if step is None or step <= s0:
            return 0
        n, period, fast = self._timer(mask, audctl, audf)
        if fast:
            first = s0 + n                                  # one count per clock from the write
        else:
            div = 114 if audctl & 0x01 else 28
            nxt = (s0 // div + 1) * div                     # the first prescaler tick after it
            first = nxt + (n - 1) * div
        if step < first:
            return 0
        return 1 + (step - first) // period

    def _sync(self, now):
        for k, (mask, _) in enumerate(TIMERS):
            w = self._wraps(mask, now)
            if w > self.seen[k]:
                if self.irqen & mask:
                    self.irqst |= mask
                self.seen[k] = w

    # -- the registers
    def write(self, reg, v, now):
        reg &= 0x0F
        v &= 0xFF
        if reg in (AUDF1, AUDF2, AUDF3, AUDF4):
            self.audf[reg >> 1] = v
        elif reg == AUDCTL:
            self.audctl = v
        elif reg == STIMER:
            self._sync(now)
            step = self.step(now)
            if step is not None:
                self.stimer = (step, self.audctl, tuple(self.audf))
                self.seen = [0, 0, 0]
        elif reg == IRQEN:
            self._sync(now)
            if self.irqst & ~v & 0xFF:
                self.irqst &= SEROC | v
            self.irqen = v
            if v & 0x07:
                self.irq_enabled = True
        elif reg == SKCTL:
            if v == self.skctl:
                return
            self._sync(now)
            self.skctl = v
            if v & 0x08:
                self.two_tone = True
            if v & 0x03 == 0:                               # reset: counters held, IRQEN cleared
                self.release = None
                self.stimer = None
                self.irqen = 0
                self.irqst &= SEROC
                self.seen = [0, 0, 0]
            elif self.release is None:
                self.release = int(now)

    def read(self, reg, now):
        reg &= 0x0F
        if reg == RANDOM:
            k = self.step(now) or 0
            if self.audctl & 0x80:
                return poly9()[k % 511] & 0xFF
            return (poly17()[k % 131071] >> 8) & 0xFF
        if reg == IRQST:
            self._sync(now)
            self.irqst_reads += 1
            return (~self.irqst) & 0xFF
        return 0xFF

    def notes(self):
        """What the program asked of this chip that the simulator cannot give it."""
        out = []
        if self.irq_enabled and not self.irqst_reads:
            out.append("enabled POKEY timer interrupts (IRQEN) and never polled IRQST: MAME and "
                       "the a7800 fork do not deliver POKEY's IRQ to the 6502, and neither does "
                       "this simulator, so an engine that runs from it does not run")
        if self.two_tone:
            out.append("set POKEY's two-tone mode (SKCTL bit 3), which the audio render does not "
                       "model")
        return out
