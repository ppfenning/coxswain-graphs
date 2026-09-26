"""Under work_state: store the driver reads a task's state from its work_items row; files stays as it was."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from core import workstore

import harness.epic as epic
import harness.store_read as read
from harness import work_mirror
from harness.store_write import Store

INITIATIVE = "i1"
RUN = "run-1"
FILE_BYTES = b'---\r\nid: t1\r\nstate: "ready" # keep me\r\nneeds: []\r\n---\r\nbody\xc3\xa9\r\n'
REPAIRED = b'---\r\nid: t1\r\nstate: "done" # keep me\r\nneeds: []\r\n---\r\nbody\xc3\xa9\r\n'


def _item(root, task, state, needs=(), body=None):
    path = root / f"{task}.md"
    path.write_bytes(body if body is not None else f"---\nid: {task}\nstate: {state}\n---\n".encode())
    return {"id": task, "phase": "p1", "state": state, "needs": list(needs), "path": str(path)}


@pytest.fixture
def items(tmp_path):
    return [_item(tmp_path, "t1", "ready", body=FILE_BYTES), _item(tmp_path, "t2", "ready", ["t1"])]


def _ctx(store_conn, work_state):
    fields = {"store": Store(store_conn), "initiative_id": INITIATIVE, "run_id": RUN}
    return type("Ctx", (), {**fields, "work_state": work_state})()


def _put(ctx, item, state):
    row = work_mirror.item_row(INITIATIVE, {**item, "state": state}, "2026-01-01T00:00:00+00:00", "alice")
    epic._upsert_row(ctx.store, row)


def _states(ctx):
    return {r["task_id"]: r["state"] for r in read.work_items(ctx.store.conn, INITIATIVE)}


def _ready(items):
    return [i["id"] for i in workstore.ready_tasks(epic._ready_view(items), phase="p1")]


def test_the_key_defaults_to_files_and_refuses_any_other_value():
    assert [epic.work_state_of(p) for p in ({}, {"work_state": "files"}, {"work_state": "store"})] == [
        "files",
        "files",
        "store",
    ]
    assert epic._Ctx.__dataclass_fields__["work_state"].default == "files"
    with pytest.raises(ValueError, match="'work_state' must be one of files, store, not 'db'"):
        epic.work_state_of({"work_state": "db"})


def test_the_driver_refuses_an_invalid_value_before_any_work(store_conn, tmp_path):
    with pytest.raises(ValueError, match="work_state"):
        epic.run_epic(
            initiative={}, repo=tmp_path, cartridge={}, runner=None, specs={}, run_id=RUN, date="2026-09-25",
            max_parallel=1, ledger_path=tmp_path / "l.jsonl", provider_profile="p", runs_dir=tmp_path / "runs",
            worktree_root=tmp_path, store=Store(store_conn), work_state="db",
        )  # fmt: skip
    assert read.work_items(store_conn, INITIATIVE) == []


def test_files_mode_leaves_a_stale_file_and_the_items_alone(store_conn, items, caplog):
    ctx = _ctx(store_conn, "files")
    _put(ctx, items[0], "done")
    before = [dict(i) for i in items]
    epic._mirror_read(ctx, items)
    assert items == before
    assert Path(items[0]["path"]).read_bytes() == FILE_BYTES
    assert _ready(items) == ["t1"]


def test_store_mode_repairs_only_the_state_line_and_the_driver_follows_the_row(store_conn, items):
    ctx = _ctx(store_conn, "store")
    _put(ctx, items[0], "done")
    epic._mirror_read(ctx, items)
    assert Path(items[0]["path"]).read_bytes() == REPAIRED
    assert items[0]["state"] == "done"
    assert _ready(items) == ["t2"]
    assert _states(ctx)["t1"] == "done"


def test_the_rewrite_is_one_log_line_naming_task_old_and_new(store_conn, items, caplog):
    ctx = _ctx(store_conn, "store")
    _put(ctx, items[0], "done")
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        epic._mirror_read(ctx, items)
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["work file state rewritten from the store: task=t1 old=ready new=done"]


def test_store_mode_with_no_row_reads_the_file_and_upserts(store_conn, items):
    ctx = _ctx(store_conn, "store")
    epic._mirror_read(ctx, items)
    rows = {r["task_id"]: (r["state"], r["needs"], r["updated_by"]) for r in read.work_items(store_conn, INITIATIVE)}
    assert rows == {"t1": ("ready", [], f"epic-driver:{RUN}"), "t2": ("ready", ["t1"], f"epic-driver:{RUN}")}
    assert Path(items[0]["path"]).read_bytes() == FILE_BYTES
    assert [i["state"] for i in items] == ["ready", "ready"]


def test_a_row_that_agrees_changes_nothing(store_conn, items):
    ctx = _ctx(store_conn, "store")
    _put(ctx, items[0], "ready")
    epic._mirror_read(ctx, items)
    assert Path(items[0]["path"]).read_bytes() == FILE_BYTES
    assert _states(ctx) == {"t1": "ready", "t2": "ready"}
    assert {r["task_id"]: r["updated_by"] for r in read.work_items(store_conn, INITIATIVE)}["t1"] == "alice"


def test_a_file_that_cannot_be_rewritten_warns_and_the_store_state_still_wins(store_conn, tmp_path, caplog):
    bare = _item(tmp_path, "t1", "ready", body=b"no frontmatter\n")
    ctx = _ctx(store_conn, "store")
    _put(ctx, bare, "done")
    with caplog.at_level(logging.WARNING, logger=epic._log.name):
        epic._mirror_read(ctx, [bare])
    assert [r.getMessage() for r in caplog.records] == [
        "work file state not rewritable, store state used: task=t1 file=ready"
    ]
    assert (bare["state"], Path(bare["path"]).read_bytes()) == ("done", b"no frontmatter\n")
