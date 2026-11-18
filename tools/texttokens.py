#!/usr/bin/env python3
"""Text in fewer bytes: tokens for a game's strings, and the 6502 that prints them.

    python tools/texttokens.py strings.txt -o text.asm
    python tools/texttokens.py strings.txt --charset " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.,:-'" --report

A string is letters' codes, one byte each, ending $FF. Here a byte $80-$FE in it is a token,
printed in its place:

  a pair      2 letters. The tokens from TKPB up; its letters at TKPR + 2 x (token - TKPB).
              It costs its 2 letters and nothing else, so a pair used three times pays.
  a longer    3 letters or more. The tokens under TKPB; its last letter has bit 7 set; TKL
  run         has its place in the list TKW (the low byte; the high is 0, 1 or 2 by its
              number against TKP1 and TKP2, the entries numbered in the list's order). It
              costs its letters and its byte of TKL. One that ends another is stored in it.

The entries are whatever runs of letters pay, words or not (`THE `, `ER`, `, `). They are
chosen greedily: again and again the run whose every use, replaced by one byte, saves the
most after its own cost, counted on the letters the best cut of every string still spells
out; entries that no longer pay are dropped and the choice run again. Each string is then
cut for the fewest bytes. No entry spans two strings.

The input is one string a line, or `LABEL: the text` to name its bytes; blank lines and
lines starting `;` are skipped. A letter's code is its place in --charset (default: ASCII
less 32, so a blank is 0), and must be under $7F.

The output (asm.py's syntax) holds each string's bytes, the tables, and `tk_print`: the
string at (TKSRC) written to (TKDST), DST left after its last letter. It needs six bytes of
zero page that you name (TKSRC, TKDST, TKTMP: two each); A, X and Y are used. It is 104
bytes; a plain letter costs about 55 cycles, a letter of a longer run about 47.

WHAT IT GIVES. Measured on one game's 222 strings (3,902 letters of station and story text
in capitals): 2,344 bytes with the tables, 40% less; whole words alone saved about a
quarter. Short, varied text gains less: look at --report before wiring it in. The idea is
Fatal Run's word tokens (its in-game messages are a byte a word into a word list).
"""
import argparse
import collections
import io
import sys

LIMIT = 127                    # tokens $80-$FE ($FF ends a string)
MAXLEN = 24


def cost(n):
    """What an entry of n letters costs."""
    return 2 if n == 2 else n + 1


