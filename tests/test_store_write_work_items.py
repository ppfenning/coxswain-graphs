from harness.store_dialect import json_load
from harness.store_write import upsert_work_item, work_item_row


def test_work_item_row_is_the_seven_columns_with_needs_kept_as_a_list():
    needs = ["t0"]
    assert work_item_row("i1", "t1", "p1", "ready", needs, "2026-09-25T00:00:00Z", "alice") == {
        "initiative": "i1",
        "task_id": "t1",
        "phase": "p1",
        "state": "ready",
        "needs_json": ["t0"],
        "updated_at": "2026-09-25T00:00:00Z",
        "updated_by": "alice",
    }
    assert needs == ["t0"]


def test_upserting_the_same_key_twice_leaves_one_row_holding_the_second_write(store_conn):
    upsert_work_item(store_conn, "i1", "t1", "p1", "ready", ["t0"], "2026-09-25T00:00:00Z", "alice")
    upsert_work_item(store_conn, "i1", "t1", "p2", "done", ["t0", "t9"], "2026-09-25T01:00:00Z", "bob")
    assert store_conn.query_one("SELECT COUNT(*) FROM work_items")[0] == 1
    phase, state, raw, updated_at, updated_by = store_conn.query_one(
        "SELECT phase, state, needs_json, updated_at, updated_by FROM work_items"
    )
    assert (phase, state, updated_at, updated_by) == ("p2", "done", "2026-09-25T01:00:00Z", "bob")
    assert json_load(raw) == ["t0", "t9"]
