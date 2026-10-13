"""The driver mirrors work-file state into work_items; the files stay authoritative."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from core import workstore

import harness.epic as epic
import harness.store_read as read
from harness.store_write import Store

REPO = Path(__file__).resolve().parent.parent
INITIATIVE = "i1"
RUN = "run-1"


def _file(root, task, state, needs=()):
    path = root / f"{task}.md"
    path.write_text(f"---\nid: {task}\nstate: {state}\n---\n")
    return {"id": task, "phase": "p1", "state": state, "needs": list(needs), "path": str(path)}


@pytest.fixture
def items(tmp_path):
    return [_file(tmp_path, "t1", "ready"), _file(tmp_path, "t2", "ready", ["t1"])]


@pytest.fixture
def ctx(store_conn):
    return type("Ctx", (), {"store": Store(store_conn), "initiative_id": INITIATIVE, "run_id": RUN})()


def _by_task(ctx):
    return {r["task_id"]: r for r in read.work_items(ctx.store.conn, INITIATIVE)}


def test_a_read_populates_rows_matching_the_files(ctx, items):
    epic._mirror_read(ctx, items)
    rows = _by_task(ctx)
    assert {t: (r["phase"], r["state"], r["needs"], r["updated_by"]) for t, r in rows.items()} == {
        "t1": ("p1", "ready", [], f"epic-driver:{RUN}"),
        "t2": ("p1", "ready", ["t1"], f"epic-driver:{RUN}"),
    }


def test_a_state_write_updates_the_row(ctx, items):
    epic._mirror_read(ctx, items)
    epic._mirror_write(ctx, items[0], "approved")
    rows = _by_task(ctx)
    assert (rows["t1"]["state"], rows["t1"]["updated_by"]) == ("approved", f"epic-driver:{RUN}")
    assert rows["t2"]["state"] == "ready"


def test_a_newer_differing_row_is_one_warning_and_the_files_still_pick_the_ready_tasks(ctx, items, caplog):
    ctx.store.conn.execute(
        "INSERT INTO work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by)"
        f" VALUES ({', '.join(ctx.store.conn.dialect.placeholder for _ in range(7))})",
        (INITIATIVE, "t1", "p1", "dropped", "[]", "2999-01-01T00:00:00+00:00", "alice"),
    )
    before = [dict(i) for i in items]
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        epic._mirror_read(ctx, items)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    for part in (INITIATIVE, "t1", "file_state=ready", "store_state=dropped", "updated_by=alice"):
        assert part in messages[0]
    assert _by_task(ctx)["t1"]["state"] == "dropped"
    assert items == before
    assert [i["id"] for i in workstore.ready_tasks(epic._ready_view(items), phase="p1")] == ["t1"]


def test_a_failing_upsert_leaves_the_file_written_warns_and_raises_nothing(ctx, items, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(epic, "upsert_work_item", boom)
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        epic._mirror_read(ctx, items)
        epic._mirror_write(ctx, items[0], "approved")
    assert len(caplog.records) == 2
    assert all("store down" in r.getMessage() for r in caplog.records)
    assert "state: ready" in Path(items[0]["path"]).read_text()


def test_no_store_is_a_no_op_and_logs_nothing(items, caplog):
    bare = type("Ctx", (), {"store": None, "initiative_id": INITIATIVE, "run_id": RUN})()
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        epic._mirror_read(bare, items)
        epic._mirror_write(bare, items[0], "approved")
    assert caplog.records == []


def test_file_times_omits_items_with_no_readable_file(tmp_path):
    a = _file(tmp_path, "a", "ready")
    os.utime(a["path"], (0, 0))
    times = epic._file_times([a, {"id": "b", "path": str(tmp_path / "gone.md")}, {"id": "c"}])
    assert times == {"a": "1970-01-01T00:00:00+00:00"}


def _moved(ctx, items, applied, monkeypatch):
    monkeypatch.setattr(epic, "auto_apply", lambda item, **_: (applied, "stub arm"))
    ctx.epoch, ctx.cartridge, ctx.runner = None, {}, None
    state = epic._Execution(landed={}, merged={}, moved={}, quarantined=[])
    by_id = {i["id"]: i for i in items}
    move = {"kind": "state_move", "target": "t1"}
    epic._execute(ctx, move, slot="state_move", subject="t1", phase="p1", state=state, by_id=by_id)
    return state


def test_an_applied_state_move_upserts_the_row_as_approved(ctx, items, monkeypatch):
    state = _moved(ctx, items, True, monkeypatch)
    assert state.moved == {"t1": True}
    row = _by_task(ctx)["t1"]
    assert (row["state"], row["updated_by"]) == ("approved", f"epic-driver:{RUN}")


def test_a_state_move_that_did_not_apply_leaves_the_store_alone(ctx, items, monkeypatch):
    state = _moved(ctx, items, False, monkeypatch)
    assert state.moved == {"t1": False}
    assert _by_task(ctx) == {}


def test_importing_the_driver_does_not_load_store_traces():
    """harness/__init__ imports epic; an eager store_read import would make `-m harness.store_traces` warn."""
    code = "import sys, harness.epic; print('harness.store_traces' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert (done.stdout.strip(), done.stderr) == ("False", "")
