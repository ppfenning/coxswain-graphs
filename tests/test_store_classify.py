import json
from typing import Any

import pytest

from harness import store_classify
from harness.cause_model import cause_evidence
from harness.store_classify import classify_pending, pending
from harness.store_migrate import open_store
from harness.store_write import Store
from runner.protocol import LimitStop

STAMP = "2026-01-01T00:00:00+00:00"
CODE = {"cause": "code", "one_line_why": "the check failed"}


class FakeRunner:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'cox.db'}"


@pytest.fixture
def store(url):
    conn = open_store(url, STAMP)
    yield Store(conn)
    conn.close()


def attempt(store, task, seq=1, cause="unknown", why=None, reason="patch did not apply", ts=STAMP):
    store.record_attempt("r1", task, seq, "p1", "quarantine", reason, ts, cause=cause, cause_why=why)


def causes(store):
    sql = "SELECT task_id, cause, cause_why FROM attempts ORDER BY task_id, seq"
    return [tuple(r) for r in store.conn.query_all(sql)]


def test_a_null_why_unknown_is_classified_and_written(store):
    attempt(store, "t1")
    report = classify_pending(store, FakeRunner(CODE), 50)
    assert causes(store) == [("t1", "code", "the check failed")]
    assert report == {"considered": 1, "classified_code": 1, "still_unknown": 0, "stopped": None}


def test_a_failed_call_why_is_pending_again(store):
    attempt(store, "t1", why="model call failed: RunnerError: x")
    attempt(store, "t2", why="classifier failed: KeyError: y")
    assert [r.task_id for r in pending(store, 50)] == ["t1", "t2"]


def test_a_model_or_human_why_is_never_selected(store):
    attempt(store, "t1", why="the model said so")
    attempt(store, "t2", cause="code", why=None)
    attempt(store, "t3", why="rule: kind quarantine")
    assert pending(store, 50) == []
    assert classify_pending(store, FakeRunner(CODE), 50)["considered"] == 0


def test_limit_bounds_the_rows_oldest_first(store):
    attempt(store, "t1", ts="2026-01-03T00:00:00+00:00")
    attempt(store, "t2", ts="2026-01-01T00:00:00+00:00")
    attempt(store, "t3", ts="2026-01-02T00:00:00+00:00")
    assert [r.task_id for r in pending(store, 2)] == ["t2", "t3"]


def test_limit_stop_ends_the_loop_and_the_report_says_so(store):
    attempt(store, "t1", ts="2026-01-01T00:00:00+00:00")
    attempt(store, "t2", ts="2026-01-02T00:00:00+00:00")
    runner = FakeRunner(CODE, LimitStop(detail="session limit reached"))
    report = classify_pending(store, runner, 50)
    assert report == {"considered": 2, "classified_code": 1, "still_unknown": 0, "stopped": "limit"}
    assert causes(store) == [("t1", "code", "the check failed"), ("t2", "unknown", None)]


def test_a_failed_model_call_leaves_the_row_pending(store):
    attempt(store, "t1")
    report = classify_pending(store, FakeRunner(RuntimeError("boom")), 50)
    assert report["still_unknown"] == 1
    assert [r.task_id for r in pending(store, 50)] == ["t1"]


def test_a_write_never_overwrites_a_why_that_landed_first(store):
    attempt(store, "t1")
    (row,) = pending(store, 50)
    store.set_attempt_cause("r1", "t1", 1, "code", "the driver decided")
    assert store_classify._write(store, row, "ticket", "late") == 0
    assert causes(store) == [("t1", "code", "the driver decided")]


def test_a_task_records_arbitration_reasoning_is_what_the_model_is_given(store):
    attempt(store, "t1", reason="quarantined")
    record = {
        "arbitration": {"reasoning": "the fix broke the build"},
        "adversary": {"objections": [{"claim": "no test"}]},
    }
    store.record_task_record("r1", "p1", "t1", record, STAMP)
    runner = FakeRunner(CODE)
    classify_pending(store, runner, 50)
    (call,) = runner.calls
    assert "the fix broke the build" in call["prompt"]
    assert "1. no test" in call["prompt"]
    assert call["task"] == "t1"


def test_without_a_task_record_the_attempt_reason_is_what_the_model_is_given(store):
    attempt(store, "t1", reason="patch did not apply")
    runner = FakeRunner(CODE)
    classify_pending(store, runner, 50)
    assert "patch did not apply" in runner.calls[0]["prompt"]


def test_cause_evidence_takes_arbitration_and_strips_claims():
    result = {
        "arbitration": {"reasoning": "  the fix broke the build  "},
        "adversary": {"objections": [{"claim": " no test "}, "plain", {"claim": ""}, "  "]},
    }
    assert cause_evidence("quarantined", result) == ("the fix broke the build", ["no test", "plain"])


def test_cause_evidence_falls_back_to_the_reason():
    assert cause_evidence("quarantined", None) == ("quarantined", [])
    assert cause_evidence("quarantined", {"arbitration": {"reasoning": " "}}) == ("quarantined", [])


def test_dry_run_lists_rows_and_calls_no_model(url, store, monkeypatch, capsys):
    attempt(store, "t1", reason="x" * 100)
    attempt(store, "t2", why="the model said so")

    def refuse(**_):
        raise AssertionError("a runner was built")

    monkeypatch.setattr(store_classify, "build_runner", refuse)
    assert store_classify.main([url, "--provider-profile", "none.yaml", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"r1\tt1\tquarantine\t{'x' * 80}"]


def test_main_opens_a_run_classifies_finishes_it_and_prints_the_report(url, store, monkeypatch, capsys):
    attempt(store, "t1")
    runner = FakeRunner(CODE)
    built: dict[str, Any] = {}

    def build(**kwargs):
        built.update(kwargs)
        return runner

    monkeypatch.setattr(store_classify, "build_runner", build)
    assert store_classify.main([url, "--provider-profile", "p.yaml"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "considered": 1,
        "classified_code": 1,
        "still_unknown": 0,
        "stopped": None,
    }
    assert built["scripted"] is None and built["workdir"] is None and built["repo"] is None
    run = store.conn.query_all("SELECT run_id, principal, status FROM runs")[0]
    assert run[0].startswith("classify-causes-") and tuple(run[1:]) == ("store_classify", "ok")
