from harness.store_dialect import POSTGRES, connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"
LATEST = len(default_modules())


def _shape(conn, table):
    """{column: (type, nullable, default)} for `table`, read from the dialect's own catalogue."""
    if conn.dialect is POSTGRES:
        sql = (
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
            f" WHERE table_schema = current_schema() AND table_name = '{table}'"
        )
        return {n: (t.upper(), nullable == "YES", d) for n, t, nullable, d in conn.query_all(sql)}
    return {r[1]: (r[2].upper(), r[3] == 0, r[4]) for r in conn.query_all(f"PRAGMA table_info({table})")}


def test_a_database_at_version_nine_gains_paused_at_and_status_and_old_rows_read_null(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:9]) == 9
        c.execute("INSERT INTO runs (run_id, status) VALUES ('r1', 'done')")
        c.execute(
            "INSERT INTO leases (name, holder, epoch, heartbeat_at, expires_at)"
            " VALUES ('l1', 'h1', 1, 'T0', 'T1')"
        )
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        assert _shape(c, "runs")["paused_at"] == ("TEXT", True, None)
        assert _shape(c, "leases")["status"] == ("TEXT", True, None)
        assert c.query_all("SELECT run_id, paused_at FROM runs") == [("r1", None)]
        assert c.query_all("SELECT name, status FROM leases") == [("l1", None)]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[9:10] == [(10, NOW2)]
    finally:
        c.close()
