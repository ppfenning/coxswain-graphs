import logging

from harness.epic import _record_run_exit, _StoreRetry, build_outcome

REASON = "phase branch coxswain/p1 is behind main and carries its own commits; rebase it through the gate"
TOTALS = {"phases_complete": 1, "phases_partial": 0, "phases_blocked": 0, "tasks_quarantined": 0}


def test_a_phase_quarantine_becomes_a_blocked_entry_with_its_reason_verbatim():
    result = {
        "phases": [{"phase": "p1", "status": "blocked", "reason": REASON}],
        "quarantined": [{"id": "p1", "phase": "p1", "grain": "phase", "reason": REASON}],
        "totals": TOTALS,
    }
    assert build_outcome(result) == {"totals": TOTALS, "blocked": [{"phase": "p1", "reason": REASON}], "waiting": []}


def test_a_clean_result_has_empty_blocked_and_waiting():
    result = {"phases": [{"phase": "p1", "status": "complete"}], "quarantined": [], "totals": TOTALS}
    assert build_outcome(result) == {"totals": TOTALS, "blocked": [], "waiting": []}


def test_a_phase_blocked_on_an_unlanded_parent_is_waiting_not_blocked():
    result = {
        "phases": [
            {"phase": "p2", "status": "blocked", "parents": ["p1"], "reason": "parent phase 'p1' did not meet its goal"}
        ],
        "quarantined": [],
        "totals": TOTALS,
    }
    assert build_outcome(result)["waiting"] == ["p2"]
    assert build_outcome(result)["blocked"] == []


def test_a_task_grain_quarantine_is_not_blocked():
    result = {"phases": [], "quarantined": [{"id": "t1", "phase": "p1", "grain": "task", "reason": "x"}], "totals": {}}
    assert build_outcome(result)["blocked"] == []


class _RaisingStore:
    def __init__(self):
        self.finished = []

    def finish_run(self, run_id, ended_at, status):
        self.finished.append((run_id, status))

    def record_outcome(self, run_id, outcome):
        raise RuntimeError("store is down")


def test_a_store_write_that_raises_is_logged_and_the_exit_still_returns(caplog):
    store = _RaisingStore()
    with caplog.at_level(logging.WARNING):
        out = _record_run_exit(store, "r1", "ok", _StoreRetry(), {"phases": [], "quarantined": [], "totals": {}})
    assert out is None
    assert store.finished == [("r1", "ok")]
    assert "run outcome for r1 not recorded" in caplog.text
