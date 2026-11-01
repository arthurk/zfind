"""Tests for the --type flag."""

from __future__ import annotations

import pytest
from helpers import ZfindRunner, entries, words


TINY = words("a b ca cb dir/ dir/c dir/d e")
TINY_FILES_ONLY = words("a b ca cb dir/c dir/d e")
TINY_DIRS_ONLY = words("dir/")


class TestType:
    def test_type_files_only(self, zfind: ZfindRunner):
        res = zfind("tiny", "--type", "f")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == TINY_FILES_ONLY

    def test_type_dirs_only(self, zfind: ZfindRunner):
        res = zfind("tiny", "--type", "d")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == TINY_DIRS_ONLY

    def test_type_repeatable_both_files_and_dirs(self, zfind: ZfindRunner):
        res = zfind("tiny", "--type", "f", "--type", "d")
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == TINY

    @pytest.mark.parametrize("args", [
        pytest.param(["--type"], id="missing-arg"),
        pytest.param(["--type", "x"], id="invalid-type-letter"),
        pytest.param(["--type", "dir"], id="word-instead-of-char"),
        pytest.param(["--type", "--limit", "5"], id="subsequent-flag-as-value"),
    ])
    def test_invalid_type_fails(self, zfind: ZfindRunner, args: list[str]):
        res = zfind("tiny", *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: expected f or d after --type\n"
        assert res.out == b""
