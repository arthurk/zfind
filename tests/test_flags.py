"""Tests for zfind CLI flags, options, filtering, and error handling."""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import NamedTuple

import pytest
from conftest import Result, ZfindRunner

DEV_FULL = Path("/dev/full")
STRACE = shutil.which("strace")
requires_strace = pytest.mark.skipif(STRACE is None, reason="requires strace")
UINT64_MAX = (1 << 64) - 1
UINT64_OVERFLOW = UINT64_MAX + 1
BUFFER_SIZE_64_KIB = 64 * 1024
BLK_DATA = 16384 - 32  # src/x86_64/zfind.asm: BLK_SZ - BLK_HDR


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


def nul_entries(out: bytes) -> list[bytes]:
    """Parse NUL-separated output into individual entry byte-strings."""
    assert out == b"" or out.endswith(b"\0"), "Output must be NUL-terminated or empty"
    return out.split(b"\0")[:-1]


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


def run_repeated(
    binary: Path, cwd: Path, *args: str, runs: int = 3, timeout: int = 60
) -> Result:
    """Run zfind several times and assert that every run is identical."""
    first = run_in(binary, cwd, *args, timeout=timeout)
    for run in range(2, runs + 1):
        again = run_in(binary, cwd, *args, timeout=timeout)
        assert again == first, (
            f"parallel run {run} for {args} differs from run 1: "
            f"first={first}; again={again}"
        )
    return first


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


def same_binary(first: Path, second: Path) -> bool:
    """Whether two configured paths select the same binary file."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


# -----------------------------------------------------------------------------
# Test Tree Expected Outputs (from mktrees.py fixtures)
# -----------------------------------------------------------------------------

TINY = words("a b ca cb dir/ dir/c dir/d e")
TINY_FILES_ONLY = words("a b ca cb dir/c dir/d e")
TINY_DIRS_ONLY = words("dir/")

HIDDEN = words(".dotdir/ .dotdir/.deep .dotdir/inside .dotfile sub/ sub/.dot2 sub/visible2 visible")
HIDDEN_SKIPPED = words("sub/ sub/visible2 visible")

IGNORES = words("""
    .git/ .gitignore .hidden a.o a.txt build/ build/b1 build/b2 build/inner/
    build/inner/b3 c.o c.txt skipme sub/ sub/.gitignore sub/build sub/keep.log
    sub/local/ sub/local/l1 sub/noise.log sub/skipme sub/x.o sub/z.txt tmp1
    tmp_keep tmp_other z.txt
""")

IGNORES_GITIGNORE = words("""
    .gitignore .hidden a.txt c.txt sub/ sub/.gitignore sub/build
    sub/keep.log sub/skipme sub/z.txt tmp_keep z.txt
""")

IGNORES_GITIGNORE_NO_HIDDEN = words(
    "a.txt c.txt sub/ sub/build sub/keep.log sub/skipme sub/z.txt tmp_keep z.txt"
)

WEIRD = [
    b"--limit", b"-0", b"-h", b"-t", b"a*b", b"back\\slash", b"c?d",
    b"caf\xc3\xa9", b"hi\xff", b"q\nr/", b"q\nr/s", b"sp ace", b"x\ny", b"z"
]

EXACT_LIMIT_SEPARATORS = [
    pytest.param("\\n", b"\n", id="newline"),
    pytest.param("\\t", b"\t", id="tab"),
    pytest.param("\\0", b"\0", id="nul"),
    pytest.param("XY", b"XY", id="xy"),
    pytest.param("aa", b"aa", id="aa"),
    pytest.param("tt", b"tt", id="tt"),
    pytest.param("bb", b"bb", id="bb"),
    pytest.param("/", b"/", id="slash"),
    pytest.param("S" * 64, b"S" * 64, id="64-byte"),
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


# -----------------------------------------------------------------------------
# Flag: --separator
# -----------------------------------------------------------------------------

class TestSeparatorHelp:
    def test_help_documents_separator_not_null(self, zfind: ZfindRunner):
        res = zfind("tiny", "--help")
        assert res.status == 0, f"Expected exit 0 for --help: {res}"
        assert b"--separator" in res.out
        assert b"--null" not in res.out
        assert res.err == b""

    def test_separator_then_help_is_prescanned(self, zfind: ZfindRunner):
        baseline = zfind("tiny", "--help")
        res = zfind("tiny", "--separator", "--help")
        assert res.status == 0, f"Expected exit 0 for --separator --help: {res}"
        assert res.out == baseline.out
        assert res.err == b""

    def test_separator_equals_help_is_literal(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=--help")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"".join(entry + b"--help" for entry in TINY)


class TestSeparatorParsing:
    @pytest.mark.parametrize("args, expected_err", [
        pytest.param(["--separator"], b"zfind: missing value for --separator\n", id="missing-value"),
        pytest.param(["--separator="], b"zfind: empty separator\n", id="empty-equals"),
        pytest.param(["--separator", ""], b"zfind: empty separator\n", id="empty-next-token"),
        pytest.param(["--separator=\\q"], b"zfind: unknown escape sequence in separator\n", id="unknown-escape"),
        pytest.param(["--separator", "\\"], b"zfind: incomplete escape sequence in separator\n", id="incomplete-escape"),
        pytest.param(["--separator=" + "x" * 65], b"zfind: separator too long (max 64 bytes)\n", id="too-long"),
    ])
    def test_separator_errors_are_distinct(self, zfind: ZfindRunner, args: list[str], expected_err: bytes):
        res = zfind("tiny", *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == expected_err
        assert res.out == b""

    def test_64_decoded_bytes_accepted(self, zfind: ZfindRunner):
        sep = b"x" * 64
        res = zfind("tiny", "--separator=" + sep.decode())
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"".join(entry + sep for entry in TINY)

    def test_65_decoded_bytes_rejected(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=" + "x" * 65)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: separator too long (max 64 bytes)\n"
        assert res.out == b""

    def test_64_escaped_bytes_accepted(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=" + "\\n" * 64)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"".join(entry + b"\n" * 64 for entry in TINY)

    @pytest.mark.parametrize("args", [
        pytest.param(["--separator=first", "--separator", "second"], id="equals-then-next"),
        pytest.param(["--separator", "first", "--separator=second"], id="next-then-equals"),
    ])
    def test_repeated_separator_last_wins(self, zfind: ZfindRunner, args: list[str]):
        res = zfind("tiny", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"".join(entry + b"second" for entry in TINY)

    def test_separator_consumes_dash_value(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "a").write_bytes(b"")
        res = run_in(zfind_path, tree, "--separator", "-x")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"a-x"

    def test_pattern_after_double_dash_with_custom_separator(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "-p").write_bytes(b"")
        (tree / "q").write_bytes(b"")
        res = run_in(zfind_path, tree, "--separator=XY", "--", "-p")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"-pXY"

    def test_separator_after_pattern_is_unexpected_argument(self, zfind: ZfindRunner):
        res = zfind("tiny", "a", "--separator=XY")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unexpected argument\n"
        assert res.out == b""

    def test_invalid_separator_fails_even_if_a_later_value_would_win(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=\\q", "--separator=OK")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unknown escape sequence in separator\n"
        assert res.out == b""

    def test_null_is_now_an_unknown_option(self, zfind: ZfindRunner):
        res = zfind("tiny", "--null")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: unknown option\n"
        assert res.out == b""


@pytest.fixture
def one_file_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a").write_bytes(b"")
    return tree


@pytest.fixture(scope="module")
def exact_limit_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """TINY's names, built outside testdata for separator/limit combinations."""
    tree = tmp_path_factory.mktemp("exact-limit-tree")
    for name in ("a", "b", "ca", "cb", "e"):
        (tree / name).write_bytes(b"")
    (tree / "dir").mkdir()
    (tree / "dir" / "c").write_bytes(b"")
    (tree / "dir" / "d").write_bytes(b"")
    return tree


