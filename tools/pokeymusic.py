#!/usr/bin/env python3
"""Compact POKEY music for a game: MIDI parts in, patterns, 6502 data and a player out.

    python pokeymusic.py build music.json -o music.asm      data + player, and a report
    python pokeymusic.py build music.json --wav a.wav        hear it (the player's output)
    python pokeymusic.py build music.json --trk a.trk        open it in trackeredit.py
    python pokeymusic.py check music.json                    the 6502 player against the model

`tracker.py export` writes nine bytes a frame, which is right for a short cue and for
checking a song on the machine, and 32K a minute. A game wants what trackers have always
done instead: notes on a musical grid, cut into patterns, each pattern stored once however
often it plays, a tempo that need not divide the frame rate, and volume envelopes run by
the player. That is this.

## The arrangement file

```
{
  "player": {"prefix": "MU", "zp": "$D0", "ram": "$2300", "pokey": "$4000",
             "channels": [1, 2, 3]},
  "instruments": {
    "pluck": {"dist": 5, "volume": [9, 8, 7, 6, 5, 4]},
    "bass":  {"dist": 6, "period": 15, "volume": [10, 9, 8]}
  },
  "songs": [
    {"name": "flight", "root": "../packs/kit03",
     "voices": [
       {"channel": 1, "file": "lead.mid", "instrument": "pluck", "transpose": -12},
       {"channel": 2, "file": "arp.mid",  "instrument": "pluck"},
       {"channel": 3, "file": "bass.mid", "instrument": "bass"}
     ]}
  ]
}
```

- `channels`: the POKEY channels (1-4) the player owns, one voice each. A song names
  voices by channel; a channel a song leaves out stays silent in it.
- An instrument is a distortion (AUDC bits 7-5: 5 pure tone, 6 the 4-bit-poly bass,
  `period` keeping that bass to one timbre -- see `tracker.pokey_best_audf`) and a volume
  envelope, one value a frame from each note's start. The last value holds until the note
  ends.
- A voice is one MIDI file (and `track`, default the first with notes), its instrument,
  `transpose` in semitones, `pick` (high, low or first: which note survives where the
  part plays several at once) and `bars` (how long the part is before it loops; default
  its notes rounded up to a bar). Every voice loops on its own, so a 4-bar lead over a
  16-bar bass plays four times.
- Per song: `bpm` (default the first file's tempo), `grid` (rows a whole note: 16 for
  sixteenths; default the coarsest grid every note lands on) and `bar` (rows a pattern;
  default one 4/4 bar). `root`: where the files are, relative to this file; `${VAR}` is
  expanded, so a pack can live outside the repository.

## The data

A pattern is a byte stream:

    $00-$3F d, f   a note: d+1 rows, AUDF f (tuned for the voice's instrument)
    $40-$7F        a tie: the note goes on, (byte & $3F)+1 rows more
    $80-$BF        a rest: (byte & $3F)+1 rows
    $FF            the end of the pattern
    ($C0-$FE are kept for later; the player treats them as rests.)

Each voice has an order list of patterns, `$FF` then the index to loop back to. The
tempo is a 16-bit fraction of a row a frame, so 110 bpm sits on the 60 Hz frame as
exactly as 16 bits allow, and rows fall on whichever frame they land in.

## The player

    LDA #song : JSR MU_START      start a song from the top
    JSR MU_TICK                   once a frame
    JSR MU_STOP                   stop, and silence its channels
    MU_MUTE                       a bit a voice (bit 0 = the first channel in
                                  `channels`): set, the player leaves that channel's
                                  registers alone, so a sound effect can borrow it.
                                  The music keeps time underneath and picks the note up
                                  again the frame the bit is cleared.

Three voices measured over a 17-second loop: `MU_TICK` took 277 cycles a frame at the
median, 321 on average, and 961 at worst, the frame where all three voices start new
patterns on one row. The player is 369 bytes. It needs AUDCTL left at 0 (the 64 kHz
clock, no 16-bit pairs): every AUDF here is tuned for that.

## How it is checked

`--wav` and `--trk` do not re-interpret the notes: they run a Python model of the player
over the compiled bytes, frame by frame, and render what it would write through
`tracker.render`, the same POKEY model the rest of the toolkit is checked against. `check`
then runs the 6502 player itself, in the toolkit's simulator, over the same bytes, and
requires every frame's register writes to match the model's.
"""
import argparse
import io
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import midi as midi_mod
import tracker as T

