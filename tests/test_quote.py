"""Tests for --quote: shell-quoted output that survives a paste."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest
from helpers import ZfindRunner, entries, run_in, run_pty
from test_config import ConfigRunner

# The diagnostic mirrors --color's, with the option name swapped. ConfigRunner
# adds the trailing newline itself.
QUOTE_MESSAGE = "expected auto, always or never after --quote"
QUOTE_ERROR = b"zfind: " + QUOTE_MESSAGE.encode() + b"\n"
CFG_ALWAYS_MESSAGE = (
    "--quote always is not allowed in the config file (use auto or never)"
)

STYLE = re.compile(rb"\x1b\[[0-9;]*m")


def unstyled(data: bytes) -> bytes:
    """Drop the ANSI styling sequences, leaving the printed bytes."""
    return STYLE.sub(b"", data)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An empty HOME with the zfind config directory, but no config file."""
    path = tmp_path / "home"
    (path / ".config" / "zfind").mkdir(parents=True)
    return path


@pytest.fixture
def quote_tree(tmp_path: Path) -> Path:
    """Three directories: two need quoting, one does not."""
    root = tmp_path / "tree"
    for name in ("don't", "plain", "sp ace"):
        (root / name).mkdir(parents=True)
    return root


@pytest.fixture
def one_file_tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a").write_bytes(b"")
    return root


# -----------------------------------------------------------------------------
# Parsing
# -----------------------------------------------------------------------------

