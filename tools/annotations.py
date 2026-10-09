#!/usr/bin/env python3
"""Check an annotations file before the disassembler quietly ignores half of it.

    python tools/annotations.py game-annotations.json
    python tools/annotations.py annotations.json --rom game.a78   # also checks it
                                                                  # against the image

disasm.py reads annotations with `d.get("labels", {})`: a key it does not know
is dropped without a word. A typo ("label" for "labels", "entry" for "entries")
therefore looks exactly like a file with nothing in it, and the first sign is a
disassembly that did not improve. This reads the file the way disasm.py does and
says what it would have ignored or choked on.

Without a ROM it checks the file alone:
  keys          anything but the known keys (and `_`-prefixed notes) is reported,
                with the nearest known key if there is one; repeated JSON keys
                (which keep only the last) are errors
  shapes        each key holds what disasm.py expects of it
  locations     `space:ADDR` parses; ram addresses parse
  labels        names are identifiers, none is given to two places, and none
                shadows a hardware register's name
  blocks        `end` after `loc`, `len` positive, `type` one the disassembler
                knows; a block that starts inside another is a warning (the
                disassembler never reaches its start, so its name and note
                vanish from the listing)

With --rom it also checks the file against the image:
  every location is in a space the cartridge has, inside it
  an entry point does not sit inside a data block you declared
  a label or comment does not point into the middle of an instruction

Exit status 1 if any error is reported; warnings are listed but do not fail
(--strict makes them). The result is advice about the file, not about the game:
a file that lints clean can still be wrong.
"""
import argparse
import difflib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

KNOWN = ("entries", "labels", "comments", "headers", "ram", "banksw", "bankat",
         "blocks", "ram_vectors", "notes")
BLOCK_KEYS = ("loc", "end", "len", "type", "name", "note", "gfx", "wide")
BLOCK_TYPES = ("bytes", "words", "text")
SCHEMA = 1
SPACE = re.compile(r"^(?:rom|[bf]\d+)$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Report(object):
    def __init__(self, name):
        self.name, self.items = name, []

    def error(self, msg):
        self.items.append(("error", msg))

    def warn(self, msg):
        self.items.append(("warning", msg))

    @property
    def errors(self):
        return [m for k, m in self.items if k == "error"]

    @property
    def warnings(self):
        return [m for k, m in self.items if k == "warning"]


def read_json_keep(path):
    """(document, newline) -- the file's own line ending, so that rewriting it does not
    turn a CRLF file into an LF one (or the reverse)."""
    with open(path, "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8-sig")), ("\r\n" if b"\r\n" in raw else "\n")


def write_json_keep(path, doc, newline="\n", indent=2):
    """Write `doc` as JSON in `newline` style, keeping non-ASCII text as it is."""
    text = json.dumps(doc, indent=indent, ensure_ascii=False)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", newline) + newline)


def parse_loc(s):
    """('f7', 0xC000) for 'f7:C000', 'f7:$C000' or 'f7:0xC000'; raises ValueError."""
    if not isinstance(s, str) or s.count(":") != 1:
        raise ValueError("%r is not `space:address`" % (s,))
    space, addr = s.split(":")
    addr = addr.strip()
    addr = addr[1:] if addr.startswith("$") else (
        addr[2:] if addr.lower().startswith("0x") else addr)
    if not SPACE.match(space):
        raise ValueError("%r: the space must be `rom`, `fN` or `bN`" % s)
    try:
        return space, int(addr, 16)
    except ValueError:
        raise ValueError("%r: the address is not hexadecimal" % s)


def _no_dupes(pairs):
    keys = [k for k, _v in pairs]
    dup = sorted(set(k for k in keys if keys.count(k) > 1))
    if dup:
        raise ValueError("repeated key%s %s (JSON keeps only the last)"
                         % ("s" if len(dup) > 1 else "", ", ".join(map(repr, dup))))
    return dict(pairs)


def hardware_names():
    import a7800
    names = set()
    for table in (a7800.TIA, a7800.MARIA, a7800.RIOT):
        names |= set(table.values())
    for table in (a7800.POKEY_WRITE, a7800.POKEY_READ):
        names |= set(table.values())
    return names