class Tokens:
    """collect() every string, build(), then encode() each. `encode` maps a string to its
    letters' codes (each under $7F). `limit` is how many token values may be used (fewer
    than 127 leaves the top values free for codes of your own, such as a new line)."""

    def __init__(self, encode, limit=LIMIT):
        assert 0 < limit <= LIMIT
        self.enc = encode
        self.limit = limit
        self.strings = []
        self.longs = None      # the longer entries, by token number (strings of chr(code))
        self.pairs = None      # the pairs, by number from TKPB
        self.offset = None     # a longer entry's place in TKW
        self.list = None       # TKW's bytes
        self.token = None      # entry -> its byte
        self.entries = None

    def collect(self, text):
        assert self.longs is None, "collect after build"
        self.strings.append(text)
        return self.enc(text)

    def _plain(self, text):
        codes = self.enc(text)
        assert all(0 <= c < 0x7F for c in codes), "a letter's code must be under $7F: %r" % (text,)
        return "".join(chr(c) for c in codes)

    @staticmethod
    def _cut(s, entries):
        """s cut for the fewest bytes with these entries: its pieces."""
        n = len(s)
        best = [0] * (n + 1)
        how = [1] * (n + 1)
        for i in range(1, n + 1):
            best[i], how[i] = best[i - 1] + 1, 1
            for length in range(2, min(MAXLEN, i) + 1):
                if best[i - length] + 1 < best[i] and s[i - length:i] in entries:
                    best[i], how[i] = best[i - length] + 1, length
        out, i = [], n
        while i:
            out.append(s[i - how[i]:i])
            i -= how[i]
        return out[::-1]

    def _choose(self, plain):
        entries = set()
        cuts = [list(s) for s in plain]
        for _round in range(6):
            while len(entries) < self.limit:
                runs = []                         # the letters still spelled out, as runs
                for cut in cuts:
                    run = ""
                    for p in cut:
                        if len(p) == 1:
                            run += p
                        elif run:
                            runs.append(run)
                            run = ""
                    if run:
                        runs.append(run)
                raw = collections.Counter()
                for run in runs:
                    for i in range(len(run) - 1):
                        for length in range(2, min(MAXLEN, len(run) - i) + 1):
                            raw[run[i:i + length]] += 1
                corpus = "".join(runs)
                cands = sorted(((c * (len(s) - 1) - cost(len(s)), s) for s, c in raw.items() if c > 1),
                               reverse=True)
                best, gain = None, 0
                for most, s in cands:             # (most: with overlapping uses counted)
                    if most <= gain:
                        break
                    g = corpus.count(s) * (len(s) - 1) - cost(len(s))
                    if g > gain:
                        best, gain = s, g
                if best is None:
                    break
                entries.add(best)
                cuts = [self._cut(s, entries) if best in s else cut for s, cut in zip(plain, cuts)]
            used = collections.Counter(p for cut in cuts for p in cut if len(p) > 1)
            drop = {e for e in entries if used[e] * (len(e) - 1) - cost(len(e)) <= 0}
            if not drop:
                break
            entries -= drop
            cuts = [self._cut(s, entries) for s in plain]
        return entries

    def build(self):
        plain = [self._plain(s) for s in self.strings]
        entries = self._choose(plain)
        used = collections.Counter(p for s in plain for p in self._cut(s, entries) if len(p) > 1)
        entries = {e for e in entries if used[e]}
        longs = sorted((e for e in entries if len(e) > 2), key=lambda e: (-used[e], e))
        inside = {}
        for k, e in enumerate(longs):
            for j, f in enumerate(longs):
                if j != k and len(f) > len(e) and f.endswith(e) and j not in inside:
                    inside[k] = j
                    break
        place, data = {}, []
        for k, e in enumerate(longs):
            if k not in inside:
                place[k] = len(data)
                data += [ord(c) for c in e[:-1]] + [ord(e[-1]) | 0x80]
        for k, j in inside.items():
            place[k] = place[j] + len(longs[j]) - len(longs[k])
        order = sorted(range(len(longs)), key=lambda k: (place[k], k))
        self.longs = [longs[k] for k in order]
        self.offset = [place[k] for k in order]
        self.list = data
        self.pairs = sorted((e for e in entries if len(e) == 2), key=lambda e: (-used[e], e))
        assert len(data) <= 768, "TKW: three pages at most (TKP1, TKP2)"
        self.token = {e: 0x80 + n for n, e in enumerate(self.longs)}
        self.token.update({e: 0x80 + len(self.longs) + n for n, e in enumerate(self.pairs)})
        self.entries = entries
        for s in self.strings:                   # (what tk_print writes is the string)
            assert self.decode(self.encode(s)) == self.enc(s), s

    def encode(self, text):
        """A string's bytes: letters and tokens (no $FF)."""
        assert self.longs is not None, "encode before build"
        return [self.token[p] if len(p) > 1 else ord(p) for p in self._cut(self._plain(text), self.entries)]

    def decode(self, data):
        """As tk_print writes it."""
        out, base = [], 0x80 + len(self.longs)
        for b in data:
            if b < 0x80:
                out.append(b)
            elif b >= base:
                out += [ord(c) for c in self.pairs[b - base]]
            else:
                p = self.offset[b - 0x80]
                while True:
                    out.append(self.list[p] & 0x7F)
                    if self.list[p] & 0x80:
                        break
                    p += 1
        return out

    def saved(self):
        """(letters, bytes of text now, bytes of tables)."""
        plain = sum(len(self.enc(s)) for s in self.strings)
        coded = sum(len(self.encode(s)) for s in self.strings)
        return plain, coded, len(self.list) + len(self.offset) + 2 * len(self.pairs)

    def uses(self, show):
        """[(entry as text, uses)], most used first; `show` maps a code to a character."""
        n = collections.Counter(b for s in self.strings for b in self.encode(s) if b >= 0x80)
        out = [("".join(show(ord(c)) for c in e), n[self.token[e]]) for e in self.longs + self.pairs]
        return sorted(out, key=lambda t: -t[1])

    def tables(self):
        """The tables' lines (asm.py's syntax): TKPB, TKP1, TKP2, TKL, TKW, TKPR."""
        first = lambda page: min([n for n, o in enumerate(self.offset) if o >= page * 256] + [LIMIT + 1])
        return ["TKPB = $%02X" % (0x80 + len(self.longs)),
                "TKP1 = %d" % first(1), "TKP2 = %d" % first(2),
                "TKL:"] + byte_lines([o & 0xFF for o in self.offset] or [0]) + [
                "TKW:"] + byte_lines(self.list or [0x80]) + [
                "TKPR:"] + byte_lines([ord(c) for e in self.pairs for c in e] or [0])


def byte_lines(vals, per=16):
    return ["    .byte " + ", ".join("$%02X" % v for v in vals[i:i + per]) for i in range(0, len(vals), per)]


