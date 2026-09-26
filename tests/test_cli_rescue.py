"""`rescue --initiative <dir> --task <id>`: one rescue from the command line, its JSON report, and its run row."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from core import workstore

import harness.epic
from harness import cli
from harness.resume import save_result
from harness.store_dialect import default_url
from harness.store_migrate import open_store
from harness.store_write import Store, upsert_work_item
from runner.protocol import LimitStop
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    git,
    new_file_patch,
    repo,
)

PHASE = "p1-foundations"
TASK = "t1-probe"
BODY = "read the vendor schema"
RUN = "rescue-1"
TS = "2026-09-01T00:00:00+00:00"
ATTEMPT = f"{{run: epic-prior, phase: {PHASE}, kind: infra, reason: apply failed, body_sha: {workstore.body_sha(BODY)}, ts: '{TS}'}}"


class StoreRunner(Runner):
    """`Runner` that records each call in `.store`, as the live runners do, so the run has usage totals to print."""

    store = None
    run_id = None

    def run(self, **kwargs):
        result = super().run(**kwargs)
        call = {"id": f"call-{len(self.calls)}", "role": kwargs["role"], "model": "claude-x", "cost_usd": 0.25, "ok": True}
        self.store.record_call(call, run_id=self.run_id, seq=len(self.calls))
        return result


def _store(tmp_path: Path) -> Store:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    return Store(open_store(default_url(runs), TS))


def _work(tmp_path: Path, cause: str | None) -> Path:
    """A work store holding one task; with a `cause`, its last run kept a patch and the store holds that attempt's cause."""
    wi = tmp_path / "wi"
    (wi / PHASE).mkdir(parents=True)
    (wi / "initiative.md").write_text("---\nid: demo-initiative\ntitle: demo\n---\n\nmake the join measurable\n")
    attempts = f"attempts:\n  - {ATTEMPT}\n" if cause else ""
    (wi / PHASE / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nphase: {PHASE}\nstate: ready\nneeds: []\nsurfaces: []\ntitle: schema probe\n"
        f"{attempts}---\n\n{BODY}\n"
    )
    if cause:
        runs = tmp_path / "runs"
        save_result({"ticket": TASK, "build": {"patch": new_file_patch("t1-probe.txt")}}, runs_dir=runs, run_id="epic-prior", phase=PHASE, task=TASK)
        # The cause is on the store's attempt row only; the work file's attempt has no such field.
        store = _store(tmp_path)
        store.record_attempt("epic-prior", TASK, 1, PHASE, "infra", "apply failed", TS, cause=cause, cause_why="seeded")
        store.conn.close()
    return wi


def _launch(monkeypatch, capsys, repo, cart, tmp_path, *, task=TASK, cause="harness", drop=None, profile="tiers: {}\n"):  # noqa: F811
    """`cli.main` over the real parser, with only the runner and the cartridge stood in. Returns (exit, stdout, stderr, runner)."""
    git("branch", f"epic/demo-initiative/{PHASE}", "main", cwd=repo)
    runner = StoreRunner({})
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(profile, encoding="utf-8")
    monkeypatch.setattr(cli, "build_runner", lambda **kw: runner)
    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **kw: (cart, {}))
    monkeypatch.setattr(cli, "role_skill_bodies", lambda *a, **kw: {})
    flags = {
        "--team": "acme", "--provider-profile": str(profile_path), "--runs-dir": str(tmp_path / "runs"),
        "--ledger": str(tmp_path / "ledger.jsonl"), "--repo": str(repo), "--initiative": str(_work(tmp_path, cause)),
        "--task": task, "--assume": "a", "--run-id": RUN, "--date": "2026-09-26",
    }  # fmt: skip
    argv = ["rescue", "--unverified-skills", *(part for flag, value in flags.items() if flag != drop for part in (flag, value))]
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err, runner


