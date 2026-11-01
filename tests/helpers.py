"""Shared helpers for black-box zfind tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import IO, NamedTuple, Optional, Protocol


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


def run_in(binary: Path, cwd: Path, *args: str, timeout: int = 60) -> Result:
    done = subprocess.run(
        [str(binary), *args], cwd=cwd, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)
