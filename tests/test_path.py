"""Tests for the automatic starting PATH and the explicit --path option."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import Result, entries, run_in

STRACE = shutil.which("strace")
requires_strace = pytest.mark.skipif(STRACE is None, reason="requires strace")


def failed(diagnostic: bytes) -> Result:
    """A run that printed nothing and exited 1 with one diagnostic."""
    return Result(out=b"", err=diagnostic, status=1)


def quoted(token: str | bytes) -> bytes:
    """The project's single-quoted diagnostic spelling of a token."""
    raw = token.encode() if isinstance(token, str) else token
    return b"zfind: unexpected argument '" + raw.replace(b"'", b"'\\''") + b"'\n"


def run_injected(binary: Path, cwd: Path, *args: str, inject: str) -> Result:
    """Run zfind under strace with one injected syscall fault."""
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
        timeout=60,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small tree with two searchable directories and assorted names."""
    root = tmp_path / "tree"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "build" / "build").mkdir(parents=True)
    (root / "my dir").mkdir()
    (root / "src" / "keep.txt").write_bytes(b"")
    (root / "src" / "nested" / "deep.txt").write_bytes(b"")
    (root / "other" / "skip.txt").write_bytes(b"")
    (root / "top.txt").write_bytes(b"")
    (root / "a.b").write_bytes(b"")
    (root / "build" / "inner.txt").write_bytes(b"")
    (root / "build" / "build" / "pruned.txt").write_bytes(b"")
    (root / "my dir" / "space file.txt").write_bytes(b"")
    return root


SRC_LISTING = [b"src/keep.txt", b"src/nested/", b"src/nested/deep.txt"]
DOT_SRC_LISTING = [b"./src/keep.txt", b"./src/nested/", b"./src/nested/deep.txt"]
DOT_LISTING = [
    b"./a.b",
    b"./build/",
    b"./build/build/",
    b"./build/build/pruned.txt",
    b"./build/inner.txt",
    b"./my dir/",
    b"./my dir/space file.txt",
    b"./other/",
    b"./other/skip.txt",
    b"./src/",
    b"./src/keep.txt",
    b"./src/nested/",
    b"./src/nested/deep.txt",
    b"./top.txt",
]


class TestPositionalClassification:
    def test_bare_name_is_a_pattern_even_when_a_directory_matches(
        self, binary: Path, tree: Path
    ):
        res = run_in(binary, tree, "src")
        assert res == Result(out=b"src/\n", err=b"", status=0)

    def test_existing_path_with_a_slash_is_the_starting_directory(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "./src") == Result(
            out=b"".join(name + b"\n" for name in DOT_SRC_LISTING), err=b"", status=0
        )

    def test_trailing_slash_is_stripped_from_the_display_root(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "./src/") == Result(
            out=b"".join(name + b"\n" for name in DOT_SRC_LISTING), err=b"", status=0
        )

    def test_trailing_slash_without_a_dot_prefix_is_stripped(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "src/") == Result(
            out=b"".join(name + b"\n" for name in SRC_LISTING), err=b"", status=0
        )

    def test_dot_is_a_starting_directory(self, binary: Path, tree: Path):
        res = run_in(binary, tree, ".")
        assert res == Result(
            out=b"".join(name + b"\n" for name in DOT_LISTING), err=b"", status=0
        )

    def test_dot_dot_is_a_starting_directory(self, binary: Path, tree: Path):
        res = run_in(binary, tree / "src", "..")
        expected = [
            name.replace(b"./", b"../") for name in DOT_LISTING
        ]
        assert res == Result(
            out=b"".join(name + b"\n" for name in expected), err=b"", status=0
        )

    def test_double_dash_forces_a_literal_dot_pattern(
        self, binary: Path, tmp_path: Path
    ):
        dots = tmp_path / "dots"
        (dots / "dir").mkdir(parents=True)
        (dots / "a.b").write_bytes(b"")
        (dots / "plain").write_bytes(b"")
        (dots / "dir" / "c.d").write_bytes(b"")

        res = run_in(binary, dots, "--", ".")
        assert res == Result(out=b"a.b\ndir/c.d\n", err=b"", status=0)

    def test_absolute_path_is_preserved_verbatim(self, binary: Path, tree: Path):
        root = str(tree / "src")
        res = run_in(binary, tree, root)
        expected = [
            f"{root}/keep.txt".encode(),
            f"{root}/nested/".encode(),
            f"{root}/nested/deep.txt".encode(),
        ]
        assert res == Result(
            out=b"".join(name + b"\n" for name in expected), err=b"", status=0
        )

    def test_path_and_pattern_work_in_either_order(self, binary: Path, tree: Path):
        expected = Result(out=b"./src/keep.txt\n", err=b"", status=0)
        assert run_in(binary, tree, "keep", "./src") == expected
        assert run_in(binary, tree, "./src", "keep") == expected

    def test_double_dash_before_a_path_like_token_keeps_it_literal(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--", "./src") == Result(
            out=b"", err=b"", status=0
        )
        assert run_in(binary, tree, "--", "a/b") == Result(
            out=b"", err=b"", status=0
        )

    def test_two_automatic_path_candidates_are_rejected(
        self, binary: Path, tree: Path
    ):
        res = run_in(binary, tree, "./src", "./other")
        assert res == failed(b"zfind: unexpected path argument './other'\n")

        # The later candidate is named even when neither one exists.
        res = run_in(binary, tree, "./missing", "./absent")
        assert res == failed(b"zfind: unexpected path argument './absent'\n")

    def test_missing_automatic_candidate_falls_back_to_a_pattern(
        self, binary: Path, tree: Path
    ):
        # A missing candidate with no other positional token is just a pattern.
        assert run_in(binary, tree, "./missing") == Result(
            out=b"", err=b"", status=0
        )

    def test_non_directory_automatic_candidate_falls_back_to_a_pattern(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "./top.txt") == Result(
            out=b"", err=b"", status=0
        )

    def test_fallback_that_leaves_two_patterns_names_the_later_token(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "./missing", "top") == failed(quoted("top"))
        assert run_in(binary, tree, "top", "./missing") == failed(quoted("./missing"))

    def test_explicit_path_accepts_only_one_pattern(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--path", "src", "a", "b") == failed(
            quoted("b")
        )

    def test_double_dash_accepts_only_one_pattern(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--", "a", "b") == failed(quoted("b"))

    def test_double_dash_after_a_positional_is_rejected(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "./src", "--", "foo") == failed(
            b"zfind: unexpected argument '--'\n"
        )

    def test_option_after_a_positional_is_rejected(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "./src", "--limit", "1") == failed(
            quoted("--limit")
        )
        assert run_in(binary, tree, "top", "--limit", "1") == failed(
            quoted("--limit")
        )
        assert run_in(binary, tree, "src", "--path", "other") == failed(
            quoted("--path")
        )


class TestExplicitPathOption:
    @pytest.mark.parametrize(
        ("args", "pattern"),
        [
            pytest.param(("--path", "src"), None, id="blank-form"),
            pytest.param(("--path=src",), None, id="equals-form"),
            pytest.param(("--path", "src", "keep"), "keep", id="blank-form-pattern"),
            pytest.param(("--path=src", "keep"), "keep", id="equals-form-pattern"),
        ],
    )
    def test_both_spellings_select_the_root(
        self, binary: Path, tree: Path, args: tuple[str, ...], pattern: str | None
    ):
        res = run_in(binary, tree, *args)

        expected = SRC_LISTING
        if pattern is not None:
            expected = [name for name in expected if b"keep" in name]
        assert res == Result(
            out=b"".join(name + b"\n" for name in expected), err=b"", status=0
        )

    def test_slash_containing_positional_after_path_is_a_literal_pattern(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--path", "src", "./other") == Result(
            out=b"", err=b"", status=0
        )

    def test_path_value_may_begin_with_a_dash(self, binary: Path, tree: Path):
        dashed = tree / "-dir"
        dashed.mkdir()
        (dashed / "file").write_bytes(b"")

        assert run_in(binary, tree, "--path", "-dir") == Result(
            out=b"-dir/file\n", err=b"", status=0
        )
        assert run_in(binary, tree, "--path=-dir") == Result(
            out=b"-dir/file\n", err=b"", status=0
        )

    def test_path_value_is_not_parsed_as_an_option(self, binary: Path, tree: Path):
        dashed = tree / "--hidden"
        dashed.mkdir()
        (dashed / "file").write_bytes(b"")

        assert run_in(binary, tree, "--path", "--hidden") == Result(
            out=b"--hidden/file\n", err=b"", status=0
        )

    def test_help_prescan_still_wins_over_a_path_value(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--no-config", "--path", "--help") == run_in(
            binary, tree, "--no-config", "--help"
        )

    def test_equals_form_keeps_help_as_a_literal_path(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--path=--help") == failed(
            b"zfind: starting path '--help' does not exist\n"
        )

    def test_path_consumes_the_next_token_even_for_config_names(
        self, binary: Path, tree: Path, tmp_path: Path
    ):
        dashed = tree / "--config"
        dashed.mkdir()
        (dashed / "file").write_bytes(b"")

        # find_config must skip "--config" here instead of reading the pattern
        # as a config file (or combining it with --no-config).
        res = run_in(
            binary, tree, "--no-config", "--path", "--config",
            str(tmp_path / "no-such-pattern"),
        )
        assert res == Result(out=b"", err=b"", status=0)

    @pytest.mark.parametrize(
        ("args", "diagnostic"),
        [
            pytest.param(("--path",), b"zfind: missing value after --path\n", id="no-value"),
            pytest.param(("--path", "--"), b"zfind: missing value after --path\n", id="double-dash-value"),
            pytest.param(("--path=",), b"zfind: --path requires a non-empty value\n", id="empty-value"),
            pytest.param(("--path", "a", "--path", "b"), b"zfind: --path may only be specified once\n", id="repeated"),
        ],
    )
    def test_value_errors(
        self, binary: Path, tree: Path, args: tuple[str, ...], diagnostic: bytes
    ):
        assert run_in(binary, tree, *args) == failed(diagnostic)


class TestRootValidationAndOutput:
    def test_root_itself_is_never_printed(self, binary: Path, tree: Path):
        result = entries(run_in(binary, tree, "--path", "src").out)
        assert b"src/" not in result
        assert b"./src/" not in result

    def test_explicit_missing_path_is_an_error(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--path", "missing") == failed(
            b"zfind: starting path 'missing' does not exist\n"
        )

    def test_explicit_non_directory_is_an_error(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--path", "top.txt") == failed(
            b"zfind: starting path 'top.txt' is not a directory\n"
        )

    def test_explicit_unreadable_directory_is_an_error(
        self, binary: Path, tree: Path
    ):
        blocked = tree / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        try:
            if os.access(blocked, os.R_OK | os.X_OK):
                pytest.skip("file permissions are not enforced (running as root?)")
            assert run_in(binary, tree, "--path", "blocked") == failed(
                b"zfind: cannot open starting path 'blocked' (EACCES)\n"
            )
        finally:
            blocked.chmod(0o700)

    def test_explicit_symlink_loop_is_an_error(self, binary: Path, tree: Path):
        (tree / "loop").symlink_to("loop")
        assert run_in(binary, tree, "--path", "loop") == failed(
            b"zfind: cannot open starting path 'loop' (ELOOP)\n"
        )

    def test_explicit_too_long_path_is_an_error(self, binary: Path, tree: Path):
        token = "a" * 4096
        assert run_in(binary, tree, "--path", token) == failed(
            f"zfind: starting path '{token}' is too long\n".encode()
        )

    def test_overlong_diagnostic_is_truncated_not_crashing(
        self, binary: Path, tree: Path
    ):
        res = run_in(binary, tree, "a", "x" * 130000)
        assert res == failed(
            b"zfind: unexpected argument '" + b"x" * 4548 + b"'\n"
        )

    def test_overlong_explicit_path_diagnostic_is_truncated(
        self, binary: Path, tree: Path
    ):
        res = run_in(binary, tree, "--path", "a" * 5000)
        assert res == failed(
            b"zfind: starting path '" + b"a" * 4554 + b"' is too long\n"
        )

    def test_automatic_unreadable_candidate_is_an_error_and_not_a_fallback(
        self, binary: Path, tree: Path
    ):
        blocked = tree / "blocked"
        blocked.mkdir()
        blocked.chmod(0)
        try:
            if os.access(blocked, os.R_OK | os.X_OK):
                pytest.skip("file permissions are not enforced (running as root?)")
            assert run_in(binary, tree, "./blocked") == failed(
                b"zfind: cannot open starting path './blocked' (EACCES)\n"
            )
        finally:
            blocked.chmod(0o700)

    def test_automatic_symlink_loop_is_an_error(self, binary: Path, tree: Path):
        (tree / "loop").symlink_to("loop")
        assert run_in(binary, tree, "./loop") == failed(
            b"zfind: cannot open starting path './loop' (ELOOP)\n"
        )

    def test_automatic_too_long_candidate_is_reported(self, binary: Path, tree: Path):
        token = "./" + "a" * 4094
        assert run_in(binary, tree, token) == failed(
            b"zfind: cannot open starting path '" + token.encode()
            + b"' (ENAMETOOLONG)\n"
        )

    def test_descendant_path_too_long_for_the_buffer_is_skipped(
        self, binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        parts = ["d" * 255] * 15 + ["d" * 240]
        rel = "/".join(parts)
        for part in parts:
            os.mkdir(part)
            os.chdir(part)
        names = [f"f{index:02d}" for index in range(15)]
        for name in names:
            Path(name).write_bytes(b"")
        # The sixteenth entry cannot fit; 16 entries also make the range build
        # split this directory into walk_range groups.
        Path("b" * 100).write_bytes(b"")

        res = run_in(binary, tmp_path, "--no-config", "--path", rel)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [
            (rel + "/" + name).encode() for name in names
        ]

    @requires_strace
    def test_automatic_open_error_is_not_a_fallback(self, zfind_path: Path, tree: Path):
        res = run_injected(
            zfind_path, tree, "--no-config", "./src",
            inject="openat:error=EIO:when=1",
        )
        assert res == failed(b"zfind: cannot open starting path './src' (EIO)\n")

    @requires_strace
    def test_root_read_failure_is_reported(self, zfind_path: Path, tree: Path):
        res = run_injected(
            zfind_path, tree, "--path", "src",
            inject="getdents64:error=EIO:when=1",
        )
        assert res == failed(b"zfind: cannot read starting path 'src' (EIO)\n")

    def test_limit_zero_still_validates_an_explicit_path(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--path", "missing", "--limit", "0") == failed(
            b"zfind: starting path 'missing' does not exist\n"
        )
        assert run_in(binary, tree, "--path", "src", "--limit", "0") == Result(
            out=b"", err=b"", status=0
        )

    def test_limit_zero_still_classifies_an_automatic_candidate(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--limit", "0", "./missing") == Result(
            out=b"", err=b"", status=0
        )
        assert run_in(binary, tree, "--limit", "0", "./missing", "top") == failed(
            quoted("top")
        )
        assert run_in(binary, tree, "--limit", "0", "./src", "./other") == failed(
            b"zfind: unexpected path argument './other'\n"
        )

    def test_no_match_is_a_successful_empty_result(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--path", "src", "nope") == Result(
            out=b"", err=b"", status=0
        )

    def test_paths_with_spaces(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--path", "my dir") == Result(
            out=b"my dir/space file.txt\n", err=b"", status=0
        )

    def test_filesystem_root_never_produces_a_double_separator(self, binary: Path):
        res = run_in(binary, Path("/"), "--path", "/", "--limit", "1")
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        result = entries(res.out)
        assert len(result) <= 1
        assert all(not name.startswith(b"//") for name in result)


class TestRootFiltersAndSymlinks:
    def test_type_filters_descendants_but_not_the_root(
        self, binary: Path, tree: Path
    ):
        assert run_in(binary, tree, "--path", "src", "--type", "d") == Result(
            out=b"src/nested/\n", err=b"", status=0
        )
        assert run_in(binary, tree, "--path", "src", "--type", "f") == Result(
            out=b"src/keep.txt\nsrc/nested/deep.txt\n", err=b"", status=0
        )

    def test_hidden_root_can_be_searched_with_no_hidden(
        self, binary: Path, tree: Path
    ):
        secret = tree / ".secret"
        (secret / ".inner").mkdir(parents=True)
        (secret / "kept.txt").write_bytes(b"")
        (secret / ".inner" / "hidden.txt").write_bytes(b"")

        res = run_in(binary, tree, "--path", ".secret", "--no-hidden")
        assert res == Result(out=b".secret/kept.txt\n", err=b"", status=0)

        res = run_in(binary, tree, "--path", ".secret")
        assert entries(res.out) == [
            b".secret/.inner/",
            b".secret/.inner/hidden.txt",
            b".secret/kept.txt",
        ]

    def test_exclude_name_does_not_apply_to_the_root(self, binary: Path, tree: Path):
        res = run_in(binary, tree, "--path", "build", "--exclude-name", "build")
        assert res == Result(out=b"build/inner.txt\n", err=b"", status=0)

    def test_explicit_symlink_root_is_followed_and_keeps_its_spelling(
        self, binary: Path, tree: Path
    ):
        (tree.parent / "link").symlink_to("tree/src")
        res = run_in(binary, tree.parent, "--path", "link")
        assert entries(res.out) == [
            b"link/keep.txt",
            b"link/nested/",
            b"link/nested/deep.txt",
        ]

    def test_automatic_symlink_root_is_followed(self, binary: Path, tree: Path):
        (tree.parent / "link").symlink_to("tree/src")
        res = run_in(binary, tree.parent, "./link")
        assert entries(res.out) == [
            b"./link/keep.txt",
            b"./link/nested/",
            b"./link/nested/deep.txt",
        ]

    def test_symlinked_directory_below_the_root_is_not_followed(
        self, binary: Path, tree: Path
    ):
        (tree / "src" / "linkdir").symlink_to("../other")
        res = run_in(binary, tree, "--path", "src")
        assert entries(res.out) == [
            b"src/keep.txt",
            b"src/linkdir",
            b"src/nested/",
            b"src/nested/deep.txt",
        ]


class TestPathGitignoreResolution:
    @requires_strace
    def test_physical_path_resolution_failure_is_reported(
        self, zfind_path: Path, tmp_path: Path
    ):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".gitignore").write_bytes(b"*.log\n")
        sub = repo / "sub"
        sub.mkdir()
        (sub / "noise.log").write_bytes(b"")

        res = run_injected(
            zfind_path, tmp_path, "--path", "repo/sub", "--gitignore",
            inject="readlink:error=EIO:when=1",
        )
        assert res == failed(b"zfind: cannot resolve starting path for --gitignore\n")


class TestPathConfigInteraction:
    def test_find_config_skips_the_path_value(self, binary: Path, tree: Path):
        # "--config" is the value of --path, so it must not select a config
        # file.  The tree has no "--config" directory, so this still fails at
        # root validation, which proves the value was consumed as a path.
        res = run_in(binary, tree, "--no-config", "--path", "--config", "x")
        assert res == failed(
            b"zfind: starting path '--config' does not exist\n"
        )
