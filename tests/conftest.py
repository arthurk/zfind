"""Test configuration and runner fixture for zfind."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import IO, NamedTuple, Optional, Protocol

import pytest

TESTS_DIR = Path(__file__).resolve().parent
DATA_DIR = TESTS_DIR / "testdata"

# Trees required by the smoke/flags test suite (see tests/mktrees.py)
TREES = ["tiny", "hidden", "ignores", "weird", "camera", "tree2"]

collect_ignore = ["testdata"]


class Result(NamedTuple):
    out: bytes
    err: bytes
    status: int

    def __repr__(self) -> str:
        # Prevents 64KB+ output dumps from blowing out terminal/AI context windows on failure
        out_preview = (
            f"{self.out[:120]!r}... ({len(self.out)} bytes)"
            if len(self.out) > 120
            else repr(self.out)
        )
        return f"Result(status={self.status}, err={self.err!r}, out={out_preview})"


class ZfindRunner(Protocol):
    def __call__(
        self,
        tree: str,
        *args: str,
        stdout: Optional[IO[bytes]] = None,
    ) -> Result:
        """Run zfind in tests/testdata/<tree>."""
        ...


def pytest_addoption(parser: pytest.Parser) -> None:
    """Allow specifying binary via CLI (`pytest --zfind=...`) or environment ($ZFIND)."""
    parser.addoption(
        "--zfind",
        action="store",
        default=os.environ.get("ZFIND"),
        help="Path to zfind binary (defaults to $ZFIND or build/<arch>/zfind)",
    )
    parser.addoption(
        "--serial-zfind",
        action="store",
        default=os.environ.get("ZFIND_SERIAL"),
        help="Path to serial zfind binary (defaults to $ZFIND_SERIAL or build/<arch>-serial/zfind)",
    )


def get_binary_path(config: pytest.Config) -> Path:
    explicit = config.getoption("--zfind")
    if explicit:
        return Path(explicit).resolve()
    # Fallback to architecture-based default (e.g., x86_64, aarch64)
    return (TESTS_DIR.parent / "build" / platform.machine() / "zfind").resolve()


def get_serial_binary_path(config: pytest.Config) -> Path:
    explicit = config.getoption("--serial-zfind")
    if explicit:
        return Path(explicit).resolve()
    return (TESTS_DIR.parent / "build" / f"{platform.machine()}-serial" / "zfind").resolve()


def pytest_sessionstart(session: pytest.Session) -> None:
    binary = get_binary_path(session.config)
    if not binary.is_file():
        pytest.exit(
            f"Binary not found: {binary}\n"
            f"Build it with `make` or specify location via --zfind or $ZFIND.",
            returncode=4,
        )

    # Ensure test trees exist using mktrees.py
    made = subprocess.run(
        [
            sys.executable,
            str(TESTS_DIR / "mktrees.py"),
            "--out",
            str(DATA_DIR),
            "--preset",
            "smoke",
            "--only",
            *TREES,
        ],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.exit(f"mktrees.py failed to create testdata:\n{made.stderr}", returncode=4)

    writer = session.config.get_terminal_writer()
    writer.line(f"zfind: {binary}")
    writer.line(f"trees: {DATA_DIR}")


def make_zfind_runner(binary: Path) -> ZfindRunner:
    def run(tree: str, *args: str, stdout: Optional[IO[bytes]] = None) -> Result:
        done = subprocess.run(
            [str(binary), *args],
            cwd=DATA_DIR / tree,
            timeout=10,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return Result(
            out=done.stdout or b"",
            err=done.stderr or b"",
            status=done.returncode,
        )

    return run


@pytest.fixture(scope="session")
def zfind(pytestconfig: pytest.Config) -> ZfindRunner:
    return make_zfind_runner(get_binary_path(pytestconfig))


@pytest.fixture(scope="session")
def serial_zfind(pytestconfig: pytest.Config) -> ZfindRunner:
    binary = get_serial_binary_path(pytestconfig)
    if not binary.is_file():
        if pytestconfig.getoption("--serial-zfind"):
            pytest.exit(f"Serial binary not found: {binary}", returncode=4)
        pytest.skip(f"Serial binary not built: {binary}; run `make test` to build it")
    return make_zfind_runner(binary)
