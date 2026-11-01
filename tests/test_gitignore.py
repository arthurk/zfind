"""Focused tests for .gitignore behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from expected import IGNORES, IGNORES_GITIGNORE, IGNORES_GITIGNORE_NO_HIDDEN
from helpers import ZfindRunner, entries, run_in


class TestGitignore:
    @pytest.mark.parametrize("args, expected", [
        pytest.param([], IGNORES, id="default-gitignore-not-applied"),
        pytest.param(["--no-gitignore"], IGNORES, id="explicit-no-gitignore"),
        pytest.param(["--gitignore"], IGNORES_GITIGNORE, id="gitignore-applied"),
        pytest.param(["--gitignore", "--no-hidden"], IGNORES_GITIGNORE_NO_HIDDEN, id="gitignore-no-hidden"),
    ])
    def test_tree_listings(self, zfind: ZfindRunner, args: list[str], expected: list[bytes]):
        res = zfind("ignores", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == expected

    def test_gitignore_only_applies_if_cwd_contains_dot_git(self, zfind: ZfindRunner):
        """ignores/sub has a .gitignore, but .git is one level up."""
        res_plain = zfind("ignores/sub")
        assert res_plain.status == 0, f"Baseline run failed: {res_plain}"
        plain = entries(res_plain.out)
        assert len(plain) > 0, "Baseline output unexpectedly empty"

        res_git = zfind("ignores/sub", "--gitignore")
        assert res_git.status == 0, f"Gitignore run failed: {res_git}"
        assert entries(res_git.out) == plain
        assert b"noise.log" in plain

    def test_gitignore_question_and_double_star_syntax(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").mkdir()
        (tree / ".gitignore").write_bytes(b"?.log\n**/gen/*.tmp\nbuild/**\n")
        (tree / ".ignore").write_bytes(b"important.log\n")
        for name in ("a.log", "ab.log", "important.log"):
            (tree / name).write_bytes(b"")
        (tree / "gen").mkdir()
        (tree / "gen" / "x.tmp").write_bytes(b"")
        (tree / "gen" / "x.keep").write_bytes(b"")
        (tree / "deep" / "gen").mkdir(parents=True)
        (tree / "deep" / "gen" / "y.tmp").write_bytes(b"")
        (tree / "build" / "sub").mkdir(parents=True)
        (tree / "build" / "sub" / "file").write_bytes(b"")

        res = run_in(zfind_path, tree, "--gitignore")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [
            b".gitignore",
            b".ignore",
            b"ab.log",
            b"build/",
            b"deep/",
            b"deep/gen/",
            b"gen/",
            b"gen/x.keep",
            b"important.log",
        ]

    def test_gitignore_case_matching_ignores_case_flags(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").mkdir()
        (tree / ".gitignore").write_bytes(b"foo\n")
        for name in ("foo", "FOO", "Foo"):
            (tree / name).write_bytes(b"")

        res = run_in(zfind_path, tree, "--gitignore", "--case-insensitive", "foo")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b"FOO", b"Foo"]
