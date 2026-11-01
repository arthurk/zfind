"""Tests for lazy path construction in the wide directory walk."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from helpers import joined, run_in, words


# -----------------------------------------------------------------------------
# Lazy path construction in walk_wide
# -----------------------------------------------------------------------------

LAZY_ALL = words("""
    .git/ .gitignore .hidden/ .hidden/inside
    aa/ aa/bb/ aa/bb/cc/ aa/bb/cc/leaf.txt aa/bb/deep/
    aa/bb/deep/nested.txt aa/ignored/ aa/ignored/inside.txt
    aa/keep.log aa/local.log aa/other.tmp
    link_dir link_file pipe root.txt
""")

# --type f prints regular files only; symlinks and FIFOs are excluded.
LAZY_FILES = [
    path for path in LAZY_ALL
    if not path.endswith(b"/") and path not in (b"link_dir", b"link_file", b"pipe")
]

# --type d prints real directories only; symlinks to directories are excluded.
LAZY_DIRS = words("""
    .git/ .hidden/ aa/ aa/bb/ aa/bb/cc/ aa/bb/deep/ aa/ignored/
""")

# Root .gitignore: *.log, !keep.log, ignored/, aa/other.tmp.
LAZY_IGNORED = {
    b".git/", b"aa/ignored/", b"aa/ignored/inside.txt",
    b"aa/local.log", b"aa/other.tmp",
}
LAZY_GITIGNORED = [path for path in LAZY_ALL if path not in LAZY_IGNORED]

LAZY_CASES = [
    pytest.param((), LAZY_ALL, id="all"),
    pytest.param(("--type", "f"), LAZY_FILES, id="files"),
    pytest.param(("--type", "d"), LAZY_DIRS, id="dirs"),
    pytest.param(("--gitignore",), LAZY_GITIGNORED, id="gitignore"),
]


@pytest.fixture(scope="module")
def lazy_path_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Nested tree exercising unknown types, ignore rules and directory groups."""
    root = tmp_path_factory.mktemp("lazy-paths")
    (root / ".git").mkdir()
    (root / ".gitignore").write_bytes(b"*.log\n!keep.log\nignored/\naa/other.tmp\n")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "inside").write_bytes(b"")
    (root / "aa" / "bb" / "cc").mkdir(parents=True)
    (root / "aa" / "bb" / "cc" / "leaf.txt").write_bytes(b"")
    (root / "aa" / "bb" / "deep").mkdir()
    (root / "aa" / "bb" / "deep" / "nested.txt").write_bytes(b"")
    (root / "aa" / "ignored").mkdir()
    (root / "aa" / "ignored" / "inside.txt").write_bytes(b"")
    for name in ("keep.log", "local.log", "other.tmp"):
        (root / "aa" / name).write_bytes(b"")
    (root / "root.txt").write_bytes(b"")
    (root / "link_dir").symlink_to("aa")
    (root / "link_file").symlink_to("aa/other.tmp")
    try:
        os.mkfifo(root / "pipe")
    except OSError as exc:
        pytest.skip(f"cannot create FIFO on this filesystem: {exc}")
    return root


def _unknown_candidate(binary: Path) -> Path:
    """Sibling unknown-type build path for a normal or serial binary."""
    name = binary.parent.name
    if name.endswith("-serial"):
        name = name[:-len("-serial")]
    return binary.parent.parent / f"{name}-unknown" / "zfind"


@pytest.fixture(scope="session")
def unknown_zfind_path(zfind_path: Path) -> Path:
    """Path to the DT_UNKNOWN/FORCE_WIDE_SPLIT test build."""
    explicit = os.environ.get("ZFIND_UNKNOWN")
    candidates = [Path(explicit)] if explicit else []
    candidates.append(_unknown_candidate(zfind_path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("unknown-type build not found; run `make test` to build it")


class TestLazyPathConstruction:
    @pytest.mark.parametrize(("args", "expected"), LAZY_CASES)
    def test_parallel_listing(
        self,
        zfind_path: Path,
        lazy_path_tree: Path,
        args: tuple[str, ...],
        expected: list[bytes],
    ):
        res = run_in(zfind_path, lazy_path_tree, *args)
        assert res.status == 0, f"Run failed: {res}"
        assert res.err == b"", f"Run wrote stderr: {res}"
        assert res.out == joined(expected, b"\n")

    @pytest.mark.parametrize(("args", "expected"), LAZY_CASES)
    def test_unknown_type_listing(
        self,
        unknown_zfind_path: Path,
        lazy_path_tree: Path,
        args: tuple[str, ...],
        expected: list[bytes],
    ):
        res = run_in(unknown_zfind_path, lazy_path_tree, *args)
        assert res.status == 0, f"Run failed: {res}"
        assert res.err == b"", f"Run wrote stderr: {res}"
        assert res.out == joined(expected, b"\n")
