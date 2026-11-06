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

        res = run_in(zfind_path, tree, "--gitignore", "--case=insensitive", "foo")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b"FOO", b"Foo"]


class TestJujutsuAndMetadataPruning:
    def test_git_only_repository_prunes_both_metadata_dirs(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").mkdir()
        (tree / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (tree / ".jj").mkdir()
        (tree / ".jj" / "repo").write_bytes(b"data\n")
        (tree / ".gitignore").write_bytes(b"*.ignored\n")
        (tree / "keep.txt").write_bytes(b"")
        (tree / "drop.ignored").write_bytes(b"")
        (tree / "nested").mkdir()
        (tree / "nested" / ".git").mkdir()
        (tree / "nested" / ".jj").mkdir()
        (tree / "nested" / "file.txt").write_bytes(b"")

        res = run_in(binary, tree, "--gitignore")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [
            b".gitignore",
            b"keep.txt",
            b"nested/",
            b"nested/file.txt",
        ]

    def test_jujutsu_only_repository_applies_gitignore_and_prunes(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".jj").mkdir()
        (tree / ".jj" / "op_heads").write_bytes(b"head\n")
        (tree / ".gitignore").write_bytes(b"*.tmp\nbuild/\n")
        (tree / "file.txt").write_bytes(b"")
        (tree / "file.tmp").write_bytes(b"")
        (tree / "build").mkdir()
        (tree / "build" / "out.bin").write_bytes(b"")
        (tree / "nested").mkdir()
        (tree / "nested" / ".git").mkdir()
        (tree / "nested" / ".jj").mkdir()
        (tree / "nested" / "keep.txt").write_bytes(b"")

        res = run_in(binary, tree, "--gitignore")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [
            b".gitignore",
            b"file.txt",
            b"nested/",
            b"nested/keep.txt",
        ]

    def test_colocated_repository_applies_rules_once_and_prunes_both(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").mkdir()
        (tree / ".git" / "config").write_bytes(b"")
        (tree / ".jj").mkdir()
        (tree / ".jj" / "repo").write_bytes(b"")
        (tree / ".gitignore").write_bytes(b"ignored.txt\n")
        (tree / "valid.txt").write_bytes(b"")
        (tree / "ignored.txt").write_bytes(b"")

        res = run_in(binary, tree, "--gitignore")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b".gitignore", b"valid.txt"]

    def test_no_repository_marker_in_cwd_does_not_discover_parent(
        self, binary: Path, tmp_path: Path
    ):
        root = tmp_path / "root"
        root.mkdir()
        (root / ".jj").mkdir()
        (root / ".git").mkdir()
        sub = root / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_bytes(b"noise.log\n")
        (sub / "noise.log").write_bytes(b"")
        (sub / "plain.txt").write_bytes(b"")
        (sub / "child").mkdir()
        (sub / "child" / ".git").mkdir()
        (sub / "child" / ".jj").mkdir()

        res = run_in(binary, sub, "--gitignore")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [
            b".gitignore",
            b"child/",
            b"child/.git/",
            b"child/.jj/",
            b"noise.log",
            b"plain.txt",
        ]

    def test_no_gitignore_does_not_apply_rules_or_prune(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".git").mkdir()
        (tree / ".jj").mkdir()
        (tree / ".gitignore").write_bytes(b"ignored.txt\n")
        (tree / "ignored.txt").write_bytes(b"")
        (tree / "normal.txt").write_bytes(b"")

        # Default (no flags)
        res_default = run_in(binary, tree)
        assert res_default.status == 0, f"Command failed: {res_default}"
        assert res_default.err == b""
        assert entries(res_default.out) == [
            b".git/",
            b".gitignore",
            b".jj/",
            b"ignored.txt",
            b"normal.txt",
        ]

        # Explicit --no-gitignore
        res_explicit = run_in(binary, tree, "--no-gitignore")
        assert res_explicit.status == 0, f"Command failed: {res_explicit}"
        assert res_explicit.err == b""
        assert entries(res_explicit.out) == [
            b".git/",
            b".gitignore",
            b".jj/",
            b"ignored.txt",
            b"normal.txt",
        ]

    def test_hidden_flag_interactions(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".jj").mkdir()
        (tree / ".gitignore").write_bytes(b"*.tmp\n")
        (tree / ".hidden").write_bytes(b"")
        (tree / "visible.txt").write_bytes(b"")
        (tree / "file.tmp").write_bytes(b"")

        # --gitignore --hidden: metadata pruned, other hidden kept, rules apply
        res_hidden = run_in(binary, tree, "--gitignore", "--hidden")
        assert res_hidden.status == 0, f"Command failed: {res_hidden}"
        assert res_hidden.err == b""
        assert entries(res_hidden.out) == [
            b".gitignore",
            b".hidden",
            b"visible.txt",
        ]

        # --gitignore --no-hidden: metadata pruned, other hidden skipped, rules apply
        res_nohidden = run_in(binary, tree, "--gitignore", "--no-hidden")
        assert res_nohidden.status == 0, f"Command failed: {res_nohidden}"
        assert res_nohidden.err == b""
        assert entries(res_nohidden.out) == [
            b"visible.txt",
        ]

    def test_pruned_metadata_not_traversed_or_counted(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".jj").mkdir()
        for i in range(10):
            (tree / ".jj" / f"f{i:02d}").write_bytes(b"")
        (tree / ".git").mkdir()
        for i in range(10):
            (tree / ".git" / f"g{i:02d}").write_bytes(b"")
        (tree / "a.txt").write_bytes(b"")
        (tree / "b.txt").write_bytes(b"")

        res = run_in(binary, tree, "--gitignore", "--limit", "2")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b"a.txt", b"b.txt"]

    def test_nested_rule_precedence_in_jj_repo(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".jj").mkdir()
        (tree / ".gitignore").write_bytes(b"*.log\n")
        sub = tree / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_bytes(b"!keep.log\n")
        (sub / "noise.log").write_bytes(b"")
        (sub / "keep.log").write_bytes(b"")

        res = run_in(binary, tree, "--gitignore")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [
            b".gitignore",
            b"sub/",
            b"sub/.gitignore",
            b"sub/keep.log",
        ]
