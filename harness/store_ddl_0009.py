"""Migration 0009: queue and claim columns on work_items.

Queue columns kind, title, surfaces_json, body and extra_json are nullable, the JSON
pair typed per dialect like needs_json. Claim columns holder and expires_at are nullable
TEXT; epoch is INTEGER and defaults to 0 so an existing row reads an unclaimed epoch
rather than NULL. Existing rows keep NULL on every column but epoch. No index, no
foreign-key clause, matching every migration since 0002.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 9
DESCRIPTION = "work_items gains queue and claim columns"

_JSON = "JSON"

Column = tuple[str, str, str | None]  # name, type (JSON resolved per dialect), default SQL or None

# Table, columns the ALTERs append to the migration 0006 table, in the order applied.
_COLUMNS: tuple[Column, ...] = (
    ("kind", "TEXT", None),
    ("title", "TEXT", None),
    ("surfaces_json", _JSON, None),
    ("body", "TEXT", None),
    ("extra_json", _JSON, None),
    ("holder", "TEXT", None),
    ("epoch", "INTEGER", "0"),
    ("expires_at", "TEXT", None),
)

# Table and column that the ALTERs append; store_copy reads this 2-tuple shape, matching ddl2/ddl3/ddl5.
_ADDED: tuple[tuple[str, str], ...] = tuple(("work_items", c) for c, _, _ in _COLUMNS)


def _alter(dialect: Dialect, column: str, kind: str, default: str | None) -> str:
    sql_type = dialect.json_type if kind == _JSON else kind
    tail = "" if default is None else f" DEFAULT {default}"
    return f"ALTER TABLE work_items ADD COLUMN {column} {sql_type}{tail}"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """One ALTER per added column, the same on both dialects but for the JSON type."""
    return tuple(_alter(dialect, c, k, d) for c, k, d in _COLUMNS)
