"""Tests for the --type flag."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Optional

import pytest
from helpers import ZfindRunner, entries, run_in, words


TINY = words("a b ca cb dir/ dir/c dir/d e")
TINY_FILES_ONLY = words("a b ca cb dir/c dir/d e")
TINY_DIRS_ONLY = words("dir/")

TYPE_ERROR = b"zfind: expected f, d, l or b after --type\n"


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
        pytest.param(["--type", "F"], id="uppercase-letter"),
        pytest.param(["--type", "dir"], id="word-instead-of-char"),
        pytest.param(["--type", ""], id="empty-value"),
        pytest.param(["--type", "--limit", "5"], id="subsequent-flag-as-value"),
        pytest.param(["--type", "--no-config"], id="config-option-as-value"),
    ])
    def test_invalid_type_fails(self, zfind: ZfindRunner, args: list[str]):
        res = zfind("tiny", *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == TYPE_ERROR
        assert res.out == b""

    def test_help_documents_the_type_values(self, zfind: ZfindRunner):
        out = zfind("tiny", "--help").out
        assert b"--type f|d|l|b" in out
        assert b"--type=V" in out


class TestTypeEqualsForm:
    """--type=VALUE is the same option as --type VALUE."""

    @pytest.mark.parametrize("args, expected", [
        pytest.param(["--type=f"], TINY_FILES_ONLY, id="files"),
        pytest.param(["--type=d"], TINY_DIRS_ONLY, id="dirs"),
        pytest.param(["--type=f", "--type=d"], TINY, id="both-equals-forms"),
        pytest.param(["--type", "f", "--type=d"], TINY, id="blank-then-equals"),
        pytest.param(["--type=f", "--type", "d"], TINY, id="equals-then-blank"),
        pytest.param(["--type=f", "--type=f"], TINY_FILES_ONLY, id="repeated-value"),
    ])
    def test_equals_form_selects_like_the_blank_form(
        self, zfind: ZfindRunner, args: list[str], expected: list[bytes]
    ):
        res = zfind("tiny", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == expected

    @pytest.mark.parametrize("letter", ["f", "d", "l"])
    def test_both_forms_print_identical_bytes(self, zfind: ZfindRunner, letter: str):
        assert zfind("tiny", f"--type={letter}") == zfind("tiny", "--type", letter)

    @pytest.mark.parametrize("arg", [
        pytest.param("--type=", id="empty-value"),
        pytest.param("--type=ff", id="two-letters"),
        pytest.param("--type=x", id="invalid-letter"),
        pytest.param("--type=F", id="uppercase-letter"),
        pytest.param("--type=dir", id="word-instead-of-char"),
        pytest.param("--type==f", id="second-equals-sign"),
        pytest.param("--type= f", id="leading-space"),
    ])
    def test_invalid_equals_value_fails(self, zfind: ZfindRunner, arg: str):
        res = zfind("tiny", arg)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == TYPE_ERROR
        assert res.out == b""

    def test_a_separator_swallows_the_option_that_follows_it(self, zfind: ZfindRunner):
        # --separator takes the next token greedily, even a would-be --type=f.
        res = zfind("tiny", "--separator", "--type=f", "e")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"e--type=f"

    def test_after_double_dash_it_is_a_pattern(self, zfind: ZfindRunner):
        res = zfind("tiny", "--", "--type=f")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b""


# -----------------------------------------------------------------------------
# --type l: symlinks
#
# The tree has regular files, real directories and every kind of symlink (to a
# file, to a directory, dangling, hidden, nested). The expected output comes
# from an oracle that walks the filesystem in Python, not from a hand-written
# list, so the same tests run unchanged against every build variant. The
# variants matter: when getdents reports DT_UNKNOWN the walkers fall back to
# lstat, in three separate places, and the plain build never takes them.
#
#   parallel  the normal build
#   serial    one walking thread
#   unknown   every type via lstat, wide prepass forced (walk_wide)
#   range     every type via lstat, big directories take walk_range, small
#             ones walk_dir's inline loop
# -----------------------------------------------------------------------------


def make_links_tree(root: Path) -> Path:
    for rel in (
        "file_a", "dir_x/inner", "wide_1/w1", "wide_2/w2", "wide_3/w3", ".hid_dir/h",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    links = {
        "lnk_file": "file_a",            # to a file
        "lnk_dir": "dir_x",              # to a directory: must not be descended
        "lnk_dangling": "nowhere",       # to nothing
        ".hid_lnk": "file_a",            # hidden
        "dir_x/lnk_in": "inner",         # inside a real directory
        "wide_1/lnk_w": "w1",
        "wide_2/lnk_up": "../dir_x",     # to a directory, from below
        "wide_3/lnk_w": "w3",
    }
    for rel, target in links.items():
        (root / rel).symlink_to(target)
    # 40 entries: enough for the range build's lowered RANGE_MIN.
    big = root / "big"
    big.mkdir()
    for i in range(1, 31):
        (big / f"f{i:02d}").write_bytes(b"")
    for i in range(1, 9):
        (big / f"l{i:02d}").symlink_to("f01")
    (big / "ldir").symlink_to("../dir_x")
    (big / "ldang").symlink_to("gone")
    return root


@pytest.fixture(scope="module")
def links_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared by this module; no test modifies it."""
    return make_links_tree(tmp_path_factory.mktemp("links"))


