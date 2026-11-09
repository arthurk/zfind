#!/usr/bin/env python3
"""Static loop report for the current zfind binary from MAQAO CQA.

    cqa.py --binary BUILD/ZFIND

Runs maqao cqa over the binary, keeps the worst dispatch estimate per loop,
and writes build/bench/cqa/<describe>.md plus the top 20 loops on stdout.

CQA is a static estimate, not an execution profile: it predicts how a loop
maps onto the target CPU, not how often it runs. The numbers also depend on
the CPU model MAQAO detects and on NASM giving no source lines (NA).

maqao is a system-wide tool, not part of the devshell. Development only.

Self-test:  python3 -m doctest scripts/cqa.py
"""
import argparse
import csv
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

CQA_DIR = "build/bench/cqa"
TOP = 20

# Column names in MAQAO's CSV export; never select them by index.
FUNCTION = "Function name"
LOOP = "ID"
ADDRESS = "Address in the binary"
DISPATCH = "Dispatch (cycles)"
INSTR = "Nb instr."
PATHS = "Nb paths"

NOTES = (
    "CQA is a static estimate from MAQAO, not an execution profile: it "
    "predicts how a loop maps onto the target CPU, not how often it runs. "
    "NASM emits no source lines, so the source-line column is NA. The target "
    "CPU model changes the numbers."
)


def parse_rows(lines):
    """Rows of a MAQAO CQA CSV, keyed by column name.

    >>> parse_rows(["Function name;ID;Nb instr.\\n", "f;1;7\\n"])
    [{'Function name': 'f', 'ID': '1', 'Nb instr.': '7'}]
    """
    return list(csv.DictReader(lines, delimiter=";"))


def parse_dispatch(cell):
    """Dispatch cycles from a CSV cell, or None if it is empty or NA.

    >>> parse_dispatch("17.00")
    17.0
    >>> parse_dispatch("")
    >>> parse_dispatch("NA")
    """
    cell = cell.strip()
    return None if cell in ("", "NA") else float(cell)


def best_loops(rows):
    """Keep the highest-dispatch row per (function, ID).

    A tie keeps the first row in CSV order.

    >>> rows = [
    ...     {"Function name": "f", "ID": "1", "Dispatch (cycles)": "10", "Nb instr.": "5"},
    ...     {"Function name": "f", "ID": "1", "Dispatch (cycles)": "12", "Nb instr.": "7"},
    ...     {"Function name": "f", "ID": "1", "Dispatch (cycles)": "12", "Nb instr.": "9"},
    ... ]
    >>> best_loops(rows)[0]["Nb instr."]
    '7'
    """
    best = {}
    for row in rows:
        key = (row.get(FUNCTION, ""), row.get(LOOP, ""))
        value = parse_dispatch(row.get(DISPATCH, ""))
        old, _ = best.get(key, (None, None))
        if key not in best or (value is not None and (old is None or value > old)):
            best[key] = (value, row)
    return [row for _, row in best.values()]


def loop_id(cell):
    """A loop ID as a sort key: numbers numerically, anything else after.

    >>> sorted(["10", "2", "x"], key=loop_id)
    ['2', '10', 'x']
    """
    return (0, int(cell)) if cell.isdigit() else (1, cell)


def sort_loops(rows):
    """Sort by dispatch descending, unvalued loops last, then function and ID.

    >>> rows = [
    ...     {"Function name": "b", "ID": "2", "Dispatch (cycles)": "1"},
    ...     {"Function name": "a", "ID": "10", "Dispatch (cycles)": "2"},
    ...     {"Function name": "a", "ID": "2", "Dispatch (cycles)": "2"},
    ...     {"Function name": "c", "ID": "1", "Dispatch (cycles)": "NA"},
    ... ]
    >>> [(r["Function name"], r["ID"]) for r in sort_loops(rows)]
    [('a', '2'), ('a', '10'), ('b', '2'), ('c', '1')]
    """
    def key(row):
        value = parse_dispatch(row.get(DISPATCH, ""))
        return (
            value is None,
            -(value or 0.0),
            row.get(FUNCTION, ""),
            loop_id(row.get(LOOP, "")),
        )

    return sorted(rows, key=key)


def address(cell):
    """An address from the binary as 0x-prefixed hex, or 'n/a'.

    >>> address("401b77")
    '0x401b77'
    >>> address("0x401b77")
    '0x401b77'
    >>> address("")
    'n/a'
    """
    cell = cell.strip()
    if not cell:
        return "n/a"
    return cell if cell.startswith("0x") else "0x" + cell


def dispatch_text(cell):
    """Dispatch cycles formatted for the report, or 'n/a'.

    >>> dispatch_text("17.00")
    '17.00'
    >>> dispatch_text("NA")
    'n/a'
    """
    value = parse_dispatch(cell)
    return "n/a" if value is None else f"{value:.2f}"


def target_processor(text):
    """The 'Target processor is:' value from MAQAO output, or 'n/a'.

    >>> target_processor("Target processor is: Zen 4 (x86_64 architecture).\\n")
    'Zen 4 (x86_64 architecture).'
    >>> target_processor("[MAQAO] Info: no loops\\n")
    'n/a'
    """
    for line in text.splitlines():
        if "Target processor is:" in line:
            return line.split("Target processor is:", 1)[1].strip()
    return "n/a"


