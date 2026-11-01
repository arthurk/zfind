"""Tests for zfind CLI flags, options, filtering, and error handling."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import Result, ZfindRunner

STRACE = shutil.which("strace")
requires_strace = pytest.mark.skipif(STRACE is None, reason="requires strace")


# -----------------------------------------------------------------------------
# Output Parsing Helpers
# -----------------------------------------------------------------------------

def words(text: str) -> list[bytes]:
    """Convert whitespace-separated listing string to byte-string list: 'a b/' -> [b'a', b'b/']"""
    return [word.encode() for word in text.split()]


def joined(entries: list[bytes], sep: bytes) -> bytes:
    """Unlimited output for entries: every result, including the last, terminated."""
    return b"".join(entry + sep for entry in entries)


def entries(out: bytes) -> list[bytes]:
    """Parse newline-separated output into individual entry byte-strings."""
    assert out == b"" or out.endswith(b"\n"), "Output must be newline-terminated or empty"
    return out.split(b"\n")[:-1]


def run_in(binary: Path, cwd: Path, *args: str, timeout: int = 60) -> Result:
    """Run zfind in an arbitrary directory (for temporary test trees)."""
    done = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)


def run_injected(
    binary: Path,
    cwd: Path,
    *args: str,
    inject: str,
    stdout=None,
    timeout: int = 60,
) -> Result:
    """Run zfind under strace with one injected syscall fault.

    The inject argument uses strace's syntax, for example
    "write:error=EINTR:when=1" or "write:retval=5:when=1".
    """
    assert STRACE is not None
    syscall = inject.split(":", 1)[0]
    done = subprocess.run(
        [
            STRACE, "-f", "-o", os.devnull,
            "-e", f"trace={syscall}",
            "-e", f"inject={inject}",
            str(binary), *args,
        ],
        cwd=cwd,
        timeout=timeout,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)


# -----------------------------------------------------------------------------
# Options shown in help output
# -----------------------------------------------------------------------------

CURRENT_HELP_OPTIONS = [
    b"--separator", b"--case-sensitive", b"--case-insensitive",
    b"--gitignore", b"--no-gitignore", b"--hidden", b"--no-hidden",
    b"--exclude-name", b"--limit", b"--type", b"--config", b"--no-config",
    b"--help",
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
        res = zfind("tiny", "pattern1", "pattern2")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unexpected argument\n"
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
        assert b"--null" not in out
        assert all(len(line) <= 80 for line in out.splitlines())


# -----------------------------------------------------------------------------
# Pattern Matching
# -----------------------------------------------------------------------------

class TestSortOrder:
    def test_results_are_sorted_for_names_with_equal_prefixes(self, zfind: ZfindRunner):
        listing = entries(zfind("camera").out)
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
        assert res.err == b"zfind: unexpected argument\n"
        assert res.out == b""


# -----------------------------------------------------------------------------
# Conflicting Flags (Mutual Exclusion)
# -----------------------------------------------------------------------------

class TestConflictingFlags:
    @pytest.mark.parametrize("flags, expected_err", [
        pytest.param(
            ["--case-sensitive", "--case-insensitive"],
            b"zfind: cannot combine --case-sensitive and --case-insensitive\n",
            id="case-conflict-sensitive-first",
        ),
        pytest.param(
            ["--case-insensitive", "--case-sensitive"],
            b"zfind: cannot combine --case-sensitive and --case-insensitive\n",
            id="case-conflict-insensitive-first",
        ),
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


@pytest.fixture
def repo_tree(tmp_path: Path) -> Path:
    """A repository whose .gitignore ignores ignored.txt."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").mkdir()
    (tree / ".gitignore").write_bytes(b"ignored.txt\nunused.tmp\n")
    (tree / "ignored.txt").write_bytes(b"")
    (tree / "keep.txt").write_bytes(b"")
    return tree


@requires_strace
class TestSyscallErrorRecovery:
    def test_write_eintr_is_retried(self, zfind_path: Path, repo_tree: Path):
        baseline = run_in(zfind_path, repo_tree)
        assert baseline.status == 0, f"Baseline run failed: {baseline}"

        res = run_injected(zfind_path, repo_tree, inject="write:error=EINTR:when=1")
        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert res.out == baseline.out

    def test_short_write_resumes_at_reported_offset(
        self, zfind_path: Path, repo_tree: Path, tmp_path: Path
    ):
        baseline = run_in(zfind_path, repo_tree)
        assert baseline.status == 0, f"Baseline run failed: {baseline}"
        assert len(baseline.out) > 5, f"Baseline unexpectedly short: {baseline}"

        # The injected write reports five bytes without writing them. Seeding
        # the file with those five bytes means a correct resume reproduces the
        # baseline exactly; restarting at offset zero would append it again.
        out = tmp_path / "stdout"
        out.write_bytes(baseline.out[:5])
        with out.open("ab") as stdout:
            res = run_injected(
                zfind_path, repo_tree, inject="write:retval=5:when=1", stdout=stdout
            )

        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert out.read_bytes() == baseline.out

    def test_read_eintr_is_retried(self, zfind_path: Path, repo_tree: Path):
        baseline = run_in(zfind_path, repo_tree, "--gitignore")
        assert baseline.status == 0, f"Baseline run failed: {baseline}"
        assert entries(baseline.out) == [b".gitignore", b"keep.txt"]

        res = run_injected(
            zfind_path, repo_tree, "--gitignore", inject="read:error=EINTR:when=1"
        )
        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert res.out == baseline.out

    def test_read_error_discards_buffered_gitignore(
        self, zfind_path: Path, repo_tree: Path
    ):
        # The first read returns the whole file; the second read fails. The
        # bytes already read must be discarded instead of parsed.
        res = run_injected(
            zfind_path, repo_tree, "--gitignore", inject="read:error=EIO:when=2"
        )
        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert entries(res.out) == [b".gitignore", b"ignored.txt", b"keep.txt"]


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
