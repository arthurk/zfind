"""Tests for the user config file: location, syntax, errors and precedence."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional

import pytest
from conftest import Result
from test_flags import entries, joined

CFG_MAX = 65536  # config file size limit (CFG_MAX in zfind.asm)

# Everything in the shared tree whose name contains "config", in output order.
CONFIG_HITS = [
    b".git/config",
    b"Config.md",
    b"d/config.x",
    b"my dir/config.y",
    b"node_modules/x/config",
]
VISIBLE_HITS = CONFIG_HITS[1:]  # --no-hidden drops .git/config
SENSITIVE_HITS = [hit for hit in CONFIG_HITS if hit != b"Config.md"]


def listing(names: list[bytes]) -> Result:
    """A successful run that printed exactly these newline-terminated names."""
    return Result(out=joined(names, b"\n"), err=b"", status=0)


def failure(diagnostic: bytes) -> Result:
    """A run that failed before traversal: no output, one diagnostic, status 1."""
    return Result(out=b"", err=diagnostic, status=1)


def run_cfg(
    binary: Path, cwd: Path, *args: str, env: Optional[Mapping[str, str]] = None
) -> Result:
    """Run zfind with exactly this environment (not the test process's)."""
    done = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        env=dict(env or {}),
        timeout=10,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout, err=done.stderr, status=done.returncode)


class ConfigRunner:
    """Runs zfind in the shared search tree with a minimal, explicit environment."""

    def __init__(self, binary: Path, tree: Path, home: Path) -> None:
        self.binary = binary
        self.tree = tree
        self.home = home
        self.path = home / ".config" / "zfind" / "config"  # the default location

    def write(self, content: str | bytes) -> Path:
        """Write the default config file exactly as given."""
        data = content.encode() if isinstance(content, str) else content
        self.path.write_bytes(data)
        return self.path

    def __call__(
        self, *args: str, env: Optional[Mapping[str, str]] = None, home: bool = True
    ) -> Result:
        """Run zfind; the environment is HOME (unless home=False) plus `env`."""
        full = {"HOME": str(self.home)} if home else {}
        full.update(env or {})
        return run_cfg(self.binary, self.tree, *args, env=full)

    def error(self, line: int, message: str, path: Optional[Path] = None) -> bytes:
        """The diagnostic for a problem in a config file (line 0: the file itself)."""
        where = str(path or self.path) + (f":{line}" if line else "")
        return f"zfind: {where}: {message}\n".encode()


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Search tree shared by this module. No test modifies it."""
    root = tmp_path_factory.mktemp("config-tree")
    for directory in (".git", "d", "node_modules/x", "my dir"):
        (root / directory).mkdir(parents=True)
    for name in (
        ".git/config", ".hidden", "Config.md", "a.txt", "a#b", "d/config.x",
        "my dir/config.y", "node_modules/x/config", "$HOME", "*",
    ):
        (root / name).write_bytes(b"")
    (root / ".gitignore").write_bytes(b"a.txt\n")
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A HOME whose .config/zfind directory exists but holds no config yet."""
    path = tmp_path / "home"
    (path / ".config" / "zfind").mkdir(parents=True)
    return path


@pytest.fixture
def cfg(zfind_path: Path, tree: Path, home: Path) -> ConfigRunner:
    return ConfigRunner(zfind_path, tree, home)


@pytest.fixture
def xdg(tmp_path: Path) -> Path:
    """An XDG_CONFIG_HOME whose zfind directory exists but holds no config yet."""
    root = tmp_path / "xdg"
    (root / "zfind").mkdir(parents=True)
    return root


