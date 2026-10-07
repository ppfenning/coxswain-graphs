"""Every epic task quarantine moves the store row, including a failed merge into the phase branch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from core import workstore

import harness.epic as epic
import harness.store_read as read
import harness.store_work_state as sws
from harness.store_write import Store
from tests.test_epic_driver import cart, drive, initiative, repo  # noqa: F401 -- cart and repo are fixtures
from tests.test_epic_store_authority_write import (  # noqa: F401 -- item is a fixture
    APPROVED,
    _ctx,
    _put,
    _row_state,
    item,
)

CONFLICT = "CONFLICT (content): Merge conflict in pyproject.toml"
STALE = "stale epoch: this driver holds epoch 1, lease 'epic' is at epoch 2 or has expired"


def _merge_ctx(store_conn, work_state):
    base = _ctx(store_conn, work_state)
    return type(
        "MergeCtx",
        (),
        {
            "store": base.store, "initiative_id": base.initiative_id, "run_id": base.run_id,
            "work_state": work_state, "epoch": None, "cartridge": {}, "runner": None,
            "draft_branch": lambda self, phase, task: f"draft/{phase}/{task}",
            "phase_worktree": lambda self, phase: f"/nowhere/{phase}",
        },
    )()


def _spy(monkeypatch) -> list:
    calls: list = []
    real = sws.set_state
    monkeypatch.setattr(sws, "set_state", lambda *a, **k: calls.append((a[3], a[2])) or real(*a, **k))
    return calls


def _fake_merge_git(monkeypatch) -> None:
    monkeypatch.setattr(epic, "_git", lambda *a, **k: (False, CONFLICT) if "merge" in a and "--no-ff" in a else (True, ""))


def _conflicting_merge(ctx, monkeypatch, by_id=None):
    """Run the merge slot for t1 with git faked: the merge fails, every other git call succeeds."""
    _fake_merge_git(monkeypatch)
    state = epic._Execution(landed={"t1": True}, merged={}, moved={}, quarantined=[])
    move = {"kind": "merge_stack", "target": "t1"}
    result = epic._execute(ctx, move, slot="merge", subject="t1", phase="p1", state=state, by_id=by_id or {})
    return state, result


def test_the_approve_then_conflicting_merge_sequence_ends_quarantined(store_conn, item, monkeypatch):  # noqa: F811
    ctx = _merge_ctx(store_conn, "store")
    _put(ctx, item, "ready")
    monkeypatch.setattr(epic, "auto_apply", lambda proposal, **_: (Path(item["path"]).write_bytes(APPROVED), (True, "stub"))[1])
    calls = _spy(monkeypatch)
    state = epic._Execution(landed={"t1": True}, merged={}, moved={}, quarantined=[])
    by_id = {"t1": item}
    moved = epic._execute(ctx, {"kind": "state_move", "target": "t1"}, slot="state_move", subject="t1", phase="p1", state=state, by_id=by_id)
    assert moved[0] is True and _row_state(ctx) == "approved"
    _fake_merge_git(monkeypatch)
    ok, detail = epic._execute(ctx, {"kind": "merge_stack", "target": "t1"}, slot="merge", subject="t1", phase="p1", state=state, by_id=by_id)
    assert (ok, detail) == (False, CONFLICT)
    assert state.quarantined == [
        {"id": "t1", "phase": "p1", "grain": "task", "reason": f"merge conflict: {CONFLICT}", "kind": "infra", "patch_kept": True}
    ]
    assert calls == [("approved", "t1"), ("quarantined", "t1")]
    assert _row_state(ctx) == "quarantined"


def test_a_conflicting_merge_of_a_never_approved_row_also_ends_quarantined(store_conn, item, monkeypatch):  # noqa: F811
    ctx = _merge_ctx(store_conn, "store")
    _put(ctx, item, "ready")
    _conflicting_merge(ctx, monkeypatch)
    assert _row_state(ctx) == "quarantined"


def test_a_conflicting_merge_under_the_files_work_state_writes_nothing(store_conn, item, monkeypatch):  # noqa: F811
    ctx = _merge_ctx(store_conn, "files")
    _put(ctx, item, "approved")
    calls = _spy(monkeypatch)
    state, (ok, _) = _conflicting_merge(ctx, monkeypatch)
    assert ok is False and len(state.quarantined) == 1
    assert calls == []
    assert _row_state(ctx) == "approved"


@pytest.mark.parametrize("approved_is_open", [True, False])
def test_the_quarantine_writer_leaves_a_done_row_alone(store_conn, item, monkeypatch, approved_is_open):  # noqa: F811
    ctx = _merge_ctx(store_conn, "store")
    _put(ctx, item, "done")
    calls = _spy(monkeypatch)
    epic._quarantine_row(ctx, "p1", "t1", approved_is_open=approved_is_open)
    _conflicting_merge(ctx, monkeypatch)
    assert calls == []
    assert _row_state(ctx) == "done"


def test_a_stale_leader_writes_no_quarantine_to_the_row(store_conn, item, monkeypatch):  # noqa: F811
    ctx = _merge_ctx(store_conn, "store")
    _put(ctx, item, "ready")
    monkeypatch.setattr(epic, "_fenced", lambda _ctx: STALE)
    calls = _spy(monkeypatch)
    epic._quarantine_row(ctx, "p1", "t1")
    epic._quarantine_row(ctx, "p1", "t1", approved_is_open=True)
    assert calls == []
    assert _row_state(ctx) == "ready"


@pytest.mark.parametrize(
    ("current", "approved_is_open", "expected"),
    [
        ("approved", True, True),
        ("approved", False, False),
        ("done", True, False),
        ("quarantined", True, False),
    ],
)
def test_row_should_quarantine_approved_is_open(current, approved_is_open, expected):
    assert epic.row_should_quarantine("store", current, approved_is_open=approved_is_open) is expected


# ── the plain `quarantined.append` sites in `_run_phase`, driven end to end under the store work state ──


def _drive_store(repo, cart, tmp_path, store_conn, work, **extra):  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=work, store=Store(store_conn), work_state="store", **extra)
    states = {r["task_id"]: r["state"] for r in read.work_items(store_conn, "demo-initiative")}
    return result, states


def _capped_work() -> dict:
    """t1-probe carries two attempts on its current body, so the attempt cap sends it to triage."""
    work = initiative(two_phases=False)
    task = next(i for i in work["items"] if i["id"] == "t1-probe")
    task["attempts"] = [
        {"run": f"epic-prior-{n}", "phase": "p1-foundations", "reason": "check failed: bad output",
         "body_sha": workstore.body_sha(task["body"]), "ts": f"2026-09-0{n}T00:00:00+00:00"}
        for n in (1, 2)
    ]
    return work


def _fake_triage(monkeypatch, triaged: list, failures: list) -> None:
    real = epic.invoke_graphs

    def invoke(invocations, **kw):
        if invocations and all(i.graph == epic.TRIAGE for i in invocations):
            return triaged, [], failures
        return real(invocations, **kw)

    monkeypatch.setattr(epic, "invoke_graphs", invoke)


def test_the_over_budget_refusal_quarantines_the_row(repo, cart, tmp_path, store_conn):  # noqa: F811
    cart["policy"]["build_budget_usd_max"] = 3.0
    work = initiative(two_phases=False)
    next(i for i in work["items"] if i["id"] == "t1-probe")["budget_usd"] = 5.0
    result, states = _drive_store(repo, cart, tmp_path, store_conn, work)
    assert any(q["id"] == "t1-probe" and q["kind"] == "no_work" for q in result["quarantined"])
    assert states["t1-probe"] == "quarantined"


@pytest.mark.parametrize(
    ("triaged", "failures", "reason_part"),
    [
        ([], ["t1-probe: triage node raised"], "triage itself failed"),
        ([{"run_id": "epic-1:p1-foundations:t1-probe", "escalated": True, "class": "c", "diagnosis": "d"}], [], "triage repeats a prior"),
        ([{"run_id": "epic-1:p1-foundations:t1-probe", "class": "c", "emit": {}}], [], "no direct write"),
    ],
    ids=["triage-failed", "triage-escalated", "triage-no-proposal"],
)
def test_each_attempt_cap_refusal_quarantines_the_row(repo, cart, tmp_path, store_conn, monkeypatch, triaged, failures, reason_part):  # noqa: F811
    _fake_triage(monkeypatch, triaged, failures)
    result, states = _drive_store(repo, cart, tmp_path, store_conn, _capped_work())
    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert reason_part in entry["reason"] and entry["kind"] == "no_work"
    assert states["t1-probe"] == "quarantined"


def test_a_harness_fault_quarantines_the_row(repo, cart, tmp_path, store_conn):  # noqa: F811
    cart["landing_areas"]["checks"] = [{"name": "ghost", "cmd": f"{sys.executable}-no-such-binary check.py"}]
    result, states = _drive_store(repo, cart, tmp_path, store_conn, initiative(two_phases=False))
    entry = next(q for q in result["quarantined"] if q["id"] == "t1-probe")
    assert entry["reason"].startswith("harness fault") and entry["kind"] == "no_work"
    assert states["t1-probe"] == "quarantined"
