"""A run's pause flag: `runs.paused_at`, nullable, set and cleared by name.

A run is paused exactly when `paused_at` is not null. Time is always an argument:
`set_paused` takes the ISO UTC timestamp to store. Nothing here reads the clock.
"""

from __future__ import annotations

from harness.store_dialect import Connection, Dialect

__all__ = ["clear_paused", "is_paused", "set_paused"]

_SET = "UPDATE runs SET paused_at = ? WHERE run_id = ?"
_CLEAR = "UPDATE runs SET paused_at = NULL WHERE run_id = ?"
_READ = "SELECT paused_at FROM runs WHERE run_id = ?"

SQL = (_SET, _CLEAR, _READ)


def _sql(dialect: Dialect, text: str) -> str:
    return text.replace("?", dialect.placeholder)


def set_paused(conn: Connection, run_id: str, at: str) -> None:
    """Set `runs.paused_at` to `at` for `run_id`. No matching row is a no-op."""
    conn.execute(_sql(conn.dialect, _SET), (at, run_id))


def clear_paused(conn: Connection, run_id: str) -> None:
    """Set `runs.paused_at` back to NULL for `run_id`. No matching row is a no-op."""
    conn.execute(_sql(conn.dialect, _CLEAR), (run_id,))


def is_paused(conn: Connection, run_id: str) -> bool:
    """True when `run_id`'s row has a non-null `paused_at`; false when null or no row."""
    row = conn.query_one(_sql(conn.dialect, _READ), (run_id,))
    return row is not None and row[0] is not None
