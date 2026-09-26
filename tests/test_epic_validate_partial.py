"""A phase is validated only when it is finished: no open task outside the run."""

from __future__ import annotations

from harness.epic import open_in_phase
from tests.test_epic_driver import cart, drive, initiative, new_file_patch, repo  # noqa: F401


def test_open_in_phase_names_only_the_open_ids_of_that_phase_outside_the_run() -> None:
    items = [
        {"id": "a", "phase": "p1", "state": "ready"},
        {"id": "b", "phase": "p1", "state": "done"},
        {"id": "c", "phase": "p1", "state": "dropped"},
        {"id": "d", "phase": "p1", "state": "approved"},
        {"id": "e", "phase": "p1", "state": "todo"},
        {"id": "f", "phase": "p2", "state": "ready"},
    ]
    assert open_in_phase(items, "p1", {"a"}) == ["d", "e"]


def _validate_calls(runner) -> list[dict]:
    return [c for c in runner.calls if c["role"] == "validate_phase"]


def test_a_phase_with_an_open_sibling_makes_no_validate_call_and_records_the_skip(repo, cart, tmp_path) -> None:  # noqa: F811
    work = initiative(two_phases=False)
    work["items"][1].update(state="todo", needs=["t9-missing"])
    result, runner = drive(repo, cart, tmp_path, work=work, patches={"t1-probe": new_file_patch("t1-probe.txt")})

    assert _validate_calls(runner) == []
    record = result["phases"][0]
    assert record["phase_verdict"] == {
        "goal_met": False,
        "skipped": "phase not finished: 1 open task(s): t2-bench",
    }
    assert record["status"] == "partial"


def test_a_finished_phase_still_makes_its_validate_call(repo, cart, tmp_path) -> None:  # noqa: F811
    result, runner = drive(repo, cart, tmp_path, work=initiative(two_phases=False))

    assert len(_validate_calls(runner)) == 1
    assert result["phases"][0]["phase_verdict"]["goal_met"] is True
