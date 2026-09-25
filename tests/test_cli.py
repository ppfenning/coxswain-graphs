"""main() leaves usage.json on disk on every exit path, not only the happy one."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import harness.cli as cli
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


def test_a_raise_inside_the_dispatched_graph_still_leaves_usage_json(monkeypatch, tmp_path) -> None:
    run_id = "runX"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)

    def _boom(**kwargs):
        row = {"role": "build", "model": "claude-x", "cost_usd": 1.0, "turns": 1, "ok": True}
        (tmp_path / f"{run_id}.calls.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        raise RuntimeError("boom")

    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", _boom)

    with pytest.raises(RuntimeError):
        cli.main([])

    written = json.loads((tmp_path / f"{run_id}.usage.json").read_text(encoding="utf-8"))
    assert written["summary"]["calls"] == 1


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


def test_close_runs_after_record_usage_so_a_clearing_close_still_leaves_full_usage(monkeypatch, tmp_path) -> None:
    run_id = "runY"

    class _Runner:
        def __init__(self) -> None:
            self.calls = [{"role": "build", "model": "claude-x", "cost_usd": 1.0, "turns": 1, "ok": True}]
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self.calls = []

    runner = _Runner()
    args = _Args(tmp_path, run_id)
    args.worktree_root = str(tmp_path)
    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    result = cli.main([])

    assert result == 0
    assert runner.closed is True
    written = json.loads((tmp_path / f"{run_id}.usage.json").read_text(encoding="utf-8"))
    assert written["summary"]["calls"] == 1


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
        call = {"id": f"call-{len(self.calls)}", "role": kwargs["role"], "ok": True}
        self.store.record_call(call, run_id=self.run_id, seq=len(self.calls))
        return result


def _store_run(monkeypatch, tmp_path, *, profile_url=None, graph=lambda runner: 0):
    """Stage `main` over a scripted runner; `graph(runner)` stands in for the dispatched graph."""
    runner = _StoreRunner({"plan": {}, "build": {}, "review": {}})
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


def test_the_parser_takes_result_out_and_defaults_it_to_none() -> None:
    parser = cli._build_parser({})
    base = ["sweep", "--team", "acme"]

    assert parser.parse_args(base).result_out is None
    assert parser.parse_args([*base, "--result-out", "x.json"]).result_out == "x.json"
