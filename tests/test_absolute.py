"""Tests for --absolute: the display root prefixed with getcwd(2)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from helpers import SPAWN_TIMEOUT, Result, entries, run_in

PATH_MAX = 4096
CYAN = b"\x1b[36m"
MATCH = b"\x1b[1;7m"
RESET = b"\x1b[0m"


def prefix_of(path: Path) -> bytes:
    """The getcwd(2) spelling of a directory: physical, as the child sees it."""
    return os.fsencode(os.path.realpath(path))


def listing(names: list[bytes], sep: bytes = b"\n") -> Result:
    """A successful run that printed exactly these terminated names."""
    return Result(out=b"".join(name + sep for name in names), err=b"", status=0)


def absolute(baseline: Result, prefix: bytes) -> list[bytes]:
    """Prefix every relative result line, dropping a literal leading './'."""
    names = [
        name[2:] if name.startswith(b"./") else name for name in entries(baseline.out)
    ]
    return [prefix + b"/" + name for name in names]


def run_config(binary: Path, cwd: Path, home: Path, *args: str) -> Result:
    """Run zfind with HOME as its whole environment, so only this config is read."""
    done = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        env={"HOME": str(home)},
        timeout=SPAWN_TIMEOUT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small tree with one searchable directory, files and a decoy directory."""
    root = tmp_path / "tree"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "other").mkdir()
    for name in (
        "src/keep.txt",
        "src/nested/deep.txt",
        "src/noise.log",
        "other/other.txt",
        "top.txt",
    ):
        (root / name).write_bytes(b"")
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A HOME whose .config/zfind directory exists but holds no config yet."""
    path = tmp_path / "home"
    (path / ".config" / "zfind").mkdir(parents=True)
    return path


class TestRelativeRootPrefixing:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param((), id="no-path"),
            pytest.param((".",), id="dot"),
            pytest.param(("./src",), id="dot-slash-src"),
            pytest.param(("src/",), id="trailing-slash"),
            pytest.param(("--path", "src"), id="path-blank"),
            pytest.param(("--path=src",), id="path-equals"),
        ],
    )
    def test_relative_root_is_prefixed_with_getcwd(
        self, binary: Path, tree: Path, args: tuple[str, ...]
    ):
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, *args)
        assert baseline.status == 0, baseline
        assert baseline.err == b""

        res = run_in(binary, tree, "--absolute", *args)
        assert res == listing(absolute(baseline, prefix))
        # No "./" component and no doubled separator survives in the prefix.
        assert b"/./" not in res.out
        assert prefix + b"//" not in res.out


class TestAbsolutePathIsUnchanged:
    def test_absolute_directory_spelling_is_a_no_op(self, binary: Path, tree: Path):
        root = str(tree / "src")
        baseline = run_in(binary, tree, "--path", root)
        assert baseline.status == 0, baseline
        assert baseline.err == b""
        assert run_in(binary, tree, "--absolute", "--path", root) == baseline

    def test_filesystem_root_path_is_a_no_op(self, binary: Path, tree: Path):
        baseline = run_in(binary, tree, "--path", "/", "--limit", "1")
        assert baseline.status == 0, baseline
        assert baseline.err == b""
        assert run_in(binary, tree, "--absolute", "--path", "/", "--limit", "1") == baseline


class TestLiteralSpelling:
    def test_dot_dot_is_not_resolved(self, binary: Path, tree: Path):
        prefix = prefix_of(tree / "src")  # the child's getcwd(2), not the tree's
        baseline = run_in(binary, tree / "src", "..")
        assert baseline.status == 0, baseline
        names = absolute(baseline, prefix)
        assert all(name.startswith(prefix + b"/../") for name in names), names
        assert run_in(binary, tree / "src", "--absolute", "..") == listing(names)

        # `..` still selects the same entries as the relative run after the OS
        # resolves the literal path.
        first = names[0].decode()
        relative = entries(baseline.out)[0].decode()
        assert os.path.realpath(first) == os.path.realpath(
            os.path.join(str(tree / "src"), relative)
        )

    def test_symlinked_path_keeps_its_spelling(self, binary: Path, tree: Path):
        (tree / "link").symlink_to("src")
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, "--path", "link")
        assert baseline.status == 0, baseline

        res = run_in(binary, tree, "--absolute", "--path", "link")
        assert res == listing(absolute(baseline, prefix))
        assert b"/src/" not in res.out

    def test_symlinked_cwd_uses_the_physical_prefix(
        self, binary: Path, tree: Path, tmp_path: Path
    ):
        link = tmp_path / "link"
        link.symlink_to(tree)
        physical = prefix_of(link)
        assert physical != os.fsencode(str(link))  # the test can tell them apart

        res = run_in(binary, link, "--absolute", "--path", "src")
        assert res.status == 0, res
        assert entries(res.out) == [physical + b"/src/keep.txt",
                                    physical + b"/src/nested/",
                                    physical + b"/src/nested/deep.txt",
                                    physical + b"/src/noise.log"]


class TestFilesystemRoot:
    def test_root_cwd_never_produces_a_double_separator(self, binary: Path):
        res = run_in(binary, Path("/"), "--absolute", "--limit", "1")
        assert res.status == 0, res
        assert res.err == b""
        names = entries(res.out)
        assert len(names) <= 1
        assert all(not name.startswith(b"//") for name in names)


class TestFlagModes:
    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param(("--absolute", "--relative"), id="absolute-first"),
            pytest.param(("--relative", "--absolute"), id="relative-first"),
        ],
    )
    def test_both_flags_in_one_source_are_a_usage_error(
        self, binary: Path, tree: Path, flags: tuple[str, ...]
    ):
        res = run_in(binary, tree, *flags)
        assert res.status == 1, res
        assert res.out == b""
        lines = res.err.splitlines()
        assert len(lines) == 1, res
        assert lines[0].startswith(b"zfind: "), res
        assert b"--absolute" in lines[0], res
        assert b"--relative" in lines[0], res

    def test_absolute_takes_no_value(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--absolute=x") == Result(
            out=b"", err=b"zfind: unknown option\n", status=1
        )

    def test_flag_does_not_consume_the_next_token(self, binary: Path, tree: Path):
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, "./src")
        assert baseline.status == 0, baseline
        res = run_in(binary, tree, "--absolute", "./src")
        assert res == listing(absolute(baseline, prefix))


class TestConfigModes:
    def test_config_absolute_is_undone_by_the_command_line(
        self, binary: Path, tree: Path, home: Path
    ):
        config = home / ".config" / "zfind" / "config"
        config.write_bytes(b"--absolute\n")
        prefix = prefix_of(tree)

        configured = run_config(binary, tree, home, "--path", "src")
        assert configured == listing(
            absolute(run_in(binary, tree, "--path", "src"), prefix)
        )

        overridden = run_config(binary, tree, home, "--relative", "--path", "src")
        assert overridden == run_in(binary, tree, "--path", "src")

    def test_config_relative_is_undone_by_the_command_line(
        self, binary: Path, tree: Path, home: Path
    ):
        config = home / ".config" / "zfind" / "config"
        config.write_bytes(b"--relative\n")
        prefix = prefix_of(tree)

        overridden = run_config(
            binary, tree, home, "--absolute", "--path", "src"
        )
        assert overridden == listing(
            absolute(run_in(binary, tree, "--path", "src"), prefix)
        )
        assert run_config(binary, tree, home, "--path", "src") == run_in(
            binary, tree, "--path", "src"
        )

    def test_config_with_both_flags_is_an_error(
        self, binary: Path, tree: Path, home: Path
    ):
        config = home / ".config" / "zfind" / "config"
        config.write_bytes(b"--absolute\n--relative\n")

        res = run_config(binary, tree, home, "--path", "src")
        assert res.status == 1, res
        assert res.out == b""
        lines = res.err.splitlines()
        assert len(lines) == 1, res
        assert os.fsencode(str(config)) + b":2:" in lines[0], res
        assert b"--absolute" in lines[0], res
        assert b"--relative" in lines[0], res


class TestSelectionInvariance:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--path", "src", "keep"), id="pattern"),
            pytest.param(("--path", ".", "--exclude-name", "nested"), id="exclude-name"),
            pytest.param(("--path", ".", "--exclude-type", "f"), id="exclude-type"),
            pytest.param(("--path", "src", "--type", "f"), id="type-files"),
            pytest.param(("--path", "src", "--type", "d"), id="type-directories"),
        ],
    )
    def test_filters_select_the_same_entries(
        self, binary: Path, tree: Path, args: tuple[str, ...]
    ):
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, *args)
        assert baseline.status == 0, baseline
        assert baseline.err == b""

        res = run_in(binary, tree, "--absolute", *args)
        assert res.status == 0, res
        assert res.err == b""
        assert entries(res.out) == absolute(baseline, prefix)

    def test_no_hidden_selects_the_same_entries(self, binary: Path, tmp_path: Path):
        root = tmp_path / "dots"
        (root / "sub").mkdir(parents=True)
        for name in ("visible", ".hidden", "sub/inside", "sub/.deep"):
            (root / name).write_bytes(b"")
        assert b".hidden" in run_in(binary, root).out  # the tree really has hidden names

        prefix = prefix_of(root)
        baseline = run_in(binary, root, "--no-hidden")
        assert baseline.status == 0, baseline
        assert baseline.err == b""
        assert sorted(entries(baseline.out)) == [b"sub/", b"sub/inside", b"visible"]

        res = run_in(binary, root, "--absolute", "--no-hidden")
        assert res == listing(absolute(baseline, prefix))
        assert b".hidden" not in res.out
        assert b".deep" not in res.out

    def test_gitignore_selects_the_same_entries(self, binary: Path, tree: Path):
        (tree / ".git").mkdir()
        (tree / ".gitignore").write_bytes(b"*.log\n")
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, "--gitignore")
        assert baseline.status == 0, baseline
        assert b"noise.log" not in baseline.out

        res = run_in(binary, tree, "--absolute", "--gitignore")
        assert res.status == 0, res
        assert res.err == b""
        assert entries(res.out) == absolute(baseline, prefix)
        assert b"noise.log" not in res.out


class TestOutputFrames:
    def test_nul_separator_keeps_one_terminated_record_per_entry(
        self, binary: Path, tree: Path
    ):
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, "--path", "src")
        assert baseline.status == 0, baseline

        res = run_in(binary, tree, "--absolute", "--separator=\\0", "--path", "src")
        assert res == listing(absolute(baseline, prefix), sep=b"\0")

    def test_limit_prints_the_absolute_prefix(self, binary: Path, tree: Path):
        prefix = prefix_of(tree)
        baseline = run_in(binary, tree, "--path", "src")
        assert baseline.status == 0, baseline

        res = run_in(binary, tree, "--absolute", "--limit", "2", "--path", "src")
        assert res == listing(absolute(baseline, prefix)[:2])

    def test_limit_zero_prints_nothing(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--absolute", "--limit", "0") == Result(
            out=b"", err=b"", status=0
        )
        assert run_in(
            binary, tree, "--absolute", "--limit", "0", "--path", "src"
        ) == Result(out=b"", err=b"", status=0)

    def test_empty_result_set_prints_nothing(self, binary: Path, tree: Path):
        assert run_in(binary, tree, "--absolute", "--path", "src", "absent") == Result(
            out=b"", err=b"", status=0
        )

    def test_color_prefix_is_one_cyan_run(self, binary: Path, tree: Path):
        prefix = prefix_of(tree)
        res = run_in(
            binary, tree, "--absolute", "--color", "always", "--path", "src", "keep"
        )
        expected = (
            CYAN + prefix + b"/src/" + RESET
            + MATCH + b"keep" + RESET + b".txt\n"
        )
        assert res == Result(out=expected, err=b"", status=0)


class TestPathMax:
    def test_combined_root_too_long_is_a_startup_error(
        self, binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        prefix = prefix_of(tmp_path)
        rel_len = PATH_MAX - len(prefix)
        if not 1 <= rel_len <= PATH_MAX - 1:
            pytest.skip("temporary directory leaves no room to reach PATH_MAX")

        count = max(1, (rel_len + 1) // 201)
        body = rel_len - (count - 1)
        base = body // count
        extra = body % count
        parts = [b"d" * base] * (count - extra) + [b"d" * (base + 1)] * extra
        rel = b"/".join(parts)
        assert len(rel) == rel_len
        assert max(len(part) for part in parts) <= 255

        monkeypatch.chdir(tmp_path)
        for part in parts:
            os.mkdir(part)
            os.chdir(part)

        # The spelling alone is openable; only the combined root overflows.
        assert run_in(binary, tmp_path, "--path", rel.decode()) == Result(
            out=b"", err=b"", status=0
        )

        combined = prefix + b"/" + rel
        assert len(combined) >= PATH_MAX
        assert run_in(binary, tmp_path, "--absolute", "--path", rel.decode()) == Result(
            out=b"",
            err=b"zfind: starting path '" + combined + b"' is too long\n",
            status=1,
        )


class TestGetcwdFailure:
    def test_a_deleted_cwd_is_a_startup_error(self, binary: Path, tmp_path: Path):
        gone = tmp_path / "gone"
        gone.mkdir()
        # preexec_fn runs in the child after chdir and before exec, so the
        # directory is gone by the time getcwd(2) is called, with no race.
        done = subprocess.run(
            [str(binary), "--absolute"],
            cwd=gone,
            preexec_fn=lambda: os.rmdir(gone),
            timeout=SPAWN_TIMEOUT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert Result(out=done.stdout, err=done.stderr, status=done.returncode) == Result(
            out=b"",
            err=b"zfind: cannot read starting path '.' (ENOENT)\n",
            status=1,
        )
