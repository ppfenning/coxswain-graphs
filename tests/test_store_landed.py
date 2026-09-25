from harness.store_dialect import json_load
from harness.store_landed import _with_landed, mark_landed
from harness.store_write import Store

KEY = ("r1", "p1", "t1")
PR = "https://example.test/org/repo/pull/7"
AT = "2026-09-25T12:00:00Z"
SEEDED = "2026-09-25T00:00:00Z"


def _stored(conn):
    raw, updated_at = conn.query_one("SELECT record_json, updated_at FROM task_records")
    return json_load(raw), updated_at


def test_with_landed_adds_the_object_and_leaves_the_argument_alone():
    record = {"id": "t1", "landed": {"pr": "old", "at": "old"}}
    assert _with_landed(record, PR, AT) == {"id": "t1", "landed": {"pr": PR, "at": AT}}
    assert record == {"id": "t1", "landed": {"pr": "old", "at": "old"}}


def test_a_stored_record_gains_the_landed_fields_and_keeps_the_rest(store_conn):
    Store(store_conn).record_task_record(*KEY, {"id": "t1", "state": "done", "n": 2}, SEEDED)
    expected = {"id": "t1", "state": "done", "n": 2, "landed": {"pr": PR, "at": AT}}
    assert mark_landed(store_conn, *KEY, PR, AT) == expected
    assert _stored(store_conn) == (expected, SEEDED)


def test_a_repeat_call_leaves_the_same_record(store_conn):
    Store(store_conn).record_task_record(*KEY, {"id": "t1", "state": "done"}, SEEDED)
    first = mark_landed(store_conn, *KEY, PR, AT)
    after_first = _stored(store_conn)
    assert mark_landed(store_conn, *KEY, PR, AT) == first
    assert _stored(store_conn) == after_first
    assert store_conn.query_one("SELECT COUNT(*) FROM task_records")[0] == 1


def test_a_missing_record_returns_none_and_writes_nothing(store_conn):
    assert mark_landed(store_conn, *KEY, PR, AT) is None
    assert store_conn.query_one("SELECT COUNT(*) FROM task_records")[0] == 0
    Store(store_conn).record_task_record("r1", "p1", "other", {"id": "other"}, SEEDED)
    assert mark_landed(store_conn, *KEY, PR, AT) is None
    assert _stored(store_conn) == ({"id": "other"}, SEEDED)
