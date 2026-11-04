#!/usr/bin/env python3
"""List the code references to an assembly symbol, with the enclosing function.

    xref.py SYMBOL [FILE]       every reference: line number, function, source line
    xref.py -c SYMBOL [FILE]    only the functions that use it, with counts

Comments are ignored, so a symbol mentioned only in prose never shows up.
Local labels work too: `xref.py walk_dir.mkpath`.

Self-test:  python3 -m doctest scripts/xref.py
"""
import argparse
import re
import sys
from collections import Counter

DEFAULT_FILE = "src/x86_64/zfind.asm"

# Lines that change "where we are" in the file.
SECTION = re.compile(r"^\s*section\s+(\S+)")  # section .text
GLOBAL = re.compile(r"^\s*global\s+(\w+)")  # global name:function (...)
LABEL = re.compile(r"^(\w+):")  # name:   (local labels start with ".", so no match)


def strip_comment(line):
    r"""Remove a trailing ; comment, but not a ; inside a string literal.

    >>> strip_comment("    mov eax, 1   ; set up")
    '    mov eax, 1   '
    >>> strip_comment('    db "a;b", 10   ; real comment')
    '    db "a;b", 10   '
    >>> strip_comment("; whole-line comment")
    ''
    """
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == ";":
            return line[:i]
    return line


def symbol_pattern(symbol):
    r"""Regex for the exact symbol: not part of a longer name, not `symbol.something`.

    >>> bool(symbol_pattern("walk_dir").search("    call walk_dir"))
    True
    >>> bool(symbol_pattern("walk_dir").search("    call walk_dir.mkpath"))
    False
    >>> bool(symbol_pattern("walk_dir").search("    call walk_dir2"))
    False
    >>> bool(symbol_pattern("walk_dir.mkpath").search("    call walk_dir.mkpath"))
    True
    """
    return re.compile(rf"(?<![\w.]){re.escape(symbol)}(?![\w.])")


def references(path, symbol):
    """Yield (line_number, enclosing_function, source_line) for each code reference."""
    pattern = symbol_pattern(symbol)
    current = "(start)"  # name of the function / section we are inside

    with open(path, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f, start=1):
            line = line.rstrip("\n")

            if match := SECTION.match(line):
                current = f"({match[1]})"
            elif match := GLOBAL.match(line):
                current = match[1]  # the header sits above the label itself
                continue  # a declaration, not a reference
            elif match := LABEL.match(line):
                current = match[1]

            if pattern.search(strip_comment(line)):
                yield number, current, line


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("symbol")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE)
    parser.add_argument(
        "-c", "--callers", action="store_true", help="only list functions, with counts"
    )
    args = parser.parse_args()

    hits = list(references(args.file, args.symbol))
    if not hits:
        print(f"no code references to {args.symbol!r} in {args.file}", file=sys.stderr)
        return 1

    if args.callers:
        # Counter keeps first-seen order, so this reads top-to-bottom through the file.
        for function, count in Counter(fn for _, fn, _ in hits).items():
            print(f"{function} ({count})")
    else:
        for number, function, text in hits:
            print(f"{number}\t{function}\t{text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
