"""Migration 0002: the graphs registry and the graph_id join columns.

runs.graph_id and node_calls.node_id are plain nullable columns with no foreign-key
clause, because SQLite cannot add a constraint by ALTER TABLE. The join to graphs and
graph_nodes is by value, and nothing in the database enforces it. Existing rows keep NULL.
graphs.graph_id equals graphs.content_hash.
"""

from __future__ import annotations

from harness.store_dialect import Dialect

VERSION = 2
DESCRIPTION = "graphs registry, graph_id and node_id join columns"

_JSON = "JSON"

Column = tuple[str, str]


def _text(*names: str) -> tuple[Column, ...]:
    return tuple((n, "TEXT") for n in names)


# Name, columns, key. The JSON token is resolved per dialect.
# version is TEXT: the registration task must write it as text.
_TABLES: tuple[tuple[str, tuple[Column, ...], tuple[str, ...]], ...] = (
    (
        "graphs",
        _text("graph_id", "name", "version", "content_hash", "registered_at") + (("definition_json", _JSON),),
        ("graph_id",),
    ),
    (
        "graph_nodes",
        _text("graph_id", "node_id") + (("ord", "INTEGER"),) + _text("role", "default_tier", "default_class", "output_schema_hash"),
        ("graph_id", "node_id"),
    ),
    ("graph_edges", _text("graph_id", "src", "dst"), ("graph_id", "src", "dst")),
)

# Table and TEXT column that the ALTERs append to a migration 0001 table.
_ADDED: tuple[tuple[str, str], ...] = (("runs", "graph_id"), ("node_calls", "node_id"))


def _table(dialect: Dialect, name: str, columns: tuple[Column, ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {dialect.json_type if t == _JSON else t}" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def statements(dialect: Dialect) -> tuple[str, ...]:
    """Three registry tables, the name/version index, then the two ALTERs."""
    return (
        *(_table(dialect, n, c, k) for n, c, k in _TABLES),
        "CREATE INDEX IF NOT EXISTS ix_graphs_name_version ON graphs (name, version)",
        *(f"ALTER TABLE {t} ADD COLUMN {c} TEXT" for t, c in _ADDED),
    )