class TestConfigLocation:
    def test_no_config_file_means_built_in_defaults(self, cfg: ConfigRunner):
        assert cfg("config") == listing(CONFIG_HITS)

    def test_default_location_is_under_home(self, cfg: ConfigRunner):
        cfg.write("--no-hidden\n")
        assert cfg("config") == listing(VISIBLE_HITS)

    def test_variables_are_matched_by_exact_name(self, cfg: ConfigRunner):
        cfg.write("--no-hidden\n")
        env = {
            "A": "1",
            "XHOME": "/nonexistent",
            "NOT_XDG_CONFIG_HOME": "/nonexistent",
            "Z": "HOME=/nonexistent",
        }
        assert cfg("config", env=env) == listing(VISIBLE_HITS)

    @pytest.mark.parametrize("env", [{}, {"HOME": ""}], ids=["no-home", "empty-home"])
    def test_no_resolvable_location_means_defaults(
        self, cfg: ConfigRunner, env: dict[str, str]
    ):
        cfg.write("--no-hidden\n")  # exists, but nothing points at it
        assert cfg("config", home=False, env=env) == listing(CONFIG_HITS)

    def test_absolute_xdg_config_home_beats_home(self, cfg: ConfigRunner, xdg: Path):
        cfg.write("--no-hidden\n")
        (xdg / "zfind" / "config").write_bytes(b"--limit 1\n")
        res = cfg("config", env={"XDG_CONFIG_HOME": str(xdg)})
        assert res == listing([b".git/config"])

    def test_xdg_config_home_works_without_home(self, cfg: ConfigRunner, xdg: Path):
        (xdg / "zfind" / "config").write_bytes(b"--limit 1\n")
        res = cfg("config", home=False, env={"XDG_CONFIG_HOME": str(xdg)})
        assert res == listing([b".git/config"])

    @pytest.mark.parametrize("value", ["relative/dir", ""], ids=["relative", "empty"])
    def test_relative_or_empty_xdg_config_home_is_ignored(
        self, cfg: ConfigRunner, value: str
    ):
        cfg.write("--no-hidden\n")
        assert cfg("config", env={"XDG_CONFIG_HOME": value}) == listing(VISIBLE_HITS)

    def test_xdg_without_a_config_file_does_not_fall_back_to_home(
        self, cfg: ConfigRunner, xdg: Path
    ):
        cfg.write("--no-hidden\n")
        assert cfg("config", env={"XDG_CONFIG_HOME": str(xdg)}) == listing(CONFIG_HITS)

    def test_default_path_below_a_non_directory_counts_as_missing(
        self, cfg: ConfigRunner, tmp_path: Path
    ):
        not_a_dir = tmp_path / "afile"
        not_a_dir.write_bytes(b"")
        res = cfg("config", home=False, env={"HOME": str(not_a_dir)})
        assert res == listing(CONFIG_HITS)

    @pytest.mark.parametrize("variable", ["HOME", "XDG_CONFIG_HOME"])
    def test_default_path_too_long_is_an_error(self, cfg: ConfigRunner, variable: str):
        res = cfg("config", home=False, env={variable: "/" + "a" * 5000})
        assert res == failure(b"zfind: config path too long\n")


