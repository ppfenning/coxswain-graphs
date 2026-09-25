"""main() prints the run's totals from the store on every exit path and writes no usage file."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import harness.cli as cli
from harness.invoke import Invocation, invoke_graphs
from runner.scripted import ScriptedRunner


class _Args:
    skills_root: ClassVar[list[str]] = ["s"]
    unverified_skills = False
    repo = None
    graph = "lifecycle"
    date = "2026-09-08"
    team = "acme"
    cartridges_dir = None
    workdir = None
    scripted = True
    provider_profile = "acme"
    keep_worktrees = False
    worktree_root = None
    node_cap_usd = None

    def __init__(self, runs_dir, run_id) -> None:
        self.runs_dir = runs_dir
        self.run_id = run_id


class _FakeParser:
    def __init__(self, args: _Args) -> None:
        self._args = args

    def parse_args(self, argv):
        return self._args

    def error(self, msg):
        raise SystemExit(msg)


class _FakeWorktrees:
    """Stands in for harness.worktree's git-backed primitives — no real git.

    Tracks a `registered` set the way a real `git worktree` administrative
    entry would, so a test can assert §6's own postcondition — "no directory
    and no registration" — on both axes without a single git subprocess.
    Also records every `(repo, worktree)` pair it is called with, so a test
    can assert cli.py never hands it this harness's own checkout as `repo`
    for a scratch directory that was never a linked worktree of it.
    """

    def __init__(self) -> None:
        self.registered: set[str] = set()
        self.remove_calls: list[tuple[Path, Path]] = []
        self.keep_calls: list[tuple[Path, Path, Path, str]] = []

    def register(self, worktree: Path) -> None:
        worktree.mkdir(parents=True)
        self.registered.add(str(worktree))

    def remove(self, repo: Path, worktree: Path) -> tuple[bool, str]:
        self.remove_calls.append((repo, worktree))
        self.registered.discard(str(worktree))
        if worktree.exists():
            shutil.rmtree(worktree)
        return True, "removed (fake)"

    def keep(self, repo: Path, worktree: Path, worktree_root: Path, run_id: str) -> tuple[bool, str]:
        self.keep_calls.append((repo, worktree, worktree_root, run_id))
        self.registered.discard(str(worktree))
        dest = Path(worktree_root) / "_kept" / run_id / worktree.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            shutil.move(str(worktree), str(dest))
        return True, f"kept at {dest} (fake)"


def _patch_common(monkeypatch, args: _Args, runner) -> _FakeWorktrees:
    monkeypatch.setattr(cli, "discover", lambda: {})
    monkeypatch.setattr(cli, "_build_parser", lambda specs: _FakeParser(args))
    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "role_skill_bodies", lambda *a, **k: {})
    monkeypatch.setattr(cli, "build_runner", lambda **k: runner)
    fake_worktrees = _FakeWorktrees()
    monkeypatch.setattr(cli, "remove_worktree", fake_worktrees.remove)
    monkeypatch.setattr(cli, "keep_worktree", fake_worktrees.keep)
    return fake_worktrees


_ONE_CALL = {"id": "c1", "role": "build", "model": "claude-x", "cost_usd": 1.0, "turns": 1, "ok": True}
_ONE_CALL_LINE = "  usage   : 1 node call(s), 1 turns, $1.0 — claude-x: 1 call(s) $1.0"


def test_a_raise_inside_the_dispatched_graph_writes_no_usage_file_and_still_prints_totals(
    monkeypatch, tmp_path, capsys
) -> None:
    run_id = "runX"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)

    def _boom(**kwargs):
        kwargs["store"].record_call(_ONE_CALL, run_id=run_id, seq=1)
        raise RuntimeError("boom")

    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", _boom)

    with pytest.raises(RuntimeError):
        cli.main([])

    assert not (tmp_path / f"{run_id}.usage.json").exists()
    assert _ONE_CALL_LINE in capsys.readouterr().out


def test_node_cap_usd_threads_from_the_flag_to_the_constructed_runner(monkeypatch, tmp_path) -> None:
    run_id = "runC"
    runner = SimpleNamespace(calls=[], node_cap_usd=None)
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    args.node_cap_usd = 1.5
    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    assert cli.main([]) == 0
    assert runner.node_cap_usd == 1.5


def test_close_runs_after_the_totals_print_and_no_usage_file_is_written(monkeypatch, tmp_path, capsys) -> None:
    run_id = "runY"
    order: list[str] = []

    class _Runner:
        def __init__(self) -> None:
            self.calls = [_ONE_CALL]
            self.closed = False

        def close(self) -> None:
            order.append("close")
            self.closed = True
            self.calls = []

    def _graph(**kwargs):
        kwargs["store"].record_call(_ONE_CALL, run_id=run_id, seq=1)
        return 0

    runner = _Runner()
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", _graph)
    real_usage_line = cli._usage_line
    monkeypatch.setattr(cli, "_usage_line", lambda s, m: order.append("usage") or real_usage_line(s, m))

    result = cli.main([])

    assert result == 0
    assert runner.closed is True
    assert order == ["usage", "close"]
    assert not (tmp_path / f"{run_id}.usage.json").exists()
    assert _ONE_CALL_LINE in capsys.readouterr().out


def test_keep_worktrees_flag_defaults_to_off_and_is_settable() -> None:
    parser = cli._build_parser(cli.discover())

    off = parser.parse_args(["lifecycle", "--team", "acme", "--unverified-skills"])
    on = parser.parse_args(["lifecycle", "--team", "acme", "--unverified-skills", "--keep-worktrees"])

    assert off.keep_worktrees is False
    assert on.keep_worktrees is True


def test_sweep_is_a_selectable_graph_choice_and_round_trips_through_parsing() -> None:
    parser = cli._build_parser(cli.discover())

    parsed = parser.parse_args(["sweep", "--team", "acme", "--unverified-skills"])

    graph_action = next(action for action in parser._actions if action.dest == "graph")
    assert "sweep" in graph_action.choices
    assert parsed.graph == "sweep"


def test_sweep_refuses_cleanly_instead_of_crashing_on_a_missing_spec(monkeypatch, tmp_path) -> None:
    run_id = "runS"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    args.graph = "sweep"
    _patch_common(monkeypatch, args, runner)

    with pytest.raises(SystemExit, match="no SPEC yet"):
        cli.main([])


def test_sweep_guard_turns_itself_off_once_a_spec_registers_it() -> None:
    class _Dispatched(Exception):
        pass

    def _run(graph_args, runner):
        raise _Dispatched("sweep's own spec was reached")

    class _RefusedInError(AssertionError):
        pass

    class _Parser:
        def error(self, msg):
            raise _RefusedInError(f"guard fired despite a registered spec: {msg}")

    specs = {"sweep": SimpleNamespace(needs=[], graph_name="sweep", run=_run)}
    args = SimpleNamespace(graph="sweep", date="2026-09-16")

    with pytest.raises(_Dispatched):
        cli._run_graph(specs=specs, parser=_Parser(), args=args, cartridge={}, runner=None, run_id="r")


def test_a_normal_run_removes_the_lifecycle_worktree_on_exit(monkeypatch, tmp_path) -> None:
    run_id = "runW"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    worktree = tmp_path / run_id

    fake = _patch_common(monkeypatch, args, runner)

    def _stub(**kwargs):
        fake.register(worktree)
        return 0

    monkeypatch.setattr(cli, "_run_graph", _stub)

    result = cli.main([])

    assert result == 0
    assert not worktree.exists()
    assert str(worktree) not in fake.registered
    # No --repo was given, so `worktree` is its own standalone scratch repo
    # (the `git init` fallback in `apply_patch`), never a linked worktree of
    # this harness's own checkout — the repo argument must be the worktree
    # itself, not REPO_ROOT.
    assert fake.remove_calls == [(worktree, worktree)]


def test_a_lifecycle_run_that_creates_no_worktree_is_not_cleaned_up(monkeypatch, tmp_path) -> None:
    run_id = "runN"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)

    fake = _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    result = cli.main([])

    assert result == 0
    assert fake.remove_calls == []
    assert fake.keep_calls == []


def test_a_run_with_repo_set_hands_that_repo_to_cleanup_not_the_worktree(monkeypatch, tmp_path) -> None:
    run_id = "runP"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    args.repo = str(tmp_path / "project")
    worktree = tmp_path / run_id

    fake = _patch_common(monkeypatch, args, runner)

    def _stub(**kwargs):
        fake.register(worktree)
        return 0

    monkeypatch.setattr(cli, "_run_graph", _stub)

    cli.main([])

    assert fake.remove_calls == [(Path(args.repo), worktree)]


def test_keep_worktrees_moves_the_dir_under_kept_run_id(monkeypatch, tmp_path) -> None:
    run_id = "runK"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    args.keep_worktrees = True
    worktree = tmp_path / run_id

    fake = _patch_common(monkeypatch, args, runner)

    def _stub(**kwargs):
        fake.register(worktree)
        return 0

    monkeypatch.setattr(cli, "_run_graph", _stub)

    result = cli.main([])

    assert result == 0
    assert not worktree.exists()
    assert str(worktree) not in fake.registered
    assert (tmp_path / "_kept" / run_id / run_id).is_dir()
    assert fake.keep_calls == [(worktree, worktree, tmp_path, run_id)]


def test_provider_profile_scope_is_deterministic_across_call_sites(tmp_path) -> None:
    profile = tmp_path / "claude-code.yaml"
    profile.write_bytes(b"model: claude\n")
    expected = f"claude-code@{hashlib.sha256(profile.read_bytes()).hexdigest()[:12]}"

    first = cli._provider_profile_scope(profile)
    second = cli._provider_profile_scope(profile)

    assert first == second == expected


def test_editing_the_profile_file_changes_the_hash_but_not_the_stem(tmp_path) -> None:
    profile = tmp_path / "claude-code.yaml"
    profile.write_bytes(b"model: claude\n")
    before = cli._provider_profile_scope(profile)

    profile.write_bytes(b"model: claude-opus\n")
    after = cli._provider_profile_scope(profile)

    before_stem, before_hash = before.split("@")
    after_stem, after_hash = after.split("@")
    assert before_stem == after_stem == "claude-code"
    assert before_hash != after_hash


def test_a_run_that_raises_after_creating_a_worktree_leaves_no_directory_and_no_registration(
    monkeypatch, tmp_path
) -> None:
    run_id = "runR"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    worktree = tmp_path / run_id

    fake = _patch_common(monkeypatch, args, runner)

    def _boom(**kwargs):
        fake.register(worktree)
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "_run_graph", _boom)

    with pytest.raises(RuntimeError):
        cli.main([])

    assert not worktree.exists()
    assert str(worktree) not in fake.registered
    assert fake.remove_calls == [(worktree, worktree)]


# ── phase: a need on a task in another initiative ───────────────────────────

FOREIGN_NEED = "other-initiative/t9-upstream"


class _Reached(Exception):
    def __init__(self, tasks) -> None:
        self.tasks = tasks


def _phase_args(phase_name: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        graph="phase", initiative="demo", phase_name=phase_name, max_parallel=2, date="2026-09-24"
    )


def _run_phase_graph(monkeypatch, foreign_state: str, phase_name: str | None) -> list[str]:
    """Drive the `phase` arm; return the ids it would have launched (empty if none)."""
    initiative = {
        "id": "demo",
        "phases": ["p1"],
        "items": [{"id": "t1", "phase": "p1", "state": "ready", "needs": [FOREIGN_NEED]}],
        "foreign": {FOREIGN_NEED: foreign_state},
    }
    monkeypatch.setattr(cli.workstore, "read_initiative", lambda name: initiative)

    def _launch(**kwargs):
        raise _Reached(kwargs["tasks"])

    monkeypatch.setattr(cli, "run_phase", _launch)
    args = _phase_args(phase_name)
    try:
        code = cli._run_graph(
            specs={"lifecycle": SimpleNamespace(run=None)},
            parser=_FakeParser(args),
            args=args,
            cartridge={},
            runner=None,
            run_id="r1",
        )
    except _Reached as reached:
        return [t["id"] for t in reached.tasks]
    assert code == 0
    return []


@pytest.mark.parametrize("phase_name", ["p1", None])
def test_phase_launches_a_task_whose_foreign_need_is_done(monkeypatch, phase_name) -> None:
    assert _run_phase_graph(monkeypatch, "done", phase_name) == ["t1"]


@pytest.mark.parametrize("phase_name", ["p1", None])
def test_phase_leaves_a_task_unready_while_its_foreign_need_is_not_done(monkeypatch, capsys, phase_name) -> None:
    assert _run_phase_graph(monkeypatch, "ready", phase_name) == []
    assert "nothing ready in demo" in capsys.readouterr().out


# --- the run-record store -------------------------------------------------------------------------------------------

_LOADED_GRAPH = SimpleNamespace(
    name="lifecycle-propose",
    version="1",
    nodes=[
        SimpleNamespace(node_id=n, role=n, default_tier="standard", default_class="coding", output_schema=None)
        for n in ("plan", "build", "review")
    ],
    edges=[("plan", "build"), ("build", "review")],
)
_CARTRIDGE = {"cartridge_sha": "sha-1", "team": "acme", "overlay_sha": None}


class _StoreRunner(ScriptedRunner):
    """A scripted runner that records each call to `.store` the way the live runners do."""

    store = None
    run_id = None

    def run(self, **kwargs):
        result = super().run(**kwargs)
        call = {
            "id": f"call-{len(self.calls)}",
            "role": kwargs["role"],
            "model": "claude-x",
            "cost_usd": 0.25,
            "turns": 2,
            "input_total": 10,
            "cache_read_tokens": 5,
            "ok": True,
        }
        self.store.record_call(call, run_id=self.run_id, seq=len(self.calls))
        return result


class _TraceRunner(_StoreRunner):
    """Writes a per-call trace file the way ClaudeCodeRunner does, and records its path in the call's detail."""

    trace_dir = None

    def run(self, **kwargs):
        result = ScriptedRunner.run(self, **kwargs)
        call_id = f"call-{len(self.calls)}"
        trace = self.trace_dir / f"{kwargs['role']}-{len(self.calls)}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text('{"type":"system"}\nnot json\n{"type":"result"}\n', encoding="utf-8")
        call = {"id": call_id, "role": kwargs["role"], "model": "claude-x", "ok": True, "trace": str(trace)}
        self.store.record_call({**call, "ts": "2026-09-25T07:20:00+00:00"}, run_id=self.run_id, seq=len(self.calls))
        return result


