#!/usr/bin/env python3
"""
Find a cartridge's music by following the code that writes to the sound chip.

    python audiotrace.py game.a78 [-c annotations.json]

`tracker.py capture` records what a game plays while it runs. This does the
static half: it disassembles the ROM, finds every store to a TIA or POKEY audio
register, works backwards to whatever supplies the value, and reports the tables
it lands on. That is the part of "where is the music?" that otherwise costs an
afternoon of reading.

What it can do, and what it cannot:

  * **Locating the player** is reliable. Audio stores cluster tightly -- a music
    routine and a sound-effect routine are usually a few hundred bytes each and
    nowhere near each other -- so grouping the stores by address separates them
    without knowing anything about the game.

  * **Finding the tables** works whenever the value comes from an indexed load,
    which is how nearly every player is written: `LDA table,Y / STA AUDC0`. The
    table's address falls straight out. Where the load goes through a zero-page
    pointer instead, it follows one more hop to whatever fills that pointer,
    which is usually the pointer table itself.

  * **Reconstructing the song** is not attempted, and should be treated with
    suspicion wherever a tool claims it. Every player invents its own format:
    Midnight Mutants nests song -> track -> pattern -> note, four levels deep,
    with a duration index and an instrument packed into one byte. Nothing
    portable can guess that. What this gives you is the addresses and the bytes;
    the shape is yours to work out, and `tracker.py capture` is the way to check
    a guess against what the game actually plays.
"""
import argparse
import os
import sys
import contextlib
import io as _io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disasm as D
import a7800
import cart as cart_module

# The registers worth watching, by offset from their chip's base.
TIA_AUDIO = {0x15: "AUDC0", 0x16: "AUDC1", 0x17: "AUDF0",
             0x18: "AUDF1", 0x19: "AUDV0", 0x1A: "AUDV1"}
POKEY_AUDIO = {0: "AUDF1", 1: "AUDC1", 2: "AUDF2", 3: "AUDC2", 4: "AUDF3",
               5: "AUDC3", 6: "AUDF4", 7: "AUDC4", 8: "AUDCTL"}

STORES = {"STA": "A", "STX": "X", "STY": "Y"}
# What an instruction leaves in a register. A read-modify-write on the
# accumulator counts: the value still came from whatever was loaded before it.
SETS = {"LDA": "A", "LDX": "X", "LDY": "Y", "TAX": "X", "TAY": "Y",
        "TXA": "A", "TYA": "A", "PLA": "A", "AND": "A", "ORA": "A",
        "EOR": "A", "ADC": "A", "SBC": "A", "LSR": "A", "ASL": "A",
        "ROL": "A", "ROR": "A", "INX": "X", "DEX": "X", "INY": "Y",
        "DEY": "Y", "LDA_": "A"}
INDEXED = {"abx", "aby", "zpx", "zpy"}
INDIRECT = {"izx", "izy"}


# Stores to an audio register, as raw opcodes. 8D = STA abs, 9D = STA abs,X,
# 99 = STA abs,Y -- between them they cover how nearly every player writes.
def _store_patterns():
    out = []
    for op in (0x8D, 0x9D, 0x99):
        for a in (0x15, 0x16, 0x17, 0x18, 0x19, 0x1A):     # TIA
            out.append(bytes([op, a, 0x00]))
        for base in (0x4000, 0x0450, 0x0800, 0x0440):      # POKEY
            for r in range(9):
                out.append(bytes([op, (base + r) & 0xFF, base >> 8]))
    return out


STORE_PATTERNS = _store_patterns()


def player_signature(rom):
    """A fingerprint of the code that writes to the sound chip.

    `rom` is the ROM bytes without the header, or a path.

    Two cartridges built on the same music engine have the same bytes around
    their audio stores, whatever the game is called. That makes this a better
    key for a format description than a title: a format file describes an
    *engine*, and the engine is what recurs. Across the library, 40 signatures
    span more than one game, covering 373 images -- and the biggest single one
    covers 27 different titles.

    Deliberately crude: the bytes either side of the first few stores, hashed.
    It is a lookup key, not a proof, and a format file that matches on it should
    still be checked against the game it lands on.
    """
    import hashlib
    if isinstance(rom, str):
        raw = open(rom, "rb").read()
        if len(raw) > 128 and raw[1:10] == b"ATARI7800":
            raw = raw[128:]
        rom = raw
    hits = []
    for pat in STORE_PATTERNS:
        i = rom.find(pat)
        while i != -1:
            hits.append(i)
            i = rom.find(pat, i + 1)
    if not hits:
        return None
    hits.sort()
    blob = b"".join(rom[max(0, i - 24):i + 24] for i in hits[:6])
    return hashlib.sha1(blob).hexdigest()[:12]