class TestConfigSelection:
    def test_no_config_skips_even_a_broken_file(self, cfg: ConfigRunner):
        cfg.write("--bogus\n")
        assert cfg("--no-config", "config") == listing(CONFIG_HITS)

    @pytest.mark.parametrize("form", ["separate", "equals", "relative"])
    def test_explicit_file_replaces_the_default(
        self, cfg: ConfigRunner, tmp_path: Path, form: str
    ):
        cfg.write("--limit 1\n")
        custom = tmp_path / "custom.conf"
        custom.write_bytes(b"--no-hidden\n--limit 2\n")
        if form == "separate":
            args = ("--config", str(custom))
        elif form == "equals":
            args = (f"--config={custom}",)
        else:  # relative to the current directory, which is the search tree
            args = ("--config", os.path.relpath(custom, cfg.tree))
        assert cfg(*args, "config") == listing(VISIBLE_HITS[:2])

    def test_repeated_config_last_wins(self, cfg: ConfigRunner, tmp_path: Path):
        custom = tmp_path / "custom.conf"
        custom.write_bytes(b"--no-hidden\n")
        res = cfg("--config", str(tmp_path / "missing"), "--config", str(custom), "config")
        assert res == listing(VISIBLE_HITS)

    def test_explicit_file_must_exist(self, cfg: ConfigRunner, tmp_path: Path):
        missing = tmp_path / "missing.conf"
        res = cfg(f"--config={missing}", "config")
        assert res == failure(cfg.error(0, "cannot read config file", missing))

    def test_directory_is_not_a_config_file(self, cfg: ConfigRunner, tmp_path: Path):
        res = cfg("--config", str(tmp_path), "config")
        assert res == failure(cfg.error(0, "cannot read config file", tmp_path))

    def test_unreadable_file_is_an_error_not_a_silent_skip(self, cfg: ConfigRunner):
        path = cfg.write("--no-hidden\n")
        path.chmod(0)
        if os.access(path, os.R_OK):
            pytest.skip("file permissions are not enforced (running as root?)")
        assert cfg("config") == failure(cfg.error(0, "cannot read config file"))

    def test_overlong_explicit_path_is_reported_without_overflowing(
        self, cfg: ConfigRunner
    ):
        res = cfg("--config", "/" + "a" * 5000, "config")
        assert res.status == 1 and res.out == b""
        assert res.err.startswith(b"zfind: /aaa")
        assert res.err.endswith(b": cannot read config file\n")
        assert len(res.err) < 4096 + 100  # the path in the message is capped

    @pytest.mark.parametrize(
        ("args", "diagnostic"),
        [
            (("--config",), b"zfind: missing value for --config\n"),
            (("--config=",), b"zfind: missing value for --config\n"),
            (("--config=x", "--no-config"), b"zfind: cannot combine --config and --no-config\n"),
            (("--no-config", "--config=x"), b"zfind: cannot combine --config and --no-config\n"),
            (("--no-config", "--config", "x"), b"zfind: cannot combine --config and --no-config\n"),
            (("config", "--config", "x"), b"zfind: unexpected argument\n"),
            (("config", "--no-config"), b"zfind: unexpected argument\n"),
            (("--no-confi",), b"zfind: unknown option\n"),
            (("--config-file=x",), b"zfind: unknown option\n"),
        ],
    )
    def test_usage_errors_are_pre_traversal(
        self, cfg: ConfigRunner, args: tuple[str, ...], diagnostic: bytes
    ):
        assert cfg(*args) == failure(diagnostic)

    def test_options_end_at_double_dash(self, cfg: ConfigRunner):
        # Treated as an option this would be "missing value for --config".
        assert cfg("--", "--config") == listing([])
        # After "--" the word is a pattern, so --help does not skip the config.
        cfg.write("--bogus\n")
        assert cfg("--", "--help") == failure(cfg.error(1, "unknown option"))

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            # The word after a value option is that option's value, not a config choice.
            (("--exclude-name", "--no-config", "config"), listing([b".git/config"])),
            (("--exclude-name", "--config", "config"), listing([b".git/config"])),
            (("--separator", "--config=x", "config"), Result(b".git/config--config=x", b"", 0)),
            (("--limit", "--no-config"), failure(b"zfind: invalid limit\n")),
            (("--type", "--no-config"), failure(b"zfind: expected f or d after --type\n")),
        ],
    )
    def test_value_options_swallow_config_option_names(
        self, cfg: ConfigRunner, args: tuple[str, ...], expected: Result
    ):
        cfg.write("--limit 1\n")  # still read: none of these chose a config file
        assert cfg(*args) == expected

    @pytest.mark.parametrize("option", ["--limit", "--type"])
    @pytest.mark.parametrize("word", ["--no-config", "--config=x"])
    def test_numeric_value_options_do_not_hide_a_broken_config(
        self, cfg: ConfigRunner, option: str, word: str
    ):
        # The value is invalid either way, so only the order of errors shows that the
        # word was a value and not a config choice: the broken file is read first.
        cfg.write("--bogus\n")
        assert cfg(option, word) == failure(cfg.error(1, "unknown option"))


class TestConfigHelp:
    @pytest.mark.parametrize(
        "args",
        [
            ("--help",),
            ("--limit", "--help"),
            ("--exclude-name", "--help"),
            ("--config", "/nonexistent/zfind.conf", "--help"),
            ("--config", "--help"),
        ],
        ids=["plain", "limit-value", "exclude-value", "missing-file", "config-value"],
    )
    def test_help_never_reads_the_config(self, cfg: ConfigRunner, args: tuple[str, ...]):
        expected = cfg("--help", home=False)
        assert expected.status == 0 and expected.out.startswith(b"usage:")
        cfg.write("--bogus\n")
        assert cfg(*args) == expected

    def test_help_describes_the_config_options(self, cfg: ConfigRunner):
        text = cfg("--help").out.decode()
        assert "--config PATH" in text and "--no-config" in text
        assert all(len(line) <= 80 for line in text.splitlines())