def _store_run(monkeypatch, tmp_path, *, profile_url=None, graph=lambda runner: 0, runner_cls=_StoreRunner):
    """Stage `main` over a scripted runner; `graph(runner)` stands in for the dispatched graph."""
    runner = runner_cls({"plan": {}, "build": {}, "review": {}})
    if runner_cls is _TraceRunner:
        runner.trace_dir = tmp_path / "runS-trace"
    args = _Args(tmp_path, "runS")
    args.worktree_root = str(tmp_path)
    if profile_url is not None:
        profile = tmp_path / "profile.yaml"
        profile.write_text(f"tiers: {{}}\nstorage_url: {profile_url}\n", encoding="utf-8")
        args.provider_profile = str(profile)
    _patch_common(monkeypatch, args, runner)
    spec = SimpleNamespace(graph_name="lifecycle-propose", graph=_LOADED_GRAPH)
    monkeypatch.setattr(cli, "discover", lambda: {"lifecycle": spec})
    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **k: (_CARTRIDGE, {}))
    monkeypatch.setattr(cli, "_run_graph", lambda **k: graph(k["runner"]))
    return runner


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _three_calls(runner) -> int:
    for role in ("plan", "build", "review"):
        runner.run(role=role, schema={}, prompt="p")
    return 0


def test_storage_url_is_the_profile_key_else_the_sqlite_file_in_the_runs_dir() -> None:
    assert cli._storage_url({"storage_url": "sqlite:///x.db"}, "runs") == "sqlite:///x.db"
    assert cli._storage_url({}, "runs") == "sqlite:///runs/cox.db"
    assert cli._storage_url({"storage_url": ""}, "runs") == "sqlite:///runs/cox.db"


