"""harness.CORE_SCHEMA is enforced against core.SCHEMA_VERSION at startup, and
stamped onto ledger rows the harness itself constructs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import harness
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


def _patch_common(monkeypatch, args, runner, *, build_runner_calls: list | None = None):
    monkeypatch.setattr(cli, "discover", lambda: {})
    monkeypatch.setattr(cli, "_build_parser", lambda specs: _FakeParser(args))
    monkeypatch.setattr(cli, "resolve_cartridge", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(cli, "role_skill_bodies", lambda *a, **k: {})

    def _build_runner(**k):
        if build_runner_calls is not None:
            build_runner_calls.append(k)
        return runner

    monkeypatch.setattr(cli, "build_runner", _build_runner)
    monkeypatch.setattr(cli, "remove_worktree", lambda repo, worktree: (True, "removed (fake)"))
    monkeypatch.setattr(cli, "keep_worktree", lambda repo, worktree, root, run_id: (True, "kept (fake)"))


def test_harness_core_schema_is_the_declared_version() -> None:
    assert harness.CORE_SCHEMA == "1.0"


def test_equal_versions_proceed_silently(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli.core, "SCHEMA_VERSION", "1.0")
    args = _Args(tmp_path, "run-equal")
    args.worktree_root = str(tmp_path)
    calls: list = []
    _patch_common(monkeypatch, args, SimpleNamespace(calls=[]), build_runner_calls=calls)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    assert cli.main([]) == 0
    assert len(calls) == 1
    assert "schema" not in capsys.readouterr().out.lower()


def test_a_major_mismatch_refuses_before_any_node_launches(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli.core, "SCHEMA_VERSION", "2.0")
    args = _Args(tmp_path, "run-major")
    args.worktree_root = str(tmp_path)
    calls: list = []
    _patch_common(monkeypatch, args, SimpleNamespace(calls=[]), build_runner_calls=calls)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    assert cli.main([]) == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "core schema 2.0" in err
    assert "CORE_SCHEMA 1.0" in err
    assert "coxswain-graphs" in err


def test_a_minor_mismatch_warns_and_proceeds(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli.core, "SCHEMA_VERSION", "1.7")
    args = _Args(tmp_path, "run-minor")
    args.worktree_root = str(tmp_path)
    calls: list = []
    _patch_common(monkeypatch, args, SimpleNamespace(calls=[]), build_runner_calls=calls)
    monkeypatch.setattr(cli, "_run_graph", lambda **k: 0)

    assert cli.main([]) == 0
    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "core schema 1.7" in out
    assert "CORE_SCHEMA 1.0" in out


def test_a_ledger_row_built_here_carries_the_installed_core_schema_version(monkeypatch) -> None:
    monkeypatch.setattr(cli.core, "SCHEMA_VERSION", "9.9")
    captured: list[dict] = []
    monkeypatch.setattr(cli.ledger, "append_observation", lambda row, path: captured.append(row))
    result = {
        "run_id": "r1",
        "triaged": [
            {
                "verified": True,
                "verification": {"trap_held": False},
                "classification": {"runbook_entry": "doc.entry"},
            }
        ],
    }
    cartridge = {"team": "acme", "write_kinds": {"doc_update": {"risk": "low"}}}

    filed = cli._observe_trap_failures(
        result,
        graph_name="triage-propose",
        ts="2026-09-16T00:00:00Z",
        cartridge=cartridge,
        provider_profile="acme",
        ledger_path="ledger.jsonl",
    )

    assert filed == 1
    assert captured[0]["schema"] == "9.9"
