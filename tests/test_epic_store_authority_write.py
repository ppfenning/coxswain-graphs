"""Under work_state: store a driver state move writes the store first, compare-and-set; files mode is unchanged."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import harness.epic as epic
import harness.store_read as read
import harness.store_work_state as sws
from harness import work_mirror
from harness.store_write import Store

INITIATIVE = "i1"
RUN = "run-1"
READY = b"---\nid: t1\nstate: ready\n---\n"
APPROVED = b"---\nid: t1\nstate: approved\n---\n"


@pytest.fixture
def item(tmp_path):
    path = tmp_path / "t1.md"
    path.write_bytes(READY)
    return {"id": "t1", "phase": "p1", "state": "ready", "needs": [], "path": str(path)}


def _ctx(store_conn, work_state):
    fields = {"store": Store(store_conn), "initiative_id": INITIATIVE, "run_id": RUN, "work_state": work_state}
    return type("Ctx", (), {**fields, "epoch": None, "cartridge": {}, "runner": None})()


def _put(ctx, item, state):
    row = work_mirror.item_row(INITIATIVE, {**item, "state": state}, "2026-01-01T00:00:00+00:00", "alice")
    epic._upsert_row(ctx.store, row)


def _row_state(ctx):
    return {r["task_id"]: r["state"] for r in read.work_items(ctx.store.conn, INITIATIVE)}.get("t1")


def _move(ctx, item, monkeypatch, events):
    """Run the state_move slot with an arm that records what the store and the file said when it was reached."""

    def arm(proposal, **_):
        events.append(("arm", _row_state(ctx), Path(item["path"]).read_bytes()))
        Path(item["path"]).write_bytes(APPROVED)
        return True, "stub arm"

    monkeypatch.setattr(epic, "auto_apply", arm)
    state = epic._Execution(landed={}, merged={}, moved={}, quarantined=[])
    move = {"kind": "state_move", "target": "t1"}
    result = epic._execute(ctx, move, slot="state_move", subject="t1", phase="p1", state=state, by_id={"t1": item})
    return state, result


def test_a_matching_move_writes_the_store_then_the_driver_writes_the_file_with_no_arm(store_conn, item, monkeypatch):
    ctx = _ctx(store_conn, "store")
    _put(ctx, item, "ready")
    events: list = []
    real = sws.set_state
    monkeypatch.setattr(sws, "set_state", lambda *a, **k: events.append(("store", k["expected"])) or real(*a, **k))
    state, result = _move(ctx, item, monkeypatch, events)
    assert events == [("store", "ready")]
    assert result == (True, "store moved to approved; file state line written")
    assert state.moved == {"t1": True}
    assert (_row_state(ctx), Path(item["path"]).read_bytes()) == ("approved", APPROVED)
    assert {r["task_id"]: r["updated_by"] for r in read.work_items(store_conn, INITIATIVE)}[
        "t1"
    ] == f"epic-driver:{RUN}"


def test_a_failed_file_write_still_applies_warns_and_leaves_the_store_moved(store_conn, item, monkeypatch, caplog):
    ctx = _ctx(store_conn, "store")
    _put(ctx, item, "ready")
    Path(item["path"]).unlink()
    events: list = []
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        state, (ok, detail) = _move(ctx, item, monkeypatch, events)
    assert (ok, events, state.moved) == (True, [], {"t1": True})
    assert detail == "store moved to approved; file state line written"
    assert _row_state(ctx) == "approved"
    assert not Path(item["path"]).exists()
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "regenerate-states" in caplog.records[0].getMessage()


def test_a_mismatch_refuses_names_both_states_and_leaves_the_file_and_the_row(store_conn, item, monkeypatch, caplog):
    ctx = _ctx(store_conn, "store")
    _put(ctx, item, "done")
    events: list = []
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        state, (ok, reason) = _move(ctx, item, monkeypatch, events)
    assert events == []
    assert ok is False
    assert reason == "state move refused: expected state 'ready' but store has 'done'"
    assert [r.getMessage() for r in caplog.records] == [f"{reason}: task=t1"]
    assert state.moved == {"t1": False}
    assert state.quarantined == []
    assert Path(item["path"]).read_bytes() == READY
    assert _row_state(ctx) == "done"
    assert item["state"] == "done"


def test_a_missing_row_refuses_and_inserts_nothing(store_conn, item, monkeypatch):
    ctx = _ctx(store_conn, "store")
    events: list = []
    state, (ok, reason) = _move(ctx, item, monkeypatch, events)
    assert (ok, events, state.moved) == (False, [], {"t1": False})
    assert reason == "state move refused: expected state 'ready' but store has no row"
    assert (item["state"], Path(item["path"]).read_bytes(), _row_state(ctx)) == ("ready", READY, None)


def test_a_store_error_refuses_the_move_and_the_file_is_not_written(store_conn, item, monkeypatch):
    ctx = _ctx(store_conn, "store")

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(sws, "set_state", boom)
    events: list = []
    state, (ok, reason) = _move(ctx, item, monkeypatch, events)
    assert (ok, events, state.moved) == (False, [], {"t1": False})
    assert reason == "state move refused, store write failed: RuntimeError: db down"
    assert Path(item["path"]).read_bytes() == READY


def test_files_mode_runs_the_arm_then_upserts_the_row_with_no_compare_and_set(store_conn, item, monkeypatch):
    ctx = _ctx(store_conn, "files")
    _put(ctx, item, "done")
    events: list = []
    monkeypatch.setattr(sws, "set_state", lambda *a, **k: events.append("store"))
    state, result = _move(ctx, item, monkeypatch, events)
    assert events == [("arm", "done", READY)]
    assert result == (True, "stub arm")
    assert state.moved == {"t1": True}
    assert (_row_state(ctx), Path(item["path"]).read_bytes()) == ("approved", APPROVED)


def test_store_mode_mirror_write_upserts_nothing(store_conn, item):
    ctx = _ctx(store_conn, "store")
    epic._mirror_write(ctx, item, "approved")
    assert _row_state(ctx) is None