def test_a_run_leaves_a_runs_row_joined_to_its_graph_and_a_node_call_per_scripted_call(monkeypatch, tmp_path) -> None:
    _store_run(monkeypatch, tmp_path, graph=_three_calls)

    assert cli.main([]) == 0

    db = tmp_path / "cox.db"
    assert db.exists()
    assert _rows(db, "SELECT principal, launched_by, cartridge_sha, cartridge_team, status FROM runs") == [
        ("lifecycle-propose", "cli", "sha-1", "acme", "ok")
    ]
    assert _rows(db, "SELECT COUNT(*) FROM runs r JOIN graphs g ON g.graph_id = r.graph_id") == [(1,)]
    assert _rows(db, "SELECT COUNT(*) FROM runs r JOIN graph_nodes n ON n.graph_id = r.graph_id") == [(3,)]
    assert _rows(db, "SELECT run_id, role FROM node_calls ORDER BY seq") == [
        ("runS", "plan"),
        ("runS", "build"),
        ("runS", "review"),
    ]
    assert _rows(db, "SELECT ended_at IS NOT NULL FROM runs") == [(1,)]


_THREE_CALLS_LINE = (
    "  usage   : 3 node call(s), 6 turns, $0.75 — claude-x: 3 call(s) $0.75, 50% of input was cache reads"
)


