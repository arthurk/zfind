"""Bounds on directory entry names and arena requests.

The two fuzz tests run on every build and are protective: they pass against a
correct binary and fail when the length a name reports disagrees with the bytes
the kernel delivered. The five mutant tests consume hostile builds made by
``scripts/inject_fault.sh`` (via ``make test``); each one turns a specific check
back off, so a regression fails here instead of running away.

Known limits:

* ``add r13, 1`` is invisible to output comparison. The kernel writes the name's
  NUL, so the extra key byte is that NUL and the extra copied byte is unused
  padding.
* ``strlen`` only ever sees ``rdi & 31`` in {19, 27, 3, 11}: ``d_name`` starts at
  record offset +19 in a 32-byte-aligned buffer and ``d_reclen`` is a multiple of
  8. The other residues would need a white-box harness for ``strlen``, which is
  out of scope.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest
from helpers import (
    RUNAWAY_TIMEOUT,
    SPAWN_TIMEOUT,
    entries,
    joined,
    run_in,
)

NAME_ERROR = b"zfind: internal: directory entry name too long\n"
SIZE_ERROR = b"zfind: directory too large\n"
ARENA_ERROR = b"zfind: tree too large\n"


def _make_tree(root: Path, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    names = {f"a" * n for n in range(1, 65)}
    names |= {"ab" + "b" * (n - 2) for n in range(3, 65)}
    names |= {"".join(rng.choice("abcdefghij") for _ in range(rng.randint(1, 255)))
              for _ in range(200)}
    root.mkdir()
    for name in names:
        (root / name).write_bytes(b"")
    return sorted(name.encode() for name in names)


def _expected(names: list[bytes], pattern: bytes) -> list[bytes]:
    """Output for a run in the tree root, including a nested copy under nest/."""
    matched = [name for name in names if pattern in name]
    return sorted(matched + [b"nest/" + name for name in matched])


def _mutant(env: str) -> Path:
    path = os.environ.get(env)
    if not path:
        pytest.fail(f"{env} is not set; run this test via `make test`")
    # run_in spawns with cwd inside a tmp tree, so a relative path must resolve.
    return Path(path).resolve()


def test_random_name_lengths(binary: Path, tmp_path: Path) -> None:
    """Every name length 1..64 plus 200 random names in 1..255 survive unchanged.

    The tree holds every length 1..64 (so the 1-byte ``a`` is always present),
    prefix-sharing short names, 200 random names and a nested copy. A truncated
    length changes output, not just a comparison: the bytes printed are the bytes
    measured.
    """
    rounds = int(os.environ.get("ZFIND_FUZZ_ROUNDS", "4"))
    seed0 = int(os.environ.get("ZFIND_FUZZ_SEED", "0"))
    for round_no in range(rounds):
        seed = seed0 + round_no
        root = tmp_path / f"tree{round_no}"
        names = _make_tree(root, seed)
        (root / "nest").mkdir()
        for name in names:
            (root / "nest" / os.fsdecode(name)).write_bytes(b"")
        # Fillers without "a" must not match the pattern.
        for filler in ("z", "z" * 7, "z" * 40):
            (root / filler).write_bytes(b"")
        expected = _expected(names, b"a")
        res = run_in(binary, root, "a")
        assert res.status == 0, f"seed {seed} ({res})"
        assert res.err == b"", f"seed {seed} ({res})"
        assert entries(res.out) == expected, (
            f"seed {seed}: replay with ZFIND_FUZZ_SEED={seed} ZFIND_FUZZ_ROUNDS=1"
        )


def test_full_batch_of_long_names(binary: Path, tmp_path: Path) -> None:
    """~160 names of ~200 bytes fill and split a 32 KiB getdents batch."""
    root = tmp_path / "tree"
    root.mkdir()
    names = [f"{i:03d}_" + "a" * (200 + i % 10) for i in range(160)]
    for name in names:
        (root / name).write_bytes(b"")
    expected = sorted(name.encode() for name in names)
    res = run_in(binary, root, "a")
    assert res.status == 0, f"{res}"
    assert res.err == b"", f"{res}"
    assert entries(res.out) == expected, f"{res}"


def test_negative_length_is_named(tmp_path: Path) -> None:
    """A wrapped (negative) length is rejected before the copy count is used."""
    binary = _mutant("ZFIND_MUTANT_LEN")
    names = _make_tree(tmp_path / "tree", 0)
    assert min(len(name) for name in names) < 8, (
        "the tree must contain a name shorter than 8 bytes or this test is vacuous"
    )
    res = run_in(binary, tmp_path / "tree", "a", timeout=RUNAWAY_TIMEOUT)
    assert res.status == 1, f"{res}"
    assert res.err == NAME_ERROR, f"{res}"
    assert res.out == b"", f"{res}"


def test_wrong_length_changes_output(tmp_path: Path) -> None:
    """A halved length is not rejected; the fuzz notices the wrong output.

    The halved length is plausible, so this must exit 0 with different output,
    not crash or time out.
    """
    binary = _mutant("ZFIND_MUTANT_SHR")
    names = _make_tree(tmp_path / "tree", 0)
    expected = joined([name for name in names if b"a" in name], b"\n")
    res = run_in(binary, tmp_path / "tree", "a", timeout=SPAWN_TIMEOUT)
    assert res.status == 0, f"{res}"
    assert res.err == b"", f"{res}"
    assert res.out != expected, "the halved length must change the output"


def test_over_estimate_is_named(tmp_path: Path) -> None:
    """An over-estimate above the padding slack is caught against d_reclen.

    This is the only test that fails if the check compares against a fixed 4096
    constant instead of the entry's own record length.
    """
    binary = _mutant("ZFIND_MUTANT_OVER")
    _make_tree(tmp_path / "tree", 0)
    res = run_in(binary, tmp_path / "tree", "a", timeout=SPAWN_TIMEOUT)
    assert res.status == 1, f"{res}"
    assert res.err == NAME_ERROR, f"{res}"
    assert res.out == b"", f"{res}"


def test_impossible_size_is_an_error_not_an_allocation(tmp_path: Path) -> None:
    """An salloc request larger than ARENA_SZ is rejected before it wraps."""
    binary = _mutant("ZFIND_MUTANT_SIZE")
    _make_tree(tmp_path / "tree", 0)
    res = run_in(binary, tmp_path / "tree", "a", timeout=RUNAWAY_TIMEOUT)
    assert res.status == 1, f"{res}"
    assert res.err == SIZE_ERROR, f"{res}"
    assert res.out == b"", f"{res}"


def test_impossible_arena_request_is_an_error(tmp_path: Path) -> None:
    """An alloc request larger than ARENA_SZ is rejected before it wraps."""
    binary = _mutant("ZFIND_MUTANT_ALLOC")
    _make_tree(tmp_path / "tree", 0)
    res = run_in(binary, tmp_path / "tree", "a", timeout=RUNAWAY_TIMEOUT)
    assert res.status == 1, f"{res}"
    assert res.err == ARENA_ERROR, f"{res}"
    assert res.out == b"", f"{res}"
