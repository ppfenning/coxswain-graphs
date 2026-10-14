import harness.store_ddl_0013 as ddl
from harness.store_dialect import connect
from harness.store_migrate import check_version, default_modules, migrate

NOW1 = "2026-10-04T00:00:00Z"
NOW2 = "2026-10-05T00:00:00Z"
NOW3 = "2026-10-06T00:00:00Z"
LATEST = len(default_modules())

_COLUMNS = "initiative, task_id, phase, state, needs_json, updated_at, updated_by, epoch"
_KEYS = ("issue_number", "project_item_id", "parent_item")


def test_a_database_at_version_twelve_gains_the_card_keys_and_the_old_row_reads_null(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()[:12]) == 12
        c.execute(f"INSERT INTO work_items ({_COLUMNS}) VALUES ('i1', 't1', 'p1', 'ready', '[]', '{NOW1}', 'chair', 3)")
        assert migrate(c, NOW2, default_modules()) == LATEST
        assert check_version(c) == (LATEST, LATEST)
        assert c.query_all(f"SELECT {_COLUMNS}, {', '.join(_KEYS)} FROM work_items") == [
            ("i1", "t1", "p1", "ready", "[]", NOW1, "chair", 3, None, None, None)
        ]
        assert c.query_all("SELECT version, applied_at FROM schema_version WHERE version = 13") == [(13, NOW2)]
    finally:
        c.close()


def test_a_second_migrate_returns_latest_and_adds_no_schema_version_row(store_url):
    c = connect(store_url)
    try:
        assert migrate(c, NOW1, default_modules()) == LATEST
        before = c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")
        assert migrate(c, NOW3, default_modules()) == LATEST
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version") == before
    finally:
        c.close()


def test_the_migration_names_the_three_columns_in_order():
    assert ddl.VERSION == 13
    assert tuple(("work_items", k) for k in _KEYS) == ddl._ADDED


def test_store_copy_carries_the_new_columns():
    """store_copy folds ddl13._ADDED into its alters, or a column left out drops silently."""
    from harness import store_copy

    tables = {n: c for n, c, _ in store_copy.tables()}
    assert all(k in tables["work_items"] for k in _KEYS)
