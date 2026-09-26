"""Migration 0008: the hosts table, one row per machine the chair may dispatch to.

There is no foreign-key clause, matching 0002, 0004, 0006 and 0007.
Every column is NOT NULL. state is active, draining or offline. beat_at is "" until the first beat.
versions_json holds whatever the host reported: cox, graphs, cartridges and claude versions, and login_ok.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 8
DESCRIPTION = "hosts table"

_JSON = "JSON"

Column = tuple[str, str]

# Name, columns, key. The JSON token is resolved per dialect.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "hosts",
        (
            ("name", "TEXT"),
            ("ssh", "TEXT"),
            ("capacity", "INTEGER"),
            ("state", "TEXT"),
            ("beat_at", "TEXT"),
            ("versions_json", _JSON),
            ("updated_at", "TEXT"),
            ("updated_by", "TEXT"),
        ),
        ("name",),
    ),
)


def _table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {dialect.json_type if t == _JSON else t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The one CREATE TABLE, with the JSON column typed per dialect."""
    return tuple(_table(dialect, n, c, k) for n, c, k in _TABLES)
