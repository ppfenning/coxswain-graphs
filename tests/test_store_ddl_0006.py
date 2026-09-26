import sqlite3

import pytest

import harness.store_ddl_0006 as ddl
from harness.store_dialect import POSTGRES, SQLITE, connect, forbidden_constructs, json_load, json_text
from harness.store_migrate import check_version, default_modules, migrate, open_store

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"

COLUMNS = ["initiative", "task_id", "phase", "state", "needs_json", "updated_at", "updated_by"]
KEY = ["initiative", "task_id"]


def integrity_error(dialect):
    """The driver's own error classes: `Connection` does not wrap driver errors."""
    if dialect.name == "postgres":
        import psycopg.errors

        return (psycopg.errors.UniqueViolation, psycopg.errors.NotNullViolation)
    return (sqlite3.IntegrityError,)


def insert(conn, initiative, task_id, state="open", needs=None):
    marks = ", ".join(conn.dialect.placeholder for _ in COLUMNS)
    conn.execute(
        f"INSERT INTO work_items ({', '.join(COLUMNS)}) VALUES ({marks})",
        (initiative, task_id, "p1", state, json_text(needs or []), NOW1, "tester"),
    )


# SQLite only: PRAGMA table_info has no Postgres counterpart.
def test_work_items_has_the_listed_columns_all_not_null_and_composite_key():
    c = open_store("sqlite:///:memory:", NOW1)
    try:
        rows = c.query_all("PRAGMA table_info(work_items)")
        assert [r[1] for r in rows] == COLUMNS
        assert [r[2] for r in rows] == ["TEXT"] * 7
        assert [r[3] for r in rows] == [1] * 7
        assert [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])] == KEY
    finally:
        c.close()


def test_a_fresh_store_reaches_the_newest_version(store_conn):
    assert check_version(store_conn) == (6, 6)
    assert store_conn.query_all("SELECT initiative FROM work_items") == []


def test_the_key_is_initiative_and_task_id(store_conn):
    insert(store_conn, "i1", "t1", needs=["t0"])
    insert(store_conn, "i2", "t1")
    with pytest.raises(integrity_error(store_conn.dialect)[0]):
        insert(store_conn, "i1", "t1", state="done")
    rows = store_conn.query_all("SELECT initiative, task_id, needs_json FROM work_items ORDER BY initiative")
    assert [(i, t, json_load(n)) for i, t, n in rows] == [("i1", "t1", ["t0"]), ("i2", "t1", [])]


def test_a_null_column_is_refused(store_conn):
    with pytest.raises(integrity_error(store_conn.dialect)):
        insert(store_conn, "i1", "t1", state=None)


def test_a_database_at_version_five_applies_only_0006():
    c = connect("sqlite:///:memory:")
    try:
        assert migrate(c, NOW1, default_modules()[:5]) == 5
        c.execute("INSERT INTO runs (run_id) VALUES ('r1')")
        assert migrate(c, NOW2, default_modules()) == 6
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[5:] == [(6, NOW2)]
        assert c.query_all("SELECT run_id FROM runs") == [("r1",)]
        assert c.query_all("SELECT COUNT(*) FROM work_items") == [(0,)]
    finally:
        c.close()


def test_applying_again_is_a_no_op():
    c = open_store("sqlite:///:memory:", NOW1)
    try:
        insert(c, "i1", "t1")
        assert migrate(c, NOW2, default_modules()) == 6
        versions = c.query_all("SELECT version FROM schema_version ORDER BY version")
        assert versions == [(1,), (2,), (3,), (4,), (5,), (6,)]
        assert c.query_all("SELECT COUNT(*) FROM work_items") == [(1,)]
    finally:
        c.close()


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_the_statement_is_portable_and_has_no_foreign_key(dialect):
    sql = ddl.statements(dialect)
    assert len(sql) == 1
    assert forbidden_constructs(sql[0]) == ()
    assert "REFERENCES" not in sql[0].upper() and "FOREIGN KEY" not in sql[0].upper()
    assert "PRIMARY KEY (initiative, task_id)" in sql[0]


def test_the_needs_column_uses_the_dialect_json_type():
    assert "needs_json TEXT NOT NULL" in ddl.statements(SQLITE)[0]
    assert "needs_json JSONB NOT NULL" in ddl.statements(POSTGRES)[0]


def test_store_copy_lists_work_items():
    """store_copy lists every migration's tables by hand; a table left out is silently not copied."""
    from harness import store_copy

    assert "work_items" in {name for name, _, _ in store_copy.tables()}