class TestConfigSyntax:
    @pytest.mark.parametrize(
        ("text", "args", "expected"),
        [
            pytest.param("", ("config",), CONFIG_HITS, id="empty-file"),
            pytest.param("# nothing\n\n  # here\n", ("config",), CONFIG_HITS, id="comments-only"),
            pytest.param(
                "# comment\n\n   # indented comment\n\t--no-hidden\t \n\n",
                ("config",), VISIBLE_HITS, id="comments-blanks-tabs-trailing-blanks",
            ),
            pytest.param("--no-hidden\r\n--limit 3\r\n", ("config",), VISIBLE_HITS[:3], id="crlf"),
            pytest.param("--no-hidden", ("config",), VISIBLE_HITS, id="no-final-newline"),
            pytest.param("\r--no-hidden\n", ("config",), VISIBLE_HITS, id="leading-cr"),
            pytest.param(" \r\t--no-hidden \r\n", ("config",), VISIBLE_HITS, id="blanks-and-cr-at-both-ends"),
            pytest.param("--no-hidden\n\n\n", ("config",), VISIBLE_HITS, id="trailing-blank-lines"),
            pytest.param("--limit=2\n", ("config",), CONFIG_HITS[:2], id="equals-form"),
            pytest.param("--limit 2\n", ("config",), CONFIG_HITS[:2], id="blank-form"),
            pytest.param("--limit \t 2\n", ("config",), CONFIG_HITS[:2], id="several-blanks"),
        ],
    )
    def test_line_forms(
        self, cfg: ConfigRunner, text: str, args: tuple[str, ...], expected: list[bytes]
    ):
        cfg.write(text)
        assert cfg(*args) == listing(expected)

    @pytest.mark.parametrize(
        "line",
        [
            "--exclude-name='my dir'",
            '--exclude-name="my dir"',
            "--exclude-name my dir",
            "--exclude-name   'my dir'  ",
            '--exclude-name  "my dir"',
        ],
        ids=["single", "double", "blank-form-unquoted", "blank-form-single", "blank-form-double"],
    )
    def test_values_with_spaces(self, cfg: ConfigRunner, line: str):
        cfg.write(line + "\n")
        expected = [hit for hit in CONFIG_HITS if not hit.startswith(b"my dir")]
        assert cfg("config") == listing(expected)

    def test_quotes_protect_edge_blanks_in_a_value(self, cfg: ConfigRunner, tmp_path: Path):
        tree = tmp_path / "blanks"
        tree.mkdir()
        for name in (" pad ", "pad", "x"):
            (tree / name).write_bytes(b"")
        config = tmp_path / "blanks.conf"
        config.write_bytes(b"--exclude-name=' pad '\n")
        res = run_cfg(cfg.binary, tree, f"--config={config}")
        assert res == listing([b"pad", b"x"])

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("--separator='\\t'\n--limit=2\n", b".git/config\tConfig.md\t"),
            ("--separator=\\t\n--limit=2\n", b".git/config\tConfig.md\t"),
            ("--separator=\\0\n--limit=2\n", b".git/config\0Config.md\0"),
            ("--separator ;\n--limit 2\n", b".git/config;Config.md;"),
            ('--separator ", "\n--limit 2\n', b".git/config, Config.md, "),
        ],
        ids=["quoted-escape", "unquoted-escape", "nul-escape", "blank-form", "quoted-blank-form"],
    )
    def test_separator_escapes_reach_the_separator_parser(
        self, cfg: ConfigRunner, text: str, expected: bytes
    ):
        cfg.write(text)
        assert cfg("config") == Result(out=expected, err=b"", status=0)

    def test_hash_inside_a_value_is_literal(self, cfg: ConfigRunner):
        assert cfg("a#b") == listing([b"a#b"])
        cfg.write("--exclude-name=a#b\n")
        assert cfg("a#b") == listing([])

    def test_quotes_inside_an_unquoted_value_are_literal(self, cfg: ConfigRunner):
        cfg.write("--exclude-name=it's\n--exclude-name=a\"b\"\n")
        assert cfg("config") == listing(CONFIG_HITS)

    def test_values_are_not_expanded(self, cfg: ConfigRunner):
        assert {b"$HOME", b"*", b"a.txt"} <= set(entries(cfg().out))
        cfg.write("--exclude-name=$HOME\n--exclude-name=*\n--exclude-name=~\n")
        names = entries(cfg().out)
        assert b"$HOME" not in names and b"*" not in names  # excluded literally
        assert b"a.txt" in names and b"Config.md" in names  # a glob would hit these

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("--exclude-name --no-config\n", CONFIG_HITS),
            ("--exclude-name --\n", CONFIG_HITS),
            ("--exclude-name --config=x\n--no-hidden\n", VISIBLE_HITS),
        ],
        ids=["no-config", "double-dash", "config-equals"],
    )
    def test_a_value_may_look_like_an_option(
        self, cfg: ConfigRunner, text: str, expected: list[bytes]
    ):
        # After an option that takes a value, the rest of the line is that value.
        cfg.write(text)
        assert cfg("config") == listing(expected)

    def test_a_separator_may_look_like_an_option(self, cfg: ConfigRunner):
        cfg.write("--separator --config=x\n--limit 1\n")
        assert cfg("config") == Result(out=b".git/config--config=x", err=b"", status=0)

    def test_type_uses_the_blank_form(self, cfg: ConfigRunner):
        cfg.write("--type f\n")
        # Directories d/, my dir/ and node_modules/ also contain a "d".
        assert cfg("d") == listing([b".hidden", b"Config.md"])

    def test_very_long_line(self, cfg: ConfigRunner):
        cfg.write("--exclude-name=" + "x" * 60000 + "\n--no-hidden\n")
        assert cfg("config") == listing(VISIBLE_HITS)

    def test_file_of_exactly_the_maximum_size(self, cfg: ConfigRunner):
        cfg.write(b"#" * (CFG_MAX - 1) + b"\n")
        assert cfg("config") == listing(CONFIG_HITS)


