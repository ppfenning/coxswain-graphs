"""A task is moved to approved only when its work is on a branch that survives the run."""

from __future__ import annotations

import harness.epic as epic
import harness.store_read as read
import harness.store_work_state as sws
from harness.epic import kept_work_refusal
from harness.resume import load_result
from harness.store_write import Store
from tests.test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- cart and repo are fixtures

TASK = "t1-probe"
DRAFT = f"epic/demo-initiative/p1-foundations--{TASK}"


# ── the pure function ───────────────────────────────────────────────────────


def test_a_merge_that_succeeded_and_left_the_phase_branch_ahead_is_kept():
    assert kept_work_refusal(landed=True, merged=True, escalated=False, ahead=True) is None


def test_an_escalated_draft_that_was_created_and_is_ahead_is_kept():
    assert kept_work_refusal(landed=True, merged=None, escalated=True, ahead=True) is None


def test_a_refused_merge_is_not_kept_and_the_reason_names_the_merge():
    assert kept_work_refusal(landed=True, merged=False, escalated=False, ahead=True) == (
        "the merge into the phase branch was refused or conflicted"
    )


def test_an_empty_merge_is_not_kept_and_the_reason_names_the_phase_branch():
    assert kept_work_refusal(landed=True, merged=True, escalated=False, ahead=False) == (
        "the merge added nothing to the phase branch"
    )


def test_a_draft_that_never_landed_is_not_kept_and_the_reason_names_the_draft():
    assert kept_work_refusal(landed=False, merged=None, escalated=False, ahead=True) == "the draft did not land"


# ── the driver ──────────────────────────────────────────────────────────────


def _drive_store(repo, cart, tmp_path, store_conn, monkeypatch):  # noqa: F811
    """Run one phase under the store work state; report every approved move that reached the store or the arm."""
    moves: list = []
    real_set = sws.set_state
    real_apply = epic.auto_apply
    monkeypatch.setattr(sws, "set_state", lambda *a, **k: moves.append(("store", a[3], a[2])) or real_set(*a, **k))

    def apply(proposal, **kw):
        if proposal.get("kind") == "state_move":
            moves.append(("arm", proposal.get("target")))
        return real_apply(proposal, **kw)

    monkeypatch.setattr(epic, "auto_apply", apply)
    result, _ = drive(
        repo, cart, tmp_path, work=initiative(two_phases=False), store=Store(store_conn), work_state="store"
    )
    states = {r["task_id"]: r["state"] for r in read.work_items(store_conn, "demo-initiative")}
    return result, states, moves


def test_a_task_with_a_kept_branch_is_approved_and_stays_approved(repo, cart, tmp_path, store_conn, monkeypatch):  # noqa: F811
    result, states, moves = _drive_store(repo, cart, tmp_path, store_conn, monkeypatch)
    task = next(t for t in result["tasks"] if t["id"] == TASK)
    assert (task["state"], task["merged"], task["outcome"]) == ("approved", True, "landed")
    assert states[TASK] == "approved"
    assert ("store", "approved", TASK) in moves
    assert [q for q in result["quarantined"] if q["id"] == TASK] == []


def test_a_task_whose_draft_never_landed_ends_quarantined_with_no_move(repo, cart, tmp_path, store_conn, monkeypatch):  # noqa: F811
    real_git = epic._git
    monkeypatch.setattr(
        epic, "_git", lambda *a, **k: (False, "no draft") if "branch" in a and DRAFT in a else real_git(*a, **k)
    )
    result, states, moves = _drive_store(repo, cart, tmp_path, store_conn, monkeypatch)

    task = next(t for t in result["tasks"] if t["id"] == TASK)
    assert (task["status"], task["merged"]) == ("quarantined", False)
    entry = next(q for q in result["quarantined"] if q["id"] == TASK)
    assert entry["kind"] == "infra"
    assert entry["reason"].startswith("work not on a surviving branch")
    saved = load_result(tmp_path / "runs", "epic-1", "p1-foundations", TASK)
    assert (saved["cause"], saved["cause_why"]) == ("harness", "work not on a surviving branch")
    assert states[TASK] == "quarantined"
    assert [m for m in moves if m == ("arm", TASK) or m[1:] == ("approved", TASK)] == []


def test_a_branch_that_does_not_exist_is_not_ahead(repo):  # noqa: F811
    ctx = type("Ctx", (), {"repo": repo, "default_ref": "main"})()
    assert epic._branch_ahead(ctx, "epic/i/p1") is False
    assert epic._branch_exists(ctx, "epic/i/p1") is False


def test_an_unkept_task_already_quarantined_by_its_merge_is_not_quarantined_twice(monkeypatch):
    ctx = type("Ctx", (), {"cartridge": {}, "epoch": None})()
    monkeypatch.setattr(epic, "_fenced", lambda _ctx: None)
    monkeypatch.setattr(epic, "_wait_if_paused", lambda _ctx: None)
    monkeypatch.setattr(epic, "_unkept_work", lambda *_: "the merge into the phase branch was refused or conflicted")
    prior = {"id": "t1", "phase": "p1", "grain": "task", "reason": "merge conflict: x", "kind": "infra"}
    state = epic._Execution(landed={"t1": True}, merged={"t1": False}, moved={}, quarantined=[prior])
    ok, _ = epic._execute(
        ctx, {"kind": "state_move"}, slot="state_move", subject="t1", phase="p1", state=state, by_id={}
    )
    assert (ok, state.moved, state.quarantined) == (False, {"t1": False}, [prior])
