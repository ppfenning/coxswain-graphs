from conftest import T0

from harness.store_read import work_items
from harness.store_work_state import ABSENT, Mismatch, set_state
from harness.store_write import upsert_work_item

NOW = "2026-09-25T01:02:03Z"


def seed(conn, state="ready"):
    upsert_work_item(conn, "i1", "t1", "p1", state, ["t0"], T0, "alice")


def row(conn):
    return next(r for r in work_items(conn, "i1") if r["task_id"] == "t1")


def test_no_expected_still_upserts_unconditionally(store_conn):
    seed(store_conn, "done")
    written = set_state(store_conn, "i1", "t1", "ready", "bob", None, NOW)
    assert written["state"] == "ready"
    assert row(store_conn)["state"] == "ready"
    assert set_state(store_conn, "i1", "t2", "ready", "bob", "p2", NOW)["phase"] == "p2"


def test_matching_expected_writes_and_keeps_phase_and_needs(store_conn):
    seed(store_conn)
    written = set_state(store_conn, "i1", "t1", "done", "bob", None, NOW, expected="ready")
    assert written == {
        "initiative": "i1",
        "task_id": "t1",
        "phase": "p1",
        "state": "done",
        "needs": ["t0"],
        "updated_at": NOW,
        "updated_by": "bob",
    }
    assert row(store_conn)["state"] == "done"


def test_mismatched_expected_leaves_the_row_and_reports_the_current_state(store_conn):
    seed(store_conn, "blocked")
    assert set_state(store_conn, "i1", "t1", "done", "bob", None, NOW, expected="ready") == Mismatch("blocked")
    after = row(store_conn)
    assert (after["state"], after["updated_by"], after["updated_at"]) == ("blocked", "alice", T0)


def test_missing_row_under_expected_is_a_mismatch_and_writes_nothing(store_conn):
    assert set_state(store_conn, "i1", "t1", "done", "bob", "p1", NOW, expected="ready") == Mismatch(None)
    assert work_items(store_conn, "i1") == []


def test_missing_row_under_absent_creates_it_when_a_phase_is_given(store_conn):
    written = set_state(store_conn, "i1", "t1", "ready", "bob", "p1", NOW, expected=ABSENT)
    assert written["state"] == "ready" and written["needs"] == []
    assert row(store_conn)["phase"] == "p1"


def test_present_row_under_absent_is_a_mismatch(store_conn):
    seed(store_conn)
    assert set_state(store_conn, "i1", "t1", "done", "bob", "p1", NOW, expected=ABSENT) == Mismatch("ready")
    assert row(store_conn)["state"] == "ready"


def test_missing_row_under_absent_without_a_phase_writes_nothing(store_conn):
    assert set_state(store_conn, "i1", "t1", "ready", "bob", None, NOW, expected=ABSENT) is None
    assert work_items(store_conn, "i1") == []
