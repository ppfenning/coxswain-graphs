import pytest

import harness.store_ddl_0001 as ddl1
import harness.store_ddl_0002 as ddl
from harness.store_dialect import POSTGRES, SQLITE, connect, forbidden_constructs
from harness.store_migrate import check_version, migrate, open_store

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"

COLUMNS = {
    "graphs": ["graph_id", "name", "version", "content_hash", "registered_at", "definition_json"],
    "graph_nodes": [
        "graph_id",
        "node_id",
        "ord",
        "role",
        "default_tier",
        "default_class",
        "output_schema_hash",
    ],
    "graph_edges": ["graph_id", "src", "dst"],
}
PKS = {"graphs": ["graph_id"], "graph_nodes": ["graph_id", "node_id"], "graph_edges": ["graph_id", "src", "dst"]}


def info(conn, table):
    return conn.query_all(f"PRAGMA table_info({table})")


@pytest.fixture
def conn():
    c = open_store("sqlite:///:memory:", NOW1)
    yield c
    c.close()


def test_an_empty_database_reaches_the_newest_version(conn):
    assert check_version(conn) == (4, 4)
    assert conn.query_one("SELECT MAX(version) FROM schema_version") == (4,)


@pytest.mark.parametrize("table", sorted(COLUMNS))
def test_registry_table_has_the_listed_columns_and_key(conn, table):
    rows = info(conn, table)
    assert [r[1] for r in rows] == COLUMNS[table]
    assert [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])] == PKS[table]


def test_ord_is_an_integer_and_the_schema_hash_is_nullable(conn):
    rows = {r[1]: r for r in info(conn, "graph_nodes")}
    assert rows["ord"][2] == "INTEGER"
    assert rows["output_schema_hash"][3] == 0


@pytest.mark.parametrize(("table", "column"), [("runs", "graph_id"), ("node_calls", "node_id")])
def test_the_join_columns_are_nullable_text(conn, table, column):
    found = next(r for r in info(conn, table) if r[1] == column)
    assert (found[2], found[3]) == ("TEXT", 0)


def test_the_name_version_index_exists(conn):
    rows = conn.query_all("SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = 'ix_graphs_name_version'")
    assert rows == [("graphs",)]
    assert [r[2] for r in conn.query_all("PRAGMA index_info(ix_graphs_name_version)")] == ["name", "version"]


def test_a_database_at_version_one_applies_only_0002():
    c = connect("sqlite:///:memory:")
    try:
        assert migrate(c, NOW1, [ddl1]) == 1
        c.execute("INSERT INTO runs (run_id) VALUES ('r1')")
        c.execute("INSERT INTO node_calls (call_id, run_id) VALUES ('c1', 'r1')")
        assert migrate(c, NOW2, [ddl1, ddl]) == 2
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == [(1, NOW1), (2, NOW2)]
        assert c.query_all("SELECT run_id, graph_id FROM runs") == [("r1", None)]
        assert c.query_all("SELECT call_id, run_id, node_id FROM node_calls") == [("c1", "r1", None)]
    finally:
        c.close()


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_every_statement_is_portable_and_has_no_foreign_key(dialect):
    sql = ddl.statements(dialect)
    assert len(sql) == 6
    assert [forbidden_constructs(s) for s in sql] == [()] * len(sql)
    assert not [s for s in sql if "REFERENCES" in s.upper() or "FOREIGN KEY" in s.upper()]


def test_the_definition_column_uses_the_dialect_json_type():
    assert "definition_json TEXT" in ddl.statements(SQLITE)[0]
    assert "definition_json JSONB" in ddl.statements(POSTGRES)[0]
