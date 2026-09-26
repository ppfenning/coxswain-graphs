from harness.work_mirror import item_row, plan_mirror

ITEM = {"id": "t1", "phase": "p1", "state": "done", "needs": ["t0", "tz"]}
STORED = {
    "initiative": "init",
    "task_id": "t1",
    "phase": "p1",
    "state": "done",
    "needs": ["t0", "tz"],
    "updated_at": "2026-09-25T10:00:00Z",
    "updated_by": "cli",
}
TIMES = {"t1": "2026-09-25T09:00:00Z"}


def test_item_row_has_the_assumed_keys_and_plans_nothing_against_itself():
    row = item_row("init", ITEM, "2026-09-25T09:00:00Z", "driver")
    assert list(row) == ["initiative", "task_id", "phase", "state", "needs", "updated_at", "updated_by"]
    assert plan_mirror("init", [ITEM], [row], TIMES, "driver") == ([], [])


def test_a_missing_row_is_upserted():
    upserts, disagreements = plan_mirror("init", [ITEM], [], TIMES, "driver")
    assert upserts == [
        {
            "initiative": "init",
            "task_id": "t1",
            "phase": "p1",
            "state": "done",
            "needs": ["t0", "tz"],
            "updated_at": "2026-09-25T09:00:00Z",
            "updated_by": "driver",
        }
    ]
    assert disagreements == []


def test_an_identical_row_is_not_upserted():
    same_time = {**STORED, "updated_at": TIMES["t1"]}
    assert plan_mirror("init", [ITEM], [same_time], TIMES, "driver") == ([], [])


def test_needs_in_another_order_or_container_is_not_a_difference():
    reordered = {**STORED, "needs": ("tz", "t0"), "updated_at": "2026-09-25T08:00:00Z"}
    assert plan_mirror("init", [ITEM], [reordered], TIMES, "driver") == ([], [])


def test_a_differing_older_row_is_upserted():
    older = {**STORED, "state": "ready", "updated_at": "2026-09-25T08:00:00Z"}
    upserts, disagreements = plan_mirror("init", [ITEM], [older], TIMES, "driver")
    assert [(r["task_id"], r["state"], r["updated_at"]) for r in upserts] == [("t1", "done", "2026-09-25T09:00:00Z")]
    assert disagreements == []


def test_a_newer_differing_row_is_a_disagreement_and_not_upserted():
    newer = {**STORED, "state": "ready", "updated_by": "human"}
    upserts, disagreements = plan_mirror("init", [ITEM], [newer], TIMES, "driver")
    assert upserts == []
    assert disagreements == [
        {
            "initiative": "init",
            "task_id": "t1",
            "file_state": "done",
            "store_state": "ready",
            "store_updated_at": "2026-09-25T10:00:00Z",
            "store_updated_by": "human",
        }
    ]


def test_a_newer_row_with_the_same_state_is_neither():
    assert plan_mirror("init", [ITEM], [STORED], TIMES, "driver") == ([], [])


def test_a_newer_row_with_the_same_state_but_other_needs_is_upserted_and_keeps_its_later_time():
    newer = {**STORED, "phase": "p2", "needs": ["t9"]}
    upserts, disagreements = plan_mirror("init", [ITEM], [newer], TIMES, "driver")
    assert [(r["phase"], r["needs"], r["updated_at"]) for r in upserts] == [("p1", ["t0", "tz"], "2026-09-25T10:00:00Z")]
    assert disagreements == []


def test_a_stored_row_with_no_time_counts_as_older():
    untimed = {**STORED, "state": "ready", "updated_at": None}
    upserts, disagreements = plan_mirror("init", [ITEM], [untimed], TIMES, "driver")
    assert [(r["state"], r["updated_at"]) for r in upserts] == [("done", "2026-09-25T09:00:00Z")]
    assert disagreements == []


def test_a_missing_row_without_a_file_time_is_upserted_with_the_fallback_time():
    upserts, _ = plan_mirror("init", [ITEM], [], {}, "driver", fallback_time="2026-09-25T11:00:00Z")
    assert [(r["task_id"], r["updated_at"]) for r in upserts] == [("t1", "2026-09-25T11:00:00Z")]
    upserts, _ = plan_mirror("init", [ITEM], [], {}, "driver")
    assert [(r["task_id"], r["updated_at"]) for r in upserts] == [("t1", None)]


def test_a_differing_stored_row_without_a_file_time_is_upserted_not_skipped():
    """No file time means no disagreement can be shown, so the default applies: upsert, never a silent skip."""
    differing = {**STORED, "state": "ready", "updated_by": "human"}
    upserts, disagreements = plan_mirror("init", [ITEM], [differing], {}, "driver")
    assert [row["state"] for row in upserts] == [ITEM["state"]]
    assert disagreements == []
