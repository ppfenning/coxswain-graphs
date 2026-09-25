"""Migration 0003: runs.host, the host the run was launched from.

The column is nullable TEXT with no default and no constraint. Existing rows keep NULL.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 3
DESCRIPTION = "runs gains host, the host the run was launched from"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The same ALTER on both dialects."""
    return ("ALTER TABLE runs ADD COLUMN host TEXT",)