class TestQuoteParsing:
    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--quote=always",), id="always-equals"),
            pytest.param(("--quote", "always"), id="always-next"),
            pytest.param(("--quote=auto",), id="auto-equals"),
            pytest.param(("--quote", "auto"), id="auto-next"),
            pytest.param(("--quote=never",), id="never-equals"),
            pytest.param(("--quote", "never"), id="never-next"),
        ],
    )
    def test_valid_values_are_accepted(
        self, binary: Path, quote_tree: Path, args: tuple[str, ...]
    ):
        res = run_in(binary, quote_tree, *args)
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(("--quote",), id="missing-next"),
            pytest.param(("--quote=",), id="empty-equals"),
            pytest.param(("--quote", ""), id="empty-next"),
            pytest.param(("--quote", "foo"), id="invalid-next"),
            pytest.param(("--quote=foo",), id="invalid-equals"),
            pytest.param(("--quote=Auto",), id="uppercase"),
            pytest.param(("--quote", "Always"), id="capitalized"),
            pytest.param(("--quote=alway",), id="prefix"),
            pytest.param(("--quote", "--config", "x"), id="value-looks-like-option"),
        ],
    )
    def test_invalid_values_use_one_message(
        self, binary: Path, tmp_path: Path, args: tuple[str, ...]
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        res = run_in(binary, tree, *args)
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.out == b""
        assert res.err == QUOTE_ERROR

    def test_the_value_is_required_not_a_pattern(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "foo").write_bytes(b"")

        res = run_in(binary, tree, "--quote", "foo")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.out == b""
        assert res.err == QUOTE_ERROR

    def test_last_value_wins(self, binary: Path, quote_tree: Path):
        quoted = run_in(binary, quote_tree, "--quote=never", "--quote=always")
        assert quoted.status == 0, f"Command failed: {quoted}"
        assert b'"sp ace/"' in quoted.out

        literal = run_in(binary, quote_tree, "--quote=always", "--quote=never")
        assert literal.status == 0, f"Command failed: {literal}"
        assert b'"' not in literal.out

    def test_value_is_consumed_greedily(self, binary: Path, one_file_tree: Path):
        # --no-config is a value here, not an option: the same rule as --separator.
        res = run_in(binary, one_file_tree, "--quote", "--no-config")
        assert res.status == 1, f"Expected exit 1: {res}"
        assert res.out == b""
        assert res.err == QUOTE_ERROR

    def test_help_lists_the_option(self, zfind: ZfindRunner):
        out = zfind("tiny", "--help").out
        assert b"--quote" in out
        assert b"auto, always or never" in out


# -----------------------------------------------------------------------------
# Paths that stay bare
# -----------------------------------------------------------------------------

class TestQuoteBare:
    def test_bare_paths_are_unchanged(self, binary: Path, tmp_path: Path):
        root = tmp_path / "tree"
        dirs = [b"reports", b".cache", b"Caf\xc3\xa9", b"alpha-beta"]
        files = [b"notes.txt", b"a+b,c:d@e%f_g.h-i", b"plain"]
        for name in dirs:
            (root / os.fsdecode(name)).mkdir(parents=True)
        for name in files:
            (root / os.fsdecode(name)).write_bytes(b"")

        expected = sorted([name + b"/" for name in dirs] + files)
        res = run_in(binary, root, "--quote=always")
        assert res.status == 0 and res.err == b""
        assert res.out == b"".join(name + b"\n" for name in expected)

    def test_a_pipe_sees_the_same_bytes(self, binary: Path, testdata: Path):
        tree = testdata / "weird"
        baseline = run_in(binary, tree)
        assert run_in(binary, tree, "--quote=auto") == baseline
        assert run_in(binary, tree, "--quote=never") == baseline

    def test_leading_dash_is_quoted_but_not_rewritten(
        self, binary: Path, tmp_path: Path
    ):
        root = tmp_path / "tree"
        (root / "d").mkdir(parents=True)
        (root / "-dash").write_bytes(b"")
        (root / "d" / "-file").write_bytes(b"")

        res = run_in(binary, root, "--quote=always")
        assert res.status == 0 and res.err == b""
        # The path's first byte decides; a dash inside the path is still bare.
        assert res.out == b'"-dash"\nd/\nd/-file\n'

    @pytest.mark.parametrize("name", [b"a\tb", b"a\nb", b"a\x01b", b"a\x7fb"])
    def test_control_bytes_are_quoted_and_left_literal(
        self, binary: Path, tmp_path: Path, name: bytes
    ):
        root = tmp_path / "tree"
        root.mkdir()
        (root / os.fsdecode(name)).write_bytes(b"")

        res = run_in(binary, root, "--quote=always")
        assert res.status == 0 and res.err == b""
        assert res.out == b'"' + name + b'"\n'


# -----------------------------------------------------------------------------
# The quote-style table from the issue, as exact bytes
# -----------------------------------------------------------------------------

QUOTE_TABLE = [
    pytest.param(b"a b", b'"a b/"', id="space"),
    pytest.param(b"a'b", b'"a\'b/"', id="apostrophe"),
    pytest.param(b"a$b", b"'a$b/'", id="dollar"),
    pytest.param(b"a!b", b"'a!b/'", id="bang"),
    pytest.param(b'a"b', b"'a\"b/'", id="double-quote"),
    pytest.param(b"a\\b", b"'a\\b/'", id="backslash"),
    pytest.param(b"a'b$c", b"'a'\\''b$c/'", id="apostrophe-and-dollar"),
]


class TestQuoteExamples:
    @pytest.mark.parametrize(("name", "printed"), QUOTE_TABLE)
    def test_quote_style_table(
        self, binary: Path, tmp_path: Path, name: bytes, printed: bytes
    ):
        root = tmp_path / "tree"
        root.mkdir()
        (root / os.fsdecode(name)).mkdir()

        res = run_in(binary, root, "--quote=always", "--type", "d")
        assert res.status == 0 and res.err == b""
        assert res.out == printed + b"\n"

    def test_whole_path_including_the_trailing_slash(
        self, binary: Path, tmp_path: Path
    ):
        root = tmp_path / "tree"
        (root / "reports/2026-01-15/quarterly report (Q1)").mkdir(parents=True)
        (root / "reports/2026-01-15/$draft notes").mkdir()

        res = run_in(binary, root, "--quote=always", "--type", "d")
        assert res.status == 0 and res.err == b""
        lines = entries(res.out)
        assert b'"reports/2026-01-15/quarterly report (Q1)/"' in lines
        assert b"'reports/2026-01-15/$draft notes/'" in lines
        # Directories made only of bare bytes stay bare.
        assert b"reports/" in lines
        assert b"reports/2026-01-15/" in lines


# -----------------------------------------------------------------------------
# auto, never and the terminal
# -----------------------------------------------------------------------------

class TestQuoteTerminal:
    def test_auto_quotes_on_a_terminal(self, binary: Path, quote_tree: Path, home: Path):
        res = run_pty(binary, quote_tree, "--quote=auto", env={"HOME": str(home)})
        assert res.status == 0, f"Command failed: {res}"
        assert res.err == b""
        assert entries(res.out) == [b'"don\'t/"', b"plain/", b'"sp ace/"']

    def test_the_default_is_auto(self, binary: Path, quote_tree: Path, home: Path):
        res = run_pty(binary, quote_tree, env={"HOME": str(home)})
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == [b'"don\'t/"', b"plain/", b'"sp ace/"']

    def test_never_is_literal_on_a_terminal(
        self, binary: Path, quote_tree: Path, home: Path
    ):
        res = run_pty(binary, quote_tree, "--quote=never", env={"HOME": str(home)})
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == [b"don't/", b"plain/", b"sp ace/"]

    def test_last_value_wins_on_a_terminal(
        self, binary: Path, quote_tree: Path, home: Path
    ):
        never = run_pty(
            binary, quote_tree, "--quote=always", "--quote=never",
            env={"HOME": str(home)},
        )
        assert entries(never.out) == [b"don't/", b"plain/", b"sp ace/"]

        always = run_pty(
            binary, quote_tree, "--quote=never", "--quote=always",
            env={"HOME": str(home)},
        )
        assert entries(always.out) == [b'"don\'t/"', b"plain/", b'"sp ace/"']

    def test_only_the_isatty_test_decides(
        self, binary: Path, quote_tree: Path, home: Path
    ):
        res = run_pty(
            binary, quote_tree, "--quote=auto",
            env={"HOME": str(home), "TERM": "dumb", "NO_COLOR": "1"},
        )
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b'"don\'t/"', b"plain/", b'"sp ace/"']


