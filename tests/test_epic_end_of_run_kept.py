"""At the end of a run, an approved task whose work is no longer on a branch is quarantined."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import harness.epic as epic
import harness.store_read as read
from harness.epic import BranchFacts, end_of_run_refusal
from harness.resume import load_result
from harness.store_write import Store
from runner.protocol import LimitStop
from tests.test_epic_driver import Runner, cart, drive, initiative, new_file_patch, repo  # noqa: F401 -- fixtures

TASK = "t1-probe"
PHASE_BRANCH = "epic/demo-initiative/p1-foundations"
DRAFT = f"{PHASE_BRANCH}--{TASK}"
SIBLING_DRAFT = f"{PHASE_BRANCH}--t2-bench"


def facts(*, exists=True, holds_merge=True, ahead=True, branch="epic/i/p1", draft="epic/i/p1--t1"):
    return BranchFacts(branch=branch, draft=draft, exists=exists, holds_merge=holds_merge, ahead=ahead)


def git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@invalid", "-c", "user.name=t", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# ── the pure function ───────────────────────────────────────────────────────


def test_a_merged_task_whose_phase_branch_holds_its_merge_is_kept():
    assert end_of_run_refusal(facts(), merged=True) is None


def test_a_missing_branch_is_named_in_the_reason():
    assert end_of_run_refusal(facts(exists=False), merged=True) == "branch epic/i/p1 no longer exists"


def test_a_phase_branch_ahead_of_the_default_ref_without_the_task_merge_is_not_kept():
    assert end_of_run_refusal(facts(holds_merge=False), merged=True) == (
        "branch epic/i/p1 no longer contains the merge of epic/i/p1--t1"
    )


def test_a_missing_draft_branch_of_an_unmerged_task_is_named_in_the_reason():
    gone = facts(exists=False, branch="agents/r/t1", draft=None)
    assert end_of_run_refusal(gone, merged=False) == "branch agents/r/t1 no longer exists"


def test_an_unmerged_task_whose_draft_is_not_ahead_of_the_default_ref_is_not_kept():
    assert end_of_run_refusal(facts(ahead=False, branch="agents/r/t1", draft=None), merged=False) == (
        "the draft branch has no commits the default ref lacks"
    )


# ── the driver ──────────────────────────────────────────────────────────────


def _drive_store(repo, cart, tmp_path, store_conn, **kw):  # noqa: F811
    work = kw.pop("work", None) or initiative(two_phases=False)
    result, _ = drive(repo, cart, tmp_path, work=work, store=Store(store_conn), work_state="store", **kw)
    states = {r["task_id"]: r["state"] for r in read.work_items(store_conn, "demo-initiative")}
    return result, states


def _before_audit(monkeypatch, change):
    real_audit = epic._audit_approved_kept

    def hooked(ctx, *args):
        change()
        return real_audit(ctx, *args)

    monkeypatch.setattr(epic, "_audit_approved_kept", hooked)


def test_a_task_whose_phase_branch_still_holds_the_merge_stays_approved(repo, cart, tmp_path, store_conn):  # noqa: F811
    result, states = _drive_store(repo, cart, tmp_path, store_conn)
    task = next(t for t in result["tasks"] if t["id"] == TASK)
    assert (task["state"], task["merged"], task["outcome"]) == ("approved", True, "landed")
    assert states[TASK] == "approved"
    assert [q for q in result["quarantined"] if q["id"] == TASK] == []


def test_a_phase_branch_deleted_after_the_move_ends_quarantined(repo, cart, tmp_path, store_conn, monkeypatch):  # noqa: F811
    _before_audit(monkeypatch, lambda: git(repo, "update-ref", "-d", f"refs/heads/{PHASE_BRANCH}"))
    result, states = _drive_store(repo, cart, tmp_path, store_conn)

    task = next(t for t in result["tasks"] if t["id"] == TASK)
    assert (task["status"], task["state"], task["outcome"]) == ("quarantined", "quarantined", "harness_fault")
    assert task["reason"] == (
        f"work not on a surviving branch at the end of the run: branch {PHASE_BRANCH} no longer exists"
    )
    entry = next(q for q in result["quarantined"] if q["id"] == TASK)
    # `no_work` carries no `patch_kept`: the branch that kept the work is the thing that is gone.
    assert entry == {
        "id": TASK,
        "phase": "p1-foundations",
        "grain": "task",
        "reason": task["reason"],
        "kind": "no_work",
    }
    # The phase's other merged task shared the deleted branch, so it is quarantined for the same reason.
    assert {q["id"] for q in result["quarantined"]} == {TASK, "t2-bench"}
    assert result["totals"]["tasks_quarantined"] == 2
    assert not any(TASK in line for line in result["exit_summary"])
    saved = load_result(tmp_path / "runs", "epic-1", "p1-foundations", TASK)
    assert (saved["cause"], saved["cause_why"]) == ("harness", "work not on a surviving branch")
    assert states[TASK] == "quarantined"


def test_a_phase_branch_recreated_without_the_task_merge_ends_quarantined(
    repo,  # noqa: F811
    cart,  # noqa: F811
    tmp_path,
    store_conn,
    monkeypatch,
):
    # Recreated on the sibling's draft: the branch exists and is ahead of main, but holds only t2-bench's work.
    _before_audit(
        monkeypatch, lambda: git(repo, "update-ref", f"refs/heads/{PHASE_BRANCH}", f"refs/heads/{SIBLING_DRAFT}")
    )
    result, states = _drive_store(repo, cart, tmp_path, store_conn)

    task = next(t for t in result["tasks"] if t["id"] == TASK)
    assert task["status"] == "quarantined"
    assert task["reason"] == (
        f"work not on a surviving branch at the end of the run: branch {PHASE_BRANCH} no longer contains the merge of {DRAFT}"
    )
    sibling = next(t for t in result["tasks"] if t["id"] == "t2-bench")
    assert (sibling["state"], states["t2-bench"]) == ("approved", "approved")
    assert [q["id"] for q in result["quarantined"]] == [TASK]
    assert states[TASK] == "quarantined"


class LimitStopInP2(Runner):
    """The build of the second phase's task stops on the session limit; the first phase finishes."""

    def run(self, *, role, prompt, **kw):
        if role == "build" and "t3-cutover" in prompt:
            raise LimitStop(detail="You've hit your session limit · resets 10:50am (America/New_York)")
        return super().run(role=role, prompt=prompt, **kw)


