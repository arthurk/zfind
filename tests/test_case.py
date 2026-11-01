"""Tests for smart case and explicit case matching flags."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import Result, ZfindRunner


def words(text: str) -> list[bytes]:
    return [word.encode() for word in text.split()]


def entries(out: bytes) -> list[bytes]:
    assert out == b"" or out.endswith(b"\n"), "Output must be newline-terminated or empty"
    return out.split(b"\n")[:-1]


def run_in(binary: Path, cwd: Path, *args: str, timeout: int = 60) -> Result:
    done = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)


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
        pytest.param(("--case-sensitive", "foo"), words("foo"), id="forced-sensitive"),
        pytest.param(
            ("--case-insensitive", "FOO"), words("FOO Foo foo"),
            id="forced-insensitive",
        ),
        pytest.param(
            ("--case-insensitive", "café"), words("café"),
            id="non-ascii-forced-insensitive",
        ),
        pytest.param(
            ("--case-sensitive", "CAFÉ"), words("CAFÉ"),
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
        pytest.param(("--case-sensitive",), id="sensitive-only"),
        pytest.param(("--case-insensitive",), id="insensitive-only"),
        pytest.param(("--case-sensitive", "--case-sensitive"), id="repeated-sensitive"),
        pytest.param(("--case-insensitive", "--case-insensitive"), id="repeated-insensitive"),
    ])
    def test_case_flags_without_pattern_are_noop(self, zfind: ZfindRunner, args: tuple[str, ...]):
        baseline = zfind("tiny")
        res = zfind("tiny", *args)
        assert res == baseline

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
        # Even though 'caf' is lowercase, flag forces sensitive match against uppercase
        res = zfind("weird", "--case-sensitive", "CAF")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == []

    def test_forced_case_insensitive(self, zfind: ZfindRunner):
        # Even though 'CAF' has uppercase, flag forces case-folding
        res = zfind("weird", "--case-insensitive", "CAF")
        assert res.status == 0, f"Command failed: {res}"
        assert b"caf\xc3\xa9" in entries(res.out)
