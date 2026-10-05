import pytest

from harness.store_queue import claim, main, read, release, upsert

T0 = "2026-09-27T00:00:00Z"
T30 = "2026-09-27T00:00:30Z"
T90 = "2026-09-27T00:01:30Z"

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


def _claim_cols(conn, initiative, task_id):
    row = conn.query_one(
        f"SELECT holder, epoch, expires_at FROM work_items WHERE initiative = {conn.dialect.placeholder}"
        f" AND task_id = {conn.dialect.placeholder}",
        (initiative, task_id),
    )
    return {"holder": row[0], "epoch": row[1], "expires_at": row[2]}


def test_upsert_then_read_round_trips_needs_surfaces_and_extra_as_json_values(store_conn):
    upsert(store_conn, ROW)
    assert read(store_conn, initiative="i1") == [{**ROW, "priority": 0, "holder": None, "epoch": 0, "expires_at": None}]


def test_a_second_identical_upsert_leaves_one_row_and_unchanged_claim_columns_and_updated_at(store_conn):
    upsert(store_conn, ROW)
    claim(store_conn, "i1", "t1", "alice", 30, T0)
    before = read(store_conn, initiative="i1")
    upsert(store_conn, ROW)
    after = read(store_conn, initiative="i1")
    assert len(after) == 1
    assert after == before
    assert after[0]["updated_at"] == T0
    assert _claim_cols(store_conn, "i1", "t1") == {"holder": "alice", "epoch": 1, "expires_at": T30}


def test_a_changed_upsert_writes_the_new_values_and_keeps_the_claim(store_conn):
    upsert(store_conn, ROW)
    claim(store_conn, "i1", "t1", "alice", 30, T0)
    changed = {**ROW, "state": "done", "phase": "p2", "extra": {"note": "moved"}, "updated_at": T30}
    assert upsert(store_conn, changed) is True
    expected = {**changed, "priority": 0, "holder": "alice", "epoch": 1, "expires_at": T30}
    assert read(store_conn, initiative="i1") == [expected]


def test_a_repeat_with_only_a_new_updated_at_keeps_the_stored_updated_at(store_conn):
    upsert(store_conn, ROW)
    assert upsert(store_conn, {**ROW, "updated_at": T30}) is False
    assert read(store_conn, initiative="i1")[0]["updated_at"] == T0


def test_the_same_holder_reclaiming_before_expiry_bumps_epoch_and_extends_expires_at(store_conn):
    upsert(store_conn, ROW)
    claim(store_conn, "i1", "t1", "alice", 30, T0)
    assert claim(store_conn, "i1", "t1", "alice", 60, T30) == {"holder": "alice", "epoch": 2, "expires_at": T90}


def test_claim_on_a_free_row_succeeds_and_a_second_holder_is_refused_before_expiry(store_conn):
    upsert(store_conn, ROW)
    won = claim(store_conn, "i1", "t1", "alice", 30, T0)
    assert won == {"holder": "alice", "epoch": 1, "expires_at": T30}
    refused = claim(store_conn, "i1", "t1", "bob", 30, T0)
    assert refused is None
    assert _claim_cols(store_conn, "i1", "t1") == {"holder": "alice", "epoch": 1, "expires_at": T30}


def test_a_claim_past_its_expires_at_can_be_taken_by_a_new_holder(store_conn):
    upsert(store_conn, ROW)
    claim(store_conn, "i1", "t1", "alice", 30, T0)
    taken = claim(store_conn, "i1", "t1", "bob", 30, T90)
    assert taken == {"holder": "bob", "epoch": 2, "expires_at": "2026-09-27T00:02:00Z"}


def test_release_with_a_non_matching_holder_leaves_holder_and_expires_at_unchanged(store_conn):
    upsert(store_conn, ROW)
    claim(store_conn, "i1", "t1", "alice", 30, T0)
    assert release(store_conn, "i1", "t1", "bob") is False
    assert _claim_cols(store_conn, "i1", "t1") == {"holder": "alice", "epoch": 1, "expires_at": T30}