def test_a_scripted_run_writes_no_usage_file_and_prints_totals_equal_to_the_sum_of_its_calls(
    monkeypatch, tmp_path, capsys
) -> None:
    _store_run(monkeypatch, tmp_path, graph=_three_calls)

    assert cli.main([]) == 0

    assert list(tmp_path.glob("*.usage.json")) == []
    assert _rows(tmp_path / "cox.db", "SELECT COUNT(*), SUM(cost_usd), SUM(turns) FROM node_calls") == [(3, 0.75, 6)]
    assert _THREE_CALLS_LINE in capsys.readouterr().out.splitlines()


def test_a_budget_stop_return_still_prints_the_totals(monkeypatch, tmp_path, capsys) -> None:
    def stop_after_three(runner) -> int:
        _three_calls(runner)
        return 1

    _store_run(monkeypatch, tmp_path, graph=stop_after_three)

    assert cli.main([]) == 1

    assert list(tmp_path.glob("*.usage.json")) == []
    assert _THREE_CALLS_LINE in capsys.readouterr().out.splitlines()


def test_usage_line_is_one_literal_string_one_entry_per_alias_and_none_for_a_run_with_no_calls() -> None:
    summary = {"calls": 3, "turns": 5, "cost_usd": 0.6000000000000001, "input_total": 30, "cache_read_tokens": 10}
    models = [
        {"model_alias": "a", "tier": "light", "calls": 1, "cost_usd": 0.1},
        {"model_alias": "a", "tier": "heavy", "calls": 1, "cost_usd": 0.3},
        {"model_alias": "b", "tier": "light", "calls": 1, "cost_usd": 0.2},
    ]
    assert cli._usage_line(summary, models) == (
        "  usage   : 3 node call(s), 5 turns, $0.6 — a: 2 call(s) $0.4, b: 1 call(s) $0.2, 33% of input was cache reads"
    )
    assert cli._usage_line({**summary, "input_total": 0, "cache_read_tokens": 0}, models).endswith("$0.2")
    assert cli._usage_line({**summary, "calls": 0}, []) is None
    assert cli._usage_line(None, []) is None


def test_undercount_note_names_the_gap_only_when_the_runner_made_more_calls_than_the_store_holds() -> None:
    assert cli._undercount_note("r", 1, 2) == "store: holds 1 of the 2 call(s) r made; the usage totals undercount"
    assert cli._undercount_note("r", 2, 2) is None
    assert cli._undercount_note("r", 3, 2) is None


def test_a_call_the_store_never_got_is_warned_about_beside_the_totals(monkeypatch, tmp_path, capsys) -> None:
    run_id = "runU"
    runner = SimpleNamespace(calls=[_ONE_CALL, {**_ONE_CALL, "id": "c2"}])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    _patch_common(monkeypatch, args, runner)

    def _graph(**kwargs):
        kwargs["store"].record_call(_ONE_CALL, run_id=run_id, seq=1)
        return 0

    monkeypatch.setattr(cli, "_run_graph", _graph)

    assert cli.main([]) == 0

    out, err = capsys.readouterr()
    assert _ONE_CALL_LINE in out
    assert "store: holds 1 of the 2 call(s) runU made; the usage totals undercount" in err


