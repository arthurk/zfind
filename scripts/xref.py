#!/usr/bin/env python3
"""Find where assembly symbols are defined and used, without reading the file.

    xref.py SYMBOL... [FILE]       references: line number, function, source line
    xref.py -c SYMBOL... [FILE]    only the functions that use it, with counts
    xref.py -d SYMBOL... [FILE]    the definition; for a function, its line range
    xref.py -C N SYMBOL...         show N lines of context around each hit

Comments are ignored, so a symbol mentioned only in prose never shows up (use
grep -n for that). SYMBOL can be a function, a data label, a %define constant,
a local label (.opt_limit_next), a local label qualified by its function the way
nm and objdump print it (walk_dir.mkpath), or a struc field (TSTATE.tab_ptr).
Output is capped (see --max and MAX_CONTEXT_LINES); the last line says what was
left out and how to get it.

Self-test:  python3 -m doctest scripts/xref.py
"""
import argparse
import os
import re
import signal
import sys
from collections import Counter

DEFAULT_FILE = "src/x86_64/zfind.asm"
DEFAULT_MAX = 25  # references listed per symbol
MAX_CONTEXT = 15  # lines of context on each side of a hit, at most
MAX_CONTEXT_LINES = 120  # lines printed per symbol in context mode

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


def merge_windows(hit_numbers, context, total):
    """Line ranges of `context` lines around each hit, overlapping ones merged.

    >>> merge_windows([10, 12, 40], 2, 100)
    [(8, 14), (38, 42)]
    >>> merge_windows([1, 99], 3, 100)
    [(1, 4), (96, 100)]
    >>> merge_windows([5, 9], 1, 100)  # 4-6 and 8-10 do not touch, so no merge
    [(4, 6), (8, 10)]
    >>> merge_windows([5, 8], 1, 100)  # 4-6 and 7-9 are adjacent
    [(4, 9)]
    """
    windows = []
    for n in sorted(hit_numbers):
        start, end = max(1, n - context), min(total, n + context)
        if windows and start <= windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    return windows


def print_rows(rows, limit):
    """One `line<TAB>function<TAB>source` row per hit, cut off at `limit`."""
    for number, function, text in rows[:limit]:
        print(f"{number}\t{function}\t{text}")
    if limit is not None and len(rows) > limit:
        print(f"... {len(rows) - limit} more; use -c for the functions, --all for every line")


def print_windows(lines, rows, context, line_cap):
    """Print grep -n -C style windows around the hits: `N:` a hit, `N-` context.

    Each window starts with `-- FUNCTION`. Stops before a window that would push
    the output past `line_cap` lines (the first window is always printed).
    Returns how many hits were shown.
    """
    function_at = {number: function for number, function, _ in rows}
    hits = set(function_at)
    printed = shown_hits = 0
    for start, end in merge_windows(hits, context, len(lines)):
        if printed and line_cap is not None and printed + (end - start + 2) > line_cap:
            break
        first_hit = min(n for n in hits if start <= n <= end)
        print(f"-- {function_at[first_hit]}")
        for n in range(start, end + 1):
            print(f"{n}{':' if n in hits else '-'}{lines[n - 1]}")
        printed += end - start + 2
        shown_hits += sum(1 for n in hits if start <= n <= end)
    return shown_hits


def report_missing(args, message):
    """With several symbols the message must sit under its `== SYMBOL ==` header."""
    print(message, file=sys.stdout if len(args.symbols) > 1 else sys.stderr)


def show_definition(lines, symbol, args):
    extent = function_extent(lines, symbol)
    if extent:
        first, label, last = extent
        print(f"{symbol}: lines {first}-{last} ({last - first + 1} lines); label at {label}")
        return True
    rows = list(definitions(lines, symbol))
    if not rows:
        report_missing(args, f"no definition of {symbol!r} in {args.file}")
        return False
    show_hits(lines, rows, args)
    return True


def show_hits(lines, rows, args):
    limit = None if args.all else args.max
    if args.context:
        line_cap = None if args.all else MAX_CONTEXT_LINES
        shown = print_windows(lines, rows[:limit], min(args.context, MAX_CONTEXT), line_cap)
        if shown < len(rows):
            print(f"... {len(rows) - shown} more hits not shown; use -c for the functions, "
                  "--all, or a smaller -C")
    else:
        print_rows(rows, limit)


def show_symbol(lines, symbol, args):
    """Print the answer for one symbol; False when there was nothing to show."""
    if args.definition:
        return show_definition(lines, symbol, args)

    rows = list(references(lines, symbol))
    if not rows:
        report_missing(args, f"no code references to {symbol!r} in {args.file}")
        return False
    if args.callers:
        # Counter keeps first-seen order, so this reads top-to-bottom through the file.
        for function, count in Counter(fn for _, fn, _ in rows).items():
            print(f"{function} ({count})")
    else:
        show_hits(lines, rows, args)
    return True


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("symbols", nargs="+", metavar="SYMBOL", help="then optionally FILE")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-c", "--callers", action="store_true", help="only list functions, with counts")
    mode.add_argument(
        "-d", "--definition", action="store_true",
        help="where each SYMBOL is defined; for a function, its line range",
    )
    parser.add_argument(
        "-C", "--context", type=int, default=0, metavar="N",
        help=f"print N lines around each hit (at most {MAX_CONTEXT})",
    )
    parser.add_argument(
        "--max", type=int, default=DEFAULT_MAX, metavar="N",
        help=f"cut the reference list after N hits per symbol (default {DEFAULT_MAX})",
    )
    parser.add_argument("--all", action="store_true", help="do not cut the output")
    args = parser.parse_intermixed_args()  # options may sit between the symbols and the file

    # The last argument is the file when it names one; otherwise every argument is a symbol.
    args.file = DEFAULT_FILE
    if len(args.symbols) > 1 and os.path.isfile(args.symbols[-1]):
        args.file = args.symbols.pop()
    elif os.path.isfile(args.symbols[0]) and len(args.symbols) == 1:
        parser.error(f"{args.symbols[0]} is a file; give a symbol to look up in it")
    if args.callers and args.context:
        parser.error("-c and -C cannot be combined")
    return args


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `| head` must not raise
    args = parse_args()
    lines = read_lines(args.file)

    found_any = False
    for symbol in args.symbols:
        if len(args.symbols) > 1:
            print(f"== {symbol} ==")
        found_any |= show_symbol(lines, symbol, args)
    return 0 if found_any else 1


if __name__ == "__main__":
    sys.exit(main())
