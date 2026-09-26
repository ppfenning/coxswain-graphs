import logging
from pathlib import Path

import pytest

import harness.store_read as read
from harness.epic import EXIT_PAUSED, _Ctx, _quarantine_task, _save_result
from harness.resume import load_result
from harness.store_write import Store, task_cause
from runner.protocol import LimitStop
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    CHUNK_BAD,
    CHUNK_OK,
    TASK_IDS,
    RefusedRunner,
    Runner,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)

REFUSED = {
    "ticket": "t1",
    "fix_loop": {"stopped": "no change"},
    "arbitration": {"verdict": "revise", "reasoning": "the patch ignores the ticket"},
    "adversary": {"objections": [{"claim": "wrong file"}]},
}
ANSWER = {"cause": "review", "one_line_why": "the verdict was the cause"}
BANNER = "You've hit your session limit · resets 10:50am (America/New_York)"


class StubRunner:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ANSWER


class Classifying:
    """Mixin for the driver's scripted runners: answers `cause_classify`, or raises what `on_classify` holds."""

    on_classify: dict | Exception = ANSWER

    def run(self, *, role, **kwargs):
        if role == "cause_classify":
            with self.lock:
                self.calls.append({"role": role, "prompt": kwargs["prompt"]})
            if isinstance(self.on_classify, Exception):
                raise self.on_classify
            return self.on_classify
        return super().run(role=role, **kwargs)


class DriverRunner(Classifying, Runner):
    pass


class RefusingRunner(Classifying, RefusedRunner):
    pass


def patches() -> dict[str, str]:
    return {t: new_file_patch(f"{t}.txt") for t in TASK_IDS}


def ctx(tmp_path: Path, store, runner) -> _Ctx:
    return _Ctx(
        repo=tmp_path,
        cartridge={},
        runner=runner,
        specs={},
        run_id="r1",
        date="2026-09-25",
        max_parallel=1,
        ledger_path=tmp_path / "ledger",
        provider_profile="p",
        runs_dir=tmp_path / "runs",
        worktree_root=tmp_path / "wt",
        assume=None,
        fix_attempts=None,
        initiative_id="i",
        default_ref="main",
        store=store,
    )


def quarantine(c: _Ctx, *, kind: str, reason: str, result=None, by_id=None) -> dict:
    return _quarantine_task(c, by_id or {}, phase="p1", task="t1", reason=reason, kind=kind, result=result)


def stored(conn, task: str = "t1", run: str = "r1", phase: str = "p1") -> tuple[tuple, tuple]:
    attempt = conn.query_all(f"SELECT cause, cause_why FROM attempts WHERE task_id = '{task}'", ())
    assert len(attempt) == 1
    return tuple(attempt[0]), task_cause(read.task_record(conn, run, phase, task))


def classifier_calls(runner) -> list[dict]:
    return [call for call in runner.calls if call["role"] == "cause_classify"]


def test_a_deterministic_kind_never_invokes_the_runner(tmp_path, store_conn):
    runner = StubRunner()
    quarantine(ctx(tmp_path, Store(store_conn), runner), kind="infra", reason="apply arm raised")
    assert runner.calls == []
    attempt, record = stored(store_conn)
    assert attempt[0] == "harness"
    assert record == attempt


def test_an_unmatched_kind_invokes_the_runner_once_and_stores_its_answer_on_file_and_row(tmp_path, store_conn):
    runner = StubRunner()
    c = ctx(tmp_path, Store(store_conn), runner)
    _save_result(c, REFUSED, phase="p1", task="t1")
    entry = quarantine(c, kind="refused", reason="the fix loop stopped", result=REFUSED)
    assert entry["kind"] == "refused"
    assert [call["role"] for call in runner.calls] == ["cause_classify"]
    assert "the patch ignores the ticket" in runner.calls[0]["prompt"]
    assert "wrong file" in runner.calls[0]["prompt"]
    attempt, record = stored(store_conn)
    assert attempt == ("review", "the verdict was the cause")
    assert record == attempt
    on_file = load_result(c.runs_dir, "r1", "p1", "t1")
    assert task_cause(on_file) == attempt
    assert read.task_record(store_conn, "r1", "p1", "t1") == on_file


def test_an_unmatched_kind_with_no_arbitration_sends_the_quarantine_reason_to_the_runner(tmp_path, store_conn):
    runner = StubRunner()
    c = ctx(tmp_path, Store(store_conn), runner)
    quarantine(c, kind="unverified", reason="validate_chunk unsatisfied: the probe reads nothing")
    assert len(runner.calls) == 1
    assert "validate_chunk unsatisfied: the probe reads nothing" in runner.calls[0]["prompt"]
    attempt, record = stored(store_conn)
    assert attempt == ("review", "the verdict was the cause")
    assert record == attempt
    on_file = load_result(c.runs_dir, "r1", "p1", "t1")
    assert on_file == {"ticket": "t1", "initiative": "i", "phase": "p1", "cause": "review", "cause_why": attempt[1]}
    assert read.task_record(store_conn, "r1", "p1", "t1") == on_file


def test_a_failing_classifier_records_unknown_and_still_quarantines(tmp_path, store_conn):
    c = ctx(tmp_path, Store(store_conn), StubRunner(error=RuntimeError("boom")))
    entry = quarantine(c, kind="refused", reason="the fix loop stopped", result=REFUSED)
    assert entry["reason"] == "the fix loop stopped"
    attempt, record = stored(store_conn)
    assert attempt[0] == "unknown"
    assert record == attempt


