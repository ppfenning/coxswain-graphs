"""Migration 0007: the chair_actions table, one row per action the chair takes.

There is no foreign-key clause, matching 0002, 0004 and 0006: the join to other tables is by value.
Every column is NOT NULL. target is the action's task_id, else initiative, else its first intake id, else "".
action_json holds the whole action line as the chair wrote it.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 7
DESCRIPTION = "chair_actions table"

_JSON = "JSON"

Column = tuple[str, str]

# Name, columns, key. The JSON token is resolved per dialect.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "chair_actions",
        (
            ("ts", "TEXT"),
            ("epoch", "INTEGER"),
            ("holder", "TEXT"),
            ("kind", "TEXT"),
            ("target", "TEXT"),
            ("status", "TEXT"),
            ("reason", "TEXT"),
            ("action_json", _JSON),
        ),
        ("ts", "holder", "kind", "target"),
    ),
)


def _table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {dialect.json_type if t == _JSON else t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, with the JSON column typed per dialect."""
    return tuple(_table(dialect, n, c, k) for n, c, k in _TABLES)