@pytest.fixture(scope="module")
def xy_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tree = tmp_path_factory.mktemp("xy-tree")
    for name in ("aXYb", "c", "d"):
        (tree / name).write_bytes(b"")
    return tree


class TestSeparatorOutput:
    @pytest.mark.parametrize("sep_arg, sep_bytes", [
        pytest.param("\\n", b"\n", id="newline"),
        pytest.param("\\t", b"\t", id="tab"),
        pytest.param("\\r", b"\r", id="carriage-return"),
        pytest.param("\\0", b"\0", id="nul"),
        pytest.param("\\\\", b"\\", id="backslash"),
    ])
    def test_escape_decoding(self, zfind_path: Path, one_file_tree: Path, sep_arg: str, sep_bytes: bytes):
        res = run_in(zfind_path, one_file_tree, "--separator=" + sep_arg)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"a" + sep_bytes

    def test_octal_like_sequence_is_nul_then_literal_digits(self, zfind_path: Path, one_file_tree: Path):
        res = run_in(zfind_path, one_file_tree, "--separator=\\012")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"a\x0012"

    def test_real_newline_argv_matches_escape(self, zfind: ZfindRunner):
        escaped = zfind("tiny", "--separator=\\n")
        literal = zfind("tiny", "--separator", "\n")
        assert escaped.status == literal.status == 0
        assert escaped.err == literal.err == b""
        assert escaped.out == literal.out

    def test_multibyte_literal_separator_includes_directories(self, zfind: ZfindRunner):
        sep = b"XY"
        res = zfind("tiny", "--separator=XY")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"".join(entry + sep for entry in TINY)

    def test_final_result_is_terminated_including_nul(self, zfind: ZfindRunner):
        res = zfind("weird", "--separator=\\0")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out.endswith(b"\0")
        assert nul_entries(res.out) == WEIRD

    def test_nul_separator_with_pattern(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=\\0", "c")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"ca\0cb\0dir/c\0"

    def test_split_xy_output_on_tiny_matches_default_entries(self, zfind: ZfindRunner):
        # Specific case only: XY is absent from these names and is not formed
        # across their result boundaries. Do not generalize split equality to
        # arbitrary separators or filenames.
        default = zfind("tiny")
        custom = zfind("tiny", "--separator=XY")
        assert default.status == custom.status == 0
        assert custom.out.split(b"XY")[:-1] == entries(default.out)

    def test_no_results_writes_nothing(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=XY", "definitely_no_such_name")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b""

    def test_default_newline_mode_splits_embedded_newlines(self, zfind: ZfindRunner):
        res = zfind("weird")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out.count(b"\n") == len(WEIRD) + 3


ZFIND_PATH_MAX = 4096  # src/x86_64/zfind.asm: PATH_MAX


class RelativeChain(NamedTuple):
    tree: Path
    relative: str
    target_len: int
    name_max: int
    path_max: int
    at_target: bool


def _pathconf_limit(path: Path, name: str, fallback: int) -> int:
    value = os.pathconf(path, name)
    return value if value > 0 else fallback


def _plan_relative_chain(target_len: int, name_max: int) -> tuple[int, int, int] | None:
    """Split target_len into parent_count * (parent_len + 1) + leaf_len.

    Every component stays at or below NAME_MAX. None means this exact length
    cannot be represented with the chosen component size (only possible when
    NAME_MAX is 1 and the target is even).
    """
    component_len = min(name_max, 255)
    if component_len > 1:
        component_len -= 1
    parent_count, remainder = divmod(target_len - 1, component_len + 1)
    leaf_len = remainder + 1
    if leaf_len > name_max:
        return None
    return parent_count, component_len, leaf_len


def _build_relative_chain(
    tree: Path, parent_count: int, parent_name: str, leaf_name: str, leaf_is_dir: bool
) -> str:
    """Create nested dirs and a leaf without naming the full path in a syscall."""
    fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(parent_count):
            os.mkdir(parent_name, dir_fd=fd)
            child = os.open(parent_name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        if leaf_is_dir:
            os.mkdir(leaf_name, dir_fd=fd)
        else:
            os.close(os.open(leaf_name, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    finally:
        os.close(fd)
    return "/".join([parent_name] * parent_count + [leaf_name])


def create_relative_chain(base: Path, leaf_is_dir: bool) -> RelativeChain:
    """Build the longest relative path this filesystem permits, up to 4095 bytes.

    The target is min(PC_PATH_MAX - 1, 4095): 4095 is the longest path zfind's
    PATH_MAX buffer can hold. Creation is dir_fd-relative, so no syscall needs
    the full host path. If the full target cannot be built, binary-search the
    largest constructible length and use that; every probe gets a fresh tree
    directory so a partial chain is never traversed by zfind.
    """
    name_max = _pathconf_limit(base, "PC_NAME_MAX", 255)
    path_max = _pathconf_limit(base, "PC_PATH_MAX", ZFIND_PATH_MAX)
    target_len = min(path_max - 1, ZFIND_PATH_MAX - 1)
    if target_len < 1:
        pytest.fail(f"invalid path limits: PC_PATH_MAX={path_max}")

    attempts = 0

    def attempt(length: int) -> tuple[Path, str] | None:
        nonlocal attempts
        plan = _plan_relative_chain(length, name_max)
        tree = base / f"tree{attempts}"
        attempts += 1
        if plan is None:
            return None
        tree.mkdir()
        parent_count, component_len, leaf_len = plan
        try:
            relative = _build_relative_chain(
                tree,
                parent_count,
                "d" * component_len,
                "z" * leaf_len,
                leaf_is_dir,
            )
        except OSError:
            return None
        return tree, relative

    best = attempt(target_len)
    at_target = best is not None
    if best is None:
        lo, hi = 0, target_len
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            candidate = attempt(mid)
            if candidate is None:
                hi = mid
            else:
                best = candidate
                lo = mid
        if best is None:
            best = attempt(1)
    if best is None:
        pytest.fail(
            "could not construct a relative path under "
            f"{base}: PC_PATH_MAX={path_max}, PC_NAME_MAX={name_max}"
        )

    tree, relative = best
    assert 0 < len(relative) <= target_len
    if at_target:
        assert len(relative) == target_len
    return RelativeChain(tree, relative, target_len, name_max, path_max, at_target)


class TestSeparatorLongPath:
    @pytest.mark.parametrize("leaf_is_dir", [False, True], ids=["file", "directory"])
    def test_path_max_minus_one_with_64_byte_separator(
        self, zfind_path: Path, serial_zfind_path: Path, serial_binary_path: Path,
        tmp_path: Path, leaf_is_dir: bool,
    ):
        chain = create_relative_chain(tmp_path, leaf_is_dir=leaf_is_dir)
        if not chain.at_target:
            warnings.warn(
                "could not create the full-length relative path; testing the "
                f"largest constructible path instead: effective length "
                f"{len(chain.relative)}, target {chain.target_len}, "
                f"PC_NAME_MAX={chain.name_max}, PC_PATH_MAX={chain.path_max}",
                stacklevel=2,
            )
        same = same_binary(zfind_path, serial_binary_path)
        sep = b"S" * 64
        args = (
            "--type", "d" if leaf_is_dir else "f",
            "--separator=" + sep.decode(),
            "--limit", "1",
            "z",
        )

        parallel = run_repeated(zfind_path, chain.tree, *args)
        expected = chain.relative.encode() + (b"/" if leaf_is_dir else b"") + sep
        context = (
            f"effective relative path length {len(chain.relative)} "
            f"(target {chain.target_len}; PC_PATH_MAX={chain.path_max}, "
            f"PC_NAME_MAX={chain.name_max})"
        )
        assert parallel.status == 0, f"parallel run failed ({context}): {parallel}"
        assert parallel.err == b"", f"parallel run wrote stderr ({context}): {parallel}"
        assert parallel.out == expected, f"parallel output mismatch ({context}): {parallel}"
        if same:
            # Both fixtures select the same executable, so serial/parallel
            # parity would compare the run above with itself.
            return
        serial = run_in(serial_zfind_path, chain.tree, *args)
        assert serial.status == 0, f"serial run failed ({context}): {serial}"
        assert serial.err == b"", f"serial run wrote stderr ({context}): {serial}"
        assert serial.out == expected, f"serial output mismatch ({context}): {serial}"
        assert parallel == serial, f"parallel and serial differ ({context})"


# -----------------------------------------------------------------------------
# Flags: --type (f | d)
# -----------------------------------------------------------------------------

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
    ])
    def test_invalid_type_fails(self, zfind: ZfindRunner, args: list[str]):
        res = zfind("tiny", *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == b"zfind: expected f or d after --type\n"
        assert res.out == b""


# -----------------------------------------------------------------------------
# Pattern Matching: Smart Case, --case-sensitive, --case-insensitive
# -----------------------------------------------------------------------------

class TestCaseSensitivity:
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


# -----------------------------------------------------------------------------
# Conflicting Flags (Mutual Exclusion)
# -----------------------------------------------------------------------------

class TestConflictingFlags:
    @pytest.mark.parametrize("flags, expected_err", [
        pytest.param(
            ["--case-sensitive", "--case-insensitive"],
            b"zfind: cannot combine --case-sensitive and --case-insensitive\n",
            id="case-conflict",
        ),
        pytest.param(
            ["--hidden", "--no-hidden"],
            b"zfind: cannot combine --hidden and --no-hidden\n",
            id="hidden-conflict",
        ),
        pytest.param(
            ["--gitignore", "--no-gitignore"],
            b"zfind: cannot combine --gitignore and --no-gitignore\n",
            id="gitignore-conflict",
        ),
    ])
    def test_mutually_exclusive_flags_fail(
        self, zfind: ZfindRunner, flags: list[str], expected_err: bytes
    ):
        res = zfind("tiny", *flags)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.err == expected_err
        assert res.out == b""


# -----------------------------------------------------------------------------
# Flags: --hidden & --no-hidden
# -----------------------------------------------------------------------------

class TestNoHidden:
    @pytest.mark.parametrize("args, expected", [
        pytest.param([], HIDDEN, id="default-includes-hidden"),
        pytest.param(["--hidden"], HIDDEN, id="explicit-hidden-flag"),
        pytest.param(["--no-hidden"], HIDDEN_SKIPPED, id="no-hidden-skips-dotfiles"),
    ])
    def test_hidden_file_visibility(self, zfind: ZfindRunner, args: list[str], expected: list[bytes]):
        res = zfind("hidden", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == expected

    def test_no_hidden_does_not_enter_hidden_directories(self, zfind: ZfindRunner):
        res = zfind("hidden", "--no-hidden")
        assert res.status == 0, f"Command failed: {res}"
        listed = entries(res.out)
        assert len(listed) == len(HIDDEN_SKIPPED)
        assert not any(b"inside" in entry for entry in listed)


# -----------------------------------------------------------------------------
# Flags: --gitignore & --no-gitignore
# -----------------------------------------------------------------------------

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

    @pytest.mark.parametrize("entry, expected_listed", [
        pytest.param(b"a.txt", True, id="no-rule-matches"),
        pytest.param(b"a.o", False, id="wildcard-*.o"),
        pytest.param(b"sub/x.o", False, id="wildcard-sub/*.o"),
        pytest.param(b"build/", False, id="directory-build/"),
        pytest.param(b"build/b1", False, id="descendant-of-ignored-directory"),
        pytest.param(b"sub/build", True, id="directory-rule-does-not-match-file"),
        pytest.param(b"skipme", False, id="root-anchored-/skipme"),
        pytest.param(b"sub/skipme", True, id="root-anchored-rule-skips-subdir"),
        pytest.param(b"tmp1", False, id="wildcard-tmp*"),
        pytest.param(b"tmp_keep", True, id="negated-rule-!tmp_keep"),
        pytest.param(b"sub/noise.log", False, id="nested-.gitignore-*.log"),
        pytest.param(b"sub/keep.log", True, id="nested-.gitignore-negated-!keep.log"),
        pytest.param(b"sub/local/", False, id="nested-.gitignore-directory"),
    ])
    def test_individual_rules(self, zfind: ZfindRunner, entry: bytes, expected_listed: bool):
        res = zfind("ignores", "--gitignore", "--no-hidden")
        assert res.status == 0, f"Command failed: {res}"
        listed = entries(res.out)
        assert len(listed) > 0, "Output unexpectedly empty"
        assert (entry in listed) is expected_listed


# -----------------------------------------------------------------------------
# Flag: --limit
# -----------------------------------------------------------------------------

def calculate_buffer_boundary_sizes(listing: list[bytes], sep_len: int = 1) -> list[int]:
    """Calculate limit counts around boundaries: ends and 64 KiB buffer overflow."""
    size, passed = 0, None
    for count, entry in enumerate(listing, 1):
        size += len(entry) + sep_len
        if size >= BUFFER_SIZE_64_KIB and passed is None:
            passed = count

    assert passed is not None, "Test tree is too small to test buffer boundaries"
    total = len(listing)
    return sorted({1, 2, passed - 1, passed, passed + 1, total - 1, total, total + 1})


class TestLimit:
    # Equivalence Partitions: 0 (early exit), 1 (min boundary), 3 (interior), 8 (exact total), 9 (overflow)
    @pytest.mark.parametrize("n", [0, 1, 3, 8, 9])
    def test_limit_prints_first_n_entries(self, zfind: ZfindRunner, n: int):
        res = zfind("tiny", "--limit", str(n))
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == TINY[:n]

    @pytest.mark.parametrize("args, expected", [
        pytest.param(["--limit=2"], TINY[:2], id="equals-syntax"),
        pytest.param(["--limit", "1", "--limit", "3"], TINY[:3], id="last-limit-wins"),
        pytest.param(["--limit", str(UINT64_MAX)], TINY, id="uint64-max-effectively-unlimited"),
        pytest.param(["--limit", "2", "c"], [b"ca", b"cb"], id="limit-with-pattern"),
    ])
    def test_limit_argument_handling(self, zfind: ZfindRunner, args: list[str], expected: list[bytes]):
        res = zfind("tiny", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == expected

    def test_limit_with_nul_separator_counts_entries(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=\\0", "--limit", "2")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"a\0b\0"

    @pytest.mark.parametrize("sep_args, sep_bytes", [
        pytest.param((), b"\n", id="newline"),
        pytest.param(("--separator=\\t",), b"\t", id="tab"),
        pytest.param(("--separator=\\0",), b"\0", id="nul"),
        pytest.param(("--separator=XY",), b"XY", id="multi-byte"),
    ])
    def test_limit_prints_exactly_n_results(
        self, zfind: ZfindRunner, sep_args: tuple[str, ...], sep_bytes: bytes
    ):
        res = zfind("tiny", *sep_args, "--limit", "3")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"".join(entry + sep_bytes for entry in TINY[:3])

    def test_limit_zero_prints_nothing_with_separator(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=XY", "--limit", "0")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b""

    def test_limit_larger_than_count_prints_everything(self, zfind: ZfindRunner):
        full = zfind("tiny", "--separator=XY")
        res = zfind("tiny", "--separator=XY", "--limit", "100")
        assert full.status == res.status == 0
        assert res.out == full.out

    def test_limit_mid_chunk_keeps_multibyte_separator_whole(self, zfind: ZfindRunner):
        res = zfind("tiny", "--separator=LL", "--limit", "3")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"aLLbLLcaLL"

    @pytest.mark.parametrize("args, expected", [
        pytest.param(["--limit", "3"], words(".git/ .gitignore .hidden"), id="counts-hidden-by-default"),
        pytest.param(["--gitignore", "--no-hidden", "--limit", "3"], words("a.txt c.txt sub/"), id="skips-ignored"),
    ])
    def test_limit_respects_visibility_filtering(
        self, zfind: ZfindRunner, args: list[str], expected: list[bytes]
    ):
        res = zfind("ignores", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == expected

    def test_limit_newline_mode_counts_results_not_embedded_newlines(self, zfind: ZfindRunner):
        res = zfind("weird", "--limit", "10")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == joined(WEIRD[:10], b"\n")
        assert b"q\nr/\n" in res.out

    def test_limit_with_nul_never_cuts_embedded_newlines(self, zfind: ZfindRunner):
        res = zfind("weird", "--separator=\\0", "--limit", "10")
        assert res.status == 0, f"Command failed: {res}"
        assert nul_entries(res.out) == WEIRD[:10]

    @pytest.mark.parametrize("args", [
        pytest.param(["--limit"], id="missing-arg"),
        pytest.param(["--limit", "x"], id="non-numeric"),
        pytest.param(["--limit", "-1"], id="negative"),
        pytest.param(["--limit", "1x"], id="trailing-alphanumeric"),
        pytest.param(["--limit="], id="empty-assignment"),
        pytest.param(["--limit", "123456789012345678901234567890"], id="large-overflow"),
        pytest.param(["--limit", str(UINT64_OVERFLOW)], id="uint64-plus-one"),
        pytest.param(["--limit", "--type"], id="subsequent-flag-as-value"),
    ])
    def test_invalid_limit_values_fail(self, zfind: ZfindRunner, args: list[str]):
        res = zfind("tiny", *args)
        assert res.status == 1, f"Expected clean exit code 1: {res}"
        assert res.err == b"zfind: invalid limit\n"
        assert res.out == b""

    @pytest.mark.parametrize("tree", ["camera", "tree2"])
    def test_limit_prints_exact_prefix_of_full_output(self, zfind: ZfindRunner, tree: str):
        base_res = zfind(tree)
        assert base_res.status == 0, f"Baseline tree walk failed: {base_res}"
        full_listing = entries(base_res.out)
        assert len(full_listing) > 0, "Tree listing unexpectedly empty"

        for n in calculate_buffer_boundary_sizes(full_listing):
            res_nl = zfind(tree, "--limit", str(n))
            assert res_nl.status == 0, f"Failed at --limit {n}: {res_nl}"
            assert entries(res_nl.out) == full_listing[:n], f"Wrong entries at --limit {n}"

        full_nul = zfind(tree, "--separator=\\0")
        assert full_nul.status == 0, f"NUL baseline failed: {full_nul}"
        assert nul_entries(full_nul.out) == full_listing
        for n in calculate_buffer_boundary_sizes(full_listing):
            expected = b"".join(entry + b"\0" for entry in full_listing[:n])
            res_nul = zfind(tree, "--separator=\\0", "--limit", str(n))
            assert res_nul.status == 0, f"Failed at NUL --limit {n}: {res_nul}"
            assert res_nul.out == expected == full_nul.out[:len(expected)]

        sep = b"XY"
        full_xy = zfind(tree, "--separator=XY")
        assert full_xy.status == 0, f"multi-byte baseline failed: {full_xy}"
        assert full_xy.out == joined(full_listing, sep)
        for n in calculate_buffer_boundary_sizes(full_listing, len(sep)):
            expected = joined(full_listing[:n], sep)
            res_xy = zfind(tree, "--separator=XY", "--limit", str(n))
            assert res_xy.status == 0, f"Failed at XY --limit {n}: {res_xy}"
            assert res_xy.out == expected == full_xy.out[:len(expected)]

    def test_separator_inside_name_does_not_count_as_a_result(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ("aXYb", "c", "d"):
            (tree / name).write_bytes(b"")

        full = run_in(zfind_path, tree, "--separator=XY")
        assert full.status == 0, f"Baseline failed: {full}"
        assert full.out == b"aXYbXYcXYdXY"

        capped = run_in(zfind_path, tree, "--separator=XY", "--limit", "2")
        assert capped.status == 0, f"Capped run failed: {capped}"
        # The XY inside aXYb is part of one result, not a result boundary.
        assert capped.out == b"aXYbXYcXY"

    def test_overlapping_separator_does_not_truncate_last_result(
        self, zfind_path: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "xa").write_bytes(b"")

        full = run_in(zfind_path, tree, "--separator=aa")
        assert full.status == 0, f"Baseline failed: {full}"
        assert full.out == b"xaaa"

        capped = run_in(zfind_path, tree, "--separator=aa", "--limit", "1")
        assert capped.status == 0, f"Capped run failed: {capped}"
        assert capped.out == b"xaaa"

    @pytest.mark.parametrize("sep_arg, sep_bytes", EXACT_LIMIT_SEPARATORS)
    @pytest.mark.parametrize("n", [0, 1, 2, 3, 7, 8, 9])
    def test_exact_limit_prefix_for_every_separator(
        self, zfind_path: Path, exact_limit_tree: Path,
        sep_arg: str, sep_bytes: bytes, n: int,
    ):
        full = run_in(zfind_path, exact_limit_tree, "--separator=" + sep_arg)
        assert full.status == 0, f"Baseline failed: {full}"
        assert full.err == b""
        assert full.out == joined(TINY, sep_bytes)

        res = run_in(
            zfind_path, exact_limit_tree, "--separator=" + sep_arg, "--limit", str(n)
        )
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        expected = joined(TINY[:n], sep_bytes)
        assert res.out == expected
        if n >= len(TINY):
            assert res.out == full.out
        if n == 0:
            assert res.out == b""

    @pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
    def test_name_containing_separator_is_one_result(
        self, zfind_path: Path, serial_zfind_path: Path, serial_binary_path: Path,
        xy_tree: Path, n: int
    ):
        entries_xy = [b"aXYb", b"c", b"d"]
        same = same_binary(zfind_path, serial_binary_path)
        full = run_in(zfind_path, xy_tree, "--separator=XY")
        assert full.status == 0, f"parallel={full}"
        assert full.err == b"", f"parallel={full}"
        assert full.out == b"aXYbXYcXYdXY"
        if not same:
            full_serial = run_in(serial_zfind_path, xy_tree, "--separator=XY")
            assert full.status == full_serial.status == 0, f"parallel={full}; serial={full_serial}"
            assert full.err == full_serial.err == b"", f"parallel={full}; serial={full_serial}"
            assert full.out == full_serial.out

        res = run_repeated(zfind_path, xy_tree, "--separator=XY", "--limit", str(n))
        expected = joined(entries_xy[:n], b"XY")
        assert res.status == 0, f"parallel={res}"
        assert res.err == b"", f"parallel={res}"
        assert res.out == expected
        if n >= len(entries_xy):
            assert res.out == full.out
        if same:
            # Both fixtures select the same executable, so only the exact-output
            # assertions above are meaningful; parity would be self-comparison.
            return
        serial = run_in(serial_zfind_path, xy_tree, "--separator=XY", "--limit", str(n))
        assert res.status == serial.status == 0, f"parallel={res}; serial={serial}"
        assert res.err == serial.err == b"", f"parallel={res}; serial={serial}"
        assert res.out == serial.out

    def test_newline_inside_name_is_one_result(self, zfind_path: Path, tmp_path: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        for name in ("a", "q\nr", "z"):
            (tree / name).write_bytes(b"")

        full = run_in(zfind_path, tree, "--separator=\\n")
        assert full.status == 0, f"Baseline failed: {full}"
        assert full.out == b"a\nq\nr\nz\n"

        capped = run_in(zfind_path, tree, "--separator=\\n", "--limit", "2")
        assert capped.status == 0, f"Capped run failed: {capped}"
        assert capped.out == b"a\nq\nr\n"

    @pytest.mark.parametrize("limit_args", [
        pytest.param(("--limit", str(UINT64_MAX)), id="separate-value"),
        pytest.param(("--limit=" + str(UINT64_MAX),), id="equals-syntax"),
    ])
    def test_uint64_max_limit_equals_full_output(
        self, zfind_path: Path, exact_limit_tree: Path, limit_args: tuple[str, ...]
    ):
        full = run_in(zfind_path, exact_limit_tree, "--separator=XY")
        res = run_in(
            zfind_path, exact_limit_tree, "--separator=XY", *limit_args,
        )
        # UINT64_MAX is framed like any other positive limit even though its
        # counter never reaches zero. Framing is internal to the output blocks
        # and cannot be observed through stdout, so this asserts full output.
        assert full.status == res.status == 0
        assert full.out == joined(TINY, b"XY")
        assert res.err == b""
        assert res.out == full.out

    @pytest.mark.parametrize(("tree", "listing", "sep", "n"), [
        pytest.param("tiny", TINY, b"aa", 1, id="tiny-aa-limit-1"),
        pytest.param("tiny", TINY, b"aa", 3, id="tiny-aa-limit-3"),
        pytest.param("ignores", IGNORES, b"tt", 5, id="ignores-tt-limit-5"),
        pytest.param("ignores", IGNORES, b"tt", 27, id="ignores-tt-limit-27"),
        pytest.param("weird", WEIRD, b"bb", 6, id="weird-bb-limit-6"),
        pytest.param("weird", WEIRD, b"bb", 14, id="weird-bb-limit-14"),
    ])
    def test_reviewed_occurrence_count_regressions(
        self, zfind: ZfindRunner, tree: str, listing: list[bytes], sep: bytes, n: int
    ):
        res = zfind(tree, "--separator=" + sep.decode(), "--limit", str(n))
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == joined(listing[:n], sep)

    def test_directory_result_keeps_trailing_slash_before_separator(
        self, zfind: ZfindRunner
    ):
        res = zfind("tiny", "--type", "d", "--separator=/", "--limit", "1")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == b"dir//"

    @pytest.mark.parametrize("sep_arg, sep_bytes", EXACT_LIMIT_SEPARATORS)
    @pytest.mark.parametrize("n", [0, 1, 2, 3, 7, 8, 9])
    def test_exact_limit_serial_matches_parallel(
        self, zfind_path: Path, serial_zfind_path: Path, serial_binary_path: Path,
        exact_limit_tree: Path, sep_arg: str, sep_bytes: bytes, n: int,
    ):
        args = ("--separator=" + sep_arg, "--limit", str(n))
        parallel = run_repeated(zfind_path, exact_limit_tree, *args)
        assert parallel.status == 0, f"parallel={parallel}"
        assert parallel.err == b"", f"parallel={parallel}"
        assert parallel.out == joined(TINY[:n], sep_bytes)
        if same_binary(zfind_path, serial_binary_path):
            # Both fixtures select the same executable; keep the exact-output
            # assertion above but do not compare the run with itself.
            return
        serial = run_in(serial_zfind_path, exact_limit_tree, *args)
        assert parallel.status == serial.status == 0, f"parallel={parallel}; serial={serial}"
        assert parallel.err == serial.err == b"", f"parallel={parallel}; serial={serial}"
        assert parallel.out == serial.out, f"parallel={parallel}; serial={serial}"

    @pytest.mark.parametrize(("tree", "args"), [
        pytest.param("tree2", (), id="wide-tree"),
        pytest.param("weird", ("--separator=\\0",), id="newline-and-byte-names"),
        pytest.param("ignores", ("--gitignore", "--no-hidden"), id="gitignore-and-hidden"),
        pytest.param("camera", ("--limit", "100"), id="limited-output"),
        pytest.param("camera", ("--separator=XY", "--limit", "100"), id="multi-byte-separator-limited"),
        pytest.param("tiny", ("--separator=aa", "--limit", "1"), id="overlapping-aa-limit-1"),
        pytest.param("tiny", ("--separator=aa", "--limit", "3"), id="overlapping-aa-limit-3"),
        pytest.param("ignores", ("--separator=tt", "--limit", "5"), id="ignores-tt-limit-5"),
        pytest.param("ignores", ("--separator=tt", "--limit", "27"), id="ignores-tt-limit-27"),
        pytest.param("weird", ("--separator=bb", "--limit", "6"), id="weird-bb-limit-6"),
        pytest.param("weird", ("--separator=bb", "--limit", "14"), id="weird-bb-limit-14"),
        pytest.param("tiny", ("--type", "d", "--separator=/", "--limit", "1"), id="directory-slash-limit"),
        pytest.param("tiny", ("--limit", str(UINT64_MAX)), id="uint64-max"),
    ])
    def test_parallel_output_matches_serial(
        self, zfind: ZfindRunner, serial_zfind: ZfindRunner,
        zfind_path: Path, serial_binary_path: Path,
        tree: str, args: tuple[str, ...],
    ):
        if same_binary(zfind_path, serial_binary_path):
            pytest.skip(
                "selected zfind binary is the serial build; serial/parallel "
                "parity would compare the same executable"
            )
        parallel = zfind(tree, *args)
        serial = serial_zfind(tree, *args)
        assert parallel.status == serial.status == 0, f"parallel={parallel}; serial={serial}"
        assert parallel == serial, f"parallel={parallel}; serial={serial}"

    @pytest.mark.parametrize(("tree", "args"), [
        pytest.param("tree2", ("--limit", "100"), id="wide-tree"),
        pytest.param("ignores", ("--separator=tt", "--limit", "5"), id="ignores-tt-limit-5"),
        pytest.param("weird", ("--separator=bb", "--limit", "14"), id="weird-bb-limit-14"),
        pytest.param("tiny", ("--separator=aa", "--limit", "3"), id="tiny-aa-limit-3"),
    ])
    def test_limit_output_is_deterministic_under_concurrency(
        self, zfind: ZfindRunner, tree: str, args: tuple[str, ...]
    ):
        outputs = set()
        for i in range(20):
            res = zfind(tree, *args)
            assert res.status == 0, f"Run {i} failed: {res}"
            assert len(res.out) > 0, f"Run {i} produced empty output: {res}"
            outputs.add(res.out)
        assert len(outputs) == 1

    @pytest.mark.skipif(not DEV_FULL.exists(), reason="Requires /dev/full (Unix only)")
    def test_failed_stdout_write_is_reported(self, zfind: ZfindRunner):
        with open(DEV_FULL, "wb") as full:
            res = zfind("tiny", "--limit", "1", stdout=full)

        assert res.status == 1, f"Expected clean exit code 1 on write error: {res}"
        assert res.err == b"zfind: write error\n"
        assert res.out == b""


@pytest.fixture(scope="module")
def flat_131072(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A flat directory with exactly RANGE_MIN (131072) entries."""
    tree = tmp_path_factory.mktemp("flat-131072")
    for i in range(131072):
        fd = os.open(tree / f"f{i:06d}", os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    return tree


class TestSeparatorRangeGroups:
    def test_flat_131072_full_output_and_limited_prefix(
        self, zfind_path: Path, serial_binary_path: Path, flat_131072: Path
    ):
        # flat_131072 has exactly RANGE_MIN entries. With the normal parallel
        # binary (nthreads > 1) this directory is range-split into ordered
        # slices by try_range_split; the serial build walks it directly. The
        # limit/order assertions below therefore cover range-group boundaries
        # when a parallel binary is selected.
        if same_binary(zfind_path, serial_binary_path):
            pytest.skip(
                "selected zfind binary is the serial build; it cannot exercise "
                "range groups"
            )
        if not serial_binary_path.is_file():
            pytest.skip(
                f"Serial binary not built: {serial_binary_path}; "
                "run `make test` to build it"
            )

        names = [f"f{i:06d}".encode() for i in range(131072)]
        full = run_in(zfind_path, flat_131072, "--separator=XY", timeout=120)
        assert full.status == 0, f"Full run failed: {full}"
        assert full.err == b""
        assert full.out.endswith(b"XY")
        assert full.out.count(b"XY") == 131072
        assert len(full.out) == 131072 * (7 + 2)
        assert full.out == joined(names, b"XY")

        parsed = full.out.split(b"XY")[:-1]
        assert parsed == names
        assert parsed[:2048] == names[:2048]
        assert parsed[2047] == b"f002047"

        for n in (2047, 2048, 2049, 4096, 131071, 131072):
            args = ("--separator=XY", "--limit", str(n))
            if n in (2048, 131072):
                res = run_repeated(zfind_path, flat_131072, *args, timeout=120)
            else:
                res = run_in(zfind_path, flat_131072, *args, timeout=120)
            serial = run_in(serial_binary_path, flat_131072, *args, timeout=120)
            assert res.status == serial.status == 0, (
                f"Limited run at {n} failed: parallel={res}; serial={serial}"
            )
            assert res.err == serial.err == b"", (
                f"Limited run at {n} wrote stderr: parallel={res}; serial={serial}"
            )
            expected = full.out[:n * (7 + len(b"XY"))]
            assert res.out == serial.out == expected, f"Wrong prefix at --limit {n}"
            assert res.out.split(b"XY")[:-1] == names[:n]


@pytest.fixture(scope="module")
def block_one_byte_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """5000 five-byte files plus one directory inserted at the block boundary.

    With a one-byte separator each file frame is 2 + 5 + 1 = 8 bytes, so the
    first 2044 frames exactly fill BLK_DATA (16352). f2043d sorts between
    f2043 and f2044 and is a directory, so its 10-byte frame starts the next
    block (2 + 6 + 1 + 1).
    """
    tree = tmp_path_factory.mktemp("block-one-byte")
    for i in range(5000):
        (tree / f"f{i:04d}").write_bytes(b"")
    (tree / "f2043d").mkdir()
    return tree


@pytest.fixture(scope="module")
def block_multibyte_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """1808 files, 8 directories, then the boundary directory, then 100 files.

    With separator XY, file frames are 2 + 5 + 2 = 9 bytes and directory
    frames are 2 + 5 + 1 + 2 = 10. The first 1808 files and 8 directories
    total 1808 * 9 + 8 * 10 = 16352 bytes, so the next directory line starts
    exactly at the second block.
    """
    tree = tmp_path_factory.mktemp("block-multibyte")
    for i in range(1808):
        (tree / f"a{i:04d}").write_bytes(b"")
    for i in range(8):
        (tree / f"b{i:04d}").mkdir()
    (tree / "c0000").mkdir()
    for i in range(100):
        (tree / f"d{i:04d}").write_bytes(b"")
    return tree


class TestSeparatorBlockBoundaries:
    def test_one_byte_frames_fill_and_cross_block_data(
        self, zfind_path: Path, serial_zfind_path: Path, serial_binary_path: Path,
        block_one_byte_tree: Path
    ):
        files = [f"f{i:04d}".encode() for i in range(5000)]
        listing = files[:2044] + [b"f2043d/"] + files[2044:]
        sep = b"\t"
        assert 2 + len(b"f0000") + len(sep) == 8
        assert 2 + len(b"f2043d/") + len(sep) == 10
        assert 2044 * 8 == BLK_DATA
        assert listing[2044] == b"f2043d/"
        same = same_binary(zfind_path, serial_binary_path)

        full = run_in(zfind_path, block_one_byte_tree, "--separator=\\t")
        assert full.status == 0, f"Full run failed: parallel={full}"
        assert full.err == b"", f"Full run wrote stderr: parallel={full}"
        assert full.out == joined(listing, sep)
        if not same:
            full_serial = run_in(serial_zfind_path, block_one_byte_tree, "--separator=\\t")
            assert full.status == full_serial.status == 0, (
                f"Full run failed: parallel={full}; serial={full_serial}"
            )
            assert full.err == full_serial.err == b""
            assert full.out == full_serial.out

        for n in (2043, 2044, 2045, 2046, 4090, len(listing) - 1, len(listing),
                  len(listing) + 1):
            args = ("--separator=\\t", "--limit", str(n))
            if n in (2044, 2045):
                res = run_repeated(zfind_path, block_one_byte_tree, *args)
            else:
                res = run_in(zfind_path, block_one_byte_tree, *args)
            expected = joined(listing[:n], sep)
            assert res.status == 0, f"Limited run at {n} failed: parallel={res}"
            assert res.err == b"", f"Limited run at {n} wrote stderr: parallel={res}"
            assert res.out == expected, f"Wrong prefix at --limit {n}"
            assert res.out == full.out[:len(expected)]
            if same:
                # Both fixtures select the same executable; skip only the
                # self-comparison that would otherwise claim parity coverage.
                continue
            serial = run_in(serial_zfind_path, block_one_byte_tree, *args)
            assert res.status == serial.status == 0, (
                f"Limited run at {n} failed: parallel={res}; serial={serial}"
            )
            assert res.err == serial.err == b"", (
                f"Limited run at {n} wrote stderr: parallel={res}; serial={serial}"
            )
            assert res.out == serial.out, f"Wrong prefix at --limit {n}"

    def test_multibyte_separator_and_directory_line_at_block_boundary(
        self, zfind_path: Path, serial_zfind_path: Path, serial_binary_path: Path,
        block_multibyte_tree: Path
    ):
        listing = (
            [f"a{i:04d}".encode() for i in range(1808)]
            + [f"b{i:04d}/".encode() for i in range(8)]
            + [b"c0000/"]
            + [f"d{i:04d}".encode() for i in range(100)]
        )
        sep = b"XY"
        assert 2 + len(b"a0000") + len(sep) == 9
        assert 2 + len(b"b0000/") + len(sep) == 10
        assert 1808 * 9 + 8 * 10 == BLK_DATA
        assert listing[1815] == b"b0007/"
        assert listing[1816] == b"c0000/"
        same = same_binary(zfind_path, serial_binary_path)

        full = run_in(zfind_path, block_multibyte_tree, "--separator=XY")
        assert full.status == 0, f"Full run failed: parallel={full}"
        assert full.err == b"", f"Full run wrote stderr: parallel={full}"
        assert full.out == joined(listing, sep)
        if not same:
            full_serial = run_in(serial_zfind_path, block_multibyte_tree, "--separator=XY")
            assert full.status == full_serial.status == 0, (
                f"Full run failed: parallel={full}; serial={full_serial}"
            )
            assert full.err == full_serial.err == b""
            assert full.out == full_serial.out

        for n in (1815, 1816, 1817, 1818, len(listing) - 1, len(listing),
                  len(listing) + 1):
            args = ("--separator=XY", "--limit", str(n))
            if n in (1816, 1817):
                res = run_repeated(zfind_path, block_multibyte_tree, *args)
            else:
                res = run_in(zfind_path, block_multibyte_tree, *args)
            expected = joined(listing[:n], sep)
            assert res.status == 0, f"Limited run at {n} failed: parallel={res}"
            assert res.err == b"", f"Limited run at {n} wrote stderr: parallel={res}"
            assert res.out == expected, f"Wrong prefix at --limit {n}"
            assert res.out == full.out[:len(expected)]
            if same:
                # Both fixtures select the same executable; skip only the
                # self-comparison that would otherwise claim parity coverage.
                continue
            serial = run_in(serial_zfind_path, block_multibyte_tree, *args)
            assert res.status == serial.status == 0, (
                f"Limited run at {n} failed: parallel={res}; serial={serial}"
            )
            assert res.err == serial.err == b"", (
                f"Limited run at {n} wrote stderr: parallel={res}; serial={serial}"
            )


# -----------------------------------------------------------------------------
# Syscall faults: read and write error handling
# -----------------------------------------------------------------------------

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
    link_dir link_file root.txt
""")

# --type f prints regular files only; symlinks are excluded.
LAZY_FILES = [
    path for path in LAZY_ALL
    if not path.endswith(b"/") and path not in (b"link_dir", b"link_file")
]

# Root .gitignore: *.log, !keep.log, ignored/, aa/other.tmp.
LAZY_IGNORED = {
    b".git/", b"aa/ignored/", b"aa/ignored/inside.txt",
    b"aa/local.log", b"aa/other.tmp",
}
LAZY_GITIGNORED = [path for path in LAZY_ALL if path not in LAZY_IGNORED]

LAZY_CASES = [
    pytest.param((), LAZY_ALL, id="all"),
    pytest.param(("--type", "f"), LAZY_FILES, id="files"),
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
    return root


@pytest.fixture(scope="session")
def unknown_zfind_path(zfind_path: Path) -> Path:
    """Path to the DT_UNKNOWN/FORCE_WIDE_SPLIT test build."""
    explicit = os.environ.get("ZFIND_UNKNOWN")
    candidates = [Path(explicit)] if explicit else []
    candidates.append(
        zfind_path.parent.parent / f"{zfind_path.parent.name}-unknown" / "zfind"
    )
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
