"""Tests for the --type flag."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest
from helpers import ZfindRunner, entries, run_in, words


TINY = words("a b ca cb dir/ dir/c dir/d e")
TINY_FILES_ONLY = words("a b ca cb dir/c dir/d e")
TINY_DIRS_ONLY = words("dir/")

TYPE_ERROR = b"zfind: expected f, d or l after --type\n"


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
        assert b"--type f|d|l" in out
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


def model(
    root: Path,
    kinds: Optional[set[str]] = None,
    *,
    substring: bytes = b"",
    hidden: bool = True,
) -> list[bytes]:
    """What zfind must print: sorted by name per directory, depth first.

    kinds is a set of "f", "d", "l", or None for no type filter. Symlinks are
    never followed, and directories (only) end with "/".
    """
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
            if (kinds is None or kind in kinds) and substring in os.fsencode(entry.name):
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

    def test_all_three_types_equal_no_filter_when_there_are_no_special_files(
        self, binary: Path, links_tree: Path
    ):
        everything = listing(binary, links_tree)
        assert everything == model(links_tree, None)
        assert listing(binary, links_tree, "--type=f", "--type=d", "--type=l") == everything

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
        assert b"--type f|d|l" in res.out

    @pytest.mark.parametrize("arg", ["--type=L", "--type=ll", "--type=sym"])
    def test_other_spellings_of_l_are_rejected(self, binary: Path, links_tree: Path, arg: str):
        res = run_in(binary, links_tree, arg)
        assert res.status == 1
        assert res.err == TYPE_ERROR
        assert res.out == b""


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