FPS = {"ntsc": 60.0, "pal": 50.0}
GRIDS = (4, 8, 12, 16, 24, 32, 48, 64)      # rows per whole note
MAXDUR = 64


class MusicError(Exception):
    pass


def num(v):
    """'$D0', '0xD0', 208 -> 208"""
    if isinstance(v, int):
        return v
    v = str(v).strip()
    if v.startswith("$"):
        return int(v[1:], 16)
    return int(v, 0)


# ------------------------------------------------------------------ compile
def load(path):
    doc = json.load(io.open(path, encoding="utf-8"))
    doc["_dir"] = os.path.dirname(os.path.abspath(path))
    return doc


def voice_notes(doc, song, v):
    root = os.path.expandvars(song.get("root", ""))
    path = os.path.expandvars(v["file"])
    if not os.path.isabs(path):
        path = os.path.join(doc["_dir"], root, path)
    if not os.path.exists(path):
        raise MusicError("song %r, channel %d: no file %s"
                         % (song.get("name"), v["channel"], path))
    m = midi_mod.read(path)
    playable = [i for i, t in enumerate(m["tracks"]) if t["notes"]]
    if not playable:
        raise MusicError("%s has no notes" % path)
    t = v.get("track", playable[0])
    return m, m["tracks"][t]["notes"], path


def pick_grid(parts):
    """The coarsest grid every note's start and end lands on (within a tenth of a row)."""
    for g in GRIDS:
        ok = True
        for m, notes in parts:
            per = 4.0 * m["division"] / g               # ticks a row
            for n in notes:
                for t in (n["tick"], n["end_tick"]):
                    if abs(t / per - round(t / per)) > 0.1:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            return g
    return GRIDS[-1]


def timeline(notes, division, grid, pick, transpose):
    """The part as segments [(row, rows, note or None, new)] on the grid, one note at a
    time. `new` marks a note that starts there, as against one carried on."""
    per = 4.0 * division / grid
    spans = []
    for n in notes:
        a = int(round(n["tick"] / per))
        b = max(a + 1, int(round(n["end_tick"] / per)))
        spans.append((a, b, n["note"] + transpose, id(n)))
    end = max(b for _a, b, _n, _i in spans)
    chosen, dropped = [], 0
    for r in range(end):
        live = [s for s in spans if s[0] <= r < s[1]]
        if not live:
            chosen.append(None)
            continue
        dropped += len(live) - 1
        if pick == "low":
            chosen.append(min(live, key=lambda s: s[2]))
        elif pick == "first":
            chosen.append(min(live, key=lambda s: s[0]))
        else:
            chosen.append(max(live, key=lambda s: s[2]))
    return chosen, end, dropped


def best_period(ch, dist, notes, region):
    """For a polynomial distortion: the one pattern period (15, 5 or 3: one timbre) that
    plays every note of the part, with the smallest worst error. None -- any period, the
    timbre changing from note to note -- if no single one plays them all.

    Not simply the longest: period 15 reaches lowest (C1 on 64 kHz) but its 15-step
    pattern spreads the sound evenly over the harmonics, a buzz with little
    fundamental. Period 5 reaches G#1 and has more body; period 3, from E2, is a pulse.
    """
    best = None
    for p in (15, 5, 3):
        worst = 0.0
        for n in notes:
            f, e = T.pokey_best_audf(ch - 1, n, 0, region, dist=dist, period=p)
            if f is None:
                worst = None
                break
            worst = max(worst, abs(e))
        if worst is not None and (best is None or worst < best[1] - 0.5):
            best = (p, worst)
    return best[0] if best else None