class TestConfigErrors:
    @pytest.mark.parametrize(
        ("text", "line", "message"),
        [
            pytest.param("--bogus\n", 1, "unknown option", id="unknown-option"),
            pytest.param("--hidden=foo\n", 1, "unknown option", id="value-on-a-flag"),
            pytest.param("--type=f\n", 1, "unknown option", id="type-has-no-equals-form"),
            pytest.param("--HIDDEN\n", 1, "unknown option", id="case-matters"),
            pytest.param("--limit\n", 1, "invalid limit", id="limit-without-value"),
            pytest.param("--limit=abc\n", 1, "invalid limit", id="limit-not-a-number"),
            pytest.param("--limit -1\n", 1, "invalid limit", id="limit-negative"),
            pytest.param("--limit=\n", 1, "invalid limit", id="limit-empty"),
            pytest.param("--separator\n", 1, "missing value for --separator", id="separator-without-value"),
            pytest.param("--separator=\n", 1, "empty separator", id="separator-empty"),
            pytest.param("--separator=\\q\n", 1, "unknown escape sequence in separator", id="separator-bad-escape"),
            pytest.param("--exclude-name\n", 1, "missing value for --exclude-name", id="exclude-without-value"),
            pytest.param("--exclude-name=''\n", 1, "empty name for --exclude-name", id="exclude-empty-quoted"),
            pytest.param("--exclude-name=a/b\n", 1, "--exclude-name matches basenames only; '/' is not allowed", id="exclude-slash"),
            pytest.param("--type\n", 1, "expected f or d after --type", id="type-without-value"),
            pytest.param("--type x\n", 1, "expected f or d after --type", id="type-bad-value"),
            pytest.param("--exclude-name='abc\n", 1, "malformed quotes in config value", id="quote-never-closed"),
            pytest.param("--exclude-name='a'b'\n", 1, "malformed quotes in config value", id="quote-inside-quoted"),
            pytest.param("--exclude-name='a'b\n", 1, "malformed quotes in config value", id="text-after-closing-quote"),
            pytest.param("--exclude-name 'a\n", 1, "malformed quotes in config value", id="blank-form-quote-never-closed"),
            pytest.param("--hidden\n--no-hidden\n", 2, "cannot combine --hidden and --no-hidden", id="conflict-hidden"),
            pytest.param("--no-hidden\n--hidden\n", 2, "cannot combine --hidden and --no-hidden", id="conflict-hidden-reversed"),
            pytest.param("--case-sensitive\n--case-insensitive\n", 2, "cannot combine --case-sensitive and --case-insensitive", id="conflict-case"),
            pytest.param("--gitignore\n--no-gitignore\n", 2, "cannot combine --gitignore and --no-gitignore", id="conflict-gitignore"),
            pytest.param("--hidden\n--hidden\n--no-hidden\n", 3, "cannot combine --hidden and --no-hidden", id="conflict-names-the-later-line"),
            pytest.param("config\n", 1, "config lines must be long options", id="positional-pattern"),
            pytest.param("-x\n", 1, "config lines must be long options", id="short-option"),
            pytest.param("-\n", 1, "config lines must be long options", id="single-dash"),
            pytest.param("--hidden foo\n", 1, "unexpected argument", id="stray-word-after-flag"),
            pytest.param("--hidden --no-config\n", 1, "unexpected argument", id="flag-then-no-config"),
            pytest.param("--hidden --config=x\n", 1, "unexpected argument", id="flag-then-config-equals"),
            pytest.param("--hidden --config=\n", 1, "unexpected argument", id="flag-then-empty-config"),
            pytest.param("--hidden --config\n", 1, "unexpected argument", id="flag-then-config"),
            pytest.param("--hidden --\n", 1, "unexpected argument", id="flag-then-double-dash"),
            pytest.param("--hidden --limit=5\n", 1, "unexpected argument", id="two-options-equals-form"),
            pytest.param("--no-hidden --limit 5\n", 1, "unexpected argument", id="two-options-blank-form"),
            pytest.param("--hidden --hidden\n", 1, "unexpected argument", id="flag-repeated-on-one-line"),
            pytest.param("--hidden --bogus\n", 1, "unexpected argument", id="flag-then-unknown-option"),
            pytest.param("--bogus value\n", 1, "unknown option", id="unknown-name-with-value"),
            pytest.param("--limt 5\n", 1, "unknown option", id="misspelled-name-with-value"),
            pytest.param("--limit 5 --hidden\n", 1, "invalid limit", id="value-is-the-rest-of-the-line"),
            pytest.param("--\n", 1, "option not allowed in a config file", id="double-dash"),
            pytest.param("-- --limit 1\n", 1, "option not allowed in a config file", id="double-dash-with-value"),
            pytest.param("--help\n", 1, "option not allowed in a config file", id="help"),
            pytest.param("--config=x\n", 1, "option not allowed in a config file", id="config-equals"),
            pytest.param("--config x\n", 1, "option not allowed in a config file", id="config-blank"),
            pytest.param("--no-config\n", 1, "option not allowed in a config file", id="no-config"),
            pytest.param("\n\n# c\n  --limit  \n", 4, "invalid limit", id="line-counting"),
            pytest.param("--no-hidden\n--wat 1\n--also-bad\n", 2, "unknown option", id="first-error-is-reported"),
        ],
    )
    def test_line_errors_name_file_and_line(
        self, cfg: ConfigRunner, text: str, line: int, message: str
    ):
        cfg.write(text)
        assert cfg("config") == failure(cfg.error(line, message))

    def test_nul_byte_is_an_error_not_a_truncated_line(self, cfg: ConfigRunner):
        cfg.write(b"--hidden\n--no\x00hidden\n")
        assert cfg("config") == failure(cfg.error(2, "NUL byte in config file"))

    def test_file_over_the_maximum_size(self, cfg: ConfigRunner):
        cfg.write(b"#" * CFG_MAX + b"\n")
        res = cfg("config")
        assert res == failure(cfg.error(0, f"config file too large (max {CFG_MAX} bytes)"))

    def test_the_65th_unique_exclude_name_in_the_file(self, cfg: ConfigRunner):
        cfg.write("".join(f"--exclude-name=n{i}\n" for i in range(65)))
        res = cfg("config")
        assert res == failure(cfg.error(65, "too many --exclude-name values (max 64)"))

    def test_a_bad_line_after_good_ones_produces_no_partial_output(self, cfg: ConfigRunner):
        cfg.write("--no-hidden\n--limit 1\n--bogus\n")
        res = cfg()
        assert res.out == b"" and res.status == 1

    def test_command_line_errors_carry_no_file_prefix(self, cfg: ConfigRunner):
        cfg.write("--no-hidden\n")
        assert cfg("--limit", "abc") == failure(b"zfind: invalid limit\n")
        assert cfg("--bogus", "config") == failure(b"zfind: unknown option\n")
        assert cfg("config", "extra") == failure(b"zfind: unexpected argument\n")

    def test_opposing_modes_on_the_command_line_still_conflict(self, cfg: ConfigRunner):
        cfg.write("--no-hidden\n")
        res = cfg("--hidden", "--no-hidden", "config")
        assert res == failure(b"zfind: cannot combine --hidden and --no-hidden\n")


