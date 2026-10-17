from harness.store_dialect import POSTGRES, connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"
LATEST = len(default_modules())
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
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        found = shape(c)
        assert [found[n] for n in NEW] == [("TEXT", True, None)] * 2
        assert c.query_all("SELECT run_id, cause, cause_why FROM attempts") == [("r1", None, None)]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[4:5] == [(5, NOW2)]
    finally:
        c.close()