@pytest.mark.parametrize("outcome", ["returns", "raises"])
def test_a_store_read_that_fails_warns_and_keeps_the_exit_close_conn_close_and_cleanup(
    monkeypatch, tmp_path, capsys, outcome
) -> None:
    run_id = "runF"
    worktree = tmp_path / run_id
    seen: dict[str, object] = {}

    class _Runner:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runner = _Runner()
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    fake = _patch_common(monkeypatch, args, runner)

    def _graph(**kwargs):
        seen["store"] = kwargs["store"]
        fake.register(worktree)
        if outcome == "raises":
            raise RuntimeError("the graph's own error")
        return 3

    def _locked(conn, run_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli, "_run_graph", _graph)
    monkeypatch.setattr(cli, "run_summary", _locked)

    if outcome == "raises":
        with pytest.raises(RuntimeError, match="the graph's own error"):
            cli.main([])
    else:
        assert cli.main([]) == 3

    assert runner.closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        seen["store"].conn.raw.execute("SELECT 1")
    assert not worktree.exists()
    assert fake.remove_calls == [(worktree, worktree)]
    assert "store: could not read the usage of runF: database is locked" in capsys.readouterr().err


def test_a_failed_return_and_a_raise_each_stamp_the_run_ended(monkeypatch, tmp_path) -> None:
    _store_run(monkeypatch, tmp_path, graph=lambda runner: 1)
    assert cli.main([]) == 1
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("failed", 1)]

    def boom(runner):
        raise RuntimeError("boom")

    other = tmp_path / "raised"
    other.mkdir()
    _store_run(monkeypatch, other, graph=boom)
    with pytest.raises(RuntimeError):
        cli.main([])
    assert _rows(other / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("error", 1)]


@pytest.mark.parametrize("bad_url", ["sqlite:///{tmp}/no/such/dir/x.db", "mysql://user@host/db"])
def test_an_unopenable_store_url_exits_nonzero_before_any_node_runs(monkeypatch, tmp_path, capsys, bad_url) -> None:
    launched: list[int] = []
    runner = _store_run(
        monkeypatch, tmp_path, profile_url=bad_url.format(tmp=tmp_path), graph=lambda r: launched.append(1) or 0
    )

    assert cli.main([]) == 1

    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith(f"store: cannot use {bad_url.split(':', 1)[0]} store: ")
    assert launched == []
    assert runner.calls == []


