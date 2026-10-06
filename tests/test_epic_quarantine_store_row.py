"""Under work_state: store, quarantining a task moves its store row to quarantined; files mode writes nothing."""

from __future__ import annotations

import pytest

import harness.epic as epic
import harness.store_work_state as sws
from tests.test_epic_store_authority_write import _ctx, _put, _row_state, item  # noqa: F401 -- item is a fixture


def _quarantine(ctx) -> None:
    epic._quarantine_task(ctx, {}, phase="p1", task="t1", reason="the fix loop stopped: checkpoint after 2 build attempts", kind="infra")


def _spy(monkeypatch) -> list:
    calls: list = []
    real = sws.set_state
    monkeypatch.setattr(sws, "set_state", lambda *a, **k: calls.append((a[3], a[2])) or real(*a, **k))
    return calls


def test_a_quarantine_under_the_store_work_state_writes_quarantined_to_the_row(store_conn, item, monkeypatch):  # noqa: F811
    ctx = _ctx(store_conn, "store")
    _put(ctx, item, "ready")
    calls = _spy(monkeypatch)
    _quarantine(ctx)
    assert calls == [("quarantined", "t1")]
    assert _row_state(ctx) == "quarantined"


def test_a_quarantine_under_the_files_work_state_makes_no_store_write(store_conn, item, monkeypatch):  # noqa: F811
    ctx = _ctx(store_conn, "files")
    _put(ctx, item, "ready")
    calls = _spy(monkeypatch)
    _quarantine(ctx)
    assert calls == []
    assert _row_state(ctx) == "ready"


@pytest.mark.parametrize("finished", ["approved", "done"])
def test_a_finished_row_is_not_overwritten(store_conn, item, monkeypatch, finished):  # noqa: F811
    ctx = _ctx(store_conn, "store")
    _put(ctx, item, finished)
    calls = _spy(monkeypatch)
    _quarantine(ctx)
    assert calls == []
    assert _row_state(ctx) == finished


@pytest.mark.parametrize(
    ("work_state", "current", "expected"),
    [
        ("store", "ready", True),
        ("store", "blocked", True),
        ("store", "approved", False),
        ("store", "done", False),
        ("store", "quarantined", False),
        ("store", None, False),
        ("files", "ready", False),
    ],
)
def test_row_should_quarantine(work_state, current, expected):
    assert epic.row_should_quarantine(work_state, current) is expected
