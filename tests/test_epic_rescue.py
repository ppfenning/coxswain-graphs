"""rescue_task: the harness's checks on a kept patch, one review round, then a gated approval or rescue_failed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from core import workstore

from harness.epic import _Ctx, rescue_task, with_stored_rescues
from harness.resume import load_result, save_result
from harness.store_migrate import open_store
from harness.store_write import Store
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    PROFILE,
    REVISE,
    Runner,
    cart,
    git,
    new_file_patch,
    repo,
)

PHASE = "p1-foundations"
TASK = "t1-probe"
BODY = "read the vendor schema"
SHA = workstore.body_sha(BODY)
HARNESS = {"run": "epic-prior", "phase": PHASE, "kind": "infra", "cause": "harness", "body_sha": SHA,
           "ts": "2026-09-01T00:00:00+00:00"}  # fmt: skip


def _setup(repo: Path, cart: dict, tmp_path: Path, patch: str, attempts: list[dict] | None = None, *, assume="a"):  # noqa: F811
    """A ctx on the fixture cartridge unchanged, and a quarantined item whose last run kept `patch`."""
    git("branch", f"epic/demo-initiative/{PHASE}", "main", cwd=repo)
    path = tmp_path / "wi" / PHASE / f"{TASK}.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"---\nid: {TASK}\nphase: {PHASE}\nstate: ready\nneeds: []\nsurfaces: []\ntitle: schema probe\n---\n\n{BODY}\n"
    )
    save_result({"ticket": TASK, "build": {"patch": patch}}, runs_dir=tmp_path / "runs", run_id="epic-prior", phase=PHASE, task=TASK)
    store = Store(open_store("sqlite:///:memory:", datetime.now(UTC).isoformat()))
    ctx = _Ctx(
        repo=repo, cartridge=cart, runner=None, specs={}, run_id="rescue-1", date="2026-09-26",
        max_parallel=1, ledger_path=tmp_path / "ledger.jsonl", provider_profile=PROFILE, runs_dir=tmp_path / "runs",
        worktree_root=tmp_path / "worktrees", assume=assume, fix_attempts=None, initiative_id="demo-initiative",
        default_ref="main", store=store,
    )  # fmt: skip
    item = {**workstore.read_item(path), "attempts": attempts if attempts is not None else [HARNESS]}
    return ctx, item, path


def _rescue(ctx: _Ctx, item: dict, runner: Runner) -> dict:
    """One rescue through `runner`, which must never be asked to plan or build."""
    result = rescue_task(replace(ctx, runner=runner), item, phase=PHASE)
    assert not {call["role"] for call in runner.calls} & {"plan", "build"}
    return result


def _one(ctx: _Ctx, sql: str) -> tuple | None:
    return ctx.store.conn.query_one(sql, ())


def test_passing_checks_and_approving_reviewers_approve_the_task_through_the_gate(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({})
    ctx, item, path = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt"))

    assert _rescue(ctx, item, runner)["status"] == "approved"
    assert workstore.read_item(path)["state"] == "approved"
    saved = load_result(ctx.runs_dir, "rescue-1", PHASE, TASK)
    assert [row["source"] for row in saved["evidence"]] == ["harness_verify"]
    assert saved["verdict"] == "approve"
    # The fixture's state_move arm is the work_state_arm model role; the move still made no call to it.
    assert [call["role"] for call in runner.calls] == ["review_charter"]
    assert _one(ctx, "SELECT kind, decision, applied FROM gate_decisions") == ("state_move", "approved", 1)
    assert _one(ctx, "SELECT state FROM tasks WHERE run_id = 'rescue-1'") == ("approved",)


def test_a_gate_that_refuses_the_move_leaves_the_task_where_it_was(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({})
    ctx, item, path = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt"), assume="r")

    assert _rescue(ctx, item, runner) == {"status": "move_refused", "why": "the gate refused the move"}
    assert workstore.read_item(path)["state"] == "ready"
    assert _one(ctx, "SELECT decision, applied FROM gate_decisions") == ("refused", 0)
    assert _one(ctx, "SELECT state FROM tasks WHERE run_id = 'rescue-1'") == ("quarantined",)


def test_a_failing_check_records_a_code_rescue_failed_and_asks_no_reviewer(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({})
    ctx, item, path = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt", "bad"))

    result = _rescue(ctx, item, runner)

    assert (result["status"], result["cause"]) == ("rescue_failed", "code")
    assert _one(ctx, "SELECT kind, cause, cause_why FROM attempts") == ("rescue_failed", "code", "rule: rescue checks failed")
    assert _one(ctx, "SELECT state FROM tasks WHERE run_id = 'rescue-1'") == ("quarantined",)
    assert workstore.read_item(path)["state"] == "ready"
    assert runner.calls == []


def test_reviewers_who_revise_record_a_review_rescue_failed(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({}, review={TASK: REVISE})
    ctx, item, path = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt"))

    result = _rescue(ctx, item, runner)

    assert (result["status"], result["cause"]) == ("rescue_failed", "review")
    assert result["reason"] == "rescue review revised: the claim and the code disagree"
    assert _one(ctx, "SELECT kind, cause, cause_why FROM attempts") == ("rescue_failed", "review", "rule: rescue review revised")
    assert workstore.read_item(path)["state"] == "ready"
    assert _one(ctx, "SELECT COUNT(*) FROM gate_decisions") == (0,)


def test_a_last_attempt_with_cause_code_is_not_eligible_and_asks_nobody(repo, cart, tmp_path) -> None:  # noqa: F811
    runner = Runner({})
    ctx, item, _ = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt"), [{**HARNESS, "cause": "code"}])

    assert _rescue(ctx, item, runner) == {"status": "not_eligible", "why": "last cause is code, not harness"}
    assert runner.calls == []
    assert _one(ctx, "SELECT COUNT(*) FROM attempts") == (0,)


def test_a_second_rescue_on_the_same_body_is_not_eligible_from_the_first_ones_own_record(repo, cart, tmp_path) -> None:  # noqa: F811
    ctx, item, path = _setup(repo, cart, tmp_path, new_file_patch("t1-probe.txt", "bad"))
    assert _rescue(ctx, item, Runner({}))["status"] == "rescue_failed"
    # core's ATTEMPT_KINDS has no rescue_failed: the file keeps no attempt, the store row is the record.
    assert workstore.read_item(path)["attempts"] == []
    # A later harness attempt on the same body, so the refusal is the rescue rule and not the last cause.
    later = {**HARNESS, "ts": "2999-01-01T00:00:00+00:00"}
    runner = Runner({})

    assert _rescue(ctx, {**workstore.read_item(path), "attempts": [HARNESS, later]}, runner) == {
        "status": "not_eligible",
        "why": "a rescue already failed on this ticket version",
    }
    assert runner.calls == []


def test_a_stored_rescue_older_than_the_current_body_is_stamped_as_an_older_body() -> None:
    item = {"body": BODY, "attempts": [HARNESS]}
    row = {"run_id": "r", "phase_id": PHASE, "kind": "rescue_failed", "reason": "x", "ts": "2026-08-01", "cause": "code"}

    assert with_stored_rescues(item, [row])["attempts"][0]["body_sha"] == "before the current body"
