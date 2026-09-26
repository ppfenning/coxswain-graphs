import sqlite3

import pytest

import harness.store_ddl_0004 as ddl
from harness.store_dialect import POSTGRES, SQLITE, connect, forbidden_constructs, json_load, json_text
from harness.store_migrate import check_version, default_modules, migrate, open_store

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"

COLUMNS = ["run_id", "phase_id", "task_id", "record_json", "updated_at"]
KEY = ["run_id", "phase_id", "task_id"]


def duplicate_key_error(dialect):
    """The driver's own error class: `Connection` does not wrap driver errors."""
    if dialect.name == "postgres":
        import psycopg.errors

        return psycopg.errors.UniqueViolation
    return sqlite3.IntegrityError


def insert(conn, task_id, record):
    marks = ", ".join(conn.dialect.placeholder for _ in COLUMNS)
    conn.execute(
        f"INSERT INTO task_records ({', '.join(COLUMNS)}) VALUES ({marks})",
        ("r1", "p1", task_id, json_text(record), NOW1),
    )


# SQLite only: PRAGMA table_info has no Postgres counterpart.
def test_task_records_has_the_listed_columns_and_composite_key():
    c = open_store("sqlite:///:memory:", NOW1)
    try:
        rows = c.query_all("PRAGMA table_info(task_records)")
        assert [r[1] for r in rows] == COLUMNS
        assert [r[2] for r in rows] == ["TEXT"] * 5
        assert [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])] == KEY
    finally:
        c.close()


def test_a_fresh_store_reaches_the_newest_version(store_conn):
    assert check_version(store_conn) == (6, 6)
    assert store_conn.query_all("SELECT run_id FROM task_records") == []


def test_the_key_is_the_whole_triple(store_conn):
    insert(store_conn, "t1", {"n": 1})
    insert(store_conn, "t2", {"n": 2})
    with pytest.raises(duplicate_key_error(store_conn.dialect)):
        insert(store_conn, "t1", {"n": 3})
    rows = store_conn.query_all("SELECT task_id, record_json FROM task_records ORDER BY task_id")
    assert [(t, json_load(j)) for t, j in rows] == [("t1", {"n": 1}), ("t2", {"n": 2})]


def test_a_database_at_version_three_applies_only_0004():
    c = connect("sqlite:///:memory:")
    try:
        assert migrate(c, NOW1, default_modules()[:3]) == 3
        c.execute("INSERT INTO runs (run_id) VALUES ('r1')")
        assert migrate(c, NOW2, default_modules()[:4]) == 4
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[3:] == [(4, NOW2)]
        assert c.query_all("SELECT run_id FROM runs") == [("r1",)]
        assert c.query_all("SELECT COUNT(*) FROM task_records") == [(0,)]
    finally:
        c.close()


def test_applying_again_is_a_no_op():
    c = open_store("sqlite:///:memory:", NOW1)
    try:
        insert(c, "t1", {"n": 1})
        assert migrate(c, NOW2, default_modules()) == 6
        assert c.query_all("SELECT version FROM schema_version ORDER BY version") == [(1,), (2,), (3,), (4,), (5,), (6,)]
        assert c.query_all("SELECT COUNT(*) FROM task_records") == [(1,)]
    finally:
        c.close()


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_the_statement_is_portable_and_has_no_foreign_key(dialect):
    sql = ddl.statements(dialect)
    assert len(sql) == 1
    assert forbidden_constructs(sql[0]) == ()
    assert "REFERENCES" not in sql[0].upper() and "FOREIGN KEY" not in sql[0].upper()
    assert "PRIMARY KEY (run_id, phase_id, task_id)" in sql[0]


def test_the_record_column_uses_the_dialect_json_type():
    assert "record_json TEXT" in ddl.statements(SQLITE)[0]
    assert "record_json JSONB" in ddl.statements(POSTGRES)[0]
