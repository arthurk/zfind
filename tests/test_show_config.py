"""Tests for zfind --show-config (issue #53)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional

import pytest
from helpers import SPAWN_TIMEOUT, Result


def run_zfind(
    binary: Path,
    *args: str,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Result:
    done = subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        timeout=SPAWN_TIMEOUT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout, err=done.stderr, status=done.returncode)


class TestShowConfigDefaults:
    """Gate 5: zfind --show-config with no config file present."""

    def test_show_config_no_config_file_built_in_defaults(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        res = run_zfind(binary, "--show-config", cwd=tmp_path, env={"HOME": str(home)})
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        lines = res.out.splitlines()
        assert len(lines) == 8, f"Unexpected line count in {lines}"
        assert lines[0] == b"# Configuration source: none (built-in defaults)"
        assert lines[1] == b"--case=smart"
        assert lines[2] == b"--color=auto"
        assert lines[3] == b"--limit=none"
        assert lines[4] in (b"--separator='\\n'", b"--separator=\\n")
        assert lines[5] == b"--hidden"
        assert lines[6] == b"--no-gitignore"
        assert lines[7] == b"--relative"
        assert res.out.endswith(b"\n")


class TestShowConfigWithFile:
    """Gate 6: zfind --show-config with config file present."""

    def test_show_config_with_home_config_file(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config"
        cfg_file.write_text("--limit 500\n--exclude-name=node_modules\n")

        res = run_zfind(binary, "--show-config", cwd=tmp_path, env={"HOME": str(home)})
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        lines = res.out.splitlines()
        assert lines[0] == f"# Configuration source: {cfg_file}".encode()
        assert lines[1] == b"--case=smart"
        assert lines[2] == b"--color=auto"
        assert lines[3] == b"--limit=500"
        assert lines[4] in (b"--separator='\\n'", b"--separator=\\n")
        assert lines[5] == b"--hidden"
        assert lines[6] == b"--no-gitignore"
        assert lines[7] == b"--relative"
        assert lines[8] == b"--exclude-name=node_modules"
        assert len(lines) == 9

    def test_show_config_with_xdg_config_file(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        xdg = tmp_path / "xdg"
        cfg_dir = xdg / "zfind"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config"
        cfg_file.write_text("--no-hidden\n--case=sensitive\n")

        res = run_zfind(
            binary,
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        lines = res.out.splitlines()
        assert lines[0] == f"# Configuration source: {cfg_file}".encode()
        assert lines[1] == b"--case=sensitive"
        assert b"--no-hidden" in lines

    def test_show_config_with_explicit_config_arg(
        self, binary: Path, tmp_path: Path
    ):
        cfg_file = tmp_path / "custom.conf"
        cfg_file.write_text("--limit=10\n")

        res = run_zfind(
            binary,
            "--config",
            str(cfg_file),
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(tmp_path)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        lines = res.out.splitlines()
        assert lines[0] == f"# Configuration source: {cfg_file}".encode()
        assert lines[3] == b"--limit=10"


class TestShowConfigNoConfigOverride:
    """Gate 7: zfind --show-config --no-config with config present."""

    def test_show_config_no_config_flag_suppresses_file(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config"
        cfg_file.write_text("--limit 500\n--no-hidden\n--case sensitive\n")

        res = run_zfind(
            binary,
            "--show-config",
            "--no-config",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        lines = res.out.splitlines()
        assert lines[0] == b"# Configuration source: none (--no-config)"
        assert lines[1] == b"--case=smart"
        assert lines[2] == b"--color=auto"
        assert lines[3] == b"--limit=none"
        assert lines[4] in (b"--separator='\\n'", b"--separator=\\n")
        assert lines[5] == b"--hidden"
        assert lines[6] == b"--no-gitignore"
        assert lines[7] == b"--relative"
        assert len(lines) == 8


class TestShowConfigTypePrecedence:
    """Gate 8: zfind --show-config --type=f when config has --type=d."""

    def test_cli_type_replaces_config_type(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config").write_text("--type=d\n")

        res = run_zfind(
            binary,
            "--show-config",
            "--type=f",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        assert b"--type=file" in res.out
        assert b"--type=directory" not in res.out
        assert b"--type=d" not in res.out
        assert b"--type=dir" not in res.out

    def test_multiple_types_canonicalized_and_ordered(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        res = run_zfind(
            binary,
            "--show-config",
            "--type=symlink",
            "--type=file",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        assert b"--type=file" in res.out
        assert b"--type=symlink" in res.out


class TestShowConfigExcludeAccumulation:
    """Gate 9: zfind --show-config --exclude-name=b when config has --exclude-name=a."""

    def test_exclude_names_accumulate_and_sort(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config").write_text("--exclude-name=b_item\n--exclude-name=a_item\n")

        res = run_zfind(
            binary,
            "--show-config",
            "--exclude-name=c_item",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert res.err == b""
        lines = res.out.splitlines()
        excl_lines = [line for line in lines if line.startswith(b"--exclude-name=")]
        assert excl_lines == [
            b"--exclude-name=a_item",
            b"--exclude-name=b_item",
            b"--exclude-name=c_item",
        ]

    def test_exclude_names_deduplicate(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config").write_text("--exclude-name=dup\n")

        res = run_zfind(
            binary,
            "--show-config",
            "--exclude-name=dup",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        lines = res.out.splitlines()
        assert lines.count(b"--exclude-name=dup") == 1

    def test_exclude_types_accumulate(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config").write_text("--exclude-type=symlink\n")

        res = run_zfind(
            binary,
            "--show-config",
            "--exclude-type=broken",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        assert b"--exclude-type=symlink" in res.out
        assert b"--exclude-type=broken" in res.out


class TestShowConfigConfigErrors:
    """Gates 10 & 11: Config file syntax errors and --show-config in config."""

    def test_syntax_error_in_config_fails_pre_dump(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config"
        cfg_file.write_text("--case=invalid_mode\n")

        res = run_zfind(binary, "--show-config", cwd=tmp_path, env={"HOME": str(home)})
        assert res.status == 1, f"Expected 1 exit: {res}"
        assert res.err == f"zfind: {cfg_file}:1: expected smart, sensitive or insensitive after --case\n".encode()
        assert res.out == b""

    def test_show_config_disallowed_in_config_file(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        cfg_file = cfg_dir / "config"
        cfg_file.write_text("# header\n--show-config\n")

        # Test normal invocation
        res = run_zfind(binary, cwd=tmp_path, env={"HOME": str(home)})
        assert res.status == 1, f"Expected 1 exit: {res}"
        assert res.err == f"zfind: {cfg_file}:2: option not allowed in a config file\n".encode()
        assert res.out == b""

        # Test with --show-config on CLI too
        res2 = run_zfind(binary, "--show-config", cwd=tmp_path, env={"HOME": str(home)})
        assert res2.status == 1, f"Expected 1 exit: {res2}"
        assert res2.err == f"zfind: {cfg_file}:2: option not allowed in a config file\n".encode()
        assert res2.out == b""


class TestShowConfigRoundTrip:
    """Gate 12: round-trip test."""

    def test_defaults_round_trip(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        res1 = run_zfind(binary, "--show-config", cwd=tmp_path, env={"HOME": str(home)})
        assert res1.status == 0, f"Expected 0 exit: {res1}"

        dump_file = tmp_path / "dump.conf"
        dump_file.write_bytes(res1.out)

        res2 = run_zfind(
            binary,
            "--config",
            str(dump_file),
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res2.status == 0, f"Expected 0 exit: {res2}"

        # The option lines should match
        opts1 = res1.out.split(b"\n", 1)[1]
        opts2 = res2.out.split(b"\n", 1)[1]
        assert opts1 == opts2

        # Re-dumping to the same file should make entire output byte-identical
        dump_file.write_bytes(res2.out)
        res3 = run_zfind(
            binary,
            "--config",
            str(dump_file),
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res3.out == res2.out

    def test_complex_config_round_trip(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        initial = tmp_path / "initial.conf"
        initial.write_text(
            "--case=sensitive\n"
            "--limit=42\n"
            "--separator=;\n"
            "--no-hidden\n"
            "--gitignore\n"
            "--absolute\n"
            "--type=file\n"
            "--type=symlink\n"
            '--exclude-name="build output"\n'
            "--exclude-name=target\n"
            "--exclude-type=broken\n"
        )

        res1 = run_zfind(
            binary,
            "--config",
            str(initial),
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res1.status == 0, f"Expected 0 exit: {res1}"

        dump = tmp_path / "dump.conf"
        dump.write_bytes(res1.out)

        res2 = run_zfind(
            binary,
            "--config",
            str(dump),
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res2.status == 0, f"Expected 0 exit: {res2}"
        assert res1.out.split(b"\n", 1)[1] == res2.out.split(b"\n", 1)[1]

        dump.write_bytes(res2.out)
        res3 = run_zfind(
            binary,
            "--config",
            str(dump),
            "--show-config",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res2.out == res3.out


class TestShowConfigPrecedence:
    def test_cli_overrides_scalar_and_mode_flags(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config").write_text(
            "--limit 100\n"
            "--case sensitive\n"
            "--color never\n"
            "--no-hidden\n"
            "--gitignore\n"
            "--absolute\n"
        )
        res = run_zfind(
            binary,
            "--show-config",
            "--limit=200",
            "--hidden",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0, f"Expected 0 exit: {res}"
        lines = res.out.splitlines()
        assert b"--case=sensitive" in lines
        assert b"--color=never" in lines
        assert b"--limit=200" in lines
        assert b"--hidden" in lines
        assert b"--no-hidden" not in lines
        assert b"--gitignore" in lines
        assert b"--absolute" in lines


class TestShowConfigEdgeCases:
    """Decided rows and edge cases from issue #53."""

    def test_conflicting_cli_flags_fail(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        for flag1, flag2, msg in [
            ("--hidden", "--no-hidden", b"zfind: cannot combine --hidden and --no-hidden\n"),
            ("--gitignore", "--no-gitignore", b"zfind: cannot combine --gitignore and --no-gitignore\n"),
            ("--absolute", "--relative", b"zfind: cannot combine --absolute and --relative\n"),
        ]:
            res = run_zfind(
                binary,
                "--show-config",
                flag1,
                flag2,
                cwd=tmp_path,
                env={"HOME": str(home)},
            )
            assert res.status == 1
            assert res.err == msg
            assert res.out == b""

    def test_conflicting_type_and_exclude_type_fail(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        # Direct CLI conflict
        res = run_zfind(
            binary,
            "--show-config",
            "--type=f",
            "--exclude-type=file",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 1
        assert res.err == b"zfind: cannot combine --type and --exclude-type for the same type category\n"
        assert res.out == b""

        # Merged config and CLI conflict
        cfg_dir = home / ".config" / "zfind"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config").write_text("--type=directory\n")
        res2 = run_zfind(
            binary,
            "--show-config",
            "--exclude-type=d",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res2.status == 1
        assert res2.err == b"zfind: cannot combine --type and --exclude-type for the same type category\n"
        assert res2.out == b""

    def test_help_takes_precedence_over_show_config(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        for args in [
            ("--show-config", "--help"),
            ("--help", "--show-config"),
        ]:
            res = run_zfind(binary, *args, cwd=tmp_path, env={"HOME": str(home)})
            assert res.status == 0
            assert b"usage: zfind" in res.out
            assert b"# Configuration source:" not in res.out
            assert res.err == b""

    def test_positional_arguments_ignored(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        # Non-existent path and pattern
        res = run_zfind(
            binary,
            "--show-config",
            "nonexistent_dir",
            "nonexistent_pattern",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0
        assert b"# Configuration source: none (built-in defaults)" in res.out
        assert res.err == b""

    def test_nonexistent_config_file_fails(
        self, binary: Path, tmp_path: Path
    ):
        missing = tmp_path / "missing.conf"
        res = run_zfind(
            binary,
            "--show-config",
            "--config",
            str(missing),
            cwd=tmp_path,
            env={"HOME": str(tmp_path)},
        )
        assert res.status == 1
        assert res.err == f"zfind: {missing}: cannot read config file\n".encode()
        assert res.out == b""

    @pytest.mark.parametrize(
        ("sep_arg", "expected_in_out"),
        [
            ("\\0", b"--separator='\\0'"),
            ("\\t", b"--separator='\\t'"),
            ("\\r", b"--separator='\\r'"),
            ("\\\\", b"--separator='\\\\'"),
            (",", b"--separator="),
        ],
    )
    def test_separator_escaping(
        self, binary: Path, tmp_path: Path, sep_arg: str, expected_in_out: bytes
    ):
        home = tmp_path / "home"
        home.mkdir()
        res = run_zfind(
            binary,
            "--show-config",
            f"--separator={sep_arg}",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0
        assert expected_in_out in res.out

    def test_exclude_name_whitespace_quoted(
        self, binary: Path, tmp_path: Path
    ):
        home = tmp_path / "home"
        home.mkdir()
        res = run_zfind(
            binary,
            "--show-config",
            "--exclude-name=build output",
            cwd=tmp_path,
            env={"HOME": str(home)},
        )
        assert res.status == 0
        assert b'--exclude-name="build output"' in res.out

    def test_show_config_after_double_dash_is_literal_pattern(
        self, binary: Path, tmp_path: Path
    ):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "--show-config").write_bytes(b"")

        res = run_zfind(binary, "--", "--show-config", cwd=tree, env={"HOME": str(tmp_path)})
        assert res.status == 0
        # Should be a search result, not a config dump
        assert b"# Configuration source:" not in res.out
        assert b"--show-config" in res.out