class TestConfigPrecedence:
    @pytest.mark.parametrize(
        ("text", "args", "expected"),
        [
            # case: an explicit command-line choice replaces the file's
            ("--case-insensitive\n", ("Config",), CONFIG_HITS),
            ("--case-insensitive\n", ("--case-sensitive", "Config"), [b"Config.md"]),
            ("--case-sensitive\n", ("config",), SENSITIVE_HITS),
            ("--case-sensitive\n", ("--case-insensitive", "config"), CONFIG_HITS),
            ("--case-sensitive\n", ("--case-sensitive", "config"), SENSITIVE_HITS),
            # hidden
            ("--no-hidden\n", ("config",), VISIBLE_HITS),
            ("--no-hidden\n", ("--hidden", "config"), CONFIG_HITS),
            ("--hidden\n", ("config",), CONFIG_HITS),
            ("--hidden\n", ("--no-hidden", "config"), VISIBLE_HITS),
            ("--no-hidden\n", ("--no-hidden", "config"), VISIBLE_HITS),
            # gitignore (the tree's .gitignore ignores a.txt)
            ("--gitignore\n", ("a.txt",), []),
            ("--gitignore\n", ("--no-gitignore", "a.txt"), [b"a.txt"]),
            ("--no-gitignore\n", ("a.txt",), [b"a.txt"]),
            ("--no-gitignore\n", ("--gitignore", "a.txt"), []),
            # one layer overriding several modes at once
            ("--gitignore\n--no-hidden\n--case-sensitive\n",
             ("--no-gitignore", "--hidden", "--case-insensitive", "config"), CONFIG_HITS),
            # modes the command line leaves alone keep the file's choice
            ("--no-hidden\n--case-sensitive\n", ("--no-gitignore", "config"),
             [hit for hit in SENSITIVE_HITS if hit != b".git/config"]),
        ],
    )
    def test_modes_command_line_replaces_file(
        self, cfg: ConfigRunner, text: str, args: tuple[str, ...], expected: list[bytes]
    ):
        cfg.write(text)
        assert cfg(*args) == listing(expected)

    @pytest.mark.parametrize(
        ("text", "args", "expected"),
        [
            ("--limit 1\n", ("config",), CONFIG_HITS[:1]),
            ("--limit 1\n", ("--limit", "2", "config"), CONFIG_HITS[:2]),
            ("--limit 1\n", ("--limit=3", "config"), CONFIG_HITS[:3]),
            ("--limit 5\n--limit 1\n", ("config",), CONFIG_HITS[:1]),  # last in a source wins
            # 0 prints nothing for now; the command line can still replace it
            ("--limit=0\n", ("config",), []),
            ("--limit=0\n", ("--limit", "2", "config"), CONFIG_HITS[:2]),
            ("--limit 1\n", ("--limit", "0", "config"), []),
        ],
    )
    def test_limit_is_overwritten_by_the_command_line(
        self, cfg: ConfigRunner, text: str, args: tuple[str, ...], expected: list[bytes]
    ):
        cfg.write(text)
        assert cfg(*args) == listing(expected)

    def test_separator_is_overwritten_by_the_command_line(self, cfg: ConfigRunner):
        cfg.write("--separator=,\n")
        configured = cfg("config")
        assert configured.out == b",".join(CONFIG_HITS) + b","
        assert cfg("--separator", "\\n", "config") == listing(CONFIG_HITS)
        assert cfg("--separator=:", "--limit", "1", "config").out == b".git/config:"

    def test_exclude_names_accumulate_across_layers(self, cfg: ConfigRunner):
        cfg.write("--exclude-name=node_modules\n")
        only_file = [h for h in CONFIG_HITS if not h.startswith(b"node_modules")]
        assert cfg("config") == listing(only_file)
        both = [h for h in only_file if not h.startswith(b"d/")]
        assert cfg("--exclude-name", "d", "config") == listing(both)
        assert cfg("--exclude-name=node_modules", "--exclude-name=d", "config") == listing(both)

    def test_types_accumulate_across_layers(self, cfg: ConfigRunner):
        cfg.write("--type f\n")
        files = entries(cfg().out)
        assert b"a.txt" in files and b"d/" not in files
        both = entries(cfg("--type", "d").out)
        assert b"a.txt" in both and b"d/" in both
        assert cfg("--type", "f").out == cfg().out  # a repeated value changes nothing

    def test_exclude_name_table_is_shared_between_layers(self, cfg: ConfigRunner):
        cfg.write("".join(f"--exclude-name=n{i}\n" for i in range(64)))
        assert cfg("config") == listing(CONFIG_HITS)
        assert cfg("--exclude-name", "n0", "config") == listing(CONFIG_HITS)  # already counted
        full = cfg("--exclude-name", "one-too-many", "config")
        assert full == failure(b"zfind: too many --exclude-name values (max 64)\n")

    def test_options_the_file_does_not_set_keep_their_defaults(self, cfg: ConfigRunner):
        cfg.write("--limit 2\n")
        assert cfg("Config") == listing([b"Config.md"])  # smart case still applies
        assert cfg("config") == listing(CONFIG_HITS[:2])  # hidden entries still searched


