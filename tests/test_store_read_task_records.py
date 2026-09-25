import pytest

import harness.store_read as read
from harness.store_dialect import json_text

UPDATED = "2026-09-25T00:00:00Z"


def put(conn, run_id, phase_id, task_id, record):
    marks = ", ".join(conn.dialect.placeholder for _ in range(5))
    conn.execute(
        f"INSERT INTO task_records (run_id, phase_id, task_id, record_json, updated_at) VALUES ({marks})",
        (run_id, phase_id, task_id, json_text(record), UPDATED),
    )


@pytest.fixture
def conn(store_conn):
    put(store_conn, "r1", "p2", "t1", {"n": "p2t1"})
    put(store_conn, "r1", "p1", "t2", {"n": "p1t2", "verify": ["a", "b"]})
    put(store_conn, "r1", "p1", "t1", {"n": "p1t1"})
    put(store_conn, "r2", "p1", "t1", {"n": "other-run"})
    return store_conn


def test_a_hit_returns_the_decoded_record(conn):
    assert read.task_record(conn, "r1", "p1", "t2") == {"n": "p1t2", "verify": ["a", "b"]}


def test_a_miss_on_any_key_part_is_none(conn):
    assert read.task_record(conn, "r1", "p1", "t9") is None
    assert read.task_record(conn, "r1", "p9", "t1") is None
    assert read.task_record(conn, "r9", "p1", "t1") is None


def test_the_same_task_id_in_another_run_is_not_returned(conn):
    assert read.task_record(conn, "r2", "p1", "t1") == {"n": "other-run"}


def test_records_of_a_run_are_keyed_and_ordered_by_phase_then_task(conn):
    assert read.task_records(conn, "r1") == {
        ("p1", "t1"): {"n": "p1t1"},
        ("p1", "t2"): {"n": "p1t2", "verify": ["a", "b"]},
        ("p2", "t1"): {"n": "p2t1"},
    }
    assert list(read.task_records(conn, "r1")) == [("p1", "t1"), ("p1", "t2"), ("p2", "t1")]


def test_records_filter_by_run_and_an_unknown_run_is_empty(conn):
    assert read.task_records(conn, "r2") == {("p1", "t1"): {"n": "other-run"}}
    assert read.task_records(conn, "r9") == {}