def test_a_profile_storage_url_sends_the_rows_there_and_not_to_the_default_file(monkeypatch, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere.db"
    _store_run(monkeypatch, tmp_path, profile_url=f"sqlite:///{elsewhere}", graph=_three_calls)

    assert cli.main([]) == 0

    assert _rows(elsewhere, "SELECT COUNT(*) FROM runs") == [(1,)]
    assert _rows(elsewhere, "SELECT COUNT(*) FROM node_calls") == [(3,)]
    assert not (tmp_path / "cox.db").exists()


# --- --result-out: the generic path hands the graph's result to its caller ------------------------------------------------

_REVIEW_RESULT = {
    "verdict": "approve",
    "findings": [{"file": "a.py", "line": 3, "detail": "d", "charter_principle": "A1"}],
    "rationale": "r",
    "checks": [],
}


def _review_diff_run(tmp_path: Path, *, result_out: Path | None, raises: Exception | None = None) -> int:
    def _run(graph_args, runner):
        if raises is not None:
            raise raises
        return dict(_REVIEW_RESULT)

    profile = tmp_path / "profile.yaml"
    profile.write_text("provider: test\n", encoding="utf-8")
    spec = SimpleNamespace(name="review-diff", graph_name="review-diff", needs=(), run=_run)
    args = SimpleNamespace(
        graph="review-diff",
        date="2026-09-24",
        runs_dir=tmp_path / "runs",
        ledger=tmp_path / "ledger.jsonl",
        provider_profile=str(profile),
        repo=None,
        assume=None,
        result_out=None if result_out is None else str(result_out),
    )
    return cli._run_graph(
        specs={"review-diff": spec},
        parser=_FakeParser(args),
        args=args,
        cartridge=_CARTRIDGE,
        runner=ScriptedRunner({}),
        run_id="r1",
    )


def test_result_out_writes_the_graphs_result_and_creates_the_parent(tmp_path, capsys) -> None:
    out = tmp_path / "out" / "result.json"

    assert _review_diff_run(tmp_path, result_out=out) == 0

    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["verdict"] == _REVIEW_RESULT["verdict"]
    assert written["findings"] == _REVIEW_RESULT["findings"]
    assert f"  result  : {out}" in capsys.readouterr().out


def test_without_result_out_no_result_file_is_written(tmp_path, capsys) -> None:
    assert _review_diff_run(tmp_path, result_out=None) == 0

    assert "result  :" not in capsys.readouterr().out
    assert not list(tmp_path.rglob("result*.json"))


def test_a_failed_graph_writes_no_result_file(tmp_path) -> None:
    from graphs._contract import ContractViolation

    out = tmp_path / "result.json"

    assert _review_diff_run(tmp_path, result_out=out, raises=ContractViolation("bad")) == 1
    assert not out.exists()


# --- a single-graph run records one phases row, its gate decisions and ledger rows, and no manifest file --------------------

_GATED = {
    "kind": "comment_add",
    "risk": "low",
    "target": "docs/x.md",
    "evidence": [{"check": "c", "output": "ok"}],
    "rationale": "r",
    "suggested_action": "amend",
}


def _gated_run(tmp_path: Path, store) -> int:
    profile = tmp_path / "profile.yaml"
    profile.write_text("provider: test\n", encoding="utf-8")
    spec = SimpleNamespace(
        name="review-diff", graph_name="review-diff", needs=(), run=lambda graph_args, runner: {"proposals": [dict(_GATED)]}
    )
    args = SimpleNamespace(
        graph="review-diff",
        date="2026-09-25",
        runs_dir=tmp_path / "runs",
        ledger=tmp_path / "ledger.jsonl",
        provider_profile=str(profile),
        repo=None,
        assume="r",
        result_out=None,
    )
    return cli._run_graph(
        specs={"review-diff": spec},
        parser=_FakeParser(args),
        args=args,
        cartridge={**_CARTRIDGE, "write_kinds": {"comment_add": {"risk": "low", "ramp": "gated"}}},
        runner=ScriptedRunner({}),
        run_id="r1",
        store=store,
    )


@pytest.fixture
def run_store(tmp_path):
    from harness.store_migrate import open_store
    from harness.store_write import Store

    conn = open_store(f"sqlite:///{tmp_path}/single.db", "2026-09-25T00:00:00+00:00")
    yield Store(conn)
    conn.close()


def test_a_single_graph_run_writes_no_manifest_file(tmp_path, run_store) -> None:
    assert _gated_run(tmp_path, run_store) == 0

    assert not (tmp_path / "runs" / "r1.json").exists()
    assert list((tmp_path / "runs").rglob("*.json")) == []


def test_a_single_graph_run_leaves_one_phases_row_with_its_gate_decision_and_ledger_rows(tmp_path, run_store) -> None:
    from core import ledger

    assert _gated_run(tmp_path, run_store) == 0

    phases = run_store.conn.query_all("SELECT run_id, phase_id, record_json FROM phases")
    assert len(phases) == 1
    run_id, phase_id, record_json = phases[0]
    record = json.loads(record_json)
    assert (run_id, phase_id) == ("r1", "review-diff")
    assert record["manifest_record"]["run_id"] == "r1"
    assert record["manifest"] == "r1:review-diff"
    assert run_store.conn.query_one("SELECT COUNT(*) FROM gate_decisions WHERE run_id = 'r1'")[0] == 1
    in_file = [row for row in ledger.read(tmp_path / "ledger.jsonl") if row.get("run_id") == "r1"]
    assert len(in_file) > 0
    assert run_store.conn.query_one("SELECT COUNT(*) FROM ledger WHERE run_id = 'r1'")[0] == len(in_file)


def test_the_summary_names_the_run_store_not_a_manifest_file(tmp_path, run_store, capsys) -> None:
    assert _gated_run(tmp_path, run_store) == 0

    out = capsys.readouterr().out
    assert "  manifest: recorded in the run store under r1" in out
    assert "r1.json" not in out


def test_a_store_failure_on_the_phase_record_warns_and_still_exits_zero(tmp_path, run_store, monkeypatch, capsys) -> None:
    def boom(self, record, epoch=None):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(type(run_store), "record_phase", boom)

    assert _gated_run(tmp_path, run_store) == 0

    assert "store: could not record r1: database is locked" in capsys.readouterr().err


def test_without_a_store_a_single_graph_run_still_appends_the_ledger_and_exits_zero(tmp_path) -> None:
    assert _gated_run(tmp_path, None) == 0

    assert (tmp_path / "ledger.jsonl").is_file()
    assert not (tmp_path / "runs" / "r1.json").exists()


def test_the_parser_takes_result_out_and_defaults_it_to_none() -> None:
    parser = cli._build_parser({})
    base = ["sweep", "--team", "acme"]

    assert parser.parse_args(base).result_out is None
    assert parser.parse_args([*base, "--result-out", "x.json"]).result_out == "x.json"


def test_the_sigterm_handler_ignores_a_repeat_stops_children_and_exits_143(monkeypatch) -> None:
    stopped = []
    monkeypatch.setattr(cli, "_terminate_children", lambda: stopped.append(True))
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as excinfo:
            cli._exit_on_sigterm(signal.SIGTERM, None)
        ignored = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert excinfo.value.code == 143
    assert ignored is signal.SIG_IGN
    assert stopped == [True]


def test_child_pids_reads_every_threads_children_file(tmp_path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    (tmp_path / "1" / "children").write_text("12 13 ", encoding="ascii")
    (tmp_path / "2" / "children").write_text("14\n", encoding="ascii")

    assert cli._child_pids(tmp_path) == [12, 13, 14]
    assert cli._child_pids(tmp_path / "absent") == []


def test_a_real_sigterm_during_a_fan_out_stamps_the_end_stops_the_node_and_drops_the_queue(
    monkeypatch, tmp_path
) -> None:
    started, children = [], []

    def node(args, runner):
        started.append(args["run_id"])
        child = subprocess.Popen(["sleep", "30"])
        children.append(child)
        os.kill(os.getpid(), signal.SIGTERM)
        child.wait()
        time.sleep(0.3)
        return {"proposals": []}

    def fan_out(runner) -> int:
        invocations = [Invocation(id=f"t{i}", graph="g", args={}) for i in range(3)]
        invoke_graphs(invocations, specs={"g": SimpleNamespace(run=node)}, runner=runner, run_id="runS", max_parallel=1)
        return 0

    _store_run(monkeypatch, tmp_path, graph=fan_out)
    finish = cli._finish_store_run

    def finish_under_a_repeat_sigterm(*args) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        finish(*args)

    monkeypatch.setattr(cli, "_finish_store_run", finish_under_a_repeat_sigterm)
    before = signal.getsignal(signal.SIGTERM)
    start = time.monotonic()

    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 143
    assert time.monotonic() - start < 10
    assert children[0].returncode == -signal.SIGTERM
    assert started == ["runS:t0"]
    assert not cli._workers_alive()
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("error", 1)]
    assert signal.getsignal(signal.SIGTERM) is before


def test_an_early_return_before_the_run_starts_still_restores_the_handler(monkeypatch, tmp_path) -> None:
    _store_run(monkeypatch, tmp_path)

    def refuse(*a, **k):
        raise cli.CartridgeError("no cartridge")

    monkeypatch.setattr(cli, "resolve_cartridge", refuse)
    before = signal.getsignal(signal.SIGTERM)

    assert cli.main([]) == 1
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_run_ended_by_sigterm_stamps_ended_at_with_status_error_and_restores_the_handler(
    monkeypatch, tmp_path
) -> None:
    def terminated(runner):
        raise SystemExit(143)

    _store_run(monkeypatch, tmp_path, graph=terminated)
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 143
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("error", 1)]
    assert signal.getsignal(signal.SIGTERM) is before


