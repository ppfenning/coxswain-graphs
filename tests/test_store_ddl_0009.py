from harness.store_dialect import POSTGRES, connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"
LATEST = len(default_modules())
TEXT_NEW = ("kind", "title", "surfaces_json", "body", "extra_json", "holder", "expires_at")


def shape(conn):
    """{column: (type, nullable, default)} for work_items, read from the dialect's own catalogue."""
    if conn.dialect is POSTGRES:
        sql = (
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'work_items'"
        )
        return {n: (t.upper(), nullable == "YES", d) for n, t, nullable, d in conn.query_all(sql)}
    return {r[1]: (r[2].upper(), r[3] == 0, r[4]) for r in conn.query_all("PRAGMA table_info(work_items)")}


def test_a_database_at_version_eight_gains_queue_and_claim_columns_and_old_rows_read_null_or_zero(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:8]) == 8
        c.execute(
            "INSERT INTO work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by)"
            f" VALUES ('i1', 't1', 'p1', 'ready', '[]', '{NOW1}', 'chair')"
        )
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        found = shape(c)
        assert [found[n][1:] for n in TEXT_NEW] == [(True, None)] * len(TEXT_NEW)
        assert found["epoch"][1] is True
        row = c.query_all(
            "SELECT kind, title, surfaces_json, body, extra_json, holder, epoch, expires_at FROM work_items"
        )
        assert row == [(None, None, None, None, None, None, 0, None)]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[8:9] == [(9, NOW2)]
    finally:
        c.close()