BROKEN_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ENAMETOOLONG})


def link_state(path: Path) -> Optional[str]:
    """What a symlink resolves to: "f", "d", "other", "b" when broken, None unknown.

    Broken means the resolution failed with ENOENT, ENOTDIR, ELOOP or
    ENAMETOOLONG. Any other error is indeterminate: it must match b nowhere.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        return "b" if exc.errno in BROKEN_ERRNOS else None
    if stat.S_ISREG(st.st_mode):
        return "f"
    if stat.S_ISDIR(st.st_mode):
        return "d"
    return "other"


def model(
    root: Path,
    kinds: Optional[set[str]] = None,
    *,
    substring: bytes = b"",
    hidden: bool = True,
) -> list[bytes]:
    """What zfind must print: sorted by name per directory, depth first.

    kinds is a set of "f", "d", "l", "b", or None for no type filter. Symlinks
    are never followed for traversal, and directories (only) end with "/".

    A link matches its own "l" kind. When "l" is combined with "f" and/or
    "d", the target decides instead: the link matches only when it resolves to
    a selected regular file or directory. "b" selects links whose target
    definitively does not exist, with or without "l"; it adds those links to
    an f/d combination. Links to special files match no f/d combination.
    """
    out: list[bytes] = []

    def selected(entry: os.DirEntry, kind: str) -> bool:
        if kinds is None:
            return True
        if kind != "l":
            return kind in kinds
        if "l" in kinds and not kinds & {"f", "d"}:
            return True                     # l alone (b adds nothing here)
        if "l" not in kinds and "b" not in kinds:
            return False
        state = link_state(Path(entry.path))
        if state == "b":
            return "b" in kinds
        if state in ("f", "d"):
            return "l" in kinds and state in kinds
        return False

    def walk(directory: Path, prefix: str) -> None:
        for entry in sorted(os.scandir(directory), key=lambda e: os.fsencode(e.name)):
            if entry.name.startswith(".") and not hidden:
                continue
            if entry.is_symlink():
                kind, slash = "l", ""
            elif entry.is_dir(follow_symlinks=False):
                kind, slash = "d", "/"
            else:
                kind, slash = "f", ""
            rel = prefix + entry.name
            if selected(entry, kind) and substring in os.fsencode(entry.name):
                out.append(os.fsencode(rel + slash))
            if kind == "d":
                walk(Path(entry.path), rel + "/")

    walk(root, "")
    return out


def listing(binary: Path, tree: Path, *args: str) -> list[bytes]:
    res = run_in(binary, tree, *args)
    assert res.status == 0, f"Command failed: {res}"
    assert res.err == b""
    return entries(res.out)


class TestTypeLinks:
    def test_the_oracle_sees_the_shapes_the_tests_rely_on(self, links_tree: Path):
        # Guards the fixture, not zfind: a tree without these shapes tests nothing.
        links = model(links_tree, {"l"})
        for name in (b"lnk_file", b"lnk_dir", b"lnk_dangling", b".hid_lnk",
                     b"dir_x/lnk_in", b"big/ldir", b"big/l01"):
            assert name in links
        assert len(links) == 8 + 10
        assert len(os.listdir(links_tree / "big")) >= 16

    def test_l_selects_every_kind_of_symlink(self, binary: Path, links_tree: Path):
        got = listing(binary, links_tree, "--type", "l")
        assert got == model(links_tree, {"l"})
        assert b"lnk_dir" in got and b"lnk_dangling" in got  # to a dir, to nothing

    def test_a_link_to_a_directory_has_no_trailing_slash_and_is_not_descended(
        self, binary: Path, links_tree: Path
    ):
        for args in (("--type", "l"), ()):
            got = listing(binary, links_tree, *args)
            assert b"lnk_dir" in got and b"lnk_dir/" not in got
            assert not [name for name in got if name.startswith(b"lnk_dir/")]
            assert not [name for name in got if name.startswith(b"big/ldir/")]

    def test_the_equals_form_selects_the_same_links(self, binary: Path, links_tree: Path):
        assert listing(binary, links_tree, "--type=l") == model(links_tree, {"l"})

    @pytest.mark.parametrize("types", [
        pytest.param(("f",), id="files"),
        pytest.param(("d",), id="dirs"),
    ])
    def test_f_and_d_still_exclude_symlinks(
        self, binary: Path, links_tree: Path, types: tuple[str, ...]
    ):
        args = [arg for kind in types for arg in ("--type", kind)]
        got = listing(binary, links_tree, *args)
        assert got == model(links_tree, set(types))
        assert not [name for name in got if b"lnk" in name or name.startswith(b"big/l")]

    @pytest.mark.parametrize("types", [
        pytest.param(("f", "l"), id="files-and-links"),
        pytest.param(("d", "l"), id="dirs-and-links"),
        pytest.param(("f", "d", "l"), id="all-three"),
    ])
    def test_types_combine(self, binary: Path, links_tree: Path, types: tuple[str, ...]):
        args = [arg for kind in types for arg in ("--type", kind)]
        assert listing(binary, links_tree, *args) == model(links_tree, set(types))

    def test_all_three_types_omit_links_that_resolve_to_nothing(
        self, binary: Path, links_tree: Path
    ):
        # A combination is target-aware, so --type f --type d --type l keeps
        # every file and directory and only the links whose target is one.
        got = listing(binary, links_tree, "--type=f", "--type=d", "--type=l")
        links = model(links_tree, {"l"})
        dangling = [name for name in links if not (links_tree / os.fsdecode(name)).exists()]
        assert dangling, "fixture must contain broken links"
        expected = [name for name in model(links_tree, None) if name not in dangling]
        assert got == expected

    def test_a_directory_that_holds_only_links_is_still_walked(
        self, binary: Path, links_tree: Path
    ):
        # --type l must traverse real directories to find the links below them.
        got = listing(binary, links_tree, "--type", "l")
        assert b"dir_x/lnk_in" in got and b"wide_2/lnk_up" in got and b"big/l08" in got

    @pytest.mark.parametrize("args, kwargs", [
        pytest.param(("lnk",), {"substring": b"lnk"}, id="pattern"),
        pytest.param(("--no-hidden",), {"hidden": False}, id="no-hidden"),
    ])
    def test_links_compose_with_other_filters(
        self, binary: Path, links_tree: Path, args: tuple[str, ...], kwargs: dict
    ):
        assert listing(binary, links_tree, "--type", "l", *args) == model(
            links_tree, {"l"}, **kwargs
        )

    def test_links_honor_limit_exclude_name_and_separator(
        self, binary: Path, links_tree: Path
    ):
        everything = model(links_tree, {"l"})
        assert listing(binary, links_tree, "--type", "l", "--limit", "3") == everything[:3]
        assert listing(binary, links_tree, "--type", "l", "--limit", "0") == []
        without = [name for name in everything if name != b"lnk_dir"]
        assert listing(binary, links_tree, "--type", "l", "--exclude-name", "lnk_dir") == without
        res = run_in(binary, links_tree, "--type", "l", "--separator=\\0")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"".join(name + b"\0" for name in everything)

    def test_help_lists_the_new_type(self, binary: Path, links_tree: Path):
        res = run_in(binary, links_tree, "--help")
        assert b"--type f|d|l|b" in res.out

    @pytest.mark.parametrize("arg", ["--type=L", "--type=ll", "--type=sym"])
    def test_other_spellings_of_l_are_rejected(self, binary: Path, links_tree: Path, arg: str):
        res = run_in(binary, links_tree, arg)
        assert res.status == 1
        assert res.err == TYPE_ERROR
        assert res.out == b""


@pytest.fixture(scope="module")
def combos_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The extended fixture from issue #9 (issue #8's tree plus four links)."""
    root = tmp_path_factory.mktemp("combos")
    for rel in ("a.txt", "FOO.txt", "foo.txt", "Foo.txt", "sub/b.txt", "sub2/c.txt"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    (root / "lnk_file").symlink_to("a.txt")
    (root / "lnk_dir").symlink_to("sub")
    (root / "lnk_dangling").symlink_to("nowhere")
    (root / "sub" / "inner_lnk").symlink_to("../sub2")
    (root / "lnk_null").symlink_to("/dev/null")          # resolves: not broken
    (root / "lnk_notdir").symlink_to("a.txt/nope")       # ENOTDIR
    (root / "lnk_loop_a").symlink_to("lnk_loop_b")       # ELOOP
    (root / "lnk_loop_b").symlink_to("lnk_loop_a")
    return root


class TestTargetAwareCombinations:
    """When l is combined with f and/or d, a link matches by its target's type."""

    LINKS = [b"lnk_dangling", b"lnk_dir", b"lnk_file", b"lnk_loop_a",
             b"lnk_loop_b", b"lnk_notdir", b"lnk_null", b"sub/inner_lnk"]
    FILES_AND_LINKS = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"lnk_file",
                       b"sub/b.txt", b"sub2/c.txt"]
    DIRS_AND_LINKS = [b"lnk_dir", b"sub/", b"sub/inner_lnk", b"sub2/"]
    ALL_THREE = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"lnk_dir",
                 b"lnk_file", b"sub/", b"sub/b.txt", b"sub/inner_lnk", b"sub2/",
                 b"sub2/c.txt"]

    @pytest.mark.parametrize("kinds, expected", [
        pytest.param(("l",), LINKS, id="links"),
        pytest.param(("f", "l"), FILES_AND_LINKS, id="files-and-links"),
        pytest.param(("d", "l"), DIRS_AND_LINKS, id="dirs-and-links"),
        pytest.param(("f", "d", "l"), ALL_THREE, id="all-three"),
    ])
    def test_the_issue_gates(
        self, binary: Path, combos_tree: Path, kinds: tuple[str, ...], expected: list[bytes]
    ):
        args = [arg for kind in kinds for arg in ("--type", kind)]
        assert listing(binary, combos_tree, *args) == expected
        assert listing(binary, combos_tree, *args) == model(combos_tree, set(kinds))

    def test_the_equals_spelling_selects_the_same_set(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type=l", "--type=f") == self.FILES_AND_LINKS

    def test_a_pattern_composes_with_the_target_rule(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type", "f", "--type", "l", "lnk") == [
            b"lnk_file"
        ]

    def test_a_limit_applies_after_the_target_rule(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type", "f", "--type", "l", "--limit", "1") == [
            b"FOO.txt"
        ]

    def test_broken_links_appear_only_with_l_alone(self, binary: Path, combos_tree: Path):
        assert b"lnk_dangling" in listing(binary, combos_tree, "--type", "l")
        for kinds in (("f", "l"), ("d", "l"), ("f", "d", "l")):
            args = [arg for kind in kinds for arg in ("--type", kind)]
            assert b"lnk_dangling" not in listing(binary, combos_tree, *args), kinds

    def test_a_link_to_a_directory_keeps_no_slash_and_is_not_descended(
        self, binary: Path, combos_tree: Path
    ):
        # A descended lnk_dir would repeat sub's entries below lnk_dir.
        for kinds in (("d", "l"), ("f", "d", "l")):
            args = [arg for kind in kinds for arg in ("--type", kind)]
            got = listing(binary, combos_tree, *args)
            assert b"lnk_dir" in got and b"lnk_dir/" not in got, kinds
            assert not [name for name in got if name.startswith(b"lnk_dir/")], kinds
            assert got.count(b"sub/b.txt") <= 1, kinds
        assert b"sub/b.txt" not in listing(binary, combos_tree, "--type", "d", "--type", "l")

    def test_a_chained_link_uses_the_final_target(self, binary: Path, tmp_path: Path):
        (tmp_path / "target").write_bytes(b"")
        (tmp_path / "dir").mkdir()
        (tmp_path / "second").symlink_to("target")
        (tmp_path / "first").symlink_to("second")
        (tmp_path / "to_dir").symlink_to("dir")
        assert listing(binary, tmp_path, "--type", "f", "--type", "l") == [
            b"first", b"second", b"target"
        ]
        assert listing(binary, tmp_path, "--type", "d", "--type", "l") == [
            b"dir/", b"to_dir"
        ]

    def test_a_link_to_a_special_file_is_dropped_from_combinations(
        self, binary: Path, tmp_path: Path
    ):
        try:
            os.mkfifo(tmp_path / "fifo")
        except OSError:
            pytest.skip("mkfifo is not available")
        (tmp_path / "lnk_fifo").symlink_to("fifo")
        assert listing(binary, tmp_path, "--type", "l") == [b"lnk_fifo"]
        assert listing(binary, tmp_path, "--type", "f", "--type", "l") == []
        assert listing(binary, tmp_path, "--type", "d", "--type", "l") == []


class TestBrokenSymlinks:
    """--type b selects links whose target definitively does not exist."""

    BROKEN = [b"lnk_dangling", b"lnk_loop_a", b"lnk_loop_b", b"lnk_notdir"]
    ALL_LINKS = TestTargetAwareCombinations.LINKS
    FILES_AND_BROKEN = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt",
                        b"lnk_dangling", b"lnk_loop_a", b"lnk_loop_b",
                        b"lnk_notdir", b"sub/b.txt", b"sub2/c.txt"]
    DIRS_AND_BROKEN = [b"lnk_dangling", b"lnk_loop_a", b"lnk_loop_b",
                       b"lnk_notdir", b"sub/", b"sub2/"]
    FILES_DIRS_AND_BROKEN = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt",
                             b"lnk_dangling", b"lnk_loop_a", b"lnk_loop_b",
                             b"lnk_notdir", b"sub/", b"sub/b.txt", b"sub2/",
                             b"sub2/c.txt"]
    FILES_LINKS_AND_BROKEN = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt",
                              b"lnk_dangling", b"lnk_file", b"lnk_loop_a",
                              b"lnk_loop_b", b"lnk_notdir", b"sub/b.txt",
                              b"sub2/c.txt"]
    EVERYTHING_AND_BROKEN = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt",
                             b"lnk_dangling", b"lnk_dir", b"lnk_file",
                             b"lnk_loop_a", b"lnk_loop_b", b"lnk_notdir", b"sub/",
                             b"sub/b.txt", b"sub/inner_lnk", b"sub2/", b"sub2/c.txt"]

    @pytest.mark.parametrize("kinds, expected", [
        pytest.param(("b",), BROKEN, id="broken"),
        pytest.param(("f", "b"), FILES_AND_BROKEN, id="files-and-broken"),
        pytest.param(("d", "b"), DIRS_AND_BROKEN, id="dirs-and-broken"),
        pytest.param(("f", "d", "b"), FILES_DIRS_AND_BROKEN, id="files-dirs-broken"),
        pytest.param(("l", "b"), ALL_LINKS, id="links-and-broken"),
        pytest.param(("f", "l", "b"), FILES_LINKS_AND_BROKEN, id="files-links-broken"),
        pytest.param(("f", "d", "l", "b"), EVERYTHING_AND_BROKEN,
                     id="files-dirs-links-broken"),
    ])
    def test_the_issue_gates(
        self, binary: Path, combos_tree: Path, kinds: tuple[str, ...], expected: list[bytes]
    ):
        args = [arg for kind in kinds for arg in ("--type", kind)]
        assert listing(binary, combos_tree, *args) == expected
        assert listing(binary, combos_tree, *args) == model(combos_tree, set(kinds))

    def test_b_lists_no_resolvable_link_and_no_trailing_slash(
        self, binary: Path, combos_tree: Path
    ):
        got = listing(binary, combos_tree, "--type", "b")
        assert got == self.BROKEN
        assert b"lnk_null" not in got          # resolves to /dev/null: not broken
        assert not [name for name in got if name.endswith(b"/")]

    def test_the_equals_form_selects_the_same_set(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type=b") == self.BROKEN

    def test_a_pattern_composes_with_the_broken_rule(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type", "b", "lnk_loop") == [
            b"lnk_loop_a", b"lnk_loop_b"
        ]

    def test_a_limit_applies_after_the_broken_rule(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type", "b", "--limit", "2") == (
            self.BROKEN[:2]
        )

    def test_b_finds_nested_broken_links_and_still_descends_directories(
        self, binary: Path, links_tree: Path
    ):
        # big/ has 40 entries, so the range build takes walk_range here.
        got = listing(binary, links_tree, "--type", "b")
        assert got == model(links_tree, {"b"})
        assert b"big/ldang" in got and b"lnk_dangling" in got

    def test_a_link_behind_an_unreadable_directory_is_not_broken(
        self, binary: Path, tmp_path: Path
    ):
        if os.geteuid() == 0:
            pytest.skip("root can read a mode-000 directory")
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "target.txt").write_bytes(b"")
        (tmp_path / "lnk_locked").symlink_to("locked/target.txt")
        locked.chmod(0)
        try:
            assert listing(binary, tmp_path, "--type", "b") == []
            assert listing(binary, tmp_path, "--type", "l") == [b"lnk_locked"]
            assert listing(binary, tmp_path, "--type", "f", "--type", "l") == []
        finally:
            locked.chmod(0o700)


