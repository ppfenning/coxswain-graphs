import pytest

import harness.store_ddl_0005 as ddl
from harness.store_dialect import POSTGRES, SQLITE, connect, forbidden_constructs
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"
NEW = ("cause", "cause_why")


def shape(conn):
    """{column: (type, nullable, default)} for attempts, read from the dialect's own catalogue."""
    if conn.dialect is POSTGRES:
        sql = (
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'attempts'"
        )
        return {n: (t.upper(), nullable == "YES", d) for n, t, nullable, d in conn.query_all(sql)}
    return {r[1]: (r[2].upper(), r[3] == 0, r[4]) for r in conn.query_all("PRAGMA table_info(attempts)")}


def test_a_database_at_version_four_gains_two_nullable_columns_and_old_rows_read_null(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:4]) == 4
        c.execute("INSERT INTO attempts (run_id, task_id, seq) VALUES ('r1', 't1', 1)")
        assert migrate(c, NOW2, default_modules()) == 6
        assert check_version(c) == (6, 6)
        found = shape(c)
        assert [found[n] for n in NEW] == [("TEXT", True, None)] * 2
        assert c.query_all("SELECT run_id, cause, cause_why FROM attempts") == [("r1", None, None)]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[4:5] == [(5, NOW2)]
    finally:
        c.close()


def test_applying_again_changes_nothing(store_conn):
    store_conn.execute("INSERT INTO attempts (run_id, task_id, seq, cause) VALUES ('r1', 't1', 1, 'x')")
    versions = store_conn.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")
    columns = shape(store_conn)
    assert migrate(store_conn, NOW2, default_modules()) == 6
    assert store_conn.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == versions
    assert shape(store_conn) == columns
    assert store_conn.query_all("SELECT cause, cause_why FROM attempts") == [("x", None)]


@pytest.mark.parametrize("dialect", [SQLITE, POSTGRES], ids=["sqlite", "postgres"])
def test_every_statement_is_a_plain_nullable_text_alter(dialect):
    sql = ddl.statements(dialect)
    assert sql == tuple(f"ALTER TABLE attempts ADD COLUMN {c} TEXT" for c in NEW)
    assert [forbidden_constructs(s) for s in sql] == [()] * len(sql)
