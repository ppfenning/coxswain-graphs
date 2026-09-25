"""Migration 0004: the task_records table, one row per task of a phase of a run.

There is no foreign-key clause, matching 0002: the join to runs and phases is by value.
record_json holds the task record; updated_at is TEXT, written by the caller.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 4
DESCRIPTION = "task_records table"

_JSON = "JSON"

Column = tuple[str, str]

# Name, columns, key. The JSON token is resolved per dialect.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "task_records",
        (
            ("run_id", "TEXT"),
            ("phase_id", "TEXT"),
            ("task_id", "TEXT"),
            ("record_json", _JSON),
            ("updated_at", "TEXT"),
        ),
        ("run_id", "phase_id", "task_id"),
    ),
)


def _table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {dialect.json_type if t == _JSON else t}" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, with the JSON column typed per dialect."""
    return tuple(_table(dialect, n, c, k) for n, c, k in _TABLES)
