from harness.store_dialect import json_load
from harness.store_write import Store, task_record_row

KEY = ("r1", "p1", "t1")


def test_task_record_row_is_the_five_columns_with_the_record_kept_as_a_value():
    record = {"id": "t1", "state": "done"}
    assert task_record_row("r1", "p1", "t1", record, "2026-09-25T00:00:00Z") == {
        "run_id": "r1",
        "phase_id": "p1",
        "task_id": "t1",
        "record_json": {"id": "t1", "state": "done"},
        "updated_at": "2026-09-25T00:00:00Z",
    }
    assert record == {"id": "t1", "state": "done"}


def test_saving_the_same_key_twice_leaves_one_row_holding_the_second_write(store_conn):
    store = Store(store_conn)
    store.record_task_record(*KEY, {"state": "ready"}, "2026-09-25T00:00:00Z")
    store.record_task_record(*KEY, {"state": "done", "n": 2}, "2026-09-25T01:00:00Z")
    assert store_conn.query_one("SELECT COUNT(*) FROM task_records")[0] == 1
    raw, updated_at = store_conn.query_one("SELECT record_json, updated_at FROM task_records")
    assert json_load(raw) == {"state": "done", "n": 2}
    assert updated_at == "2026-09-25T01:00:00Z"
