"""Tests for --color: ANSI rendering, mode resolution, config and invariants."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import time
import tty
from pathlib import Path
from typing import Mapping, Optional

import pytest
from helpers import Result, ZfindRunner, entries

ESC = b"\x1b"
CYAN = ESC + b"[36m"
MATCH = ESC + b"[1;7m"
RESET = ESC + b"[0m"
SEQS = (CYAN, MATCH, RESET)

COLOR_MESSAGE = b"expected auto, always or never after --color\n"
COLOR_ERROR = b"zfind: " + COLOR_MESSAGE
CFG_ALWAYS_ERROR = (
    b"--color always is not allowed in the config file (use auto or never)\n"
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def strip_color(data: bytes) -> bytes:
    """Remove exactly the three sequences this feature emits."""
    for seq in SEQS:
        data = data.replace(seq, b"")
    return data


def run_env(
    binary: Path,
    cwd: Path,
    *args: str,
    env: Optional[Mapping[str, str]] = None,
) -> Result:
    """Run zfind with exactly this environment and stdout on a pipe."""
    done = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        env=dict(env or {}),
        timeout=60,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)


def run_pty(
    binary: Path,
    cwd: Path,
    *args: str,
    env: Optional[Mapping[str, str]] = None,
) -> Result:
    """Run zfind with stdout on a pty, so auto sees a terminal.

    The pty is put in raw mode so that output post-processing (ONLCR) does not
    turn newlines into CRLF and byte comparisons stay exact.
    """
    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        proc = subprocess.Popen(
            [str(binary), *args],
            cwd=cwd,
            env=dict(env or {}),
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=subprocess.PIPE,
        )
    except BaseException:
        os.close(slave)
        os.close(master)
        raise
    os.close(slave)

    # Read while the child runs. Waiting for subprocess.run to finish first
    # deadlocks once the output fills the pty buffer, so drain it here.
    chunks: list[bytes] = []
    deadline = time.monotonic() + 60
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired([str(binary), *args], 60)
            ready, _, _ = select.select([master], [], [], min(remaining, 0.1))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # EIO once the child side is fully closed
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            elif proc.poll() is not None:
                # The child is gone: one more read drains the pty or reports EIO.
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        stderr = proc.communicate(
            timeout=max(0.1, deadline - time.monotonic())
        )[1]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
    return Result(out=b"".join(chunks), err=stderr or b"", status=proc.returncode)


def parse_runs(data: bytes) -> list[tuple[int, bytes]]:
    """Split styled output into (style, text) runs, merging adjacent runs.

    style: 0 default, 1 cyan, 2 match. The three sequences are the only bytes
    interpreted; everything else is text.
    """
    runs: list[tuple[int, bytes]] = []
    style = 0
    buf = bytearray()

    def flush() -> None:
        if buf:
            runs.append((style, bytes(buf)))
            buf.clear()

    i = 0
    while i < len(data):
        if data.startswith(CYAN, i):
            flush()
            style = 1
            i += len(CYAN)
        elif data.startswith(MATCH, i):
            flush()
            style = 2
            i += len(MATCH)
        elif data.startswith(RESET, i):
            flush()
            style = 0
            i += len(RESET)
        else:
            buf.append(data[i])
            i += 1
    flush()
    return runs


def assert_closed_styles(out: bytes, sep: bytes) -> None:
    """The style is default immediately before every separator and at the end."""
    style = 0
    i = 0
    while i < len(out):
        if out.startswith(CYAN, i):
            style = 1
            i += len(CYAN)
        elif out.startswith(MATCH, i):
            style = 2
            i += len(MATCH)
        elif out.startswith(RESET, i):
            style = 0
            i += len(RESET)
        elif sep and out.startswith(sep, i):
            assert style == 0, f"styled run left open before separator at byte {i}"
            i += len(sep)
        else:
            i += 1
    assert style == 0, "styled run left open at end of output"


def basename_start(text: bytes) -> int:
    """Offset of the basename, with a directory's trailing slash kept in it."""
    end = len(text) - 1 if text.endswith(b"/") else len(text)
    return text.rfind(b"/", 0, end) + 1


