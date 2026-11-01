"""Focused tests for --separator behavior and output framing."""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import NamedTuple

import pytest
from helpers import Result, ZfindRunner, joined, run_in

BLK_DATA = 16384 - 32  # src/x86_64/zfind.asm: BLK_SZ - BLK_HDR
TINY = [word.encode() for word in "a b ca cb dir/ dir/c dir/d e".split()]
WEIRD = [
    b"--limit", b"-0", b"-h", b"-t", b"a*b", b"back\\slash", b"c?d",
    b"caf\xc3\xa9", b"hi\xff", b"q\nr/", b"q\nr/s", b"sp ace", b"x\ny", b"z"
]


def nul_entries(out: bytes) -> list[bytes]:
    """Parse NUL-separated output into individual entry byte-strings."""
    assert out == b"" or out.endswith(b"\0"), "Output must be NUL-terminated or empty"
    return out.split(b"\0")[:-1]


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


def same_binary(first: Path, second: Path) -> bool:
    """Whether two configured paths select the same binary file."""
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


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
            if n == 2048:
                res = run_repeated(zfind_path, flat_131072, *args, timeout=120)
            else:
                res = run_in(zfind_path, flat_131072, *args, timeout=120)
            assert res.status == 0, f"Limited run at {n} failed: {res}"
            assert res.err == b"", f"Limited run at {n} wrote stderr: {res}"
            expected = full.out[:n * (7 + len(b"XY"))]
            assert res.out == expected, f"Wrong prefix at --limit {n}"
            assert res.out.split(b"XY")[:-1] == names[:n]

        serial = run_in(
            serial_binary_path, flat_131072,
            "--separator=XY", "--limit", "131072", timeout=120,
        )
        assert serial.status == 0, f"Serial run failed: {serial}"
        assert serial.err == b"", f"Serial run wrote stderr: {serial}"
        assert serial.out == full.out


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
            assert res.out == serial.out, f"Wrong prefix at --limit {n}"
