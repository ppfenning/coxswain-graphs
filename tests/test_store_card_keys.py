from harness.store_card_keys import backfill, card_keys, main
from harness.store_migrate import open_store
from harness.store_queue import upsert

T0 = "2026-09-27T00:00:00Z"

BASE = {
    "initiative": "i1",
    "kind": "task",
    "phase": "p1",
    "state": "ready",
    "needs": [],
    "title": "t",
    "surfaces": {},
    "body": "",
    "updated_at": T0,
    "updated_by": "chair",
}


def _add(conn, task_id, extra):
    row = {**BASE, "task_id": task_id}
    upsert(conn, row if extra is None else {**row, "extra": extra})


def _cols(conn, task_id):
    ph = conn.dialect.placeholder
    return conn.query_one(
        f"SELECT issue_number, project_item_id, parent_item FROM work_items WHERE task_id = {ph}", (task_id,)
    )


def test_a_row_with_issue_in_extra_gets_issue_number(store_conn):
    _add(store_conn, "t1", {"issue": "1647"})
    assert backfill(store_conn) == 1
    assert _cols(store_conn, "t1") == (1647, None, None)


def test_an_issue_number_already_set_is_kept(store_conn):
    _add(store_conn, "t1", {"issue": "1647"})
    store_conn.execute("UPDATE work_items SET issue_number = 9 WHERE task_id = 't1'")
    assert backfill(store_conn) == 0
    assert _cols(store_conn, "t1") == (9, None, None)


def test_an_unusable_issue_or_no_extra_leaves_all_three_null(store_conn):
    _add(store_conn, "t1", {"issue": "n/a"})
    _add(store_conn, "t2", None)
    assert backfill(store_conn) == 0
    assert _cols(store_conn, "t1") == (None, None, None)
    assert _cols(store_conn, "t2") == (None, None, None)


def test_a_second_backfill_returns_zero_and_changes_nothing(store_conn):
    _add(store_conn, "t1", {"issue": 12, "project_item_id": "PVT_1", "parent_item": "PVT_0"})
    _add(store_conn, "t2", {"issue": "7"})
    assert backfill(store_conn) == 2
    first = (_cols(store_conn, "t1"), _cols(store_conn, "t2"))
    assert backfill(store_conn) == 0
    assert (_cols(store_conn, "t1"), _cols(store_conn, "t2")) == first
    assert first == ((12, "PVT_1", "PVT_0"), (7, None, None))


def test_card_keys_takes_only_what_extra_states():
    assert card_keys({"issue": "1647"}) == {"issue_number": 1647}
    assert card_keys({"issue": 5, "project_item_id": "P", "parent_item": "Q"}) == {
        "issue_number": 5,
        "project_item_id": "P",
        "parent_item": "Q",
    }
    assert card_keys({"issue": True, "project_item_id": "", "parent_item": 3}) == {}
    assert card_keys({"issue": "12a"}) == {}
    assert card_keys(None) == {}


def test_main_prints_the_changed_count(tmp_path, capsys):
    url = f"sqlite:///{tmp_path}/store.db"
    conn = open_store(url, T0)
    try:
        _add(conn, "t1", {"issue": "3"})
    finally:
        conn.close()
    assert main([url]) == 0
    assert capsys.readouterr().out.strip() == "1"