class TestTypeLinksAndGitignore:
    """A dir-only rule (ign/) is about directories; a symlink is not one."""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".gitignore").write_bytes(b"ign/\n")
        (tmp_path / "target").write_bytes(b"")
        (tmp_path / "ign").symlink_to("target")  # same name as the rule, but a link
        (tmp_path / "sub" / "ign").mkdir(parents=True)  # a real ignored directory
        (tmp_path / "sub" / "ign" / "x").write_bytes(b"")
        return tmp_path

    def test_a_link_is_not_hit_by_a_directory_only_rule(self, binary: Path, repo: Path):
        assert listing(binary, repo, "--gitignore", "--type", "l") == [b"ign"]
        assert listing(binary, repo, "--gitignore") == [b".gitignore", b"ign", b"sub/", b"target"]

    def test_without_gitignore_both_are_listed(self, binary: Path, repo: Path):
        assert listing(binary, repo, "--type", "l") == [b"ign"]
        assert listing(binary, repo, "--type", "d") == [b".git/", b"sub/", b"sub/ign/"]


# -----------------------------------------------------------------------------
# --exclude-type
#
# Exclusion is the union of its values and uses the same meanings as --type:
# f and d test what an entry behaves like (a link by its target), l and b test
# the link itself. A result must match --type, when given, and must not match
# --exclude-type. The oracle walks the filesystem in Python so the same tests
# run against every build variant.

