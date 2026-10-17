from harness.store_dialect import connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-10-04T00:00:00Z"
NOW2 = "2026-10-05T00:00:00Z"
LATEST = len(default_modules())

_COLUMNS = "initiative, task_id, phase, state, needs_json, updated_at, updated_by, epoch"


def test_a_database_at_version_eleven_gains_priority_and_the_old_row_reads_zero(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:11]) == 11
        c.execute(f"INSERT INTO work_items ({_COLUMNS}) VALUES ('i1', 't1', 'p1', 'ready', '[]', '{NOW1}', 'chair', 3)")
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        assert c.query_all(f"SELECT {_COLUMNS}, priority FROM work_items") == [
            ("i1", "t1", "p1", "ready", "[]", NOW1, "chair", 3, 0)
        ]
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[11:12] == [(12, NOW2)]
    finally:
        c.close()


def test_a_row_inserted_without_naming_priority_gets_zero(store_conn):
    store_conn.execute(
        f"INSERT INTO work_items ({_COLUMNS}) VALUES ('i1', 't1', 'p1', 'ready', '[]', '{NOW1}', 'chair', 0)"
    )
    assert store_conn.query_all("SELECT priority FROM work_items") == [(0,)]
