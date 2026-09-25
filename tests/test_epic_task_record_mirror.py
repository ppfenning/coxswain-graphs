import logging
from pathlib import Path

import pytest

import harness.store_read as read
from harness.epic import _Ctx, _save_result, _task_record_mirror
from harness.resume import load_result, result_path
from harness.store_write import Store

RESULT = {"ticket": "t1", "initiative": "i", "phase": "p1", "proposals": [{"kind": "draft_pr_create"}], "n": 1}


def ctx(tmp_path: Path, store) -> _Ctx:
    return _Ctx(
        repo=tmp_path,
        cartridge={},
        runner=None,
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


class RaisingStore:
    def record_task_record(self, *args, **kwargs):
        raise RuntimeError("store is down")


def test_no_store_means_nothing_to_mirror():
    assert _task_record_mirror(None, RESULT, "r1", "p1", "t1", "ts") is None


def test_a_store_yields_the_upsert_arguments_with_the_record_as_the_file_holds_it():
    result = {"path": Path("/x"), "n": 1}
    args = _task_record_mirror(object(), result, "r1", "p1", "t1", "ts")
    assert args == ("r1", "p1", "t1", {"path": "/x", "n": 1}, "ts")


def test_the_row_exists_and_matches_the_file_and_a_second_save_replaces_it(tmp_path, store_conn):
    c = ctx(tmp_path, Store(store_conn))
    _save_result(c, RESULT, phase="p1", task="t1")
    assert result_path(c.runs_dir, "r1", "p1", "t1").exists()
    on_file = load_result(c.runs_dir, "r1", "p1", "t1")
    assert on_file == RESULT
    assert read.task_record(store_conn, "r1", "p1", "t1") == on_file
    _save_result(c, {**RESULT, "n": 2}, phase="p1", task="t1")
    assert read.task_records(store_conn, "r1") == {("p1", "t1"): {**RESULT, "n": 2}}


def test_a_raising_store_leaves_the_file_logs_a_warning_and_raises_nothing(tmp_path, caplog):
    c = ctx(tmp_path, RaisingStore())
    with caplog.at_level(logging.WARNING, logger="harness.epic"):
        _save_result(c, RESULT, phase="p1", task="t1")
    assert load_result(c.runs_dir, "r1", "p1", "t1") == RESULT
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "harness.epic"]
    assert len(warnings) == 1
    assert "r1/p1/t1" in warnings[0].getMessage()
    assert "store is down" in warnings[0].getMessage()


def test_with_no_store_the_file_is_written_and_nothing_is_logged(tmp_path, caplog):
    c = ctx(tmp_path, None)
    with caplog.at_level(logging.WARNING):
        _save_result(c, RESULT, phase="p1", task="t1")
    assert load_result(c.runs_dir, "r1", "p1", "t1") == RESULT
    assert caplog.records == []


@pytest.mark.parametrize("bad", [OSError("disk"), ValueError("bad")])
def test_any_store_exception_type_is_caught(tmp_path, bad):
    class Boom:
        def record_task_record(self, *a):
            raise bad

    _save_result(ctx(tmp_path, Boom()), RESULT, phase="p1", task="t1")