# -----------------------------------------------------------------------------
# Quoting with --color and --limit
# -----------------------------------------------------------------------------

class TestQuoteColorAndLimit:
    @pytest.fixture
    def apostrophe_tree(self, tmp_path: Path) -> Path:
        root = tmp_path / "tree"
        root.mkdir()
        for i in range(10):
            (root / f"x'${i}").write_bytes(b"")
        return root

    def test_color_wraps_the_quoted_path_and_limit_is_a_prefix(
        self, binary: Path, apostrophe_tree: Path
    ):
        pattern = "'"
        plain = run_in(
            binary, apostrophe_tree, "--quote=always", "--color=never", pattern
        )
        colored = run_in(
            binary, apostrophe_tree, "--quote=always", "--color=always", pattern
        )
        assert plain.status == colored.status == 0
        assert plain.err == colored.err == b""
        assert unstyled(colored.out) == plain.out

        lines = entries(plain.out)
        assert len(lines) == 10
        for line in entries(colored.out):
            # Quotes sit outside the styled spans.
            assert line.startswith(b"'") and line.endswith(b"'")

        for line in lines:
            parsed = shlex.split(os.fsdecode(line))
            assert len(parsed) == 1
            assert parsed[0].startswith("x'$")

        for n in (1, 5, 9, 10, 11):
            limited = run_in(
                binary, apostrophe_tree, "--quote=always", "--color=always",
                "--limit", str(n), pattern,
            )
            assert limited.status == 0 and limited.err == b""
            assert limited.out == colored.out[: len(limited.out)]
            assert len(entries(limited.out)) == min(n, 10)


# -----------------------------------------------------------------------------
# Quoting with --limit and no --color: print_entry frames the quoted payload
# -----------------------------------------------------------------------------

class TestQuoteLimit:
    @pytest.fixture
    def limit_tree(self, tmp_path: Path) -> Path:
        """One bare path and both quote styles, in output order."""
        root = tmp_path / "tree"
        root.mkdir()
        (root / "a b").write_bytes(b"")
        (root / "c.d").write_bytes(b"")
        (root / "don't$").write_bytes(b"")
        (root / "e f").write_bytes(b"")
        return root

    QUOTED = (
        b'"a b"\n'
        b"c.d\n"
        b"'don'\\''t$'\n"
        b'"e f"\n'
    )

    def test_the_limit_prefix_is_the_quoted_bytes(
        self, binary: Path, limit_tree: Path
    ):
        full = run_in(binary, limit_tree, "--quote=always")
        assert full.status == 0 and full.err == b""
        assert full.out == self.QUOTED

        lines = entries(self.QUOTED)
        for n in (1, 3):
            limited = run_in(
                binary, limit_tree, "--quote=always", "--limit", str(n)
            )
            assert limited.status == 0 and limited.err == b""
            # Exact bytes for the first n: a payload length short by the two
            # quote bytes or by an escaped apostrophe would drop them.
            assert limited.out == b"".join(line + b"\n" for line in lines[:n])
            assert limited.out == full.out[: len(limited.out)]


