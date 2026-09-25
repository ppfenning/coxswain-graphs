"""Migration 0003: runs.host, the host the run was launched from.

The column is nullable TEXT with no default and no constraint. Existing rows keep NULL.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 3
DESCRIPTION = "runs gains host, the host the run was launched from"


# Table and TEXT column that the ALTER appends to a migration 0001 table.
_ADDED: tuple[tuple[str, str], ...] = (("runs", "host"),)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The same ALTER on both dialects."""
    return tuple(f"ALTER TABLE {t} ADD COLUMN {c} TEXT" for t, c in _ADDED)
