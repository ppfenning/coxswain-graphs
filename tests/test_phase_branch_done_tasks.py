"""A phase-branch commit whose task is done in the work store counts as landed.

Landing can amend a task's patch (a README tweak, say), so `git cherry` still
shows its commit as `+` against main. The work store is the record of what
landed; the commit subject names the task.
"""

from __future__ import annotations

from test_epic_driver import cart, drive, git, initiative, is_ancestor, repo  # noqa: F401

from harness.epic import unlanded

BRANCH = "epic/demo-initiative/p1-foundations"


def test_a_plus_task_commit_whose_task_is_done_is_dropped() -> None:
    assert unlanded(["+ abc123 epic r-1: t1"], {"t1"}) == []


def test_a_plus_task_commit_whose_task_is_not_done_is_returned() -> None:
    assert unlanded(["+ abc123 epic r-1: t1"], {"t2"}) == ["epic r-1: t1"]


def test_a_minus_line_is_never_returned() -> None:
    assert unlanded(["- abc123 epic r-1: t1", "- def456 something else"], set()) == []


def test_a_merge_commit_is_dropped_whether_or_not_its_task_is_done() -> None:
    assert unlanded(["+ abc123 epic r-1: merge t1 into p1"], set()) == []


def test_a_subject_in_any_other_form_is_returned() -> None:
    lines = ["+ a1 fix the thing", "+ b2 epic r-1 t1", "+ c3 epic r-1: trim p1", "+ d4 t1: epic r-1"]
    assert unlanded(lines, {"t1"}) == ["fix the thing", "epic r-1 t1", "epic r-1: trim p1", "t1: epic r-1"]


def test_the_task_is_what_follows_the_first_colon_space() -> None:
    assert unlanded(["+ a1 epic r-1: t1: extra"], {"t1"}) == ["epic r-1: t1: extra"]


def test_no_lines_is_no_subjects() -> None:
    assert unlanded([], {"t1"}) == []


def _branch_whose_t1_main_holds_as_a_different_patch(repo, cart, tmp_path) -> str:  # noqa: F811
    """Run r-1 to build t1 on the phase branch, then land t1 on main with a README amend beside it."""
    work = initiative(two_phases=False)
    work["items"] = work["items"][:1]
    first, _ = drive(repo, cart, tmp_path, work=work, run_id="r-1")
    assert first["phases"][0]["status"] == "complete"

    (repo / "t1-probe.txt").write_text("ok\n", encoding="utf-8")
    (repo / "README.md").write_text("# demo\namended at land\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "land t1, README amended", cwd=repo)

    assert "+ " in git("cherry", "-v", "main", BRANCH, cwd=repo)
    return git("rev-parse", BRANCH, cwd=repo)


def test_a_done_task_whose_landed_patch_differs_is_recreated_and_builds(repo, cart, tmp_path) -> None:  # noqa: F811
    before = _branch_whose_t1_main_holds_as_a_different_patch(repo, cart, tmp_path)

    work = initiative(two_phases=False, done=("t1-probe",))
    result, runner = drive(repo, cart, tmp_path, work=work, run_id="r-2")

    assert result["phases"][0]["status"] == "complete"
    assert "recreated" in result["phases"][0]
    assert is_ancestor(repo, "main", BRANCH)
    assert not is_ancestor(repo, before, BRANCH)
    assert any(c["role"] == "build" and "t2-bench" in c["prompt"] for c in runner.calls)
    assert [p for p in result["proposals"] if p["kind"] == "stack_rebase"] == []


def test_a_task_not_done_still_blocks_the_phase(repo, cart, tmp_path) -> None:  # noqa: F811
    before = _branch_whose_t1_main_holds_as_a_different_patch(repo, cart, tmp_path)

    result, runner = drive(repo, cart, tmp_path, work=initiative(two_phases=False), run_id="r-2")

    assert result["phases"][0]["status"] == "blocked"
    assert not any(c["role"] == "build" for c in runner.calls)
    assert git("rev-parse", BRANCH, cwd=repo) == before
    assert [p["target"] for p in result["proposals"] if p["kind"] == "stack_rebase"] == [BRANCH]