def test_the_session_limit_propagates_before_anything_is_written(tmp_path, store_conn, monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr("harness.epic.record_attempt", lambda *a, **k: written.append(k))
    c = ctx(tmp_path, Store(store_conn), StubRunner(error=LimitStop(detail=BANNER)))
    with pytest.raises(LimitStop):
        quarantine(c, kind="refused", reason="the fix loop stopped", result=REFUSED, by_id={"t1": {"path": "t1.md"}})
    assert written == []
    assert c.store.total_rows("attempts") == 0
    assert read.task_record(store_conn, "r1", "p1", "t1") is None
    assert load_result(c.runs_dir, "r1", "p1", "t1") is None


def test_a_classifier_session_limit_pauses_the_run_with_no_attempt_or_cause(repo, cart, tmp_path, store_conn):  # noqa: F811
    store = Store(store_conn)
    runner = RefusingRunner(patches(), refused="t1-probe")
    runner.on_classify = LimitStop(detail=BANNER)
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), store=store)
    assert len(classifier_calls(runner)) == 1
    assert result["quarantined"] == []
    assert result["paused_until"] == "10:50am (America/New_York)"
    assert result["exit_code"] == EXIT_PAUSED
    assert store.total_rows("attempts") == 0


def test_a_refused_quarantine_in_a_run_classifies_from_the_loop_s_reason(repo, cart, tmp_path, store_conn):  # noqa: F811
    store = Store(store_conn)
    runner = RefusingRunner(patches(), refused="t1-probe")
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), store=store)
    assert [q["kind"] for q in result["quarantined"]] == ["refused"]
    assert len(classifier_calls(runner)) == 1
    assert "no_progress" in classifier_calls(runner)[0]["prompt"]
    attempt, record = stored(store_conn, "t1-probe", "epic-1", "p1-foundations")
    assert attempt == ("review", "the verdict was the cause")
    assert record == attempt


def test_a_run_where_every_task_lands_invokes_no_classifier_and_stores_no_cause(repo, cart, tmp_path, store_conn):  # noqa: F811
    store = Store(store_conn)
    runner = DriverRunner(patches())
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), store=store)
    assert result["quarantined"] == []
    assert classifier_calls(runner) == []
    assert store.total_rows("attempts") == 0
    records = read.task_records(store_conn, "epic-1")
    assert records and all(task_cause(r) == (None, None) for r in records.values())


def test_a_chunk_validator_quarantine_sends_its_gaps_to_the_classifier(repo, cart, tmp_path, store_conn):  # noqa: F811
    store = Store(store_conn)
    runner = DriverRunner(patches(), chunk={"t1-probe": CHUNK_BAD})
    result, _ = drive(repo, cart, tmp_path, runner=runner, work=initiative(two_phases=False), store=store)
    assert [q["id"] for q in result["quarantined"]] == ["t1-probe"]
    assert len(classifier_calls(runner)) == 1
    assert "the probe reads nothing" in classifier_calls(runner)[0]["prompt"]
    attempt, record = stored(store_conn, "t1-probe", "epic-1", "p1-foundations")
    assert attempt == ("review", "the verdict was the cause")
    assert record == attempt


def test_a_resumed_task_that_lands_does_not_inherit_the_earlier_quarantine_cause(repo, cart, tmp_path, store_conn):  # noqa: F811
    store = Store(store_conn)
    first = DriverRunner(patches(), chunk={"t1-probe": CHUNK_BAD})
    work = initiative(two_phases=False)
    work["items"] = work["items"][:1]  # t1-probe alone: the other task's patch would not re-apply on the kept branch
    drive(repo, cart, tmp_path, runner=first, work=work, store=store)
    earlier = load_result(tmp_path / "runs", "epic-1", "p1-foundations", "t1-probe")
    assert task_cause(earlier)[0] == "review"

    second = DriverRunner(patches(), chunk={"t1-probe": CHUNK_OK})
    result, _ = drive(
        repo, cart, tmp_path, runner=second, work=work, store=store, run_id="epic-2", resume_from="epic-1"
    )
    assert "t1-probe" in result["phases"][0]["reused_tasks"]
    assert result["quarantined"] == []
    assert classifier_calls(second) == []
    now = load_result(tmp_path / "runs", "epic-2", "p1-foundations", "t1-probe")
    assert task_cause(now) == (None, None)
    assert task_cause(read.task_record(store_conn, "epic-2", "p1-foundations", "t1-probe")) == (None, None)
    assert task_cause(read.task_record(store_conn, "epic-1", "p1-foundations", "t1-probe"))[0] == "review"


def test_a_failed_task_record_mirror_keeps_the_attempt_and_the_file_and_warns(
    tmp_path, store_conn, monkeypatch, caplog
):
    def down(self, *args, **kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(Store, "record_task_record", down)
    c = ctx(tmp_path, Store(store_conn), StubRunner())
    with caplog.at_level(logging.WARNING, logger="harness.epic"):
        entry = quarantine(c, kind="refused", reason="the fix loop stopped", result=REFUSED)
    assert entry["kind"] == "refused"
    rows = store_conn.query_all("SELECT cause FROM attempts WHERE task_id = 't1'", ())
    assert [tuple(r) for r in rows] == [("review",)]
    assert task_cause(load_result(c.runs_dir, "r1", "p1", "t1")) == ("review", "the verdict was the cause")
    assert any("store is down" in r.getMessage() for r in caplog.records if r.name == "harness.epic")