def assert_match_runs_are_in_basenames(out: bytes, sep: bytes) -> None:
    """No match run touches the directory prefix or splits a UTF-8 sequence."""
    chunks = out.split(sep)[:-1]
    for chunk in chunks:
        runs = parse_runs(chunk)
        text = b"".join(payload for _, payload in runs)
        start = basename_start(text)
        offset = 0
        for style, payload in runs:
            if style == 2:
                end = offset + len(payload)
                assert offset >= start, (
                    f"match run {payload!r} at {offset} starts in the prefix "
                    f"of {text!r} (basename at {start})"
                )
                assert text[offset] & 0xC0 != 0x80, (
                    f"match run starts inside a UTF-8 sequence in {text!r}"
                )
                assert end == len(text) or text[end] & 0xC0 != 0x80, (
                    f"match run ends inside a UTF-8 sequence in {text!r}"
                )
            offset += len(payload)


def has_color(res: Result) -> bool:
    return ESC in res.out


def base_env(home: Path, **extra: str) -> dict[str, str]:
    """A minimal environment with an isolated HOME and no color variables."""
    env = {"HOME": str(home)}
    env.update(extra)
    return env


def touch_tree(root: Path, paths: list[str]) -> None:
    for path in paths:
        target = root / path
        if path.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An empty HOME with the zfind config directory, but no config file."""
    path = tmp_path / "home"
    (path / ".config" / "zfind").mkdir(parents=True)
    return path


@pytest.fixture
def color_tree(tmp_path: Path) -> Path:
    """One directory and one file, so auto has something to color."""
    root = tmp_path / "tree"
    (root / "d").mkdir(parents=True)
    (root / "d" / "f").write_bytes(b"")
    return root


class ColorConfig:
    """Runs zfind over a small tree with an explicit config file."""

    def __init__(self, binary: Path, tree: Path, home: Path) -> None:
        self.binary = binary
        self.tree = tree
        self.home = home
        self.path = home / ".config" / "zfind" / "config"

    def write(self, content: str | bytes) -> Path:
        data = content.encode() if isinstance(content, str) else content
        self.path.write_bytes(data)
        return self.path

    def __call__(
        self,
        *args: str,
        env: Optional[Mapping[str, str]] = None,
        terminal: bool = False,
    ) -> Result:
        full = base_env(self.home)
        full.update(env or {})
        runner = run_pty if terminal else run_env
        return runner(self.binary, self.tree, *args, env=full)

    def error(self, line: int, message: bytes, path: Optional[Path] = None) -> bytes:
        where = os.fsencode(str(path or self.path)) + (b":%d" % line if line else b"")
        return b"zfind: " + where + b": " + message


@pytest.fixture
def cfg(zfind_path: Path, color_tree: Path, home: Path) -> ColorConfig:
    return ColorConfig(zfind_path, color_tree, home)


# -----------------------------------------------------------------------------
# Parsing
# -----------------------------------------------------------------------------

class TestColorParsing:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--color=always",), id="always-equals"),
            pytest.param(("--color", "always"), id="always-next"),
            pytest.param(("--color=auto",), id="auto-equals"),
            pytest.param(("--color", "auto"), id="auto-next"),
            pytest.param(("--color=never",), id="never-equals"),
            pytest.param(("--color", "never"), id="never-next"),
        ],
    )
    def test_valid_values_are_accepted(
        self, zfind_path: Path, color_tree: Path, home: Path, args: tuple[str, ...]
    ):
        res = run_env(zfind_path, color_tree, *args, env=base_env(home))
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--color",), id="missing-next"),
            pytest.param(("--color=",), id="empty-equals"),
            pytest.param(("--color", ""), id="empty-next"),
            pytest.param(("--color", "foo"), id="invalid-next"),
            pytest.param(("--color=foo",), id="invalid-equals"),
            pytest.param(("--color=Auto",), id="uppercase"),
            pytest.param(("--color", "Always"), id="capitalized"),
            pytest.param(("--color=alway",), id="prefix"),
            pytest.param(("--color", "--config", "x"), id="value-looks-like-option"),
        ],
    )
    def test_invalid_values_use_one_message(
        self, zfind_path: Path, color_tree: Path, home: Path, args: tuple[str, ...]
    ):
        res = run_env(zfind_path, color_tree, *args, env=base_env(home))
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.out == b""
        assert res.err == COLOR_ERROR

    def test_the_value_is_required_not_a_pattern(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "foo").write_bytes(b"")

        res = run_env(zfind_path, tree, "--color", "foo", env=base_env(home))
        assert res.status == 1
        assert res.out == b""
        assert res.err == COLOR_ERROR

    def test_last_value_wins(self, zfind_path: Path, color_tree: Path, home: Path):
        never = run_env(
            zfind_path, color_tree, "--color=always", "--color=never", env=base_env(home)
        )
        assert never.status == 0 and not has_color(never)

        always = run_env(
            zfind_path, color_tree, "--color=never", "--color=always", env=base_env(home)
        )
        assert always.status == 0 and has_color(always)

    def test_after_double_dash_color_is_a_literal_pattern(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "--color").write_bytes(b"")

        res = run_env(
            zfind_path, tree, "--color=always", "--", "--color", env=base_env(home)
        )
        assert res == Result(out=MATCH + b"--color" + RESET + b"\n", err=b"", status=0)

    def test_help_lists_the_option(self, zfind: ZfindRunner):
        out = zfind("tiny", "--help").out
        assert b"--color" in out
        assert b"auto, always or never" in out
        assert b"NO_COLOR" in out
        assert b"TERM=dumb" in out


# -----------------------------------------------------------------------------
# Rendering: every example from the plan, as exact bytes
# -----------------------------------------------------------------------------

class TestRenderExamples:
    def test_file_without_pattern(self, zfind_path: Path, tmp_path: Path, home: Path):
        tree = tmp_path / "tree"
        (tree / "src/cache/foobar").mkdir(parents=True)
        (tree / "src/cache/foobar/1.txt").write_bytes(b"")

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "f", env=base_env(home)
        )
        expected = CYAN + b"src/cache/foobar/" + RESET + b"1.txt\n"
        assert res == Result(out=expected, err=b"", status=0)

    def test_file_with_one_match(self, zfind_path: Path, tmp_path: Path, home: Path):
        tree = tmp_path / "tree"
        (tree / "src/cache/foobar").mkdir(parents=True)
        (tree / "src/cache/foobar/1.txt").write_bytes(b"")

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "f", "1", env=base_env(home)
        )
        expected = (
            CYAN + b"src/cache/foobar/" + RESET
            + MATCH + b"1" + RESET + b".txt\n"
        )
        assert res == Result(out=expected, err=b"", status=0)

    def test_parent_directory_is_never_highlighted(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        (tree / "src/foo").mkdir(parents=True)
        (tree / "src/foo/foobar.txt").write_bytes(b"")

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "f", "foo", env=base_env(home)
        )
        expected = (
            CYAN + b"src/foo/" + RESET
            + MATCH + b"foo" + RESET + b"bar.txt\n"
        )
        assert res == Result(out=expected, err=b"", status=0)

    def test_adjacent_matches_merge(self, zfind_path: Path, tmp_path: Path, home: Path):
        tree = tmp_path / "tree"
        (tree / "docs").mkdir(parents=True)
        (tree / "docs/abab_ab.md").write_bytes(b"")

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "f", "ab", env=base_env(home)
        )
        expected = (
            CYAN + b"docs/" + RESET
            + MATCH + b"abab" + RESET + b"_"
            + MATCH + b"ab" + RESET + b".md\n"
        )
        assert res == Result(out=expected, err=b"", status=0)

    def test_directory_without_pattern(self, zfind_path: Path, tmp_path: Path, home: Path):
        tree = tmp_path / "tree"
        (tree / "src/cache").mkdir(parents=True)

        res = run_env(zfind_path, tree, "--color=always", "--type", "d", env=base_env(home))
        expected = (
            CYAN + b"src/" + RESET + b"\n"
            + CYAN + b"src/cache/" + RESET + b"\n"
        )
        assert res == Result(out=expected, err=b"", status=0)

    def test_directory_match_in_the_middle(self, zfind_path: Path, tmp_path: Path, home: Path):
        tree = tmp_path / "tree"
        (tree / "src/cache").mkdir(parents=True)

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "d", "ach", env=base_env(home)
        )
        expected = (
            CYAN + b"src/c" + RESET
            + MATCH + b"ach" + RESET
            + CYAN + b"e/" + RESET + b"\n"
        )
        assert res == Result(out=expected, err=b"", status=0)

    def test_directory_match_at_the_start_has_no_empty_run(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        (tree / "cache").mkdir(parents=True)

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "d", "cache", env=base_env(home)
        )
        expected = MATCH + b"cache" + RESET + CYAN + b"/" + RESET + b"\n"
        assert res == Result(out=expected, err=b"", status=0)

    def test_links_render_like_files(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        (tree / "dir").mkdir(parents=True)
        (tree / "file").write_bytes(b"")
        (tree / "docs").mkdir()
        (tree / "docs/lnk_dir").symlink_to("../dir")
        (tree / "docs/lnk_file").symlink_to("../file")

        res = run_env(
            zfind_path, tree, "--color=always", "--type", "l", env=base_env(home)
        )
        expected = (
            CYAN + b"docs/" + RESET + b"lnk_dir\n"
            + CYAN + b"docs/" + RESET + b"lnk_file\n"
        )
        assert res == Result(out=expected, err=b"", status=0)

    def test_smart_case_highlights_the_original_casing(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "Foo.txt").write_bytes(b"")

        lower = run_env(zfind_path, tree, "--color=always", "foo", env=base_env(home))
        assert lower == Result(out=MATCH + b"Foo" + RESET + b".txt\n", err=b"", status=0)

        # An uppercase letter makes the pattern case-sensitive; Foo still matches.
        upper = run_env(zfind_path, tree, "--color=always", "Foo", env=base_env(home))
        assert upper == Result(
            out=MATCH + b"Foo" + RESET + b".txt\n", err=b"", status=0
        )

        # A lowercase name is not listed by the sensitive pattern.
        (tree / "Foo.txt").unlink()
        (tree / "foo.txt").write_bytes(b"")
        upper = run_env(zfind_path, tree, "--color=always", "Foo", env=base_env(home))
        assert upper == Result(out=b"", err=b"", status=0)

    def test_case_options_control_highlighting(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "Foo.txt").write_bytes(b"")

        insensitive = run_env(
            zfind_path, tree, "--color=always", "--case=insensitive", "FOO",
            env=base_env(home),
        )
        assert insensitive == Result(
            out=MATCH + b"Foo" + RESET + b".txt\n", err=b"", status=0
        )

        sensitive = run_env(
            zfind_path, tree, "--color=always", "--case=sensitive", "foo",
            env=base_env(home),
        )
        assert sensitive == Result(out=b"", err=b"", status=0)

    @pytest.mark.parametrize("pattern", [(), ("",)], ids=["no-pattern", "empty-pattern"])
    def test_no_pattern_highlights_nothing(
        self, zfind_path: Path, tmp_path: Path, home: Path, pattern: tuple[str, ...]
    ):
        tree = tmp_path / "tree"
        (tree / "dir").mkdir(parents=True)
        (tree / "dir/file").write_bytes(b"")

        res = run_env(zfind_path, tree, "--color=always", *pattern, env=base_env(home))
        assert res.status == 0 and res.err == b""
        assert MATCH not in res.out
        assert res.out == (
            CYAN + b"dir/" + RESET + b"\n"
            + CYAN + b"dir/" + RESET + b"file\n"
        )

    def test_spaces_and_multibyte_names(self, zfind_path: Path, tmp_path: Path, home: Path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "sp ace.txt").write_bytes(b"")
        (tree / os.fsdecode(b"caf\xc3\xa9.txt")).write_bytes(b"")

        space = run_env(zfind_path, tree, "--color=always", "ace", env=base_env(home))
        assert space == Result(
            out=b"sp " + MATCH + b"ace" + RESET + b".txt\n", err=b"", status=0
        )

        # A match that ends right before a multi-byte character stays whole.
        multibyte = run_env(
            zfind_path, tree, "--color=always", "af", env=base_env(home)
        )
        assert multibyte == Result(
            out=b"c" + MATCH + b"af" + RESET + b"\xc3\xa9.txt\n", err=b"", status=0
        )
        assert_match_runs_are_in_basenames(multibyte.out, b"\n")

    def test_multibyte_pattern_highlights_whole_sequence(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / os.fsdecode(b"caf\xc3\xa9.txt")).write_bytes(b"")

        res = run_env(
            zfind_path, tree, "--color=always", os.fsdecode(b"\xc3\xa9"),
            env=base_env(home),
        )
        assert res == Result(
            out=b"caf" + MATCH + b"\xc3\xa9" + RESET + b".txt\n", err=b"", status=0
        )

    def test_invalid_utf8_pattern_does_not_split_a_sequence(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / os.fsdecode(b"caf\xc3\xa9.txt")).write_bytes(b"")

        # A lone continuation byte is not valid UTF-8, but argv can carry it.
        # It matches the second byte of the two-byte 'é'; the renderer must
        # leave the sequence whole, so the entry is printed unhighlighted.
        res = run_env(
            zfind_path, tree, "--color=always", os.fsdecode(b"\xa9"),
            env=base_env(home),
        )
        assert res == Result(out=b"caf\xc3\xa9.txt\n", err=b"", status=0)

    def test_nul_separator_with_forced_color(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        (tree / "src").mkdir(parents=True)
        (tree / "src/foo.txt").write_bytes(b"")

        colored = run_env(
            zfind_path, tree, "--color=always", "--type", "f",
            "--separator=\\0", "foo", env=base_env(home),
        )
        expected = CYAN + b"src/" + RESET + MATCH + b"foo" + RESET + b".txt\0"
        assert colored == Result(out=expected, err=b"", status=0)
        assert_closed_styles(colored.out, b"\0")

        plain = run_env(
            zfind_path, tree, "--color=never", "--type", "f",
            "--separator=\\0", "foo", env=base_env(home),
        )
        assert plain == Result(out=b"src/foo.txt\0", err=b"", status=0)

    def test_color_off_is_the_plain_output(
        self, zfind_path: Path, tmp_path: Path, home: Path
    ):
        tree = tmp_path / "tree"
        (tree / "src/cache").mkdir(parents=True)
        (tree / "src/cache/1.txt").write_bytes(b"")

        res = run_env(zfind_path, tree, "--color=never", env=base_env(home))
        expected = b"src/\nsrc/cache/\nsrc/cache/1.txt\n"
        assert res == Result(out=expected, err=b"", status=0)


# -----------------------------------------------------------------------------
# Mode resolution
# -----------------------------------------------------------------------------

class TestModeResolution:
    def test_pipe_default_is_plain(self, zfind_path: Path, color_tree: Path, home: Path):
        res = run_env(zfind_path, color_tree, env=base_env(home))
        assert res.status == 0 and not has_color(res)

    def test_pipe_with_term_is_still_plain(
        self, zfind_path: Path, color_tree: Path, home: Path
    ):
        res = run_env(
            zfind_path, color_tree, env=base_env(home, TERM="xterm-256color")
        )
        assert not has_color(res)

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({}, id="no-variables"),
            pytest.param({"TERM": "xterm-256color"}, id="term"),
            pytest.param({"TERM": "dumb"}, id="term-dumb"),
            pytest.param({"NO_COLOR": "1", "TERM": "xterm-256color"}, id="no-color"),
            pytest.param({"NO_COLOR": "0", "TERM": "xterm-256color"}, id="no-color-zero"),
        ],
    )
    def test_always_overrides_the_environment(
        self, zfind_path: Path, color_tree: Path, home: Path, env: dict[str, str]
    ):
        res = run_env(
            zfind_path, color_tree, "--color=always", env=base_env(home, **env)
        )
        assert res.status == 0 and has_color(res)

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({}, id="no-variables"),
            pytest.param({"TERM": "xterm-256color"}, id="term"),
            pytest.param({"TERM": "dumb"}, id="term-dumb"),
            pytest.param({"NO_COLOR": "1", "TERM": "xterm-256color"}, id="no-color"),
        ],
    )
    def test_never_ignores_the_environment(
        self, zfind_path: Path, color_tree: Path, home: Path, env: dict[str, str]
    ):
        res = run_env(
            zfind_path, color_tree, "--color=never", env=base_env(home, **env)
        )
        assert res.status == 0 and not has_color(res)

    @pytest.mark.parametrize(
        ("env", "colored"),
        [
            pytest.param({"TERM": "xterm-256color"}, True, id="terminal"),
            pytest.param({"TERM": "xterm"}, True, id="terminal-short-term"),
            pytest.param({}, False, id="term-unset"),
            pytest.param({"TERM": "dumb"}, False, id="term-dumb"),
            pytest.param({"TERM": "dumbish"}, True, id="term-prefix-not-dumb"),
            pytest.param({"TERM": "xterm", "NO_COLOR": "1"}, False, id="no-color-set"),
            pytest.param({"TERM": "xterm", "NO_COLOR": "0"}, False, id="no-color-non-empty"),
            pytest.param({"TERM": "xterm", "NO_COLOR": ""}, True, id="no-color-empty"),
            pytest.param({"NO_COLOR": "1"}, False, id="no-color-and-no-term"),
        ],
    )
    def test_auto_on_a_terminal(
        self,
        zfind_path: Path,
        color_tree: Path,
        home: Path,
        env: dict[str, str],
        colored: bool,
    ):
        res = run_pty(zfind_path, color_tree, env=base_env(home, **env))
        assert res.status == 0, f"Command failed: {res}"
        assert has_color(res) is colored

    @pytest.mark.parametrize(
        ("env", "args", "colored"),
        [
            pytest.param(
                {"TERM": "xterm", "NO_COLOR": "1"}, ("--color=always",), True,
                id="always-beats-no-color",
            ),
            pytest.param(
                {"TERM": "dumb"}, ("--color=always",), True,
                id="always-beats-dumb",
            ),
            pytest.param(
                {"TERM": "xterm"}, ("--color=never",), False,
                id="never-on-terminal",
            ),
            pytest.param(
                {"TERM": "xterm", "NO_COLOR": "1"}, ("--color=auto",), False,
                id="explicit-auto-honors-no-color",
            ),
            pytest.param(
                {"TERM": "xterm", "NO_COLOR": ""}, ("--color=auto",), True,
                id="explicit-auto-empty-no-color",
            ),
        ],
    )
    def test_explicit_values_on_a_terminal(
        self,
        zfind_path: Path,
        color_tree: Path,
        home: Path,
        env: dict[str, str],
        args: tuple[str, ...],
        colored: bool,
    ):
        res = run_pty(zfind_path, color_tree, *args, env=base_env(home, **env))
        assert res.status == 0, f"Command failed: {res}"
        assert has_color(res) is colored

    def test_mode_is_decided_once_for_the_whole_run(
        self, zfind: ZfindRunner
    ):
        res = zfind("tree2", "--color=always", "--type", "d", "--limit", "50")
        assert res.status == 0 and res.err == b""
        lines = entries(res.out)
        assert len(lines) == 50
        assert all(line.startswith(CYAN) and line.endswith(RESET) for line in lines)


# -----------------------------------------------------------------------------
# Config file
# -----------------------------------------------------------------------------

class TestColorConfig:
    def test_config_never_on_a_terminal(self, cfg: ColorConfig):
        cfg.write("--color never\n")
        res = cfg("--type", "f", terminal=True, env={"TERM": "xterm"})
        assert res.status == 0 and not has_color(res)

    def test_config_never_without_config_is_colored(self, cfg: ColorConfig):
        res = cfg("--type", "f", terminal=True, env={"TERM": "xterm"})
        assert res.status == 0 and has_color(res)

    def test_command_line_overrides_config(self, cfg: ColorConfig):
        cfg.write("--color never\n")
        res = cfg("--color=always", "--type", "f", terminal=True, env={"TERM": "xterm"})
        assert res.status == 0 and has_color(res)

    def test_config_auto_still_honors_no_color(self, cfg: ColorConfig):
        cfg.write("--color auto\n")
        res = cfg("--type", "f", terminal=True, env={"TERM": "xterm", "NO_COLOR": "1"})
        assert res.status == 0 and not has_color(res)

    def test_last_value_wins_within_the_config(self, cfg: ColorConfig):
        cfg.write("--color auto\n--color never\n")
        res = cfg("--type", "f", terminal=True, env={"TERM": "xterm"})
        assert res.status == 0 and not has_color(res)

        cfg.write("--color never\n--color auto\n")
        res = cfg("--type", "f", terminal=True, env={"TERM": "xterm"})
        assert res.status == 0 and has_color(res)

    def test_config_auto_is_plain_on_a_pipe(self, cfg: ColorConfig):
        cfg.write("--color auto\n")
        res = cfg("--type", "f", env={"TERM": "xterm"})
        assert res.status == 0 and not has_color(res)

    @pytest.mark.parametrize("line", ["--color always", "--color=always"])
    def test_config_always_is_rejected(self, cfg: ColorConfig, line: str):
        path = cfg.write(line + "\n")
        res = cfg("--type", "f")
        assert res.status == 1
        assert res.out == b""
        assert res.err == cfg.error(1, CFG_ALWAYS_ERROR, path)

    def test_config_always_error_names_the_line(self, cfg: ColorConfig):
        path = cfg.write("# a comment\n--color always\n")
        res = cfg()
        assert res.status == 1
        assert res.err == cfg.error(2, CFG_ALWAYS_ERROR, path)

    def test_config_always_is_rejected_even_when_the_cli_overrides(
        self, cfg: ColorConfig
    ):
        path = cfg.write("--color always\n")
        res = cfg("--color=never")
        assert res.status == 1
        assert res.err == cfg.error(1, CFG_ALWAYS_ERROR, path)

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("--color foo", id="invalid"),
            pytest.param("--color=foo", id="invalid-equals"),
            pytest.param("--color", id="missing-value"),
            pytest.param("--color=", id="empty-value"),
            pytest.param("--color Auto", id="uppercase"),
        ],
    )
    def test_config_invalid_values_keep_the_location_prefix(
        self, cfg: ColorConfig, line: str
    ):
        path = cfg.write(line + "\n")
        res = cfg()
        assert res.status == 1
        assert res.out == b""
        assert res.err == cfg.error(1, COLOR_MESSAGE, path)

    def test_explicit_config_path_is_used_in_the_error(
        self, zfind_path: Path, color_tree: Path, home: Path, tmp_path: Path
    ):
        path = tmp_path / "my config"
        path.write_bytes(b"--color always\n")
        res = run_env(
            zfind_path, color_tree, "--config", str(path), env=base_env(home)
        )
        assert res.status == 1
        assert res.err == (
            b"zfind: " + os.fsencode(str(path)) + b":1: " + CFG_ALWAYS_ERROR
        )

    def test_config_color_does_not_break_the_config_prescan(
        self, zfind_path: Path, color_tree: Path, home: Path, tmp_path: Path
    ):
        path = tmp_path / "config"
        path.write_bytes(b"--color never\n")
        res = run_env(
            zfind_path, color_tree, "--config", str(path), "--type", "f",
            env=base_env(home),
        )
        assert res.status == 0 and res.err == b""
        assert res.out == b"d/f\n"


# -----------------------------------------------------------------------------
# Invariants over the fixture trees
# -----------------------------------------------------------------------------

# (tree, pattern or None, other args, separator)
INVARIANT_QUERIES = [
    ("tiny", None, (), b"\n"),
    ("tiny", "", (), b"\n"),
    ("tiny", "c", (), b"\n"),
    ("tiny", "c", ("--limit", "2"), b"\n"),
    ("tiny", None, ("--limit", "0"), b"\n"),
    ("tiny", None, ("--type", "d"), b"\n"),
    ("tiny", None, ("--type", "f"), b"\n"),
    ("tiny", None, ("--separator=XY",), b"XY"),
    ("tiny", "i", ("--type", "d", "--separator=" + "S" * 64), b"S" * 64),
    ("tiny", "i", ("--type", "d", "--separator=\\0"), b"\0"),
    ("hidden", None, (), b"\n"),
    ("hidden", None, ("--no-hidden",), b"\n"),
    ("hidden", ".dot", (), b"\n"),
    ("ignores", None, ("--gitignore",), b"\n"),
    ("ignores", None, ("--gitignore", "--no-hidden"), b"\n"),
    ("weird", None, ("--separator=\\0",), b"\0"),
    ("weird", "a", ("--separator=\\0",), b"\0"),
    ("weird", "caf", ("--separator=\\0",), b"\0"),
    ("weird", os.fsdecode(b"\xc3\xa9"), ("--separator=\\0",), b"\0"),
    ("weird", "hi", ("--separator=\\0",), b"\0"),
    ("weird", "z", ("--separator=\\0", "--limit", "5"), b"\0"),
    ("weird", None, ("--separator=XY", "--limit", "7"), b"XY"),
    ("tree2", None, ("--limit", "100"), b"\n"),
    ("camera", "IMG", ("--limit", "50"), b"\n"),
]


class TestColorInvariants:
    @pytest.mark.parametrize(
        ("tree", "pattern", "args", "sep"),
        INVARIANT_QUERIES,
        ids=[f"{tree}-{pattern!r}-{args}" for tree, pattern, args, _ in INVARIANT_QUERIES],
    )
    def test_strip_equality_and_never_equals_auto(
        self,
        zfind: ZfindRunner,
        tree: str,
        pattern: Optional[str],
        args: tuple[str, ...],
        sep: bytes,
    ):
        argv = (*args, *((pattern,) if pattern is not None else ()))
        always = zfind(tree, "--color=always", *argv)
        never = zfind(tree, "--color=never", *argv)
        auto = zfind(tree, "--color=auto", *argv)
        assert always.status == never.status == auto.status == 0
        assert always.err == never.err == auto.err == b""
        assert never.out == auto.out, "--color=never and auto on a pipe must agree"
        assert ESC not in never.out
        assert strip_color(always.out) == never.out
        assert_closed_styles(always.out, sep)

    @pytest.mark.parametrize(
        ("tree", "pattern", "args", "sep"),
        INVARIANT_QUERIES,
        ids=[f"{tree}-{pattern!r}-{args}" for tree, pattern, args, _ in INVARIANT_QUERIES],
    )
    def test_highlight_coverage_and_basename_only(
        self,
        zfind: ZfindRunner,
        tree: str,
        pattern: Optional[str],
        args: tuple[str, ...],
        sep: bytes,
    ):
        argv = (*args, *((pattern,) if pattern is not None else ()))
        always = zfind(tree, "--color=always", *argv)
        assert always.status == 0 and always.err == b""

        if pattern:
            chunks = always.out.split(sep)[:-1]
            assert chunks, f"pattern {pattern!r} produced no entries in {tree}"
            for chunk in chunks:
                assert MATCH in chunk, f"entry without a highlight: {chunk!r}"
        else:
            assert MATCH not in always.out

        assert_match_runs_are_in_basenames(always.out, sep)


# -----------------------------------------------------------------------------
# Worst-case colored line and block boundaries
# -----------------------------------------------------------------------------

PATH_LIMIT = 4096
DEEP_DEPTH = 19
DEEP_COMPONENT = 200
DEEP_FILES = 10


@pytest.fixture(scope="module")
def deep_tree(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[bytes]]:
    """A near-PATH_MAX path whose basename alternates matches and non-matches.

    Each 'a' becomes its own match run, so the line is several times longer
    than its path. Ten such lines overflow one 48 KiB output block, which puts
    a colored frame at a block boundary; --limit frames never span blocks.

    The tree is built with dir_fd-relative syscalls: the relative path zfind
    walks is near PATH_MAX, so the absolute path under pytest's tmp root would
    otherwise exceed PATH_MAX before the fixture could create it.
    """
    root = tmp_path_factory.mktemp("color-deep")
    parts = ["d" * DEEP_COMPONENT] * DEEP_DEPTH
    names = ["ab" * 124 + f"{i:02d}" for i in range(DEEP_FILES)]  # 250 bytes

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            os.mkdir(part, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        for name in names:
            file_fd = os.open(name, os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
            os.close(file_fd)
    finally:
        os.close(fd)

    rel = [("/".join(parts + [name])).encode() for name in names]
    assert all(len(path) < PATH_LIMIT for path in rel)
    assert len(rel[0]) > PATH_LIMIT - 100, "path is not near PATH_MAX"
    return root, rel


def expected_colored_line(rel: bytes) -> bytes:
    prefix, base = rel.rsplit(b"/", 1)
    body = (MATCH + b"a" + RESET + b"b") * 124 + base[-2:]
    return CYAN + prefix + b"/" + RESET + body + b"\n"


class TestColorWorstCase:
    def test_worst_case_line_is_correct_and_strippable(
        self, zfind_path: Path, home: Path, deep_tree: tuple[Path, list[bytes]]
    ):
        root, rel = deep_tree
        env = base_env(home)
        always = run_env(zfind_path, root, "--color=always", "a", env=env)
        never = run_env(zfind_path, root, "--color=never", "a", env=env)
        assert always.status == never.status == 0
        assert always.err == never.err == b""

        expected = b"".join(expected_colored_line(path) for path in rel)
        assert always.out == expected
        assert strip_color(always.out) == never.out
        assert_closed_styles(always.out, b"\n")
        assert_match_runs_are_in_basenames(always.out, b"\n")

    def test_worst_case_line_with_limit(
        self, zfind_path: Path, home: Path, deep_tree: tuple[Path, list[bytes]]
    ):
        root, rel = deep_tree
        env = base_env(home)
        full = run_env(zfind_path, root, "--color=always", "a", env=env)
        assert full.status == 0 and full.err == b""
        plain_full = run_env(zfind_path, root, "--color=never", "a", env=env)
        assert plain_full.status == 0

        for n in (1, DEEP_FILES - 1, DEEP_FILES, DEEP_FILES + 1):
            for color in ("always", "never"):
                res = run_env(
                    zfind_path, root, f"--color={color}", "--limit", str(n), "a",
                    env=env,
                )
                assert res.status == 0 and res.err == b""
                if color == "always":
                    expected = b"".join(
                        expected_colored_line(path) for path in rel[:n]
                    )
                    assert res.out == expected, f"--limit {n} with --color={color}"
                    assert res.out == full.out[:len(expected)]
                else:
                    expected = b"".join(path + b"\n" for path in rel[:n])
                    assert res.out == expected, f"--limit {n} with --color={color}"
                    assert res.out == plain_full.out[:len(expected)]


# -----------------------------------------------------------------------------
# The renderer is shared by every build variant
# -----------------------------------------------------------------------------

class TestColorBuildVariants:
    @pytest.mark.parametrize(
        "variant",
        ["serial_zfind_path", "unknown_zfind_path", "range_zfind_path"],
    )
    def test_color_is_identical_across_builds(
        self,
        request: pytest.FixtureRequest,
        variant: str,
        zfind_path: Path,
        color_tree: Path,
        home: Path,
    ):
        binary = request.getfixturevalue(variant)
        args = ("--color=always", "--type", "f", "f")
        expected = run_env(zfind_path, color_tree, *args, env=base_env(home))
        res = run_env(binary, color_tree, *args, env=base_env(home))
        assert res == expected
