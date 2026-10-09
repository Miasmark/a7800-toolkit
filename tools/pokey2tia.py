#!/usr/bin/env python3
"""Turn a POKEY game's music into TIA music: two voices out of up to eight.

    python tools/pokey2tia.py a7800-audio.log -o port-sound
    python tools/pokey2tia.py log -o out --offset 100 --map groups --groups 1+2,3+4
    python tools/pokey2tia.py log -o out --mash arp --fit

The input is what probes/audio.lua writes for a cartridge with a POKEY
(`A7800_POKEY=0x4000 ... audio.lua`). The output is a TIA song, in the tracker's
own forms: `tia.trk` (editable text, `tracker.py` reads it), `tia.asm` (data and
a player), `tia.wav`, and `orig.wav` (the POKEY song as the tracker's model plays
it) so the two can be listened to side by side, and `report.txt`.

The TIA has two channels and a few hundred fixed pitches, so this is a
translation with losses, and every choice is an option:

  Which voices. By default each frame's two LOUDEST voices play, and a voice that
  keeps being chosen keeps its channel, so notes do not hop between them.
  `--map groups --groups 1+2,3+4` instead gives TIA channel 1 to POKEY voices 1
  and 2, and channel 2 to voices 3 and 4.

  Mashing. When more voices want a channel than it has, `--mash loudest` (the
  default) lets the loudest win. `--mash arp` shares it: the voices take turns,
  one per `--arp N` frames (default 1), which is an arpeggio at 60/N a second.
  It keeps every voice in the music and sounds buzzy; try N = 2 or 3 for a
  chord-like shimmer. It can sound messy, which is why it is not the default.

  Pitch. TIA's pitches are sparse and fixed (AUDC $4, $C, $6 and $E, thirty-two
  dividers each), so a POKEY note lands up to about a quarter tone away, more in
  the bass. `--offset CENTS` moves the whole tune to sit better on them;
  `--fit` finds the offset that minimises the error, weighted by volume.
  Notes above the TIA's top (15.7 kHz) are silent, as they were mostly inaudible.

  Noise. Any distortion that is not a pure tone becomes TIA's 9-bit noise
  (AUDC $8) at the nearest divider rate; `--buzz tone` renders the 4-bit-poly
  distortion ($C0) as a tone instead, which is nearer what it sounds like.

Volume passes through (both are 4-bit). WHAT IT DOES NOT DO: model the
high-pass filter, volume-only samples (the tracker's capture format drops that
bit), or anything finer than one change per frame -- a game that changes pitch
inside a frame gets one pitch.
"""
import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tracker as T  # noqa: E402

TONE_MODES = (0x4, 0xC, 0x6, 0xE)       # purest first: the order breaks ties
NOISE_MODE = 0x8


def tia_tones(region="ntsc"):
    """[(hz, audc, audf)] one per distinct pitch, highest first."""
    seen, out = set(), []
    for mode_i, audc in enumerate(TONE_MODES):
        for audf in range(T.AUDF_MAX + 1):
            hz = T.frequency(audc, audf, region)
            out.append((hz, mode_i, audc, audf))
    out.sort(key=lambda t: (-t[0], t[1]))
    res = []
    for hz, _m, audc, audf in out:
        k = round(hz, 6)
        if k not in seen:
            seen.add(k)
            res.append((hz, audc, audf))
    return res


def nearest(tones, hz):
    """(hz, audc, audf) of the tone nearest `hz` in pitch."""
    best, bd = None, None
    for t in tones:
        d = abs(math.log(t[0] / hz))
        if bd is None or d < bd:
            best, bd = t, d
    return best


def cents(a, b):
    return 1200.0 * math.log(a / b, 2)


class Voice(object):
    def __init__(self, idx, kind, hz, vol, rate):
        self.idx, self.kind, self.hz, self.vol, self.rate = idx, kind, hz, vol, rate


def voices(song, i, row, buzz="noise"):
    """The voices playing on frame `i`: [Voice]; also how many were volume-only."""
    out = []
    for chip in range(max(1, song.nchips)):
        first = chip * 4
        ctl = song.ctl_of(i, first)
        audfs = [row[first + k][1] for k in range(4)]
        lows = set(T.pokey_joined(ctl).values())
        for k in range(4):
            if k in lows:
                continue
            dist, audf, vol = row[first + k]
            if vol == 0:
                continue
            rate = T.pokey_rate(k, audfs, ctl, song.region)
            tone = dist in (5, 7) or (buzz == "tone" and dist == 6)
            out.append(Voice(first + k + 1, "tone" if tone else "noise",
                             rate / 2.0, vol, rate))
    return out


