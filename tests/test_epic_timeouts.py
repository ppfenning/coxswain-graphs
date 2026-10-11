"""Git push, `gh pr create` and `pytest --collect-only` are bounded; a timeout is a value, not a raise."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from harness import epic


def _timing_out(calls: list[dict]):
    def fake(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    return fake


def test_git_push_times_out_after_120(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(epic.subprocess, "run", _timing_out(calls))
    result = epic._git("-C", "/r", "push", "origin", "b:refs/heads/x")
    assert result == (False, "git -C /r push origin b:refs/heads/x timed out after 120s")
    assert calls[0]["timeout"] == 120


def test_gh_pr_create_times_out_after_120(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(epic.subprocess, "run", _timing_out(calls))
    ctx = SimpleNamespace(repo=Path("/r"))
    result = epic._open_review_pr(ctx, title="t", body="b", head="h", base="main")
    assert result == (False, "gh pr create timed out after 120s")
    assert calls[0]["timeout"] == 120


def test_collect_only_times_out_after_300(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(epic.subprocess, "run", _timing_out(calls))
    assert epic._collected_ids(Path("/w")) is None
    assert calls[0]["timeout"] == 300


def test_git_success_tuple_is_unchanged(monkeypatch):
    def fake(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(epic.subprocess, "run", fake)
    assert epic._git("status") == (True, "ok")