def events_of(chosen, start, length):
    """Rows start..start+length of a part as [(kind, rows, note)], kind note/tie/rest,
    each no longer than MAXDUR."""
    out = []
    r = start
    while r < start + length:
        c = chosen[r] if r < len(chosen) else None
        q = r + 1
        while q < start + length and (chosen[q] if q < len(chosen) else None) is c:
            q += 1
        n = q - r
        if c is None:
            kind = "rest"
        elif r == start and r > 0 and r - 1 < len(chosen) and chosen[r - 1] is c:
            kind = "tie"                               # carried over from the last pattern
        else:
            kind = "note"
        while n > 0:
            d = min(n, MAXDUR)
            out.append((kind, d, None if c is None else c[2]))
            if kind == "note":
                kind = "tie"
            n -= d
        r = q
    return out


def encode(events, audf_of):
    b = []
    for kind, d, note in events:
        if kind == "note":
            b += [d - 1, audf_of(note)]
        elif kind == "tie":
            b.append(0x40 | (d - 1))
        else:
            b.append(0x80 | (d - 1))
    b.append(0xFF)
    return bytes(b)


def compile_music(doc, region="ntsc"):
    pl = doc.get("player", {})
    chans = pl.get("channels", [1, 2, 3])
    if not chans or len(set(chans)) != len(chans) or not all(1 <= c <= 4 for c in chans):
        raise MusicError("player.channels: distinct channels 1-4")
    insts = doc.get("instruments", {})
    inames = sorted(insts)
    if not inames:
        raise MusicError("no instruments")
    env, env_start, dist_byte = [], [], []
    for name in inames:
        i = insts[name]
        vol = [int(x) for x in i.get("volume", [8])]
        if not vol or any(not 0 <= x <= 15 for x in vol):
            raise MusicError("instrument %r: volumes 0-15, at least one" % name)
        env_start.append(len(env))
        env += vol + [0xFF]
        dist_byte.append((int(i.get("dist", 5)) & 7) << 5)
    if len(env) > 255:
        raise MusicError("the envelopes come to %d bytes; the player indexes 255" % len(env))

    patterns, pat_index = [], {}
    songs, report = [], []

    def pattern(bts):
        if bts not in pat_index:
            pat_index[bts] = len(patterns)
            patterns.append(bts)
        return pat_index[bts]

    silent = pattern(bytes([0x80 | (MAXDUR - 1), 0xFF]))
    for s in doc.get("songs", []):
        byc = {v["channel"]: v for v in s.get("voices", [])}
        for c in byc:
            if c not in chans:
                raise MusicError("song %r uses channel %d, which the player does not own"
                                 % (s.get("name"), c))
        parts = {}
        for c, v in byc.items():
            m, notes, path = voice_notes(doc, s, v)
            parts[c] = (m, notes, path)
        first = parts[sorted(parts)[0]][0] if parts else None
        bpm = s.get("bpm") or (60e6 / first["tempo_map"][0][1] if first else 120.0)
        grid = s.get("grid") or pick_grid([(m, n) for m, n, _p in parts.values()])
        bar = s.get("bar") or grid                       # a 4/4 bar
        rpf = bpm / 60.0 * grid / 4.0 / FPS[region]
        tempo = int(round(rpf * 65536))
        if not 0 < tempo < 65536:
            raise MusicError("song %r: %.2f rows a frame; the player does under one"
                             % (s.get("name"), rpf))
        song = {"name": s.get("name", "song%d" % len(songs)), "tempo": tempo,
                "bpm": bpm, "grid": grid, "bar": bar, "voices": []}
        for c in chans:
            if c not in byc:
                song["voices"].append({"channel": c, "inst": 0, "orders": [silent],
                                       "loop": 0, "rows": bar, "unused": True})
                continue
            v = byc[c]
            m, notes, path = parts[c]
            iname = v.get("instrument")
            if iname not in insts:
                raise MusicError("song %r channel %d: no instrument %r"
                                 % (song["name"], c, iname))
            inst = insts[iname]
            chosen, end, dropped = timeline(notes, m["division"], grid,
                                            v.get("pick", "high"), int(v.get("transpose", 0)))
            bars = v.get("bars") or max(1, int(math.ceil(end / float(bar) - 0.05)))
            length = bars * bar
            dist = int(inst.get("dist", 5))
            period = inst.get("period", "auto")
            if dist not in (5, 7) and period == "auto":
                period = best_period(c, dist, set(x[2] for x in chosen if x), region)
            cents, tuned = [], {}

            def audf_of(note, _c=c, _d=dist, _p=period):
                if note not in tuned:
                    f, e = T.pokey_best_audf(_c - 1, note, 0, region, dist=_d, period=_p)
                    if f is None:
                        raise MusicError("channel %d: distortion %d has no note %s"
                                         % (_c, _d, midi_mod.note_name(note)))
                    tuned[note] = (f, e)
                cents.append(abs(tuned[note][1]))
                return tuned[note][0]

            orders = []
            for p0 in range(0, length, bar):
                orders.append(pattern(encode(events_of(chosen, p0, bar), audf_of)))
            notes_kept = sum(1 for r in range(min(end, length))
                             if chosen[r] is not None and (r == 0 or chosen[r - 1] is not chosen[r]))
            song["voices"].append({"channel": c, "inst": inames.index(iname),
                                   "orders": orders, "loop": int(v.get("loop", 0)),
                                   "rows": length, "unused": False})
            cs = sorted(cents) or [0]
            report.append("  %-10s ch%d  %-34s %2d bars, %3d notes%s; tuning median %2.0f "
                          "cents, worst %2.0f%s"
                          % (song["name"][:10], c, os.path.basename(path)[-34:], bars,
                             notes_kept, (", %d overlapping dropped" % dropped) if dropped
                             else "", cs[len(cs) // 2], cs[-1],
                             (" (period %d)" % period) if dist not in (5, 7) and period
                             else ""))
            if end > length:
                report.append("             (the part runs %d rows; %d bars keep %d)"
                              % (end, bars, length))
        songs.append(song)
    if len(patterns) > 255:
        raise MusicError("%d patterns; the order lists hold 255" % len(patterns))
    return {"player": pl, "channels": chans, "instruments": inames, "env": env,
            "env_start": env_start, "dist": dist_byte, "patterns": patterns,
            "songs": songs, "report": report, "region": region}


# ------------------------------------------------------------------ 6502 output
def layout(mus):
    """Where the player keeps its state: zero page for the pointer, RAM for the rest."""
    pl = mus["player"]
    px = pl.get("prefix", "MU")
    zp = num(pl.get("zp", "$D0"))
    ram = num(pl.get("ram", "$2300"))
    nv = len(mus["channels"])
    names = ["PL", "PH", "OL", "OH", "OI", "WT", "FQ", "IN", "EI", "VL", "GT"]
    addr = {"P": zp, "T": zp + 2}
    a = ram
    for n in names:
        addr[n] = a
        a += nv
    for n in ("ACCL", "ACCH", "TPL", "TPH", "ON", "MUTE"):
        addr[n] = a
        a += 1
    return px, addr, a - ram


def hexb(bs):
    return ",".join("$%02X" % x for x in bs)


def data_lines(lines, label, bs, per=16):
    lines.append("%s:" % label)
    for i in range(0, len(bs), per):
        lines.append("    .byte " + hexb(bs[i:i + per]))


def asm(mus, data_only=False):
    px, addr, ramsize = layout(mus)
    pl = mus["player"]
    pokey = num(pl.get("pokey", "$4000"))
    nv = len(mus["channels"])
    L = []
    nbytes = sum(len(p) for p in mus["patterns"])
    L.append("; --- %s: POKEY music (a7800-toolkit pokeymusic.py) -------------------" % px)
    L.append("; %d song(s), %d voice(s) on channel(s) %s, %d pattern(s) of %d bytes in all"
             % (len(mus["songs"]), nv, ",".join(str(c) for c in mus["channels"]),
                len(mus["patterns"]), nbytes))
    L.append("; State: %s_P/%s_T ($%02X-$%02X) in zero page, %d bytes from $%04X."
             % (px, px, addr["P"], addr["T"], ramsize, addr["PL"]))
    for k in ("P", "T", "PL", "PH", "OL", "OH", "OI", "WT", "FQ", "IN", "EI", "VL", "GT",
              "ACCL", "ACCH", "TPL", "TPH", "ON", "MUTE"):
        L.append("%s_%s = $%04X" % (px, k, addr[k]) if addr[k] > 0xFF
                 else "%s_%s = $%02X" % (px, k, addr[k]))
    L.append("%s_AUDF = $%04X" % (px, pokey))
    L.append("%s_AUDC = $%04X" % (px, pokey + 1))
    L.append("%s_NV = %d" % (px, nv))
    if not data_only:
        code = PLAYER.replace("MU_", px + "_").replace("#NVM1", "#%d" % (nv - 1))             .replace("#NV", "#%d" % nv)
        # its own branch labels carry the prefix too, so they cannot meet a game's
        code = re.sub(r"(ms|mt|mk|ev)_", lambda m: "%s_%s_" % (px.lower(), m.group(1)), code)
        L.append(code)
    # tables
    songs = mus["songs"]
    data_lines(L, px + "_TEMPO_L", [s["tempo"] & 0xFF for s in songs])
    data_lines(L, px + "_TEMPO_H", [s["tempo"] >> 8 for s in songs])
    data_lines(L, px + "_SBASE", [i * nv for i in range(len(songs))])
    L.append("%s_SORD_L:" % px)
    for i, s in enumerate(songs):
        L.append("    .byte " + ",".join("<%s_O%d_%d" % (px, i, k) for k in range(nv)))
    L.append("%s_SORD_H:" % px)
    for i, s in enumerate(songs):
        L.append("    .byte " + ",".join(">%s_O%d_%d" % (px, i, k) for k in range(nv)))
    data_lines(L, px + "_SINST", [v["inst"] for s in songs for v in s["voices"]])
    data_lines(L, px + "_CHAN", [2 * (c - 1) for c in mus["channels"]])
    data_lines(L, px + "_BIT", [1 << k for k in range(nv)])
    data_lines(L, px + "_DIST", mus["dist"])
    data_lines(L, px + "_ENVS", mus["env_start"])
    data_lines(L, px + "_ENV", mus["env"])
    L.append("%s_PAT_L:" % px)
    for i in range(0, len(mus["patterns"]), 8):
        L.append("    .byte " + ",".join("<%s_PT%d" % (px, k)
                                         for k in range(i, min(i + 8, len(mus["patterns"])))))
    L.append("%s_PAT_H:" % px)
    for i in range(0, len(mus["patterns"]), 8):
        L.append("    .byte " + ",".join(">%s_PT%d" % (px, k)
                                         for k in range(i, min(i + 8, len(mus["patterns"])))))
    L.append("%s_END:" % px)
    L.append("    .byte $FF")
    for i, s in enumerate(songs):
        for k, v in enumerate(s["voices"]):
            L.append("%s_O%d_%d:                 ; %s, channel %d" % (px, i, k, s["name"], v["channel"]))
            L.append("    .byte " + hexb(v["orders"] + [0xFF, v["loop"]]))
    for k, p in enumerate(mus["patterns"]):
        data_lines(L, "%s_PT%d" % (px, k), p)
    return "\n".join(L) + "\n"


PLAYER = r"""
; MU_START: A = the song. Starts it from the top on the next MU_TICK.
MU_START:
    TAX
    LDA MU_TEMPO_L,X
    STA MU_TPL
    LDA MU_TEMPO_H,X
    STA MU_TPH
    LDA #$FF                  ; so the first frame falls on a row
    STA MU_ACCL
    STA MU_ACCH
    LDY MU_SBASE,X
    LDX #0
ms_voice:
    LDA MU_SORD_L,Y
    STA MU_OL,X
    LDA MU_SORD_H,Y
    STA MU_OH,X
    LDA MU_SINST,Y
    STA MU_IN,X
    LDA #$FF                  ; before the first pattern
    STA MU_OI,X
    LDA #<MU_END              ; an empty pattern: the first row moves on
    STA MU_PL,X
    LDA #>MU_END
    STA MU_PH,X
    LDA #1
    STA MU_WT,X
    LDA #0
    STA MU_GT,X
    STA MU_VL,X
    STA MU_FQ,X
    INY
    INX
    CPX #NV
    BNE ms_voice
    LDA #1
    STA MU_ON
    RTS

; MU_STOP: stops, and silences the channels it is not lending out.
MU_STOP:
    LDA #0
    STA MU_ON
    LDX #NVM1
mt_voice:
    LDA MU_MUTE
    AND MU_BIT,X
    BNE mt_next
    LDY MU_CHAN,X
    LDA #0
    STA MU_AUDC,Y
mt_next:
    DEX
    BPL mt_voice
    RTS

; MU_TICK: once a frame.
MU_TICK:
    LDA MU_ON
    BNE mk_on
    RTS
mk_on:
    CLC                       ; the tempo: a 16-bit fraction of a row a frame
    LDA MU_ACCL
    ADC MU_TPL
    STA MU_ACCL
    LDA MU_ACCH
    ADC MU_TPH
    STA MU_ACCH
    BCC mk_frame
    LDX #NVM1                 ; a row: each voice counts down its event
mk_row:
    DEC MU_WT,X
    BNE mk_rnext
    JSR MU_EVENT
mk_rnext:
    DEX
    BPL mk_row
mk_frame:
    LDX #NVM1                 ; every frame: the envelopes, then the registers
mk_voice:
    LDA MU_GT,X
    BEQ mk_quiet
    LDY MU_EI,X
    LDA MU_ENV,Y
    CMP #$FF                  ; the envelope's end: hold the last volume
    BEQ mk_out
    STA MU_VL,X
    INC MU_EI,X
    JMP mk_out
mk_quiet:
    LDA #0
    STA MU_VL,X
mk_out:
    LDA MU_MUTE               ; lent to a sound effect: leave the channel alone
    AND MU_BIT,X
    BNE mk_vnext
    LDY MU_IN,X
    LDA MU_DIST,Y
    ORA MU_VL,X
    LDY MU_CHAN,X
    STA MU_AUDC,Y
    LDA MU_FQ,X
    STA MU_AUDF,Y
mk_vnext:
    DEX
    BPL mk_voice
    RTS

; MU_EVENT: X = the voice. Its next event, from its pattern (and order list).
MU_EVENT:
    LDA MU_PL,X
    STA MU_P
    LDA MU_PH,X
    STA MU_P+1
    LDY #0
ev_read:
    LDA (MU_P),Y
    INY
    CMP #$FF
    BEQ ev_next
    CMP #$80
    BCS ev_rest
    CMP #$40
    BCS ev_tie
    ADC #1                    ; a note (carry clear): its rows, its AUDF
    STA MU_WT,X
    LDA (MU_P),Y
    INY
    STA MU_FQ,X
    STY MU_T
    LDY MU_IN,X
    LDA MU_ENVS,Y             ; the envelope from its start
    STA MU_EI,X
    LDA #1
    STA MU_GT,X
    LDY MU_T
    JMP ev_done
ev_tie:
    AND #$3F                  ; the note goes on
    STA MU_WT,X
    INC MU_WT,X
    JMP ev_done
ev_rest:
    AND #$3F
    STA MU_WT,X
    INC MU_WT,X
    LDA #0
    STA MU_GT,X
ev_done:
    TYA                       ; past the event
    CLC
    ADC MU_P
    STA MU_PL,X
    LDA MU_P+1
    ADC #0
    STA MU_PH,X
    RTS
ev_next:
    LDA MU_OL,X               ; the next pattern in the voice's order list
    STA MU_P
    LDA MU_OH,X
    STA MU_P+1
    INC MU_OI,X
    LDY MU_OI,X
    LDA (MU_P),Y
    CMP #$FF
    BNE ev_pattern
    INY                       ; the end: back to the loop point
    LDA (MU_P),Y
    STA MU_OI,X
    TAY
    LDA (MU_P),Y
ev_pattern:
    TAY
    LDA MU_PAT_L,Y
    STA MU_P
    STA MU_PL,X
    LDA MU_PAT_H,Y
    STA MU_P+1
    STA MU_PH,X
    LDY #0
    JMP ev_read
"""


# ------------------------------------------------------------------ the model
class Model(object):
    """The player, in Python, over the compiled bytes: what MU_TICK writes each frame."""

    def __init__(self, mus):
        self.m = mus
        self.nv = len(mus["channels"])
        self.mute = 0

    def start(self, song):
        s = self.m["songs"][song]
        self.tempo = s["tempo"]
        self.acc = 0xFFFF
        self.v = []
        for k in range(self.nv):
            vc = s["voices"][k]
            self.v.append({"orders": vc["orders"], "loop": vc["loop"], "oi": -1,
                           "pat": b"\xff", "pos": 0, "wt": 1, "fq": 0,
                           "inst": vc["inst"], "ei": 0, "vl": 0, "gt": 0})
        self.on = True

    def event(self, v):
        while True:
            b = v["pat"][v["pos"]]
            v["pos"] += 1
            if b == 0xFF:
                v["oi"] += 1
                if v["oi"] >= len(v["orders"]):
                    v["oi"] = v["loop"]
                v["pat"] = self.m["patterns"][v["orders"][v["oi"]]]
                v["pos"] = 0
                continue
            if b >= 0x80:
                v["wt"] = (b & 0x3F) + 1
                v["gt"] = 0
            elif b >= 0x40:
                v["wt"] = (b & 0x3F) + 1
            else:
                v["wt"] = b + 1
                v["fq"] = v["pat"][v["pos"]]
                v["pos"] += 1
                v["ei"] = self.m["env_start"][v["inst"]]
                v["gt"] = 1
            return

    def tick(self):
        """{channel: (AUDF, AUDC)} written this frame."""
        if not self.on:
            return {}
        self.acc += self.tempo
        if self.acc > 0xFFFF:
            self.acc &= 0xFFFF
            for k in range(self.nv - 1, -1, -1):
                v = self.v[k]
                v["wt"] = (v["wt"] - 1) & 0xFF
                if v["wt"] == 0:
                    self.event(v)
        out = {}
        env = self.m["env"]
        for k in range(self.nv - 1, -1, -1):
            v = self.v[k]
            if v["gt"]:
                e = env[v["ei"]]
                if e != 0xFF:
                    v["vl"] = e
                    v["ei"] = (v["ei"] + 1) & 0xFF
            else:
                v["vl"] = 0
            if not self.mute & (1 << k):
                out[self.m["channels"][k]] = (v["fq"], self.m["dist"][v["inst"]] | v["vl"])
        return out


def frames_for(mus, song):
    """One full loop of the song's longest voice, in frames."""
    s = mus["songs"][song]
    rows = max(v["rows"] for v in s["voices"])
    return int(math.ceil(rows * 65536.0 / s["tempo"])) + 1


def to_song(mus, song, frames):
    """The model's output as a tracker song: one row a frame, four channels."""
    md = Model(mus)
    md.start(song)
    trk = T.Song(title="%s (pokeymusic)" % mus["songs"][song]["name"], region=mus["region"],
                 chip="pokey")
    cur = {c: (0, 0, 0) for c in range(1, 5)}
    for _ in range(frames):
        for c, (f, audc) in md.tick().items():
            cur[c] = ((audc >> 5) & 7, f, audc & 15)
        trk.add([cur[c] for c in range(1, 5)], audctl=0)
    return trk


# ------------------------------------------------------------------ check in the simulator
class FlatBus(object):
    """64K of RAM for the simulator, logging POKEY writes."""

    def __init__(self, base):
        self.mem = bytearray(0x10000)
        self.base = base
        self.mram = None
        self.writes = []

    def read(self, a):
        return self.mem[a & 0xFFFF]

    def write(self, a, v):
        a &= 0xFFFF
        if self.base <= a < self.base + 8:
            self.writes.append((a - self.base, v & 0xFF))
        self.mem[a] = v & 0xFF


def check(mus, frames=None, org=0x8000):
    """Run the 6502 player over the compiled song(s) in the simulator, frame by frame,
    against the model. Returns (frames compared, first mismatch or None)."""
    import asm as asm_mod
    import sim
    px, _addr, _n = layout(mus)
    src = [".org $%04X" % org, "    BRK"] + asm(mus).split("\n")
    a = asm_mod.Assembler()
    code = bytes(a.assemble(src))
    pokey = num(mus["player"].get("pokey", "$4000"))
    total = 0
    for song in range(len(mus["songs"])):
        bus = FlatBus(pokey)
        bus.mem[org:org + len(code)] = code
        bus.mem[0xFFFC] = org & 0xFF
        bus.mem[0xFFFD] = org >> 8
        cpu = sim.CPU(bus)
        sentinel = 0xFFF0

        def call(addr, A=0):
            ret = sentinel - 1
            cpu.push(ret >> 8)
            cpu.push(ret & 0xFF)
            cpu.a = A
            cpu.pc = addr
            n = 0
            while cpu.pc != sentinel:
                cpu.step()
                n += 1
                if n > 20000:
                    raise MusicError("the player ran away at $%04X" % cpu.pc)
            return n

        call(a.sym[px + "_START"], song)
        md = Model(mus)
        md.start(song)
        nf = frames or frames_for(mus, song)
        worst = 0
        for f in range(nf):
            bus.writes = []
            c0 = cpu.cycles
            call(a.sym[px + "_TICK"])
            worst = max(worst, cpu.cycles - c0)
            got = {}
            for reg, v in bus.writes:
                ch = reg // 2 + 1
                fq, ac = got.get(ch, (None, None))
                if reg & 1:
                    ac = v
                else:
                    fq = v
                got[ch] = (fq, ac)
            want = md.tick()
            if got != want:
                return total + f, ("song %d frame %d: the 6502 wrote %s, the model %s"
                                   % (song, f, got, want)), worst
            total += 1
    return total, None, worst


# ------------------------------------------------------------------ main
def cmd_build(args):
    doc = load(args.music)
    mus = compile_music(doc, args.region)
    text = asm(mus)
    if args.out:
        io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    nbytes = sum(len(p) for p in mus["patterns"])
    orders = sum(len(v["orders"]) + 2 for s in mus["songs"] for v in s["voices"])
    print("%s: %d song(s), %d pattern(s), %d pattern bytes, %d order bytes, %d envelope bytes"
          % (os.path.basename(args.music), len(mus["songs"]), len(mus["patterns"]), nbytes,
             orders, len(mus["env"])))
    for s in mus["songs"]:
        print("  %-10s %.1f bpm, grid 1/%d, %d rows a pattern, %.4f rows a frame"
              % (s["name"][:10], s["bpm"], s["grid"], s["bar"], s["tempo"] / 65536.0))
    for line in mus["report"]:
        print(line)
    if args.out:
        print("wrote %s" % args.out)
    song = args.song
    if args.wav or args.trk:
        frames = int(args.seconds * FPS[args.region]) if args.seconds else frames_for(mus, song)
        trk = to_song(mus, song, frames)
        if args.trk:
            io.open(args.trk, "w", encoding="utf-8").write(T.dump(trk))
            print("wrote %s (%d frames)" % (args.trk, frames))
        if args.wav:
            T.render(trk, args.wav)
            print("wrote %s (%.1f s)" % (args.wav, frames / FPS[args.region]))
    return 0


def cmd_check(args):
    doc = load(args.music)
    mus = compile_music(doc, args.region)
    n, bad, worst = check(mus, int(args.seconds * FPS[args.region]) if args.seconds else None)
    if bad:
        print("MISMATCH after %d frames: %s" % (n, bad))
        return 1
    print("%d frames: the 6502 player wrote what the model wrote, every frame "
          "(worst %d cycles a frame)" % (n, worst))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="data and player, a report, and optionally a WAV")
    b.add_argument("music", help="the arrangement (.json)")
    b.add_argument("-o", "--out", help="the .asm to write")
    b.add_argument("--wav", help="render the player's output to a WAV")
    b.add_argument("--trk", help="the player's output as a tracker file")
    b.add_argument("--song", type=int, default=0)
    b.add_argument("--seconds", type=float, help="how long to render (default one loop)")
    b.add_argument("--region", default="ntsc", choices=sorted(FPS))
    b.set_defaults(func=cmd_build)
    c = sub.add_parser("check", help="run the 6502 player in the simulator against the model")
    c.add_argument("music")
    c.add_argument("--seconds", type=float, help="how long (default one loop of each song)")
    c.add_argument("--region", default="ntsc", choices=sorted(FPS))
    c.set_defaults(func=cmd_check)
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except (MusicError, midi_mod.MidiError, IOError, ValueError) as e:
        sys.stderr.write("%s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