def analyse(rom, config=None, low=None, mapper=None):
    """Disassemble, and hand back the analyzer plus the cart."""
    grab = {}
    orig = D.Analyzer.name_all

    def spy(self):
        orig(self)
        grab["an"] = self

    D.Analyzer.name_all = spy
    argv = ["audiotrace", rom, "-o", os.devnull + "_dir"]
    if config:
        argv += ["-c", config]
    if low:
        argv += ["--low", low]
    if mapper:
        argv += ["--mapper", mapper]
    saved, sys.argv = sys.argv, argv
    try:
        import tempfile
        out = tempfile.mkdtemp(prefix="audiotrace-")
        sys.argv = [x if x != os.devnull + "_dir" else out for x in argv]
        with contextlib.redirect_stdout(_io.StringIO()):
            rc = D.main()
    finally:
        sys.argv = saved
        D.Analyzer.name_all = orig
    if "an" not in grab:
        raise RuntimeError("the disassembler did not get far enough to analyse "
                           "this image (rc=%s)" % rc)
    return grab["an"]


def audio_map(cart):
    """Which absolute addresses are audio registers on this cartridge."""
    regs = {}
    for mirror in (0x000, 0x100, 0x200):
        for off, name in TIA_AUDIO.items():
            regs[mirror + off] = ("TIA", name)
    for base in cart.pokeys():
        for off, name in POKEY_AUDIO.items():
            regs[base + off] = ("POKEY", name)
    return regs


def prev_insn(an, sp, addr):
    """The instruction that falls through into `addr`, if there is one."""
    for back in (1, 2, 3):
        c = (sp, addr - back)
        if c in an.insn and an.insn[c][3] == back:
            return c
    return None


def source_of(an, loc, reg, depth=16):
    """Walk back through straight-line code for what last set `reg`.

    Straight-line only: following a branch backwards would need to know which
    way it went, and a wrong guess here invents a table that does not exist.
    Stopping early and saying so is the honest failure.
    """
    cur = loc
    for _ in range(depth):
        cur = prev_insn(an, *cur)
        if cur is None:
            return None
        mn, mode, operand, _ln = an.insn[cur]
        if SETS.get(mn) == reg:
            return cur, mn, mode, operand
    return None


def zp_filler(an, space, zp, before, depth=400):
    """What last wrote the zero-page pointer `zp`, looking back from `before`.

    A player that reads its notes through `LDA (ptr),Y` keeps the table address
    in RAM, so the interesting address is one hop further back: whatever stored
    into that pointer, which is usually an indexed load from a table of
    pointers.
    """
    best = None
    for (sp, a), (mn, mode, operand, _ln) in an.insn.items():
        if sp != space or mn != "STA" or operand != zp:
            continue
        if a >= before or (best and a <= best[0][1]):
            continue
        src = source_of(an, (sp, a), "A")
        if src:
            best = ((sp, a), src)
    return best


def find_writers(an, cart):
    """Every audio store, with whatever supplies its value."""
    regs = audio_map(cart)
    out = []
    for loc in sorted(an.insn):
        mn, mode, operand, _ln = an.insn[loc]
        if mn not in STORES or operand is None or operand not in regs:
            continue
        chip, name = regs[operand]
        src = source_of(an, loc, STORES[mn])
        entry = {"at": loc, "chip": chip, "reg": name, "src": src, "hop": None}
        if src and src[2] in INDIRECT:
            entry["hop"] = zp_filler(an, loc[0], src[3], loc[1])
        out.append(entry)
    return out


def cluster(writers, gap=0x200):
    """Group writers into routines: same space, within `gap` bytes."""
    groups = []
    for w in sorted(writers, key=lambda w: (w["at"][0], w["at"][1])):
        sp, a = w["at"]
        if groups and groups[-1]["space"] == sp and a - groups[-1]["hi"] <= gap:
            groups[-1]["hi"] = a
            groups[-1]["writers"].append(w)
        else:
            groups.append({"space": sp, "lo": a, "hi": a, "writers": [w]})
    return groups


def tables_in(group):
    """The distinct data addresses a group reads, and how."""
    seen = {}
    for w in group["writers"]:
        for src, why in ((w["src"], "direct"), (w["hop"][1] if w["hop"] else None,
                                                "through a pointer")):
            if not src:
                continue
            _sloc, mn, mode, operand = src
            if operand is None or mode in ("imm", "imp", "acc"):
                continue
            if operand < 0x4000:            # RAM, not a table in ROM
                continue
            key = (operand, mode)
            seen.setdefault(key, {"addr": operand, "mode": mode, "how": why,
                                  "regs": set()})
            seen[key]["regs"].add(w["reg"])
    return sorted(seen.values(), key=lambda t: t["addr"])