def lint(text, name="annotations.json"):
    """Check annotations text; return (Report, parsed document or None)."""
    r = Report(name)
    try:
        d = json.loads(text, object_pairs_hook=_no_dupes)
    except ValueError as e:
        r.error("not valid JSON: %s" % e)
        return r, None
    if not isinstance(d, dict):
        r.error("the top level must be an object, not %s" % type(d).__name__)
        return r, None

    for k in d:
        if k.startswith("_"):
            if k == "_schema" and d[k] != SCHEMA:
                r.warn("_schema is %r; this tool knows version %d" % (d[k], SCHEMA))
            continue
        if k not in KNOWN:
            near = difflib.get_close_matches(k, KNOWN, 1, 0.6)
            r.error("unknown key %r -- disasm.py ignores it%s" % (
                k, "; did you mean %r?" % near[0] if near else ""))

    def mapping(key, kind=str):
        v = d.get(key, {})
        if not isinstance(v, dict):
            r.error("%r must be an object mapping location to %s" % (key, kind.__name__))
            return {}
        return v

    locs = []                                     # (where, space, addr)

    def loc(where, s):
        try:
            sp, a = parse_loc(s)
        except ValueError as e:
            r.error("%s: %s" % (where, e))
            return None
        locs.append((where, sp, a))
        return sp, a

    ent = d.get("entries", [])
    if not isinstance(ent, list):
        r.error("'entries' must be a list of locations")
        ent = []
    entry_locs = []
    for i, s in enumerate(ent):
        p = loc("entries[%d]" % i, s)
        if p:
            entry_locs.append((p, s))
    seen_e = {}
    for p, s in entry_locs:
        if p in seen_e:
            r.warn("entry %s is listed twice" % s)
        seen_e[p] = True

    labels = mapping("labels")
    byname = {}
    try:
        hw = hardware_names()
    except Exception:                                        # noqa: BLE001
        hw = set()
    for k, v in labels.items():
        p = loc("labels[%r]" % k, k)
        if not isinstance(v, str) or not IDENT.match(v):
            r.error("labels[%r]: %r is not a usable name (letters, digits and "
                    "underscores, not starting with a digit)" % (k, v))
            continue
        if v in hw:
            r.error("labels[%r]: %r is a hardware register's name; the "
                    "assembler would see two definitions" % (k, v))
        if p:
            other = byname.get(v)
            if other and other != p:
                r.error("label %r is given to both %s:%04X and %s:%04X; a name "
                        "may mean only one place" % (v, other[0], other[1], p[0], p[1]))
            byname.setdefault(v, p)

    for key in ("comments", "headers"):
        for k, v in mapping(key).items():
            loc("%s[%r]" % (key, k), k)
            if not isinstance(v, str):
                r.error("%s[%r] must be text" % (key, k))

    for k, v in mapping("ram").items():
        try:
            int(k, 16)
        except (ValueError, TypeError):
            r.error("ram[%r]: the address is not hexadecimal" % (k,))
        if not isinstance(v, str) or not IDENT.match(v):
            r.error("ram[%r]: %r is not a usable name" % (k, v))

    for k, v in mapping("banksw", object).items():
        loc("banksw[%r]" % k, k)
        ok = (v == "keep" or (isinstance(v, int) and not isinstance(v, bool))
              or (isinstance(v, list) and v and all(
                  isinstance(x, int) and not isinstance(x, bool) for x in v)))
        if not ok:
            r.error("banksw[%r]: %r is not a bank number, a list of them, or "
                    "\"keep\"" % (k, v))
    for k, v in mapping("bankat", object).items():
        loc("bankat[%r]" % k, k)
        if not isinstance(v, int) or isinstance(v, bool):
            r.error("bankat[%r]: %r is not a bank number" % (k, v))

    rv = d.get("ram_vectors", [])
    if not isinstance(rv, list):
        r.error("'ram_vectors' must be a list of [low, high] address pairs")
        rv = []
    for i, pair in enumerate(rv):
        try:
            assert isinstance(pair, list) and len(pair) == 2
            [int(str(x), 0) for x in pair]
        except (AssertionError, ValueError):
            r.error("ram_vectors[%d]: %r is not a pair of addresses" % (i, pair))

    blocks = d.get("blocks", [])
    if not isinstance(blocks, list):
        r.error("'blocks' must be a list")
        blocks = []
    spans = {}
    for i, b in enumerate(blocks):
        where = "blocks[%d]" % i
        if not isinstance(b, dict) or "loc" not in b:
            r.error("%s needs a `loc`" % where)
            continue
        for k in b:
            if k not in BLOCK_KEYS:
                near = difflib.get_close_matches(k, BLOCK_KEYS, 1, 0.6)
                r.warn("%s (%s): unknown key %r%s" % (where, b["loc"], k,
                       "; did you mean %r?" % near[0] if near else ""))
        p = loc(where, b["loc"])
        if "end" in b and "len" in b:
            r.warn("%s (%s) has both `end` and `len`; disasm.py uses `end`" % (where, b["loc"]))
        if not p:
            continue
        sp, a = p
        if "end" in b:
            q = loc(where + ".end", b["end"])
            if not q:
                continue
            if q[0] != sp:
                r.error("%s (%s): `end` is in another space" % (where, b["loc"]))
                continue
            end = q[1]
        else:
            n = b.get("len", 1)
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                r.error("%s (%s): `len` must be a positive number" % (where, b["loc"]))
                continue
            end = a + n
        if end <= a:
            r.error("%s (%s): ends at $%04X, at or before it starts" % (where, b["loc"], end))
            continue
        if "type" in b and b["type"] not in BLOCK_TYPES:
            r.warn("%s (%s): type %r is not one of %s; it is listed as bytes"
                   % (where, b["loc"], b["type"], ", ".join(BLOCK_TYPES)))
        spans.setdefault(sp, []).append((a, end, b["loc"]))
    for sp, items in spans.items():
        items.sort()
        for (a1, e1, l1), (a2, e2, l2) in zip(items, items[1:]):
            if a2 < e1:
                # not an error: the file still disassembles and round-trips. But
                # the disassembler emits the first block through its end and
                # never reaches the second's start, so the second's name and
                # note are silently absent from the listing.
                r.warn("block %s starts inside block %s (which runs to $%04X in "
                       "%s): its name and note will not appear in the listing"
                       % (l2, l1, e1, sp))
    d["_spans"] = spans
    d["_locs"] = locs
    d["_entry_locs"] = [p for p, _s in entry_locs]
    return r, d


