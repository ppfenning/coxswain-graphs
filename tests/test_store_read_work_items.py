import pytest

import harness.store_read as read
from harness.store_dialect import json_text

UPDATED = "2026-09-25T00:00:00Z"


def put(conn, initiative, task_id, phase, state, needs, by):
    marks = ", ".join(conn.dialect.placeholder for _ in range(7))
    conn.execute(
        "INSERT INTO work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by)"
        f" VALUES ({marks})",
        (initiative, task_id, phase, state, json_text(needs), UPDATED, by),
    )


@pytest.fixture
def conn(store_conn):
    put(store_conn, "i1", "t2", "p1", "planned", [], "alice")
    put(store_conn, "i1", "t1", "p1", "done", ["t0"], "bob")
    put(store_conn, "i2", "t1", "p9", "planned", [], "carol")
    return store_conn


def test_only_the_initiatives_rows_come_back_in_task_id_order(conn):
    assert read.work_items(conn, "i1") == [
        {
            "initiative": "i1",
            "task_id": "t1",
            "phase": "p1",
            "state": "done",
            "needs": ["t0"],
            "updated_at": UPDATED,
            "updated_by": "bob",
        },
        {
            "initiative": "i1",
            "task_id": "t2",
            "phase": "p1",
            "state": "planned",
            "needs": [],
            "updated_at": UPDATED,
            "updated_by": "alice",
        },
    ]


def test_needs_is_a_decoded_list_and_needs_json_is_not_a_key(conn):
    first = read.work_items(conn, "i1")[0]
    assert first["needs"] == ["t0"]
    assert "needs_json" not in first


def test_an_unknown_initiative_is_an_empty_list(conn):
    assert read.work_items(conn, "nope") == []
