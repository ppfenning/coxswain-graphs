"""main() leaves usage.json on disk on every exit path, not only the happy one."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import harness.cli as cli


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
