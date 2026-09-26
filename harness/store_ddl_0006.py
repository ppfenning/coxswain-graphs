"""Migration 0006: the work_items table, one row per task of an initiative.

There is no foreign-key clause, matching 0002 and 0004: the join to other tables is by value.
Every column is NOT NULL. needs_json holds the task's needs; updated_at is TEXT, written by the caller.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 6
DESCRIPTION = "work_items table"

_JSON = "JSON"

Column = tuple[str, str]

# Name, columns, key. The JSON token is resolved per dialect.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "work_items",
        (
            ("initiative", "TEXT"),
            ("task_id", "TEXT"),
            ("phase", "TEXT"),
            ("state", "TEXT"),
            ("needs_json", _JSON),
            ("updated_at", "TEXT"),
            ("updated_by", "TEXT"),
        ),
        ("initiative", "task_id"),
    ),
)


def _table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {dialect.json_type if t == _JSON else t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, with the JSON column typed per dialect."""
    return tuple(_table(dialect, n, c, k) for n, c, k in _TABLES)
