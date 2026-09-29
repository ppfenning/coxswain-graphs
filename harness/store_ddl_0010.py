"""Migration 0010: runs.paused_at and leases.status.

runs.paused_at is a nullable TEXT timestamp; a run is paused exactly when it is not
null. leases.status is a nullable TEXT column recording what a lease's last heartbeat
was doing (for example "paused"), alongside heartbeat_at. Both columns have no default
and no constraint, so existing rows keep NULL. No index, no foreign-key clause, matching
every migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 10
DESCRIPTION = "runs gains paused_at, leases gains status"

Column = tuple[str, str, str]  # table, column, type

# Table, column, type for each ALTER, in the order applied.
_COLUMNS: tuple[Column, ...] = (
    ("runs", "paused_at", "TEXT"),
    ("leases", "status", "TEXT"),
)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9.
_ADDED: tuple[tuple[str, str], ...] = tuple((t, c) for t, c, _ in _COLUMNS)


def _alter(table: str, column: str, kind: str) -> str:
    return f"ALTER TABLE {table} ADD COLUMN {column} {kind}"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """One ALTER per added column, the same on both dialects: both columns are plain TEXT."""
    return tuple(_alter(t, c, k) for t, c, k in _COLUMNS)