# -----------------------------------------------------------------------------
# --separator and --absolute
# -----------------------------------------------------------------------------

class TestQuoteSeparator:
    def test_nul_separator_is_never_quoted(self, binary: Path, tmp_path: Path):
        root = tmp_path / "tree"
        root.mkdir()
        (root / "a b").write_bytes(b"")

        quoted = run_in(binary, root, "--quote=always", "--separator=\\0")
        assert quoted.status == 0 and quoted.err == b""
        assert quoted.out == b'"a b"\0'

        plain = run_in(binary, root, "--quote=never", "--separator=\\0")
        assert plain.status == 0 and plain.out == b"a b\0"


class TestQuoteAbsolute:
    def test_the_quoted_path_is_the_absolute_one(
        self, binary: Path, tmp_path: Path
    ):
        root = tmp_path / "tree"
        (root / "d").mkdir(parents=True)
        (root / "d" / "in ner").write_bytes(b"")
        prefix = os.fsencode(os.path.realpath(root))

        plain = run_in(binary, root, "--absolute", "--type", "f")
        quoted = run_in(binary, root, "--quote=always", "--absolute", "--type", "f")
        assert plain.status == quoted.status == 0
        assert plain.err == quoted.err == b""
        assert plain.out == prefix + b"/d/in ner\n"
        # One argument after the shell parses it, spelled absolutely.
        assert shlex.split(os.fsdecode(quoted.out)) == [
            os.fsdecode(prefix) + "/d/in ner"
        ]


# -----------------------------------------------------------------------------
# Config file
# -----------------------------------------------------------------------------

@pytest.fixture
def quote_cfg(binary: Path, tmp_path: Path, home: Path) -> ConfigRunner:
    root = tmp_path / "tree"
    root.mkdir()
    for name in ("a b", "c", "d"):
        (root / name).write_bytes(b"")
    return ConfigRunner(binary, root, home)


class TestQuoteConfig:
    @pytest.mark.parametrize("line", ["--quote always", "--quote=always"])
    def test_config_always_is_rejected(self, quote_cfg: ConfigRunner, line: str):
        path = quote_cfg.write(line + "\n")
        res = quote_cfg()
        assert res.status == 1
        assert res.out == b""
        assert res.err == quote_cfg.error(1, CFG_ALWAYS_MESSAGE, path)

    def test_config_always_error_names_the_line(self, quote_cfg: ConfigRunner):
        path = quote_cfg.write("# a comment\n--quote always\n")
        res = quote_cfg()
        assert res.status == 1
        assert res.out == b""
        assert res.err == quote_cfg.error(2, CFG_ALWAYS_MESSAGE, path)

    def test_config_always_is_rejected_even_when_the_cli_overrides(
        self, quote_cfg: ConfigRunner
    ):
        path = quote_cfg.write("--quote always\n")
        res = quote_cfg("--quote=never")
        assert res.status == 1
        assert res.err == quote_cfg.error(1, CFG_ALWAYS_MESSAGE, path)

    @pytest.mark.parametrize("value", ["auto", "never"])
    def test_config_auto_and_never_are_accepted(
        self, quote_cfg: ConfigRunner, value: str
    ):
        quote_cfg.write(f"--quote {value}\n")
        res = quote_cfg()
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b"a b", b"c", b"d"]

    def test_config_auto_quotes_on_a_terminal(
        self, quote_cfg: ConfigRunner, home: Path
    ):
        quote_cfg.write("--quote auto\n")
        res = run_pty(quote_cfg.binary, quote_cfg.tree, env={"HOME": str(home)})
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b'"a b"', b"c", b"d"]

    def test_config_never_is_literal_on_a_terminal(
        self, quote_cfg: ConfigRunner, home: Path
    ):
        quote_cfg.write("--quote never\n")
        res = run_pty(quote_cfg.binary, quote_cfg.tree, env={"HOME": str(home)})
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b"a b", b"c", b"d"]

    @pytest.mark.parametrize(
        "line", ["--quote foo", "--quote=foo", "--quote", "--quote="]
    )
    def test_config_invalid_values_keep_the_location_prefix(
        self, quote_cfg: ConfigRunner, line: str
    ):
        path = quote_cfg.write(line + "\n")
        res = quote_cfg()
        assert res.status == 1
        assert res.out == b""
        assert res.err == quote_cfg.error(1, QUOTE_MESSAGE, path)

    def test_quote_value_is_skipped_by_the_config_prescan(
        self, quote_cfg: ConfigRunner
    ):
        # If find_config's pre-scan does not skip the --quote value it misses
        # --no-config, reads this file, and applies --limit 1.
        quote_cfg.write("--limit 1\n")
        res = quote_cfg("--quote", "never", "--no-config")
        assert res.status == 0 and res.err == b""
        assert entries(res.out) == [b"a b", b"c", b"d"]


