"""Tests for general zfind CLI behavior and argument parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import ZfindRunner, entries, run_in


# -----------------------------------------------------------------------------
# Options shown in help output
# -----------------------------------------------------------------------------

CURRENT_HELP_OPTIONS = [
    b"--separator", b"--case", b"--gitignore", b"--no-gitignore", b"--hidden",
    b"--no-hidden", b"--exclude-name", b"--limit", b"--type", b"--color",
    b"--config", b"--no-config", b"--path", b"--help",
]


# -----------------------------------------------------------------------------
# General CLI: Help, Delimiters, Errors & End-of-Options
# -----------------------------------------------------------------------------

class TestCliBasics:
    def test_help_prints_usage_and_exits_zero(self, zfind: ZfindRunner):
        res = zfind("tiny", "--help")
        assert res.status == 0, f"Expected exit 0 for --help: {res}"
        assert b"usage: zfind" in res.out
        assert res.err == b""

    def test_unknown_option_fails(self, zfind: ZfindRunner):
        res = zfind("tiny", "--non-existent-flag")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unknown option\n"
        assert res.out == b""

    def test_unexpected_positional_arguments_fail(self, zfind: ZfindRunner):
        res = zfind("tiny", "pattern1", "pattern2", "pattern3")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unexpected argument 'pattern3'\n"
        assert res.out == b""

    def test_double_dash_escapes_option_like_patterns(self, zfind: ZfindRunner):
        """-- marks end of options, allowing searching for filenames like -0."""
        res = zfind("weird", "--", "-0")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == [b"-0"]

    def test_pattern_with_no_matches_exits_zero_with_empty_output(self, zfind: ZfindRunner):
        res = zfind("tiny", "non_existent_match_string")
        assert res.status == 0, f"Expected exit 0 on empty search: {res}"
        assert res.out == b""
        assert res.err == b""

    @pytest.mark.parametrize("args", [
        pytest.param(("foo", "--help"), id="after-pattern"),
        pytest.param(("--bogus", "--help"), id="after-unknown-option"),
        pytest.param(("--hidden", "--no-hidden", "--help"), id="after-conflicting-flags"),
        pytest.param(("--limit", "notanumber", "--help"), id="after-invalid-value"),
    ])
    def test_help_prescan_wins_over_other_arguments(self, zfind: ZfindRunner, args: tuple[str, ...]):
        baseline = zfind("tiny", "--help")
        res = zfind("tiny", *args)
        assert res.status == 0, f"Expected help for {args}: {res}"
        assert res.out == baseline.out
        assert res.err == b""

    def test_help_after_double_dash_is_literal(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "--help").write_bytes(b"")
        (tree / "-h").write_bytes(b"")

        res = run_in(zfind_path, tree, "--", "--help")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b"--help"]

        # Matching is a literal substring, so -h also matches the --help filename.
        res = run_in(zfind_path, tree, "--", "-h")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b"--help", b"-h"]

    @pytest.mark.parametrize("arg", [
        pytest.param("--ignore-case", id="old-case-spelling"),
        pytest.param("--ignore-case=foo", id="old-case-spelling-with-value"),
        pytest.param("--case-sensitive", id="removed-case-sensitive"),
        pytest.param("--case-insensitive", id="removed-case-insensitive"),
        pytest.param("-h", id="removed-short-help"),
        pytest.param("-0", id="removed-short-null"),
    ])
    def test_removed_options_are_unknown(self, zfind: ZfindRunner, arg: str):
        res = zfind("tiny", arg)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unknown option\n"
        assert res.out == b""

    def test_help_lists_current_options_and_fits_width(self, zfind: ZfindRunner):
        out = zfind("tiny", "--help").out
        for option in CURRENT_HELP_OPTIONS:
            assert option in out
        assert b"--ignore-case" not in out
        assert b"--case-sensitive" not in out
        assert b"--case-insensitive" not in out
        assert b"--null" not in out
        assert all(len(line) <= 80 for line in out.splitlines())


# -----------------------------------------------------------------------------
# Pattern Matching
# -----------------------------------------------------------------------------

class TestSortOrder:
    def test_results_are_sorted_for_names_with_equal_prefixes(self, zfind: ZfindRunner):
        res = zfind("camera")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        listing = entries(res.out)
        assert len({name[:8] for name in listing}) < len(listing)
        assert listing == sorted(listing)


class TestPatternMatching:
    def test_pattern_matches_basename_not_relative_path(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        (tree / "dir").mkdir(parents=True)
        (tree / "dir" / "c").write_bytes(b"")

        res = run_in(zfind_path, tree, "dir")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == [b"dir/"]

        res = run_in(zfind_path, tree, "c")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == [b"dir/c"]

        res = run_in(zfind_path, tree, "dir/c")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == []


class TestParserEdges:
    def test_optional_pattern_double_dash_and_bare_dash(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        (tree / "dir").mkdir(parents=True)
        for name in ("-", "-x", "a"):
            (tree / name).write_bytes(b"")
        (tree / "dir" / "c").write_bytes(b"")
        all_entries = [b"-", b"-x", b"a", b"dir/", b"dir/c"]

        res = run_in(zfind_path, tree, "")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == all_entries

        res = run_in(zfind_path, tree, "--")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == all_entries

        res = run_in(zfind_path, tree, "-")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == [b"-", b"-x"]

        res = run_in(zfind_path, tree, "--", "p", "q")
        assert res.status == 1
        assert res.err == b"zfind: unexpected argument 'q'\n"
        assert res.out == b""


# -----------------------------------------------------------------------------
# Conflicting Flags (Mutual Exclusion)
# -----------------------------------------------------------------------------

class TestConflictingFlags:
    @pytest.mark.parametrize("flags, expected_err", [
        pytest.param(
            ["--hidden", "--no-hidden"],
            b"zfind: cannot combine --hidden and --no-hidden\n",
            id="hidden-conflict-hidden-first",
        ),
        pytest.param(
            ["--no-hidden", "--hidden"],
            b"zfind: cannot combine --hidden and --no-hidden\n",
            id="hidden-conflict-no-hidden-first",
        ),
        pytest.param(
            ["--gitignore", "--no-gitignore"],
            b"zfind: cannot combine --gitignore and --no-gitignore\n",
            id="gitignore-conflict-gitignore-first",
        ),
        pytest.param(
            ["--no-gitignore", "--gitignore"],
            b"zfind: cannot combine --gitignore and --no-gitignore\n",
            id="gitignore-conflict-no-gitignore-first",
        ),
    ])
    def test_mutually_exclusive_flags_fail(
        self, zfind: ZfindRunner, flags: list[str], expected_err: bytes
    ):
        res = zfind("tiny", *flags)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == expected_err
        assert res.out == b""
