"""One way to read an address typed on a command line.

    $C000   0xC000   C000   0xc000        all mean 49152

An address on this machine is hexadecimal, so a bare number is hex too. That is a
change for the tools that used `int(x, 0)`, which read `8000` as decimal 8000 --
a wrong address with no error -- and refused `$8000`; and for the tools that used
`int(x, 16)`, which refused `0x8000`. Bank numbers, counts, frames and lines are
not addresses and stay decimal.

    from addr import address                     # as an argparse `type=`
    ap.add_argument("--base", type=address)
    parse_addr("$D000")                          # 53248, for a string already in hand

A tool that has to run as a single copied file carries a three-line inline
version of the same rule rather than importing this.
"""
import argparse


def parse_addr(text):
    """The number an address string means; ValueError if it is not one."""
    s = str(text).strip()
    if s.startswith("$"):
        s = s[1:]
    elif s[:2].lower() == "0x":
        s = s[2:]
    return int(s, 16)


def address(text):
    """argparse type: an address, with a message that says what was expected."""
    try:
        return parse_addr(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "%r is not an address: write $C000, 0xC000 or C000 (hexadecimal)" % text)