class TestNoConfigIsInert:
    ARGS = [
        (),
        ("config",),
        ("--no-hidden", "config"),
        ("--type", "f"),
        ("--type", "d"),
        ("--limit", "2"),
        ("--limit", "0"),
        ("--separator", ";", "config"),
        ("--exclude-name", "node_modules", "config"),
        ("--gitignore", "a.txt"),
        ("--case-sensitive", "Config"),
        ("--case-insensitive", "CONFIG"),
        ("--", "-x"),
        ("--help",),
        ("--bogus",),
        ("--limit", "abc"),
        ("--hidden", "--no-hidden"),
        ("config", "extra"),
    ]

    @pytest.mark.parametrize("args", ARGS, ids=lambda args: " ".join(args) or "no-args")
    def test_no_config_matches_a_run_with_no_config_at_all(
        self, cfg: ConfigRunner, args: tuple[str, ...]
    ):
        cfg.write("--no-hidden\n--limit 1\n--case-sensitive\n--exclude-name=d\n--type f\n")
        baseline = cfg(*args, home=False)  # no HOME, no XDG: nothing to read
        assert cfg("--no-config", *args) == baseline

    @pytest.mark.parametrize("args", ARGS, ids=lambda args: " ".join(args) or "no-args")
    def test_an_empty_config_changes_nothing(
        self, cfg: ConfigRunner, args: tuple[str, ...]
    ):
        baseline = cfg(*args, home=False)
        cfg.write("# just a comment\n\n")
        assert cfg(*args) == baseline


@pytest.mark.parametrize("kind", ["serial", "unknown"])
def test_config_output_matches_build_variants(
    kind: str, cfg: ConfigRunner, zfind_path: Path, serial_zfind_path: Path
):
    variant = serial_zfind_path
    if kind == "unknown":
        variant = zfind_path.parent.parent / f"{zfind_path.parent.name}-unknown" / "zfind"
        if not variant.is_file():
            pytest.skip("unknown-type build not found; run `make test` to build it")
    cfg.write("--no-hidden\n--exclude-name=node_modules\n--separator=;\n")
    env = {"HOME": str(cfg.home)}
    baseline = cfg("config")
    assert baseline.out == b"Config.md;d/config.x;my dir/config.y;"
    assert run_cfg(variant, cfg.tree, "config", env=env) == baseline