PRINT = """tk_print:                     ; the string at (TKSRC) to (TKDST): letters, and tokens for the
    LDY #$00                  ;   runs texttokens.py chose; DST left after its last letter
    LDA (TKSRC),Y
    INC TKSRC
    BNE tp_s
    INC TKSRC+1
tp_s:
    CMP #$FF                  ; the end
    BEQ tp_done
    CMP #$80
    BCS tp_tok
    JSR tk_out                ; a letter
    JMP tk_print
tp_tok:
    CMP #TKPB                 ; from TKPB a pair: its two letters at TKPR + 2 x its number
    BCC tp_long
    SBC #TKPB
    ASL A
    TAY
    LDA TKPR,Y
    JSR tk_out                ; (tk_out keeps Y)
    LDA TKPR+1,Y
    JSR tk_out
    JMP tk_print
tp_long:                      ; under it a longer run: its letters from TKW, the last with
    AND #$7F                  ;   bit 7. Its place: TKL the low byte, the high by its number
    TAX
    LDY #$00
    CPX #TKP1
    BCC tp_p
    INY
    CPX #TKP2
    BCC tp_p
    INY
tp_p:
    LDA TKL,X
    CLC
    ADC #<TKW
    STA TKTMP
    TYA
    ADC #>TKW
    STA TKTMP+1
    LDY #$00
tp_l:
    LDA (TKTMP),Y
    PHA
    AND #$7F
    JSR tk_out
    INY
    PLA
    BPL tp_l
    JMP tk_print
tp_done:
    RTS
tk_out:                       ; A to (TKDST), and DST on; keeps Y
    LDX #$00
    STA (TKDST,X)
    INC TKDST
    BNE to_s
    INC TKDST+1
to_s:
    RTS
""".rstrip("\n").split("\n")


def read_strings(text):
    """[(label or None, string)] from the input file's text."""
    out = []
    for ln in text.split("\n"):
        ln = ln.rstrip("\r")
        if not ln.strip() or ln.lstrip().startswith(";"):
            continue
        label, sep, rest = ln.partition(": ")
        if sep and label.replace("_", "").isalnum() and not label[0].isdigit() and " " not in label:
            out.append((label, rest))
        else:
            out.append((None, ln))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("strings", help="a text file, one string a line (`LABEL: text` names it)")
    ap.add_argument("-o", "--out", help="write the strings' bytes, the tables and tk_print here")
    ap.add_argument("--charset", help="the letters in code order (default: ASCII less 32)")
    ap.add_argument("--limit", type=int, default=LIMIT,
                    help="how many token values to use (default 127: $80-$FE)")
    ap.add_argument("--no-code", action="store_true", help="leave tk_print out of the output")
    ap.add_argument("--report", action="store_true", help="list the entries and their uses")
    args = ap.parse_args(argv)
    rows = read_strings(io.open(args.strings, encoding="utf-8").read())
    if not rows:
        sys.exit("texttokens: no strings in %s" % args.strings)
    if args.charset:
        if len(args.charset) > 0x7F or len(set(args.charset)) != len(args.charset):
            sys.exit("texttokens: --charset needs at most 127 different characters")

        def enc(s):
            bad = sorted({c for c in s if c not in args.charset})
            if bad:
                sys.exit("texttokens: %r is not in --charset (in %r)" % ("".join(bad), s))
            return [args.charset.index(c) for c in s]
        show = lambda c: args.charset[c]
    else:
        def enc(s):
            bad = sorted({c for c in s if not 32 <= ord(c) < 32 + 0x7F})
            if bad:
                sys.exit("texttokens: %r has no code (ASCII 32-158 only; see --charset) in %r" % ("".join(bad), s))
            return [ord(c) - 32 for c in s]
        show = lambda c: chr(c + 32)
    tk = Tokens(enc, args.limit)
    for _label, s in rows:
        tk.collect(s)
    tk.build()
    plain, coded, tables = tk.saved()
    was, now = plain + len(rows), coded + len(rows) + tables
    print("%d strings, %d letters: %d bytes as they are (a byte a letter and an end each)" % (len(rows), plain, was))
    print("with tokens: %d bytes of text and ends, %d of tables: %d (%d%% less), before tk_print's "
          "104" % (coded + len(rows), tables, now, round(100.0 * (was - now) / was) if was else 0))
    print("%d pairs, %d longer runs" % (len(tk.pairs), len(tk.longs)))
    if args.report:
        for text, n in tk.uses(show):
            print("  %4d  [%s]" % (n, text))
    if args.out:
        lines = ["; made by tools/texttokens.py from %s: do not edit" % args.strings.replace("\\", "/").split("/")[-1]]
        for k, (label, s) in enumerate(rows):
            lines += ["%s:" % (label or "TKS%d" % k)] + byte_lines(tk.encode(s) + [0xFF])
        lines += tk.tables()
        if not args.no_code:
            lines += PRINT
        io.open(args.out, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
        print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
