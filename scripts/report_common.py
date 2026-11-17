#!/usr/bin/env python3
"""Shared report helpers for bench.py and cqa.py.

One copy of the formatting, hashing and git-describe helpers so both reports
stay byte-identical. Importing this module has no side effects.

Self-test:  python3 -m doctest scripts/report_common.py
"""
import hashlib
import os
import subprocess
import sys


def table(rows):
    """Pad ROWS into columns two spaces apart.

    >>> print(table([["workload", "wall min"], ["floor", "6.1 ms"]]))
    workload  wall min
    floor     6.1 ms
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


def error(message):
    """Print MESSAGE to stderr and exit 1."""
    print(message, file=sys.stderr)
    sys.exit(1)


def git_describe(tool):
    """TOOL-prefixed `git describe --tags --always --dirty`, or exit 1."""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        error(f"{tool}: git describe failed: {exc}")
    if proc.returncode != 0:
        error(f"{tool}: git describe failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def sha256(path):
    """SHA-256 of the file at PATH."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def commit_from(describe):
    """The commit part of `git describe --dirty` output.

    >>> commit_from("v0.0.3-3-gabc-dirty")
    'v0.0.3-3-gabc'
    >>> commit_from("v0.0.3")
    'v0.0.3'
    """
    return describe.removesuffix("-dirty")


def report_stem(describe):
    """Report filename stem: `git describe` output without its slashes.

    >>> report_stem("v0.0.3")
    'v0.0.3'
    >>> report_stem("feature/x-3-gabc-dirty")
    'feature_x-3-gabc-dirty'
    >>> report_stem("feature/x-3-gabc")
    'feature_x-3-gabc'
    """
    return describe.replace("/", "_")


def display_path(path, start="."):
    """PATH relative to START when it stays inside it, else absolute.

    >>> display_path("/repo/build/bench/x.md", "/repo")
    'build/bench/x.md'
    >>> display_path("/repo/build/bench/cqa/x.md", "/repo")
    'build/bench/cqa/x.md'
    >>> display_path("/elsewhere/x.md", "/repo")
    '/elsewhere/x.md'
    """
    relative = os.path.relpath(path, start)
    return path if relative.startswith("..") else relative