def coverage(an, cart):
    """Bytes reached as code, per space, as a fraction.

    This decides how much the rest of the report is worth. A static trace can
    only see code the disassembler reached, so a player behind an indirect jump
    or a RAM vector is invisible -- and the report would otherwise say
    "constants only" and read like "this game has no music", which is a
    different claim entirely.
    """
    out = {}
    for sp in cart.spaces():
        size = cart.size_of(sp)
        n = sum(ln for (s2, _a), (_m, _md, _o, ln) in an.insn.items() if s2 == sp)
        out[sp] = (n, size)
    return out


def dump(cart, space, addr, n=16):
    try:
        return " ".join("%02X" % b for b in cart.slice(space, addr, n))
    except Exception:                        # noqa: BLE001
        return "(outside this space)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-c", "--config")
    ap.add_argument("--low", choices=["none", "ram", "bank6", "rom"])
    ap.add_argument("--mapper", choices=["linear", "supergame", "absolute"])
    ap.add_argument("--bytes", type=int, default=16,
                    help="how much of each table to show")
    ap.add_argument("--signature", action="store_true",
                    help="print this cartridge's player fingerprint and stop, "
                         "for pasting into a format file's match block")
    ap.add_argument("--json", action="store_true",
                    help="emit annotation blocks for the tables found")
    args = ap.parse_args()

    if args.signature:
        sig = player_signature(args.rom)
        if not sig:
            print("no audio stores in this image, so it has no player "
                  "fingerprint")
            return 1
        print(sig)
        return 0

    try:
        an = analyse(args.rom, args.config, args.low, args.mapper)
    except (RuntimeError, cart_module.UnknownMapper,
            cart_module.UnknownSpace) as e:
        sys.stderr.write("%s\n" % e)
        return 2
    cart = an.cart

    writers = find_writers(an, cart)
    if not writers:
        print("No audio stores in the traced code.")
        print("Either the player is only reached through a path the tracer did "
              "not follow\n(add an entry point to the annotations), or this "
              "cartridge makes no sound.")
        return 1

    cov = coverage(an, cart)
    reached = sum(n for n, _t in cov.values())
    total = sum(t for _n, t in cov.values())
    frac = 100.0 * reached / max(total, 1)

    groups = cluster(writers)
    print("%d audio store%s in %d routine%s, from %.1f%% of the ROM traced\n"
          % (len(writers), "" if len(writers) == 1 else "s",
             len(groups), "" if len(groups) == 1 else "s", frac))

    blocks = []
    for g in groups:
        regs = sorted({w["reg"] for w in g["writers"]})
        chips = sorted({w["chip"] for w in g["writers"]})
        span = "%s:$%04X-$%04X" % (g["space"], g["lo"], g["hi"])
        print("%s  %d stores, %s: %s" % (span, len(g["writers"]),
                                         "/".join(chips), " ".join(regs)))
        tabs = tables_in(g)
        if not tabs:
            print("    writes constants only -- initialisation or silence\n")
            continue
        for t in tabs:
            idx = {"abx": ",X", "aby": ",Y", "zpx": ",X", "zpy": ",Y"}.get(t["mode"], "")
            print("    table $%04X%-2s %-16s -> %s"
                  % (t["addr"], idx, "(%s)" % t["how"], " ".join(sorted(t["regs"]))))
            print("        %s" % dump(cart, g["space"], t["addr"], args.bytes))
            blocks.append({"loc": "%s:%04X" % (g["space"], t["addr"]),
                           "len": args.bytes,
                           "note": "audio table -> %s" % " ".join(sorted(t["regs"]))})
        print()

    constant_only = sum(1 for g in groups if not tables_in(g))
    if frac < 25.0 and constant_only:
        print("Only %.1f%% of this ROM was traced, and %d of the %d routines "
              "write\nnothing but constants. That reads like \"no music\", but "
              "it is far more\nlikely the player was never reached: a static "
              "trace cannot follow an\nindirect jump or a handler installed "
              "through a RAM vector.\n"
              "\n  * `tracker.py capture` will still record it -- that watches "
              "the running\n    machine and does not care how the code is "
              "reached.\n"
              "  * To find it statically, give the disassembler a way in: add "
              "the address\n    to \"entries\" in an annotations file, or "
              "\"ram_vectors\" if the handler is\n    installed into RAM.\n"
              % (frac, constant_only, len(groups)))

    print("The addresses are solid; the format is not something a tool can "
          "guess.\nCheck any reading of it against `tracker.py capture`, which "
          "records what\nthe game actually plays.")

    if args.json:
        import json
        print("\n; paste into the annotations' \"blocks\":")
        print(json.dumps(blocks, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
