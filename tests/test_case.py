"""Tests for smart case and the --case option."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import ZfindRunner, entries, run_in, words


@pytest.fixture
def case_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "case-tree"
    tree.mkdir()
    for name in ("foo", "FOO", "Foo", "café", "CAFÉ"):
        (tree / name).write_bytes(b"")
    return tree


class TestCaseSensitivity:
    @pytest.mark.parametrize(("args", "expected"), [
        pytest.param(("foo",), words("FOO Foo foo"), id="smart-lowercase"),
        pytest.param(("Foo",), words("Foo"), id="smart-uppercase"),
        pytest.param(("--case=smart", "foo"), words("FOO Foo foo"), id="smart-lowercase-explicit"),
        pytest.param(("--case=smart", "Foo"), words("Foo"), id="smart-uppercase-explicit"),
        pytest.param(("--case=sensitive", "foo"), words("foo"), id="sensitive-equals"),
        pytest.param(("--case", "sensitive", "foo"), words("foo"), id="sensitive-space"),
        pytest.param(
            ("--case=insensitive", "FOO"), words("FOO Foo foo"),
            id="insensitive-equals",
        ),
        pytest.param(
            ("--case", "insensitive", "FOO"), words("FOO Foo foo"),
            id="insensitive-space",
        ),
        pytest.param(
            ("--case=insensitive", "café"), words("café"),
            id="non-ascii-forced-insensitive",
        ),
        pytest.param(
            ("--case=sensitive", "CAFÉ"), words("CAFÉ"),
            id="non-ascii-forced-sensitive",
        ),
    ])
    def test_case_matrix_lists_exact_matches(
        self, zfind_path: Path, case_tree: Path, args: tuple[str, ...],
        expected: list[bytes],
    ):
        res = run_in(zfind_path, case_tree, *args)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == expected

    @pytest.mark.parametrize("args", [
        pytest.param(("--case=sensitive",), id="sensitive-only"),
        pytest.param(("--case=insensitive",), id="insensitive-only"),
        pytest.param(("--case=smart",), id="smart-only"),
        pytest.param(("--case", "sensitive"), id="sensitive-only-space"),
        pytest.param(("--case=sensitive", "--case=sensitive"), id="repeated-sensitive"),
        pytest.param(("--case=insensitive", "--case=insensitive"), id="repeated-insensitive"),
    ])
    def test_case_options_without_pattern_are_noop(self, zfind: ZfindRunner, args: tuple[str, ...]):
        baseline = zfind("tiny")
        res = zfind("tiny", *args)
        assert res == baseline

    @pytest.mark.parametrize(("args", "expected"), [
        pytest.param(
            ("--case=sensitive", "--case=insensitive", "foo"),
            words("FOO Foo foo"), id="sensitive-then-insensitive",
        ),
        pytest.param(
            ("--case=insensitive", "--case=sensitive", "foo"),
            words("foo"), id="insensitive-then-sensitive",
        ),
        pytest.param(
            ("--case", "sensitive", "--case=insensitive", "foo"),
            words("FOO Foo foo"), id="space-then-equals",
        ),
        pytest.param(
            ("--case=insensitive", "--case", "smart", "Foo"),
            words("Foo"), id="smart-last-restores-the-default",
        ),
    ])
    def test_the_last_case_value_wins(
        self, zfind_path: Path, case_tree: Path, args: tuple[str, ...],
        expected: list[bytes],
    ):
        res = run_in(zfind_path, case_tree, *args)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == expected

    def test_smart_case_lowercase_pattern_is_case_insensitive(self, zfind: ZfindRunner):
        # 'caf' matches 'café'
        res = zfind("weird", "caf")
        assert res.status == 0, f"Command failed: {res}"
        assert b"caf\xc3\xa9" in entries(res.out)

    def test_smart_case_uppercase_pattern_is_case_sensitive(self, zfind: ZfindRunner):
        # 'CAF' contains uppercase, so it should not match 'café'
        res = zfind("weird", "CAF")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == []

    def test_forced_case_sensitive(self, zfind: ZfindRunner):
        # Even though 'caf' is lowercase, the option forces a sensitive match
        res = zfind("weird", "--case=sensitive", "CAF")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == []

    def test_forced_case_insensitive(self, zfind: ZfindRunner):
        # Even though 'CAF' has uppercase, the option forces case-folding
        res = zfind("weird", "--case=insensitive", "CAF")
        assert res.status == 0, f"Command failed: {res}"
        assert b"caf\xc3\xa9" in entries(res.out)


class TestCaseValueErrors:
    MESSAGE = b"zfind: expected smart, sensitive or insensitive after --case\n"

    @pytest.mark.parametrize("args", [
        pytest.param(("--case=",), id="equals-empty"),
        pytest.param(("--case=Smart",), id="capitalized"),
        pytest.param(("--case=s",), id="abbreviation-smart"),
        pytest.param(("--case=sens",), id="abbreviation-sensitive"),
        pytest.param(("--case=bogus",), id="unknown-value"),
        pytest.param(("--case",), id="missing-value"),
        pytest.param(("--case", "--no-config"), id="next-option-is-the-value"),
        pytest.param(("--case", "--config", "/nonexistent"), id="config-is-the-value"),
    ])
    def test_bad_values_report_one_error(self, zfind: ZfindRunner, args: tuple[str, ...]):
        res = zfind("tiny", *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == self.MESSAGE
        assert res.out == b""

    def test_separator_consumes_case_as_its_value(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "a.txt").write_bytes(b"")

        res = run_in(zfind_path, tree, "--separator", "--case=smart", "a.txt")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"a.txt--case=smart"