EXCLUDE_TYPE_ERROR = b"zfind: expected f, d, l or b after --exclude-type\n"
TYPE_CONFLICT_ERROR = (
    b"zfind: cannot combine --type and --exclude-type for the same value\n"
)


def excluded_values(args: list[str]) -> set[str]:
    """The kinds named by the --exclude-type options in args."""
    kinds: set[str] = set()
    i = 0
    while i < len(args):
        if args[i] == "--exclude-type":
            kinds.add(args[i + 1])
            i += 2
        elif args[i].startswith("--exclude-type="):
            kinds.add(args[i].split("=", 1)[1])
            i += 1
        else:
            i += 1
    return kinds


def excluded_model(
    root: Path,
    drop: set[str],
    *,
    substring: bytes = b"",
    hidden: bool = True,
) -> list[bytes]:
    """What zfind must print under --exclude-type for each kind in drop."""

    def excluded(entry: os.DirEntry, kind: str) -> bool:
        if kind != "l":
            return kind in drop
        if "l" in drop:
            return True                      # l tests the link: no target lookup
        state = link_state(Path(entry.path))
        if state == "b":
            return "b" in drop
        if state in ("f", "d"):
            return state in drop             # f/d test what it behaves like
        return False

    out: list[bytes] = []

    def walk(directory: Path, prefix: str) -> None:
        for entry in sorted(os.scandir(directory), key=lambda e: os.fsencode(e.name)):
            if entry.name.startswith(".") and not hidden:
                continue
            if entry.is_symlink():
                kind, slash = "l", ""
            elif entry.is_dir(follow_symlinks=False):
                kind, slash = "d", "/"
            else:
                kind, slash = "f", ""
            rel = prefix + entry.name
            if not excluded(entry, kind) and substring in os.fsencode(entry.name):
                out.append(os.fsencode(rel + slash))
            if kind == "d":
                walk(Path(entry.path), rel + "/")

    walk(root, "")
    return out


