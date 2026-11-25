"""Shared helpers for black-box zfind tests."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import time
import tty
from pathlib import Path
from typing import IO, Mapping, NamedTuple, Optional, Protocol

# Time bounds for every spawn. At about 2 GiB/s of runaway growth these allow
# roughly 10 GiB and 30 GiB: they bound time, not memory.
SPAWN_TIMEOUT = 5
SLOW_TIMEOUT = 15
RUNAWAY_TIMEOUT = 1

_raw = os.environ.get("ZFIND_TEST_TIMEOUT_SCALE", "").strip()
if not _raw:
    SCALE = 1
else:
    try:
        _value = int(_raw)
    except ValueError:
        _value = None
    if _value is None or _value < 1:
        raise SystemExit(
            f"ZFIND_TEST_TIMEOUT_SCALE must be an integer >= 1, not {_raw!r}"
        )
    SCALE = _value
SPAWN_TIMEOUT *= SCALE
SLOW_TIMEOUT *= SCALE          # RUNAWAY_TIMEOUT is a damage bound: never scaled


class Result(NamedTuple):
    out: bytes
    err: bytes
    status: int

    def __repr__(self) -> str:
        # Keep large outputs out of failure messages.
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


def words(text: str) -> list[bytes]:
    return [word.encode() for word in text.split()]


def joined(names: list[bytes], sep: bytes) -> bytes:
    return b"".join(name + sep for name in names)


def entries(out: bytes) -> list[bytes]:
    assert out == b"" or out.endswith(b"\n"), "Output must be newline-terminated or empty"
    return out.split(b"\n")[:-1]


def run_in(binary: Path, cwd: Path, *args: str, timeout: int = SPAWN_TIMEOUT) -> Result:
    started = time.monotonic()
    try:
        done = subprocess.run(
            [str(binary), *args], cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        raise TimeoutError(
            f"zfind exceeded {timeout}s (elapsed {elapsed:.1f}s, scale {SCALE})"
        ) from exc
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
    deadline = time.monotonic() + SLOW_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired([str(binary), *args], SLOW_TIMEOUT)
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
