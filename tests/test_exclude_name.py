"""Tests for exact basename exclusions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import Result, ZfindRunner
from test_flags import entries, run_in, words


class TestExcludeNameParser:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (("--exclude-name=a",), words("b ca cb dir/ dir/c dir/d e")),
            (("--exclude-name", "a"), words("b ca cb dir/ dir/c dir/d e")),
            (("--exclude-name=a", "--exclude-name=dir"), words("b ca cb e")),
            (("--exclude-name=a", "--exclude-name=a"), words("b ca cb dir/ dir/c dir/d e")),
            (("--exclude-name=.", "--exclude-name=.."), words("a b ca cb dir/ dir/c dir/d e")),
            (("--exclude-name=dir", "--type", "f"), words("a b ca cb e")),
            (("--exclude-name=dir", "--type", "d"), []),
            (("--exclude-name=a", "--limit", "1"), words("b")),
        ],
    )
    def test_forms_accumulate_and_filter_before_other_options(
        self, zfind: ZfindRunner, args: tuple[str, ...], expected: list[bytes]
    ):
        res = zfind("tiny", *args)
        assert res == Result(out=b"".join(name + b"\n" for name in expected), err=b"", status=0)

    @pytest.mark.parametrize(
        ("args", "diagnostic"),
        [
            (("--exclude-name",), b"zfind: missing value for --exclude-name\n"),
            (("--exclude-name=",), b"zfind: empty name for --exclude-name\n"),
            (("--exclude-name", ""), b"zfind: empty name for --exclude-name\n"),
            (("--exclude-name=a/b",), b"zfind: --exclude-name matches basenames only; '/' is not allowed\n"),
            (("--exclude-name", "a/"), b"zfind: --exclude-name matches basenames only; '/' is not allowed\n"),
            (("--exclude-name=a", "pattern", "--exclude-name=b"), b"zfind: unexpected argument\n"),
            (("--exclude-nam=x",), b"zfind: unknown option\n"),
            (("--exclude_name=x",), b"zfind: unknown option\n"),
            (("--EXCLUDE-NAME=x",), b"zfind: unknown option\n"),
        ],
    )
    def test_usage_errors_are_pre_traversal(
        self, zfind: ZfindRunner, args: tuple[str, ...], diagnostic: bytes
    ):
        res = zfind("tiny", *args)
        assert res.status == 1
        assert res.out == b""
        assert res.err == diagnostic

    def test_help_prescan_and_value_greediness(self, zfind: ZfindRunner):
        help_out = zfind("tiny", "--help").out
        for args in (("--exclude-name=", "--help"), ("--exclude-name", "--help")):
            res = zfind("tiny", *args)
            assert res.status == 0 and res.out == help_out and res.err == b""

        consumed = zfind("weird", "--exclude-name", "-h")
        assert consumed.status == 0 and b"-h\n" not in consumed.out

    def test_capacity_deduplicates_and_rejects_the_65th_unique_value(
        self, zfind: ZfindRunner
    ):
        repeated = zfind("tiny", *("--exclude-name=a" for _ in range(65)))
        assert repeated.status == 0 and repeated.err == b""
        full = zfind("tiny", *(f"--exclude-name=n{i}" for i in range(65)))
        assert full.status == 1
        assert full.out == b""
        assert full.err == b"zfind: too many --exclude-name values (max 64)\n"

    def test_oversized_name_is_accepted(self, zfind: ZfindRunner):
        res = zfind("tiny", "--exclude-name=" + "x" * 300)
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == words("a b ca cb dir/ dir/c dir/d e")

    def test_equals_greediness_and_end_of_options(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ("a=b", "=foo", "--", "foo", "bar", "foobar", "x--exclude-name=foo"):
            (tree / name).write_bytes(b"")

        res = run_in(zfind_path, tree, "--exclude-name=a=b", "--exclude-name==foo")
        assert entries(res.out) == [b"--", b"bar", b"foo", b"foobar", b"x--exclude-name=foo"]

        greedy = run_in(zfind_path, tree, "--exclude-name", "--")
        assert b"--\n" not in greedy.out

        positional = run_in(zfind_path, tree, "--", "--exclude-name=foo")
        assert entries(positional.out) == [b"x--exclude-name=foo"]

        exclusion_then_pattern = run_in(zfind_path, tree, "--exclude-name", "foo", "bar")
        assert entries(exclusion_then_pattern.out) == [b"bar", b"foobar"]

    def test_names_are_compared_as_literal_bytes(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ("sp ace", "back\\slash", "line\nbreak"):
            (tree / name).write_bytes(b"")
        for name in (b"hi\xff", b"hi\xfe"):
            (tree / os.fsdecode(name)).write_bytes(b"")

        args = (
            "--separator=\\0",
            "--exclude-name=sp ace",
            r"--exclude-name=back\slash",
            "--exclude-name=line\nbreak",
            "--exclude-name=" + os.fsdecode(b"hi\xff"),
        )
        res = run_in(zfind_path, tree, *args)
        assert res.status == 0 and res.err == b""
        assert res.out == b"hi\xfe\0"


class TestExcludeNameMatching:
    def test_literal_matching_prunes_directories_and_is_case_sensitive(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        (tree / "dir").mkdir(parents=True)
        for name in ("a*b", "c?d", "[ab]", "Foo", "foo", "dir/a*b"):
            (tree / name).write_bytes(b"")

        res = run_in(zfind_path, tree, "--case-insensitive", "--exclude-name=a*b")
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b"Foo", b"[ab]", b"c?d", b"dir/", b"foo"]

        case = run_in(zfind_path, tree, "--case-insensitive", "--exclude-name=Foo")
        assert b"Foo\n" not in case.out and b"foo\n" in case.out

        pruned = run_in(zfind_path, tree, "--exclude-name=dir", "--type", "f")
        assert entries(pruned.out) == [b"Foo", b"[ab]", b"a*b", b"c?d", b"foo"]
        assert b"dir/" not in pruned.out and b"dir/a*b" not in pruned.out

    def test_special_entries_and_search_root(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "node_modules"
        (tree / "node_modules").mkdir(parents=True)
        (tree / "keep").write_bytes(b"")
        (tree / "node_modules" / "inside").write_bytes(b"")
        (tree / "link").symlink_to("keep")
        try:
            os.mkfifo(tree / "pipe")
        except OSError as exc:
            pytest.skip(f"cannot create FIFO on this filesystem: {exc}")

        res = run_in(zfind_path, tree, "--exclude-name=node_modules")
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b"keep", b"link", b"pipe"]

        hidden_link = run_in(zfind_path, tree, "--exclude-name=link")
        assert b"link\n" not in hidden_link.out
        assert b"keep\n" in hidden_link.out
        hidden_pipe = run_in(zfind_path, tree, "--exclude-name=pipe")
        assert b"pipe\n" not in hidden_pipe.out

    def test_exclusion_overrides_ignore_and_excluding_gitignore_keeps_rules(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "repo"
        (tree / ".git").mkdir(parents=True)
        (tree / ".gitignore").write_bytes(b"!keep\nignored/\n")
        (tree / "keep").write_bytes(b"")
        (tree / "ignored").mkdir()
        (tree / "ignored" / "inside").write_bytes(b"")

        res = run_in(zfind_path, tree, "--gitignore", "--exclude-name=.gitignore")
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b"keep"]

        negated = run_in(zfind_path, tree, "--gitignore", "--exclude-name=keep")
        assert b"keep\n" not in negated.out

    def test_starting_directory_name_is_not_excluded(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "same"
        tree.mkdir()
        (tree / "same").write_bytes(b"")
        (tree / "keep").write_bytes(b"")
        res = run_in(zfind_path, tree, "--exclude-name=same")
        assert entries(res.out) == [b"keep"]


@pytest.mark.parametrize("kind", ["serial", "unknown"])
def test_exclusion_output_matches_build_variants(
    kind: str,
    zfind_path: Path,
    serial_zfind_path: Path,
    tmp_path: Path,
):
    variant = serial_zfind_path
    if kind == "unknown":
        variant = zfind_path.parent.parent / f"{zfind_path.parent.name}-unknown" / "zfind"
        if not variant.is_file():
            pytest.skip("unknown-type build not found; run `make test` to build it")
    tree = tmp_path / "tree"
    (tree / "skip").mkdir(parents=True)
    (tree / "skip" / "nested").write_bytes(b"")
    (tree / "keep").write_bytes(b"")
    args = ("--exclude-name=skip",)
    baseline = run_in(zfind_path, tree, *args)
    compared = run_in(variant, tree, *args)
    assert compared == baseline