def check_rom(r, d, rom):
    """Checks that need the cartridge: spaces, ranges, entries in data, labels
    that point into instructions."""
    import cart as cartlib
    try:
        c = cartlib.Cart(rom)
    except Exception as e:                                   # noqa: BLE001
        r.error("cannot read %s: %s" % (rom, e))
        return
    spaces = {sp: (c.base_of(sp), c.base_of(sp) + c.size_of(sp)) for sp in c.spaces()}
    for where, sp, a in d["_locs"]:
        if sp not in spaces:
            r.error("%s: this cartridge has no space %r (it has %s)"
                    % (where, sp, ", ".join(sorted(spaces))))
        elif not spaces[sp][0] <= a < spaces[sp][1]:
            r.error("%s: $%04X is outside %s ($%04X-$%04X)"
                    % (where, a, sp, spaces[sp][0], spaces[sp][1] - 1))
    for sp, a in d["_entry_locs"]:
        for a1, e1, l1 in d["_spans"].get(sp, ()):
            if a1 <= a < e1:
                r.error("entry %s:%04X is inside the data block %s; a block wins "
                        "over an entry, so the code there is never traced"
                        % (sp, a, l1))
    # label / comment inside an instruction: needs a disassembly
    out = tempfile.mkdtemp(prefix="annlint-")
    cfg = os.path.join(out, "a.json")
    clean = {k: v for k, v in d.items() if not k.startswith("_")}
    with io.open(cfg, "w", encoding="utf-8") as f:
        json.dump(clean, f)
    p = subprocess.run([sys.executable, os.path.join(HERE, "disasm.py"), rom, "-c",
                        cfg, "-o", out], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        r.error("disasm.py fails on this file: %s" % p.stdout.decode("utf-8", "replace").strip().splitlines()[-1:])
        return
    inside = {}
    for name in os.listdir(out):
        if not name.endswith(".asm"):
            continue
        sp = name[:-4]
        for ln in io.open(os.path.join(out, name), encoding="utf-8"):
            m = re.search(r";\s+([0-9A-F]{4}): ((?:[0-9A-F]{2} ?)+)", ln)
            if m and not ln.lstrip().startswith(".byte"):
                start = int(m.group(1), 16)
                n = len(m.group(2).split())
                for k in range(1, n):
                    inside[(sp, start + k)] = start
    for section in ("labels", "comments", "headers"):
        for k in d.get(section, {}):
            try:
                sp, a = parse_loc(k)
            except ValueError:
                continue
            if (sp, a) in inside:
                r.warn("%s[%r] points into the middle of the instruction at "
                       "%s:%04X" % (section, k, sp, inside[(sp, a)]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.strip().split("\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="+")
    ap.add_argument("--rom", help="also check against this cartridge")
    ap.add_argument("--strict", action="store_true", help="warnings fail too")
    ap.add_argument("-q", "--quiet", action="store_true", help="report problems only")
    args = ap.parse_args(argv)
    bad = 0
    for path in args.file:
        if not os.path.isfile(path):
            print("annotations: no such file: %s" % path)
            return 2
        r, d = lint(io.open(path, encoding="utf-8").read(), path)
        if d is not None and args.rom:
            check_rom(r, d, args.rom)
        for kind, msg in r.items:
            print("%s: %s: %s" % (path, kind, msg))
        if r.errors or (args.strict and r.warnings):
            bad += 1
        elif not args.quiet:
            print("%s: ok%s" % (path, " (%d warning%s)" % (
                len(r.warnings), "" if len(r.warnings) == 1 else "s") if r.warnings else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