# -----------------------------------------------------------------------------
# Worst-case quoted lines and output blocks
# -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def apostrophe_chain(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[bytes]]:
    """A nested path of about 2000 apostrophes, so quoting must expand it."""
    root = tmp_path_factory.mktemp("quote-chain")
    part = "'" * 249 + "$"
    path = root
    relative: list[bytes] = []
    for _ in range(8):
        path = path / part
        path.mkdir()
        relative.append(os.fsencode(str(path.relative_to(root))) + b"/")
    return root, relative


@pytest.fixture(scope="module")
def apostrophe_block_tree(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, list[bytes]]:
    """70 directories of 248 apostrophes, enough quoted output to cross a block."""
    root = tmp_path_factory.mktemp("quote-block")
    names = ["'" * 248 + f"{i:04d}" + "$" for i in range(70)]
    for name in names:
        (root / name).mkdir()
    return root, sorted(os.fsencode(name) + b"/" for name in names)


class TestQuoteBlockIntegrity:
    def test_two_thousand_apostrophes_round_trip(
        self, binary: Path, apostrophe_chain: tuple[Path, list[bytes]]
    ):
        root, relative = apostrophe_chain
        res = run_in(binary, root, "--quote=always", "--color=always", "'")
        assert res.status == 0 and res.err == b""
        lines = entries(res.out)
        assert len(lines) == len(relative)
        for line, raw in zip(lines, relative):
            assert shlex.split(os.fsdecode(unstyled(line))) == [os.fsdecode(raw)]

    def test_quoted_lines_survive_block_boundaries(
        self, binary: Path, apostrophe_block_tree: tuple[Path, list[bytes]]
    ):
        root, expected = apostrophe_block_tree
        res = run_in(binary, root, "--quote=always", "--color=always", "'")
        assert res.status == 0 and res.err == b""
        lines = entries(res.out)
        assert len(lines) == len(expected)
        # The expanded output is larger than one block: it had to be split.
        assert len(res.out) > 65504
        names = [shlex.split(os.fsdecode(unstyled(line))) for line in lines]
        assert [args[0] for args in names] == [os.fsdecode(raw) for raw in expected]

        limited = run_in(
            binary, root, "--quote=always", "--color=always",
            "--limit", "10", "'",
        )
        assert limited.status == 0 and limited.err == b""
        assert limited.out == res.out[: len(limited.out)]
        assert len(entries(limited.out)) == 10

    def test_quoted_limit_without_color_survives_block_boundaries(
        self, binary: Path, apostrophe_block_tree: tuple[Path, list[bytes]]
    ):
        root, expected = apostrophe_block_tree
        full = run_in(binary, root, "--quote=always", "'")
        assert full.status == 0 and full.err == b""
        assert len(full.out) > 65504

        # limit_mode on with no --color sizes each frame from the quoted length;
        # stripping the frames must give the unlimited bytes back.
        limited = run_in(binary, root, "--quote=always", "--limit", "70", "'")
        assert limited.status == 0 and limited.err == b""
        assert limited.out == full.out

        names = [shlex.split(os.fsdecode(line)) for line in entries(full.out)]
        assert [args[0] for args in names] == [os.fsdecode(raw) for raw in expected]

        small = run_in(binary, root, "--quote=always", "--limit", "10", "'")
        assert small.status == 0 and small.err == b""
        assert small.out == full.out[: len(small.out)]
        assert len(entries(small.out)) == 10
