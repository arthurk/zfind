"""Tests for zfind CLI flags, options, filtering, and error handling."""

from __future__ import annotations

from pathlib import Path
import pytest
from conftest import ZfindRunner

DEV_FULL = Path("/dev/full")
UINT64_MAX = (1 << 64) - 1
UINT64_OVERFLOW = UINT64_MAX + 1
BUFFER_SIZE_64_KIB = 64 * 1024


# -----------------------------------------------------------------------------
# Output Parsing Helpers
# -----------------------------------------------------------------------------

def words(text: str) -> list[bytes]:
    """Convert whitespace-separated listing string to byte-string list: 'a b/' -> [b'a', b'b/']"""
    return [word.encode() for word in text.split()]


def entries(out: bytes) -> list[bytes]:
    """Parse newline-separated output into individual entry byte-strings."""
    assert out == b"" or out.endswith(b"\n"), "Output must be newline-terminated or empty"
    return out.split(b"\n")[:-1]


def nul_entries(out: bytes) -> list[bytes]:
    """Parse NUL-separated output into individual entry byte-strings."""
    assert out == b"" or out.endswith(b"\0"), "Output must be NUL-terminated or empty"
    return out.split(b"\0")[:-1]


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
# Flag: --null
# -----------------------------------------------------------------------------

class TestNull:
    @pytest.mark.parametrize("args, expected", [
        pytest.param([], b"a\nb\nca\ncb\ndir/\ndir/c\ndir/d\ne\n", id="default-newline"),
        pytest.param(["--null"], b"a\0b\0ca\0cb\0dir/\0dir/c\0dir/d\0e\0", id="null-delimiter"),
        pytest.param(["--null", "c"], b"ca\0cb\0dir/c\0", id="null-with-pattern"),
    ])
    def test_output_delimiter(self, zfind: ZfindRunner, args: list[str], expected: bytes):
        res = zfind("tiny", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert res.out == expected

    def test_null_preserves_filenames_containing_newlines(self, zfind: ZfindRunner):
        res = zfind("weird", "--null")
        assert res.status == 0, f"Command failed: {res}"
        assert nul_entries(res.out) == WEIRD

    def test_default_newline_mode_splits_embedded_newlines(self, zfind: ZfindRunner):
        res = zfind("weird")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out.count(b"\n") == len(WEIRD) + 3


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

def calculate_buffer_boundary_sizes(listing: list[bytes]) -> list[int]:
    """Calculate limit counts around boundaries: ends and 64 KiB buffer overflow."""
    size, passed = 0, None
    for count, entry in enumerate(listing, 1):
        size += len(entry) + 1
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
        pytest.param(["--limit", str(UINT64_MAX)], TINY, id="uint64-max-means-unlimited"),
        pytest.param(["--limit", "2", "c"], [b"ca", b"cb"], id="limit-with-pattern"),
    ])
    def test_limit_argument_handling(self, zfind: ZfindRunner, args: list[str], expected: list[bytes]):
        res = zfind("tiny", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == expected

    def test_limit_with_null_counts_entries(self, zfind: ZfindRunner):
        res = zfind("tiny", "--null", "--limit", "2")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out == b"a\0b\0"

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

    def test_limit_newline_mode_counts_newlines_not_entries(self, zfind: ZfindRunner):
        res = zfind("weird", "--limit", "10")
        assert res.status == 0, f"Command failed: {res}"
        assert res.out.endswith(b"hi\xff\nq\n")

    def test_limit_with_null_never_cuts_embedded_newlines(self, zfind: ZfindRunner):
        res = zfind("weird", "--null", "--limit", "10")
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
        pytest.param(["--limit", "--null"], id="subsequent-flag-as-value"),
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

            res_nul = zfind(tree, "--null", "--limit", str(n))
            assert res_nul.status == 0, f"Failed at --null --limit {n}: {res_nul}"
            assert nul_entries(res_nul.out) == full_listing[:n], f"Wrong entries at --null --limit {n}"

    @pytest.mark.parametrize(("tree", "args"), [
        pytest.param("tree2", (), id="wide-tree"),
        pytest.param("weird", ("--null",), id="newline-and-byte-names"),
        pytest.param("ignores", ("--gitignore", "--no-hidden"), id="gitignore-and-hidden"),
        pytest.param("camera", ("--limit", "100"), id="limited-output"),
    ])
    def test_parallel_output_matches_serial(
        self, zfind: ZfindRunner, serial_zfind: ZfindRunner,
        tree: str, args: tuple[str, ...],
    ):
        parallel = zfind(tree, *args)
        serial = serial_zfind(tree, *args)
        assert parallel.status == serial.status == 0, f"parallel={parallel}; serial={serial}"
        assert parallel == serial, f"parallel={parallel}; serial={serial}"

    def test_limit_output_is_deterministic_under_concurrency(self, zfind: ZfindRunner):
        outputs = set()
        for i in range(20):
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
