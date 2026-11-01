"""Tests for read and write error recovery."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import Result, entries, run_in

STRACE = shutil.which("strace")
requires_strace = pytest.mark.skipif(STRACE is None, reason="requires strace")


def run_injected(
    binary: Path,
    cwd: Path,
    *args: str,
    inject: str,
    stdout=None,
    timeout: int = 60,
) -> Result:
    """Run zfind under strace with one injected syscall fault.

    The inject argument uses strace's syntax, for example
    "write:error=EINTR:when=1" or "write:retval=5:when=1".
    """
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
        timeout=timeout,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return Result(out=done.stdout or b"", err=done.stderr or b"", status=done.returncode)

@pytest.fixture
def repo_tree(tmp_path: Path) -> Path:
    """A repository whose .gitignore ignores ignored.txt."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").mkdir()
    (tree / ".gitignore").write_bytes(b"ignored.txt\nunused.tmp\n")
    (tree / "ignored.txt").write_bytes(b"")
    (tree / "keep.txt").write_bytes(b"")
    return tree


@requires_strace
class TestSyscallErrorRecovery:
    def test_write_eintr_is_retried(self, zfind_path: Path, repo_tree: Path):
        baseline = run_in(zfind_path, repo_tree)
        assert baseline.status == 0, f"Baseline run failed: {baseline}"

        res = run_injected(zfind_path, repo_tree, inject="write:error=EINTR:when=1")
        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert res.out == baseline.out

    def test_short_write_resumes_at_reported_offset(
        self, zfind_path: Path, repo_tree: Path, tmp_path: Path
    ):
        baseline = run_in(zfind_path, repo_tree)
        assert baseline.status == 0, f"Baseline run failed: {baseline}"
        assert len(baseline.out) > 5, f"Baseline unexpectedly short: {baseline}"

        # The injected write reports five bytes without writing them. Seeding
        # the file with those five bytes means a correct resume reproduces the
        # baseline exactly; restarting at offset zero would append it again.
        out = tmp_path / "stdout"
        out.write_bytes(baseline.out[:5])
        with out.open("ab") as stdout:
            res = run_injected(
                zfind_path, repo_tree, inject="write:retval=5:when=1", stdout=stdout
            )

        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert out.read_bytes() == baseline.out

    def test_read_eintr_is_retried(self, zfind_path: Path, repo_tree: Path):
        baseline = run_in(zfind_path, repo_tree, "--gitignore")
        assert baseline.status == 0, f"Baseline run failed: {baseline}"
        assert entries(baseline.out) == [b".gitignore", b"keep.txt"]

        res = run_injected(
            zfind_path, repo_tree, "--gitignore", inject="read:error=EINTR:when=1"
        )
        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert res.out == baseline.out

    def test_read_error_discards_buffered_gitignore(
        self, zfind_path: Path, repo_tree: Path
    ):
        # The first read returns the whole file; the second read fails. The
        # bytes already read must be discarded instead of parsed.
        res = run_injected(
            zfind_path, repo_tree, "--gitignore", inject="read:error=EIO:when=2"
        )
        assert res.status == 0, f"Injected run failed: {res}"
        assert res.err == b"", f"Injected run wrote stderr: {res}"
        assert entries(res.out) == [b".gitignore", b"ignored.txt", b"keep.txt"]