def test_a_paused_run_still_audits_the_tasks_approved_before_the_pause(repo, cart, tmp_path, store_conn, monkeypatch):  # noqa: F811
    _before_audit(monkeypatch, lambda: git(repo, "update-ref", "-d", f"refs/heads/{PHASE_BRANCH}"))
    runner = LimitStopInP2({t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench", "t3-cutover")})
    result, states = _drive_store(repo, cart, tmp_path, store_conn, work=initiative(), runner=runner)

    assert result["paused_until"] == "10:50am (America/New_York)"
    assert {q["id"] for q in result["quarantined"]} == {TASK, "t2-bench"}
    assert states[TASK] == "quarantined"


# ── the audit on a task that never merged ───────────────────────────────────


def test_an_escalated_task_is_audited_on_its_scratch_branch(repo, monkeypatch):  # noqa: F811
    """A task that never merged keeps its work on `agents/<run>/<task>`: present and ahead stays, missing is quarantined."""
    commit = git(repo, "commit-tree", "HEAD^{tree}", "-p", "main", "-m", "escalated work")
    git(repo, "update-ref", "refs/heads/agents/r/t-kept", commit)
    ctx = SimpleNamespace(
        repo=repo, default_ref="main", run_id="r", scratch_branch=lambda t: f"agents/r/{t}", phase_branch=lambda p: p
    )
    calls: list = []
    monkeypatch.setattr(
        epic, "_quarantine_task", lambda *a, **k: calls.append(k) or {"id": k["task"], "reason": k["reason"]}
    )
    tasks = [
        {"id": "t-kept", "phase": "p1", "state": "approved", "merged": False, "draft": None},
        {"id": "t-gone", "phase": "p1", "state": "approved", "merged": False, "draft": None},
    ]
    quarantined: list = []

    epic._audit_approved_kept(ctx, {}, tasks, quarantined)

    assert [(c["task"], c["kind"], c["approved_is_open"]) for c in calls] == [("t-gone", "no_work", True)]
    assert calls[0]["reason"].endswith("branch agents/r/t-gone no longer exists")
    assert [t["state"] for t in tasks] == ["approved", "quarantined"]
    assert [q["id"] for q in quarantined] == ["t-gone"]