def test_release_with_the_matching_holder_clears_the_claim(store_conn):
    upsert(store_conn, ROW)
    claim(store_conn, "i1", "t1", "alice", 30, T0)
    assert release(store_conn, "i1", "t1", "alice") is True
    assert _claim_cols(store_conn, "i1", "t1") == {"holder": None, "epoch": 1, "expires_at": None}


def test_an_upsert_without_a_stamp_is_written_with_the_write_time_and_this_module_as_author(store_conn):
    bare = {k: v for k, v in ROW.items() if k not in ("updated_at", "updated_by")}
    assert upsert(store_conn, bare)
    (stored,) = read(store_conn)
    assert stored["updated_at"] and stored["updated_by"] == "store_queue"


def test_a_row_upserted_with_no_priority_reads_back_priority_0(store_conn):
    upsert(store_conn, ROW)
    assert read(store_conn)[0]["priority"] == 0


def test_a_row_upserted_with_priority_5_reads_back_5(store_conn):
    upsert(store_conn, {**ROW, "priority": 5})
    assert read(store_conn)[0]["priority"] == 5


def test_a_second_upsert_with_priority_5_leaves_one_row_and_an_unchanged_updated_at(store_conn):
    upsert(store_conn, {**ROW, "priority": 5})
    assert upsert(store_conn, {**ROW, "priority": 5, "updated_at": T30}) is False
    (stored,) = read(store_conn)
    assert (stored["priority"], stored["updated_at"]) == (5, T0)


def test_a_later_upsert_with_priority_7_reads_back_7(store_conn):
    upsert(store_conn, {**ROW, "priority": 5})
    assert upsert(store_conn, {**ROW, "priority": 7, "updated_at": T30}) is True
    (stored,) = read(store_conn)
    assert (stored["priority"], stored["updated_at"]) == (7, T30)


def test_an_upsert_that_omits_priority_on_a_row_with_priority_5_still_reads_back_5(store_conn):
    upsert(store_conn, {**ROW, "priority": 5})
    assert upsert(store_conn, {**ROW, "state": "done", "updated_at": T30}) is True
    (stored,) = read(store_conn)
    assert (stored["priority"], stored["state"]) == (5, "done")


def test_a_state_only_upsert_keeps_the_stored_kind_title_surfaces_body_and_extra(store_conn):
    upsert(store_conn, ROW)
    partial = {"initiative": "i1", "task_id": "t1", "phase": "p1", "state": "done", "needs": ["t0"],
               "updated_at": T30, "updated_by": "epic-driver:r1"}
    assert upsert(store_conn, partial) is True
    (stored,) = read(store_conn)
    assert {k: stored[k] for k in ("kind", "title", "surfaces", "body", "extra", "state")} == {
        "kind": "task", "title": "do the thing", "surfaces": {"files": ["a.py", "b.py"]},
        "body": "some body text", "extra": {"note": "extra data", "n": 2}, "state": "done"}


def test_an_explicit_none_still_clears_a_stored_column(store_conn):
    upsert(store_conn, ROW)
    upsert(store_conn, {**ROW, "extra": None, "updated_at": T30})
    (stored,) = read(store_conn)
    assert stored["extra"] is None


@pytest.mark.parametrize("bad", ["5", True, 1.5])
def test_a_non_integer_priority_is_refused_and_writes_nothing(store_conn, bad):
    with pytest.raises(ValueError, match="priority must be an integer"):
        upsert(store_conn, {**ROW, "priority": bad})
    assert read(store_conn) == []


def test_the_upsert_verb_exits_2_on_a_non_integer_priority_and_writes_nothing(store_url, store_conn):
    assert main([store_url, "upsert", '{"initiative": "i1", "task_id": "t1", "priority": "x"}']) == 2
    assert read(store_conn) == []