def test_main_installs_the_handler_for_the_run_and_puts_the_previous_one_back(monkeypatch, tmp_path) -> None:
    seen = []
    _store_run(monkeypatch, tmp_path, graph=lambda runner: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
    before = signal.getsignal(signal.SIGTERM)

    assert cli.main([]) == 0

    assert seen == [cli._exit_on_sigterm]
    assert signal.getsignal(signal.SIGTERM) is before


# ── the run lease ────────────────────────────────────────────────────────────


def _lease_row(tmp_path: Path, name: str) -> tuple | None:
    rows = _rows(
        tmp_path / "cox.db", f"SELECT name, holder, epoch, heartbeat_at, expires_at FROM leases WHERE name = '{name}'"
    )
    return rows[0] if rows else None


def _hold_lease(tmp_path: Path, name: str, holder: str) -> None:
    from datetime import UTC, datetime

    from harness.store_lease import acquire
    from harness.store_migrate import open_store

    now = datetime.now(UTC).isoformat()
    conn = open_store(f"sqlite:///{tmp_path}/cox.db", now)
    assert acquire(conn, name, holder, now, 120).ok
    conn.close()


def test_a_run_refuses_to_start_while_another_run_holds_the_prefix_lease(monkeypatch, tmp_path, capsys) -> None:
    ran = []
    _store_run(monkeypatch, tmp_path, graph=lambda runner: ran.append(1) or 0)
    _hold_lease(tmp_path, "runs:runS", "runS-1")

    assert cli.main([]) == 2

    assert ran == []
    assert "run: runS-1 holds runs:runS; refusing to start runS" in capsys.readouterr().err
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("refused", 1)]
    assert _lease_row(tmp_path, "runs:runS")[1] == "runS-1"


def test_a_finished_run_leaves_its_lease_released_and_its_row_ended(monkeypatch, tmp_path) -> None:
    from datetime import UTC, datetime

    from harness.store_lease import lease_state

    _store_run(monkeypatch, tmp_path, graph=_three_calls)

    assert cli.main([]) == 0

    row = _lease_row(tmp_path, "runs:runS")
    assert (row[1], row[2]) == ("runS", 1)
    assert lease_state(row, datetime.now(UTC).isoformat()) == "expired"
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("ok", 1)]


def test_a_raising_graph_still_releases_the_lease(monkeypatch, tmp_path) -> None:
    from datetime import UTC, datetime

    from harness.store_lease import lease_state

    def boom(runner) -> int:
        raise RuntimeError("boom")

    _store_run(monkeypatch, tmp_path, graph=boom)

    with pytest.raises(RuntimeError):
        cli.main([])

    assert lease_state(_lease_row(tmp_path, "runs:runS"), datetime.now(UTC).isoformat()) == "expired"


def _expired(tmp_path: Path) -> bool:
    from datetime import UTC, datetime

    from harness.store_lease import lease_state

    return lease_state(_lease_row(tmp_path, "runs:runS"), datetime.now(UTC).isoformat()) == "expired"


