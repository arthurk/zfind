"""Resource limits the suite must impose on the binaries it spawns."""

import ast
import resource
import signal
import subprocess
import sys
from pathlib import Path

import pytest

CAPPED = pytest.mark.skipif(
    resource.getrlimit(resource.RLIMIT_CORE)[1] == 0,
    reason="caller already capped the hard core limit at 0",
)


@CAPPED
def test_core_dumps_are_disabled_for_subprocesses() -> None:
    out = subprocess.run(
        [sys.executable, "-c",
         "import resource; print(resource.getrlimit(resource.RLIMIT_CORE))"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ast.literal_eval(out) == (1, 1)


@CAPPED
def test_crashing_child_leaves_no_core(tmp_path: Path) -> None:
    try:
        pattern = Path("/proc/sys/kernel/core_pattern").read_text().strip()
    except OSError as exc:
        pytest.skip(f"cannot read core_pattern: {exc}")
    # | pipe, @ socket, and any path: the dump would land outside this directory.
    if pattern.startswith(("|", "@")) or "/" in pattern:
        pytest.skip(f"core_pattern {pattern!r} does not write into the cwd")
    done = subprocess.run([sys.executable, "-c", "import os; os.abort()"], cwd=tmp_path)
    assert done.returncode == -signal.SIGABRT
    assert not list(tmp_path.glob("core*"))