def _runs(tmp_path: Path) -> list[tuple]:
    conn = sqlite3.connect(tmp_path / "runs" / "cox.db")
    try:
        return conn.execute("SELECT run_id, principal, status, ended_at IS NOT NULL FROM runs").fetchall()
    finally:
        conn.close()


def test_an_eligible_task_prints_only_the_approved_report_and_ends_the_run_ok(monkeypatch, capsys, repo, cart, tmp_path) -> None:  # noqa: F811
    code, out, err, runner = _launch(monkeypatch, capsys, repo, cart, tmp_path)

    assert code == 0
    assert json.loads(out)["status"] == "approved"
    assert "usage   : 1 node call(s)" in err
    assert workstore.read_item(tmp_path / "wi" / PHASE / f"{TASK}.md")["state"] == "approved"
    assert (runner.run_id, runner.store is not None) == (RUN, True)
    assert _runs(tmp_path) == [(RUN, harness.epic.PRINCIPAL, "ok", 1)]


@pytest.mark.parametrize(
    ("cause", "why"), [(None, "no patch kept"), ("code", "last cause is code, not harness")]
)
def test_an_ineligible_task_prints_not_eligible_and_exits_zero(monkeypatch, capsys, repo, cart, tmp_path, cause, why) -> None:  # noqa: F811
    code, out, _, runner = _launch(monkeypatch, capsys, repo, cart, tmp_path, cause=cause)

    assert code == 0
    assert json.loads(out) == {"status": "not_eligible", "why": why}
    assert runner.calls == []
    assert _runs(tmp_path) == [(RUN, harness.epic.PRINCIPAL, "ok", 1)]


def test_under_work_state_store_the_rows_state_is_the_one_the_move_compares(monkeypatch, capsys, repo, cart, tmp_path) -> None:  # noqa: F811
    store = _store(tmp_path)
    upsert_work_item(store.conn, "demo-initiative", TASK, PHASE, "todo", [], TS, "seed")
    store.conn.close()

    code, out, _, _ = _launch(monkeypatch, capsys, repo, cart, tmp_path, profile="tiers: {}\nwork_state: store\n")

    assert (code, json.loads(out)["status"]) == (0, "approved")
    conn = sqlite3.connect(tmp_path / "runs" / "cox.db")
    assert conn.execute("SELECT state FROM work_items WHERE task_id = ?", (TASK,)).fetchall() == [("approved",)]
    conn.close()


def test_an_unknown_task_id_exits_nonzero_and_prints_no_report(monkeypatch, capsys, repo, cart, tmp_path) -> None:  # noqa: F811
    code, out, _, _ = _launch(monkeypatch, capsys, repo, cart, tmp_path, task="t9-nope")

    assert code == 1
    assert out == ""
    assert _runs(tmp_path) == [(RUN, harness.epic.PRINCIPAL, "failed", 1)]


@pytest.mark.parametrize("flag", ["--initiative", "--task", "--repo"])
def test_an_omitted_flag_is_a_usage_error(monkeypatch, capsys, repo, cart, tmp_path, flag) -> None:  # noqa: F811
    with pytest.raises(SystemExit) as caught:
        _launch(monkeypatch, capsys, repo, cart, tmp_path, drop=flag)

    assert caught.value.code == 2
    assert f"rescue needs {flag}" in capsys.readouterr().err
    assert _runs(tmp_path)[0][2:] == ("error", 1)


def test_a_limit_stop_reports_stopped_exits_zero_and_ends_the_run_stopped(monkeypatch, capsys, repo, cart, tmp_path) -> None:  # noqa: F811
    def limit(ctx, item, *, phase):
        raise LimitStop(detail="session limit reached")

    monkeypatch.setattr(harness.epic, "rescue_task", limit)
    code, out, _, _ = _launch(monkeypatch, capsys, repo, cart, tmp_path)

    assert code == 0
    assert json.loads(out) == {"status": "stopped", "why": "session limit reached"}
    assert _runs(tmp_path) == [(RUN, harness.epic.PRINCIPAL, "stopped", 1)]