@pytest.mark.parametrize("step", ["build_runner", "_lifecycle_worktree"])
def test_a_setup_step_that_raises_after_the_lease_still_releases_it_and_ends_the_run(
    monkeypatch, tmp_path, step
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError(f"{step} failed")

    working = cli._lifecycle_worktree
    _store_run(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, step, boom)

    with pytest.raises(RuntimeError):
        cli.main([])

    assert _expired(tmp_path)
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("error", 1)]

    # The corrected relaunch of the same prefix starts; the dead run does not hold it.
    relaunch = _Args(tmp_path, "runS-2")
    relaunch.worktree_root = str(tmp_path)
    _store_run(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_lifecycle_worktree", working)
    monkeypatch.setattr(cli, "_build_parser", lambda specs: _FakeParser(relaunch))

    assert cli.main([]) == 0


def _record_quiet_then_release(monkeypatch) -> list[str]:
    from harness import store_lease

    order: list[str] = []
    monkeypatch.setattr(cli, "_stop_children_until_quiet", lambda: order.append("quiet"))
    monkeypatch.setattr(cli, "release", lambda *a: order.append("release") or store_lease.release(*a))
    return order


def test_sigterm_during_the_graph_frees_the_lease_only_after_the_workers_are_quiet(monkeypatch, tmp_path) -> None:
    def terminated(runner):
        raise SystemExit(143)

    _store_run(monkeypatch, tmp_path, graph=terminated)
    order = _record_quiet_then_release(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 143
    assert order[:2] == ["quiet", "release"]
    assert _expired(tmp_path)


def test_sigterm_during_setup_frees_the_lease_after_the_workers_are_quiet_and_ends_the_run(
    monkeypatch, tmp_path
) -> None:
    def terminated(**kwargs):
        raise SystemExit(143)

    _store_run(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "build_runner", terminated)
    order = _record_quiet_then_release(monkeypatch)

    with pytest.raises(SystemExit):
        cli.main([])

    assert order[:2] == ["quiet", "release"]
    assert _expired(tmp_path)
    assert _rows(tmp_path / "cox.db", "SELECT status, ended_at IS NOT NULL FROM runs") == [("error", 1)]


def test_a_release_error_warns_and_leaves_the_exit_code(monkeypatch, tmp_path, capsys) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("store gone")

    _store_run(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "release", boom)

    assert cli.main([]) == 0
    assert "lease: could not release runs:runS: store gone" in capsys.readouterr().err


def test_the_epic_path_hands_run_epic_the_epoch_and_the_lease_name(monkeypatch, tmp_path) -> None:
    import harness.epic

    seen = {}

    def fake_run_epic(**kwargs):
        seen.update(epoch=kwargs["epoch"], lease_name=kwargs["lease_name"])
        return {}

    runner = ScriptedRunner({"plan": {}, "build": {}, "review": {}})
    args = _Args(tmp_path, "runS-2")
    args.worktree_root = str(tmp_path)
    args.graph = "epic"
    args.initiative = "demo"
    args.repo = str(tmp_path)
    args.max_parallel = 1
    args.ledger = tmp_path / "ledger.jsonl"
    args.assume = False
    args.fix_attempts = 0
    args.resume_from = None
    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **k: (_CARTRIDGE, {}))
    monkeypatch.setattr(cli.workstore, "read_initiative", lambda name: {})
    monkeypatch.setattr(cli, "_provider_profile_scope", lambda profile: "acme")
    monkeypatch.setattr(harness.epic, "run_epic", fake_run_epic)

    assert cli.main([]) == 0

    assert seen == {"epoch": 1, "lease_name": "runs:runS"}


# ── trace compaction at run end ──────────────────────────────────────────────


def test_trace_helpers_read_the_path_and_the_events_from_literals() -> None:
    assert cli._trace_path('{"trace": "/a/b.jsonl"}') == Path("/a/b.jsonl")
    assert cli._trace_path({"trace": "/a/b.jsonl"}) == Path("/a/b.jsonl")
    assert [cli._trace_path(d) for d in (None, "{", "[]", {"trace": ""}, {"x": 1})] == [None] * 5
    assert cli._read_events('{"a": 1}\nnot json\n\n[2]\n{"b": 2}\n') == [{"a": 1}, {"b": 2}]


def test_a_run_compacts_its_trace_files_into_the_trace_store_and_removes_the_trace_dir(monkeypatch, tmp_path) -> None:
    pytest.importorskip("zstandard")
    from harness import store_traces

    seen = []

    def graph(runner) -> int:
        _three_calls(runner)
        seen.append(sorted(p.name for p in (tmp_path / "runS-trace").iterdir()))  # the live views' contract
        return 0

    _store_run(monkeypatch, tmp_path, graph=graph, runner_cls=_TraceRunner)

    assert cli.main([]) == 0

    assert seen == [["build-2.jsonl", "plan-1.jsonl", "review-3.jsonl"]]
    assert not (tmp_path / "runS-trace").exists()
    for call_id in ("call-1", "call-2", "call-3"):
        assert store_traces.read_call(tmp_path / "traces", "runS", call_id) == [{"type": "system"}, {"type": "result"}]
    assert (tmp_path / "traces" / "2026" / "09" / "25" / "runS.jsonl.zst").is_file()


def test_a_failing_append_leaves_the_files_and_the_exit_code_unchanged(monkeypatch, tmp_path, capsys) -> None:
    from harness import store_traces

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_traces, "append_call", boom)
    _store_run(monkeypatch, tmp_path, graph=_three_calls, runner_cls=_TraceRunner)

    assert cli.main([]) == 0

    assert len(list((tmp_path / "runS-trace").iterdir())) == 3
    assert capsys.readouterr().err.count("traces: could not compact") == 3


def test_without_zstandard_compaction_warns_once_and_leaves_the_files(monkeypatch, tmp_path, capsys) -> None:
    from harness import store_traces

    def unavailable(*args, **kwargs):
        raise store_traces.TracesUnavailable("reading or writing traces needs zstandard")

    monkeypatch.setattr(store_traces, "append_call", unavailable)
    _store_run(monkeypatch, tmp_path, graph=_three_calls, runner_cls=_TraceRunner)

    assert cli.main([]) == 0

    assert len(list((tmp_path / "runS-trace").iterdir())) == 3
    assert capsys.readouterr().err.count("traces: not compacted") == 1
