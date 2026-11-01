"""Tests for --hidden and --no-hidden."""

from __future__ import annotations

import pytest
from helpers import ZfindRunner, entries, words


HIDDEN = words(".dotdir/ .dotdir/.deep .dotdir/inside .dotfile sub/ sub/.dot2 sub/visible2 visible")
HIDDEN_SKIPPED = words("sub/ sub/visible2 visible")


# -----------------------------------------------------------------------------
# Flags: --hidden & --no-hidden
# -----------------------------------------------------------------------------

class TestNoHidden:
    @pytest.mark.parametrize("args, expected", [
        pytest.param([], HIDDEN, id="default-includes-hidden"),
        pytest.param(["--hidden"], HIDDEN, id="explicit-hidden-flag"),
        pytest.param(["--no-hidden"], HIDDEN_SKIPPED, id="no-hidden-skips-dotfiles"),
    ])
    def test_hidden_file_visibility(self, zfind: ZfindRunner, args: list[str], expected: list[bytes]):
        res = zfind("hidden", *args)
        assert res.status == 0, f"Command failed: {res}"
        assert entries(res.out) == expected
