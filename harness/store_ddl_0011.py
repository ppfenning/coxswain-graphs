"""Migration 0011: id_sequence table, and short_id on work_items and runs.

id_sequence holds one row per scope, the next short id to allocate from that scope;
scope is the primary key and next is a plain counter with no default. work_items.short_id
and runs.short_id are nullable TEXT: existing rows keep NULL until something allocates
one. No index, no foreign-key clause, matching every migration since 0002. No allocation
or backfill logic lives here, only the schema.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 11
DESCRIPTION = "id_sequence table; work_items and runs gain short_id"

Column = tuple[str, str]

# Name, columns, key.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "id_sequence",
        (
            ("scope", "TEXT"),
            ("next", "INTEGER"),
        ),
        ("scope",),
    ),
)

AlterColumn = tuple[str, str, str]  # table, column, type

# Table, column, type for each ALTER, in the order applied.
_COLUMNS: tuple[AlterColumn, ...] = (
    ("work_items", "short_id", "TEXT"),
    ("runs", "short_id", "TEXT"),
)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5/ddl9/ddl10.
_ADDED: tuple[tuple[str, str], ...] = tuple((t, c) for t, c, _ in _COLUMNS)


def _table(name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {t} NOT NULL" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def _alter(table: str, column: str, kind: str) -> str:
    return f"ALTER TABLE {table} ADD COLUMN {column} {kind}"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """The id_sequence CREATE TABLE, then one ALTER per added column, the same on both dialects."""
    return (
        *(_table(n, c, k) for n, c, k in _TABLES),
        *(_alter(t, c, k) for t, c, k in _COLUMNS),
    )
