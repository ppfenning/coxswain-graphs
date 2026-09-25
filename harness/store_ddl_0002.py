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


def _table(name: str, columns: tuple[tuple[str, str], ...], key: tuple[str, ...]) -> str:
    defs = ",\n".join(f"    {c} {t}" for c, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{defs},\n    PRIMARY KEY ({', '.join(key)})\n)"


def _text(*names: str) -> tuple[tuple[str, str], ...]:
    return tuple((n, "TEXT") for n in names)


def statements(dialect: Dialect) -> tuple[str, ...]:
    """Three registry tables, the name/version index, then the two ALTERs."""
    return (
        # version is TEXT: the registration task must write it as text.
        _table(
            "graphs",
            _text("graph_id", "name", "version", "content_hash", "registered_at") + (("definition_json", dialect.json_type),),
            ("graph_id",),
        ),
        _table(
            "graph_nodes",
            _text("graph_id", "node_id") + (("ord", "INTEGER"),) + _text("role", "default_tier", "default_class", "output_schema_hash"),
            ("graph_id", "node_id"),
        ),
        _table("graph_edges", _text("graph_id", "src", "dst"), ("graph_id", "src", "dst")),
        "CREATE INDEX IF NOT EXISTS ix_graphs_name_version ON graphs (name, version)",
        "ALTER TABLE runs ADD COLUMN graph_id TEXT",
        "ALTER TABLE node_calls ADD COLUMN node_id TEXT",
    )