class TestExcludeType:
    """The issue #10 gates on the issue #9 fixture."""

    FILES = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"sub/b.txt", b"sub2/c.txt"]
    NO_DIRS = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"lnk_dangling",
               b"lnk_file", b"lnk_loop_a", b"lnk_loop_b", b"lnk_notdir",
               b"lnk_null", b"sub/b.txt", b"sub2/c.txt"]
    NO_LINKS = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"sub/",
                b"sub/b.txt", b"sub2/", b"sub2/c.txt"]
    NO_FILES = [b"lnk_dangling", b"lnk_dir", b"lnk_loop_a", b"lnk_loop_b",
                b"lnk_notdir", b"lnk_null", b"sub/", b"sub/inner_lnk", b"sub2/"]
    NO_BROKEN = [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"lnk_dir",
                 b"lnk_file", b"lnk_null", b"sub/", b"sub/b.txt",
                 b"sub/inner_lnk", b"sub2/", b"sub2/c.txt"]
    LINKS_EXCEPT_DIRS = [b"lnk_dangling", b"lnk_file", b"lnk_loop_a",
                         b"lnk_loop_b", b"lnk_notdir", b"lnk_null"]

    @pytest.mark.parametrize("args, expected", [
        pytest.param(["--exclude-type", "d"], NO_DIRS, id="no-directories"),
        pytest.param(["--exclude-type", "l"], NO_LINKS, id="no-links"),
        pytest.param(["--exclude-type", "f"], NO_FILES, id="no-files"),
        pytest.param(["--exclude-type", "b"], NO_BROKEN, id="no-broken-links"),
        pytest.param(["--type", "l", "--exclude-type", "d"], LINKS_EXCEPT_DIRS,
                     id="links-except-dirs"),
        pytest.param(["--type", "f", "--exclude-type", "d"], FILES,
                     id="files-except-dirs"),
        pytest.param(["--exclude-type", "d", "--exclude-type", "b"],
                     [b"FOO.txt", b"Foo.txt", b"a.txt", b"foo.txt", b"lnk_file",
                      b"lnk_null", b"sub/b.txt", b"sub2/c.txt"],
                     id="no-dirs-no-broken"),
        pytest.param(["--exclude-type=l"], NO_LINKS, id="equals-form"),
        pytest.param(["--exclude-type", "l", "--exclude-type", "d"], FILES,
                     id="exclusions-union"),
    ])
    def test_the_issue_gates(
        self, binary: Path, combos_tree: Path, args: list[str], expected: list[bytes]
    ):
        got = listing(binary, combos_tree, *args)
        assert got == expected
        if "--type" not in args and not any(arg.startswith("--type=") for arg in args):
            assert got == excluded_model(combos_tree, excluded_values(args))

    def test_pattern_and_limit_compose(self, binary: Path, combos_tree: Path):
        got = listing(binary, combos_tree, "--exclude-type", "l", "--limit", "3", "txt")
        assert got == [b"FOO.txt", b"Foo.txt", b"a.txt"]

    @pytest.mark.parametrize("args", [
        pytest.param(["--exclude-type"], id="missing-arg"),
        pytest.param(["--exclude-type", "z"], id="invalid-letter"),
        pytest.param(["--exclude-type", "F"], id="uppercase-letter"),
        pytest.param(["--exclude-type", "dir"], id="word-instead-of-char"),
        pytest.param(["--exclude-type", ""], id="empty-value"),
        pytest.param(["--exclude-type", "--limit", "5"], id="subsequent-flag-as-value"),
        pytest.param(["--exclude-type", "--no-config"], id="config-option-as-value"),
        pytest.param(["--exclude-type="], id="empty-equals"),
        pytest.param(["--exclude-type=ff"], id="two-letters"),
    ])
    def test_invalid_values_fail(self, binary: Path, combos_tree: Path, args: list[str]):
        res = run_in(binary, combos_tree, *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == EXCLUDE_TYPE_ERROR
        assert res.out == b""

    @pytest.mark.parametrize("args", [
        pytest.param(["--type", "f", "--exclude-type", "f"], id="files"),
        pytest.param(["--type", "d", "--exclude-type=d"], id="dirs-equals"),
        pytest.param(["--type", "l", "--exclude-type", "l"], id="links"),
        pytest.param(["--type", "f", "--type", "d", "--exclude-type", "d"],
                     id="one-value-of-a-set"),
    ])
    def test_the_same_value_in_both_sets_fails(
        self, binary: Path, combos_tree: Path, args: list[str]
    ):
        res = run_in(binary, combos_tree, *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == TYPE_CONFLICT_ERROR
        assert res.out == b""

    def test_no_contradiction_when_the_sets_differ(self, binary: Path, combos_tree: Path):
        assert listing(binary, combos_tree, "--type", "f", "--exclude-type", "d") == self.FILES

    def test_help_documents_exclude_type(self, binary: Path, combos_tree: Path):
        out = run_in(binary, combos_tree, "--help").out
        assert b"--exclude-type f|d|l|b" in out
        assert b"--exclude-type=V" in out


class TestExcludeTypeAcrossTreeShapes:
    """Every walker path sees the same exclusion over the links fixture."""

    @pytest.mark.parametrize("drop", [
        pytest.param({"d"}, id="dirs"),
        pytest.param({"l"}, id="links"),
        pytest.param({"f"}, id="files"),
        pytest.param({"b"}, id="broken"),
        pytest.param({"f", "d"}, id="files-and-dirs"),
        pytest.param({"l", "d"}, id="links-and-dirs"),
        pytest.param({"d", "b"}, id="dirs-and-broken"),
    ])
    def test_the_oracle_agrees(self, binary: Path, links_tree: Path, drop: set[str]):
        args = [arg for kind in sorted(drop) for arg in ("--exclude-type", kind)]
        assert listing(binary, links_tree, *args) == excluded_model(links_tree, drop)
