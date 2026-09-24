"""A ticket's own `verify:` commands run in the harness and land as evidence rows."""

from __future__ import annotations

from test_epic_driver import TASK_IDS, Runner, cart, drive, initiative, new_file_patch, repo  # noqa: F401

from harness.checks import checks_evidence


def _rows(result: dict, task: str) -> dict[str, str]:
    draft = next(p for p in result["proposals"] if p["kind"] == "draft_pr_create" and p["target"] == task)
    return {row["check"]: row["output"] for row in draft["evidence"]}


def _work(verify: list[str] | None) -> dict:
    work = initiative(two_phases=False)
    if verify is not None:
        next(i for i in work["items"] if i["id"] == "t1-probe")["verify"] = verify
    return work


def test_verify_commands_become_verify_rows_and_a_failure_is_not_a_quarantine(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=_work(["echo one", "false"]))
    rows = _rows(result, "t1-probe")
    assert rows["verify:1"].startswith("pass") and "one" in rows["verify:1"]
    assert rows["verify:2"].startswith("FAIL")
    assert not any(q["id"] == "t1-probe" for q in result["quarantined"])


def test_the_harness_hands_each_tasks_verify_list_to_a_runner_that_can_use_it(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    runner.verify_by_task = {}
    drive(repo, cart, tmp_path, runner=runner, work=_work(["echo one", "false"]))
    assert runner.verify_by_task["t1-probe"] == ["echo one", "false"]


def test_a_runner_without_the_attribute_is_left_alone(repo, cart, tmp_path) -> None:  # noqa: F811
    _, runner = drive(repo, cart, tmp_path, work=_work(["echo one"]))
    assert not hasattr(runner, "verify_by_task")


def test_a_task_without_verify_has_no_verify_rows(repo, cart, tmp_path) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=_work(None))
    assert not [name for name in _rows(result, "t1-probe") if name.startswith("verify:")]


def test_checks_evidence_prefix_defaults_to_checks_and_a_tail_is_opt_in() -> None:
    passed = [{"name": "a", "cmd": "echo hi", "passed": True, "exit_code": 0, "output_tail": "hi\n"}]
    assert checks_evidence(passed)[0]["check"] == "checks:a"
    assert "hi" not in checks_evidence(passed)[0]["output"]
    row = checks_evidence(passed, prefix="verify", always_tail=True)[0]
    assert row["check"] == "verify:a" and "cmd: echo hi" in row["output"] and "hi" in row["output"]