def assign(cands, prev, nslots, mash, arp, frame, groups=None):
    """Which voice plays on each TIA channel this frame: a list of Voice or None."""
    slots = [None] * nslots
    if groups:
        for s, members in enumerate(groups[:nslots]):
            pool = sorted((v for v in cands if v.idx in members),
                          key=lambda v: v.idx)
            if not pool:
                continue
            if mash == "arp" and len(pool) > 1:
                slots[s] = pool[(frame // arp) % len(pool)]
            else:
                slots[s] = max(pool, key=lambda v: (v.vol, -v.idx))
        return slots
    ranked = sorted(cands, key=lambda v: (-v.vol, v.idx))
    chosen = ranked[:nslots]
    if mash == "arp" and len(ranked) > nslots:
        rest = ranked[nslots - 1:]
        chosen = ranked[:nslots - 1] + [rest[(frame // arp) % len(rest)]]
    for v in chosen:                          # a voice keeps its channel
        for s in range(nslots):
            if slots[s] is None and prev[s] is not None and prev[s].idx == v.idx:
                slots[s] = v
                break
    for v in chosen:
        if v not in slots:
            slots[slots.index(None)] = v
    return slots


def noise_audf(rate, region="ntsc"):
    return max(0, min(T.AUDF_MAX, int(round(T.CLOCK[region] / rate - 1))))


def convert(song, offset=0, fit=False, mash="loudest", arp=1, groups=None,
            buzz="noise", frames=None):
    """POKEY Song -> (TIA Song, stats dict)."""
    tones = tia_tones(song.region)
    lo, hi = (frames or (0, None))
    plan, prev = [], [None, None]
    stats = {"frames": 0, "over": 0, "mashed": 0, "dropped": 0, "swaps": 0,
             "noise": 0, "silent_high": 0, "max_voices": 0}
    for i, row in enumerate(song.states()):
        if i < lo or (hi is not None and i >= hi):
            continue
        cands = voices(song, i, row, buzz)
        stats["frames"] += 1
        stats["max_voices"] = max(stats["max_voices"], len(cands))
        slots = assign(cands, prev, 2, mash, arp, i, groups)
        if len(cands) > 2:
            stats["over"] += 1
            used = sum(1 for s in slots if s)
            if mash == "arp" and not groups:
                stats["mashed"] += 1
            stats["dropped"] += max(0, len(cands) - used) if mash != "arp" else 0
        stats["swaps"] += sum(1 for s, p in zip(slots, prev)
                              if s is not None and p is not None and s.idx != p.idx)
        plan.append(slots)
        prev = slots
    if fit:
        entries = [(v.hz, v.vol) for slots in plan for v in slots
                   if v is not None and v.kind == "tone"]
        offset = best_offset(tones, entries)
    shift = 2 ** (offset / 1200.0)
    out = T.Song(title=song.title, region=song.region, rate=song.rate, chip="tia")
    errs = []
    for slots in plan:
        cells = []
        for v in slots:
            if v is None:
                cells.append((0, 0, 0))
            elif v.kind == "noise":
                stats["noise"] += 1
                cells.append((NOISE_MODE, noise_audf(v.rate, song.region), v.vol))
            elif v.hz * shift > tones[0][0] * 1.03:
                stats["silent_high"] += 1
                cells.append((0, 0, 0))
            else:
                hz, audc, audf = nearest(tones, v.hz * shift)
                errs.append((cents(hz, v.hz * shift), v.vol))
                cells.append((audc, audf, v.vol))
        out.add(cells)
    stats["offset"] = offset
    stats["errors"] = errs
    return out, stats


def best_offset(tones, entries, span=600, step=5):
    if not entries:
        return 0
    best, bc = 0, None
    total = sum(w for _hz, w in entries)
    for off in range(-span, span + 1, step):
        sh = 2 ** (off / 1200.0)
        # a small charge for moving the tune at all, so a tune that already sits
        # on the TIA's pitches is left where it is
        cost = 0.05 * abs(off) * total
        for hz, w in entries:
            t = nearest(tones, hz * sh)
            cost += abs(cents(t[0], hz * sh)) * w
        if bc is None or cost < bc - 1e-9:
            best, bc = off, cost
    return best


def report(stats, song):
    errs = stats["errors"]
    tw = sum(w for _e, w in errs) or 1
    mean = sum(abs(e) * w for e, w in errs) / tw
    worst = max((abs(e) for e, _w in errs), default=0)
    within = sum(1 for e, _w in errs if abs(e) <= 25)
    lines = ["%d frames (%.1f s), offset %+d cents" % (
        stats["frames"], stats["frames"] / song.rate, stats["offset"]),
        "most voices at once: %d" % stats["max_voices"],
        "frames with more than two voices: %d (%d dropped voice-frames; %d frames "
        "mashed onto a shared channel)" % (stats["over"], stats["dropped"], stats["mashed"]),
        "tone pitch error: mean %.0f cents (volume-weighted), worst %.0f, %d of %d "
        "within 25 cents" % (mean, worst, within, len(errs)),
        "noise voices: %d; notes above the TIA's range, silenced: %d"
        % (stats["noise"], stats["silent_high"]),
        "voices that changed channel: %d times" % stats["swaps"]]
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="POKEY log from probes/audio.lua")
    ap.add_argument("-o", "--out", required=True, help="output folder")
    ap.add_argument("--region", default="ntsc", choices=sorted(T.CLOCK))
    ap.add_argument("--chip", default="pokey", choices=["pokey", "pokey2"])
    ap.add_argument("--map", default="loudest", choices=["loudest", "groups"],
                    dest="mapping")
    ap.add_argument("--groups", help="with --map groups: voices for TIA channel 1 "
                                     "and 2, e.g. 1+2,3+4")
    ap.add_argument("--mash", default="loudest", choices=["loudest", "arp"])
    ap.add_argument("--arp", type=int, default=1,
                    help="frames each voice holds a shared channel (default 1)")
    ap.add_argument("--offset", type=int, default=0, help="shift pitch, in cents")
    ap.add_argument("--fit", action="store_true", help="choose the offset automatically")
    ap.add_argument("--buzz", default="noise", choices=["noise", "tone"])
    ap.add_argument("--frames", default="0-", help="a range, e.g. 0-1800")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.log):
        sys.exit("pokey2tia: no such file: %s" % args.log)
    groups = None
    if args.mapping == "groups":
        if not args.groups:
            sys.exit("pokey2tia: --map groups needs --groups, e.g. --groups 1+2,3+4")
        try:
            groups = [set(int(x) for x in g.split("+")) for g in args.groups.split(",")]
        except ValueError:
            sys.exit("pokey2tia: --groups is voice numbers joined by +, "
                     "separated by commas: %r" % args.groups)
        if len(groups) != 2:
            sys.exit("pokey2tia: the TIA has two channels, so --groups needs two "
                     "groups, not %d" % len(groups))
    if args.arp < 1:
        sys.exit("pokey2tia: --arp must be 1 or more")
    lo, _, hi = args.frames.partition("-")
    try:
        frames = (int(lo or 0), int(hi) if hi else None)
    except ValueError:
        sys.exit("pokey2tia: --frames is START-END, e.g. 0-1800")
    song = T.read_capture(args.log, args.region, chip=args.chip)
    if not len(song):
        sys.exit("pokey2tia: %s holds no POKEY frames. Is it a POKEY log (nine "
                 "values a line)? A TIA log needs no conversion." % args.log)
    tia, stats = convert(song, args.offset, args.fit, args.mash, args.arp, groups,
                         args.buzz, frames)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "tia.trk"), "w", encoding="utf-8") as f:
        f.write(T.dump(tia))
    with open(os.path.join(args.out, "tia.asm"), "w", encoding="utf-8") as f:
        f.write(T.export_asm(tia))
    T.render(tia, os.path.join(args.out, "tia.wav"))
    notes = []
    try:
        T.render(song, os.path.join(args.out, "orig.wav"))
    except ValueError as e:
        notes.append("orig.wav not written: %s" % str(e).split("\n")[0])
    lines = report(stats, song) + notes
    if not stats["max_voices"]:
        lines.append("NO VOICE EVER SOUNDED. Wrong POKEY base address when the log "
                     "was made (cart.py names it), or the game was silent in that "
                     "window: play on, or run the probe longer.")
    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("wrote tia.trk, tia.asm, tia.wav, report.txt in %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
