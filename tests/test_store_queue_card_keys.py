import pytest

from harness.store_queue import read, upsert

T0 = "2026-09-27T00:00:00Z"
T30 = "2026-09-27T00:00:30Z"

ROW = {
    "initiative": "i1",
    "task_id": "t1",
    "kind": "task",
    "phase": "p1",
    "state": "ready",
    "needs": ["t0"],
    "title": "do the thing",
    "surfaces": {"files": ["a.py", "b.py"]},
    "body": "some body text",
    "extra": {"note": "extra data", "n": 2},
    "updated_at": T0,
    "updated_by": "chair",
}
KEYS = {"issue_number": 1648, "project_item_id": "PVTI_task1", "parent_item": "PVTI_init1"}
NO_KEYS = {"issue_number": None, "project_item_id": None, "parent_item": None}


def _card_keys(conn):
    (row,) = read(conn, initiative="i1")
    return {k: row[k] for k in KEYS}, row


def test_an_unkeyed_row_reads_none_for_all_three_card_keys(store_conn):
    upsert(store_conn, ROW)
    assert _card_keys(store_conn)[0] == NO_KEYS


def test_an_upsert_carrying_the_three_keys_reads_back_the_same_values_at_the_top_level(store_conn):
    upsert(store_conn, {**ROW, **KEYS})
    keys, row = _card_keys(store_conn)
    assert keys == KEYS
    assert row["extra"] == ROW["extra"]


def test_an_upsert_that_omits_the_keys_keeps_them(store_conn):
    upsert(store_conn, {**ROW, **KEYS})
    assert upsert(store_conn, {**ROW, "state": "done", "updated_at": T30}) is True
    assert _card_keys(store_conn)[0] == KEYS


def test_an_upsert_sending_none_clears_the_keys(store_conn):
    upsert(store_conn, {**ROW, **KEYS})
    assert upsert(store_conn, {**ROW, **NO_KEYS, "updated_at": T30}) is True
    assert _card_keys(store_conn)[0] == NO_KEYS


def test_a_repeat_upsert_with_identical_keys_returns_false_and_keeps_updated_at(store_conn):
    upsert(store_conn, {**ROW, **KEYS})
    assert upsert(store_conn, {**ROW, **KEYS, "updated_at": T30}) is False
    assert _card_keys(store_conn)[1]["updated_at"] == T0


@pytest.mark.parametrize("bad", ["12", True, 1.5])
def test_a_non_integer_issue_number_raises_value_error(store_conn, bad):
    with pytest.raises(ValueError, match="issue_number must be an integer"):
        upsert(store_conn, {**ROW, "issue_number": bad})
