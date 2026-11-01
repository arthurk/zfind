"""Tests for --limit and its output-prefix behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from helpers import Result, ZfindRunner, entries, joined, run_in, words
from expected import IGNORES

DEV_FULL = Path("/dev/full")
UINT64_MAX = (1 << 64) - 1
UINT64_OVERFLOW = UINT64_MAX + 1
BUFFER_SIZE_64_KIB = 64 * 1024
TINY = [word.encode() for word in "a b ca cb dir/ dir/c dir/d e".split()]
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


def nul_entries(out: bytes) -> list[bytes]:
    assert out == b"" or out.endswith(b"\0"), "Output must be NUL-terminated or empty"
    return out.split(b"\0")[:-1]


def run_repeated(
    binary: Path, cwd: Path, *args: str, runs: int = 3, timeout: int = 60
) -> Result:
    first = run_in(binary, cwd, *args, timeout=timeout)
    for run in range(2, runs + 1):
        again = run_in(binary, cwd, *args, timeout=timeout)
        assert again == first, (
            f"parallel run {run} for {args} differs from run 1: "
            f"first={first}; again={again}"
        )
    return first


def same_binary(first: Path, second: Path) -> bool:
    if first == second:
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


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
    def test_unlimited_output_for_every_separator(
        self, zfind_path: Path, exact_limit_tree: Path, sep_arg: str, sep_bytes: bytes
    ):
        res = run_in(zfind_path, exact_limit_tree, "--separator=" + sep_arg)
        assert res.status == 0, f"Baseline failed: {res}"
        assert res.err == b"", f"Baseline wrote stderr: {res}"
        assert res.out == joined(TINY, sep_bytes)

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
        pytest.param("ignores", IGNORES, b"tt", 5, id="ignores-tt-limit-5"),
        pytest.param("ignores", IGNORES, b"tt", 27, id="ignores-tt-limit-27"),
        pytest.param("weird", WEIRD, b"bb", 6, id="weird-bb-limit-6"),
        pytest.param("weird", WEIRD, b"bb", 14, id="weird-bb-limit-14"),
    ])
    def test_limit_prefix_with_overlapping_separators_on_larger_trees(
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

    @pytest.mark.parametrize("n", [0, 1, 2, 3, 7, 8, 9])
    @pytest.mark.parametrize("sep_arg, sep_bytes", EXACT_LIMIT_SEPARATORS)
    def test_exact_limit_prefix_and_serial_parity(
        self, zfind_path: Path, serial_binary_path: Path,
        exact_limit_tree: Path, sep_arg: str, sep_bytes: bytes, n: int,
    ):
        args = ("--separator=" + sep_arg, "--limit", str(n))
        expected = joined(TINY[:n], sep_bytes)

        parallel = run_repeated(zfind_path, exact_limit_tree, *args)
        assert parallel.status == 0, f"parallel={parallel}"
        assert parallel.err == b"", f"parallel={parallel}"
        assert parallel.out == expected

        if same_binary(zfind_path, serial_binary_path):
            return
        if not serial_binary_path.is_file():
            pytest.skip(
                f"serial binary not built: {serial_binary_path}; exact-limit "
                "assertions passed, serial parity skipped; run `make test`"
            )

        serial = run_in(serial_binary_path, exact_limit_tree, *args)
        assert parallel.status == serial.status == 0, (
            f"parallel={parallel}; serial={serial}"
        )
        assert parallel.err == serial.err == b"", (
            f"parallel={parallel}; serial={serial}"
        )
        assert parallel.out == serial.out == expected, (
            f"parallel={parallel}; serial={serial}"
        )

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

    def test_limit_output_is_deterministic_under_concurrency(
        self, zfind: ZfindRunner
    ):
        outputs = set()
        for i in range(10):
            res = zfind("tree2", "--limit", "100")
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






# -----------------------------------------------------------------------------
# Syscall faults: read and write error handling
# -----------------------------------------------------------------------------
