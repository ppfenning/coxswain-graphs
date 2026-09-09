"""main() leaves usage.json on disk on every exit path, not only the happy one."""

from __future__ import annotations

import json
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


def _patch_common(monkeypatch, args: _Args, runner) -> None:
    monkeypatch.setattr(cli, "discover", lambda: {})
    monkeypatch.setattr(cli, "_build_parser", lambda specs: _FakeParser(args))
    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "role_skill_bodies", lambda *a, **k: {})
    monkeypatch.setattr(cli, "build_runner", lambda **k: runner)


def test_a_raise_inside_the_dispatched_graph_still_leaves_usage_json(monkeypatch, tmp_path) -> None:
    run_id = "runX"
    runner = SimpleNamespace(calls=[])
    args = _Args(tmp_path, run_id)

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
    _patch_common(monkeypatch, args, runner)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    result = cli.main([])

    assert result == 0
    assert runner.closed is True
    written = json.loads((tmp_path / f"{run_id}.usage.json").read_text(encoding="utf-8"))
    assert written["summary"]["calls"] == 1