def table(rows):
    """Pad ROWS into columns two spaces apart.

    >>> print(table([["function", "dispatch"], ["walk_dir", "16.00"]]))
    function  dispatch
    walk_dir  16.00
    """
    widths = [
        max(len(row[i]) for row in rows if i < len(row))
        for i in range(max(map(len, rows)))
    ]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    )


def report_table(rows):
    """The report columns for ROWS.

    >>> report_table([{
    ...     "Function name": "walk_dir", "ID": "90",
    ...     "Address in the binary": "4048ec", "Dispatch (cycles)": "16.00",
    ...     "Nb instr.": "84", "Nb paths": "455",
    ... }])
    [['function', 'loop', 'address', 'dispatch', 'instr', 'paths'], ['walk_dir', '90', '0x4048ec', '16.00', '84', '455']]
    """
    columns = [["function", "loop", "address", "dispatch", "instr", "paths"]]
    for row in rows:
        columns.append([
            row.get(FUNCTION, "n/a"),
            row.get(LOOP, "n/a"),
            address(row.get(ADDRESS, "")),
            dispatch_text(row.get(DISPATCH, "")),
            row.get(INSTR, "n/a") or "n/a",
            row.get(PATHS, "n/a") or "n/a",
        ])
    return columns


def commit_from(describe):
    """The commit part of `git describe --dirty` output.

    >>> commit_from("v0.0.3-3-gabc-dirty")
    'v0.0.3-3-gabc'
    """
    return describe.removesuffix("-dirty")


def report_stem(describe):
    """Report filename stem: `git describe` output without its slashes.

    >>> report_stem("feature/x-3-gabc")
    'feature_x-3-gabc'
    """
    return describe.replace("/", "_")


def display_path(path, start="."):
    """PATH relative to START when it stays inside it, else absolute.

    >>> display_path("/repo/build/bench/cqa/x.md", "/repo")
    'build/bench/cqa/x.md'
    >>> display_path("/elsewhere/x.md", "/repo")
    '/elsewhere/x.md'
    """
    relative = os.path.relpath(path, start)
    return path if relative.startswith("..") else relative


def cqa_csv_command(maqao, binary, csv_path):
    """The maqao argv for the CSV export.

    >>> cqa_csv_command("maqao", "/b/zfind", "/b/cqa/v0.0.3.csv")
    ['maqao', 'cqa', '/b/zfind', 'fct-loops=.*', '-of=csv', '-op=/b/cqa/v0.0.3.csv']
    """
    return [maqao, "cqa", binary, "fct-loops=.*", "-of=csv", f"-op={csv_path}"]


def cqa_text_command(maqao, binary):
    """The maqao argv for the cheap pass that prints the target CPU.

    >>> cqa_text_command("maqao", "/b/zfind")
    ['maqao', 'cqa', '/b/zfind', 'fct-loops=^$']
    """
    return [maqao, "cqa", binary, "fct-loops=^$"]


def error(message):
    """Print MESSAGE to stderr and exit 1."""
    print(message, file=sys.stderr)
    sys.exit(1)


def git_describe():
    """`git describe --tags --always --dirty` output, or exit 1."""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        error(f"cqa: git describe failed: {exc}")
    if proc.returncode != 0:
        error(f"cqa: git describe failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run(command):
    """Run COMMAND and return (returncode, stdout, stderr)."""
    proc = subprocess.run(command, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def sha256(path):
    """SHA-256 of the file at PATH."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--binary", required=True, metavar="PATH",
        help="zfind binary to analyse (the Makefile passes $(BIN))",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    binary = os.path.abspath(args.binary)
    if not os.path.isfile(binary):
        error(f"cqa: binary not found: {args.binary}")

    maqao = shutil.which("maqao")
    if maqao is None:
        error("cqa: maqao not found in PATH")

    describe = git_describe()
    stem = report_stem(describe)

    cqa_dir = os.path.abspath(CQA_DIR)
    os.makedirs(cqa_dir, exist_ok=True)  # MAQAO exits 12 without it
    csv_path = os.path.join(cqa_dir, f"{stem}.csv")
    report_path = os.path.join(cqa_dir, f"{stem}.md")

    status, stdout, stderr = run(cqa_csv_command(maqao, binary, csv_path))
    if status != 0:
        error(f"cqa: maqao failed:\n{(stderr or stdout).strip()}")

    target = "n/a"
    status, stdout, stderr = run(cqa_text_command(maqao, binary))
    if status == 0:
        target = target_processor(stdout + stderr)

    rows = []
    if os.path.isfile(csv_path):
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            rows = parse_rows(f.read().splitlines())
    loops = sort_loops(best_loops(rows))

    facts = [
        ("commit", commit_from(describe)),
        ("dirty", "yes" if describe.endswith("-dirty") else "no"),
        ("binary", f"{display_path(binary)}  sha256:{sha256(binary)}"),
        ("target", target),
        ("date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")),
    ]
    lines = [f"{key + ':':<8} {text}" for key, text in facts]
    lines += ["", NOTES, ""]
    if loops:
        lines.append(table(report_table(loops)))
    else:
        lines.append("no loops")
    report = "\n".join(lines) + "\n"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"target: {target}")
    print(f"wrote {display_path(report_path)}")
    print()
    print(table(report_table(loops[:TOP])) if loops else "no loops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
