#!/usr/bin/env python3
"""Find where an assembly symbol is defined and used, without reading the file.

    xref.py SYMBOL [FILE]       references: line number, function, source line
    xref.py -c SYMBOL [FILE]    only the functions that use it, with counts
    xref.py -d SYMBOL [FILE]    the definition; for a function, its line range

Comments are ignored, so a symbol mentioned only in prose never shows up (use
grep -n for that). SYMBOL can be a function, a data label, a %define constant,
a local label (.opt_limit_next), a local label qualified by its function the way
nm and objdump print it (walk_dir.mkpath), or a struc field (TSTATE.tab_ptr).
A long reference list is cut after --max lines: use -c, or --all.

Self-test:  python3 -m doctest scripts/xref.py
"""
import argparse
import re
import sys
from collections import Counter

DEFAULT_FILE = "src/x86_64/zfind.asm"
DEFAULT_MAX = 25

# Lines that change "where we are" in the file.
SECTION = re.compile(r"^\s*section\s+(\S+)")  # section .text
STRUC = re.compile(r"^\s*struc\s+(\w+)")  # struc TSTATE
ENDSTRUC = re.compile(r"^\s*endstruc\b")
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


def patterns(symbol):
    r"""Return (pattern, scope, short): how to recognise SYMBOL in a line of code.

    `pattern` matches the symbol as written. A local label qualified by its
    function (walk_dir.mkpath) is written as plain .mkpath inside that function,
    so `short` matches that form, but only while the current function is `scope`.

    >>> pattern, scope, short = patterns("walk_dir.mkpath")
    >>> scope
    'walk_dir'
    >>> bool(short.search("    jmp .mkpath"))
    True
    >>> bool(short.search("    call walk_dir.mkpath"))
    False
    >>> patterns("walk_dir")[1:]
    (None, None)
    >>> patterns(".again")[1:]
    (None, None)
    """
    scope = short = None
    if not symbol.startswith(".") and "." in symbol:
        scope, label = symbol.split(".", 1)
        short = symbol_pattern("." + label)
    return symbol_pattern(symbol), scope, short


def read_lines(path):
    """The file as a list of lines; line N is element N-1."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().split("\n")


def references(lines, symbol):
    """Yield (line_number, enclosing_function, source_line) for each code reference.

    >>> src = [
    ...     "section .text",
    ...     "    global f:function (f.end_func - f)",
    ...     "f:",
    ...     "    call g        ; mentions g again",
    ...     ".again:",
    ...     "    jnz .again",
    ...     "f.end_func:",
    ... ]
    >>> list(references(src, "g"))
    [(4, 'f', '    call g        ; mentions g again')]
    >>> [number for number, _, _ in references(src, "f.again")]
    [5, 6]
    """
    pattern, scope, short = patterns(symbol)
    section = current = "(start)"  # name of the function / section we are inside

    for number, line in enumerate(lines, start=1):
        if match := SECTION.match(line):
            section = current = f"({match[1]})"
        elif match := STRUC.match(line):
            current = match[1]
        elif ENDSTRUC.match(line):
            current = section
        elif match := GLOBAL.match(line):
            current = match[1]  # the header sits above the label itself
            continue  # a declaration, not a reference
        elif match := LABEL.match(line):
            current = match[1]

        code = strip_comment(line)
        if pattern.search(code) or (short and current == scope and short.search(code)):
            yield number, current, line


def is_definition(line, symbol):
    r"""True if this line defines SYMBOL: a label, a data label, a constant or a macro.

    >>> is_definition("walk_dir:", "walk_dir")
    True
    >>> is_definition("    git_repo:   resq 1   ; flag", "git_repo")
    True
    >>> is_definition("    gitname     db '.git', 0", "gitname")
    True
    >>> is_definition("%define PATH_MAX    4096", "PATH_MAX")
    True
    >>> is_definition(".again:", ".again")
    True
    >>> is_definition("    call walk_dir", "walk_dir")
    False
    """
    name = re.escape(symbol)
    code = strip_comment(line)
    return bool(
        re.match(rf"\s*{name}:", code)
        or re.match(rf"\s*(?:%define|%xdefine|%assign|%macro|struc)\s+{name}\b", code)
        or re.match(rf"\s*{name}\s+(?:equ|times|d[bwdqot]|res[bwdqot])\b", code)
    )


def definitions(lines, symbol):
    """Yield the references that are definitions of SYMBOL."""
    scope = patterns(symbol)[1]
    short_name = "." + symbol.split(".", 1)[1] if scope else None
    for number, function, line in references(lines, symbol):
        if is_definition(line, symbol) or (
            short_name and function == scope and is_definition(line, short_name)
        ):
            yield number, function, line


def function_extent(lines, name):
    """Return (first, label, last) line numbers of function NAME, or None.

    `first` includes the `global` line and the comment block above the label,
    where the register contract lives. `last` is the NAME.end_func label.

    >>> src = ["; f: does a thing", "    global f:function (f.end_func - f)",
    ...        "f:", "    ret", "f.end_func:", "", "g:"]
    >>> function_extent(src, "f")
    (1, 3, 5)
    >>> function_extent(src, "g") is None
    True
    """
    label = next((i for i, l in enumerate(lines) if l.startswith(name + ":")), None)
    last = next((i for i, l in enumerate(lines) if l.startswith(name + ".end_func:")), None)
    if label is None or last is None or last < label:
        return None

    first = above = label
    while above > 0 and not lines[above - 1].strip():
        above -= 1  # a blank line may separate `global` from the label
    if above > 0 and GLOBAL.match(lines[above - 1]):
        first = above - 1
        while first > 0 and lines[first - 1].lstrip().startswith(";"):
            first -= 1
    return first + 1, label + 1, last + 1


def show_definition(lines, symbol, path):
    extent = function_extent(lines, symbol)
    if extent:
        first, label, last = extent
        print(f"{symbol}: lines {first}-{last} ({last - first + 1} lines); label at {label}")
        return 0
    found = list(definitions(lines, symbol))
    if not found:
        print(f"no definition of {symbol!r} in {path}", file=sys.stderr)
        return 1
    for number, function, text in found:
        print(f"{number}\t{function}\t{text}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("symbol")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-c", "--callers", action="store_true", help="only list functions, with counts"
    )
    mode.add_argument(
        "-d", "--definition", action="store_true",
        help="where SYMBOL is defined; for a function, its line range",
    )
    parser.add_argument(
        "--max", type=int, default=DEFAULT_MAX, metavar="N",
        help=f"cut the reference list after N lines (default {DEFAULT_MAX})",
    )
    parser.add_argument("--all", action="store_true", help="do not cut the list")
    args = parser.parse_args()

    lines = read_lines(args.file)
    if args.definition:
        return show_definition(lines, args.symbol, args.file)

    hits = list(references(lines, args.symbol))
    if not hits:
        print(f"no code references to {args.symbol!r} in {args.file}", file=sys.stderr)
        return 1

    if args.callers:
        # Counter keeps first-seen order, so this reads top-to-bottom through the file.
        for function, count in Counter(fn for _, fn, _ in hits).items():
            print(f"{function} ({count})")
        return 0

    limit = None if args.all else args.max
    for number, function, text in hits[:limit]:
        print(f"{number}\t{function}\t{text}")
    if limit is not None and len(hits) > limit:
        print(f"... {len(hits) - limit} more; use -c for the functions, --all for every line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
