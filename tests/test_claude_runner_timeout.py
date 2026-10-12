"""A claude run that times out takes its tools with it, so the call is bounded."""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import pytest

import runner.claude_code_runner as ccr
from runner.claude_code_runner import ClaudeCodeRunner, _NodeTimeout

PROFILE = {
    "profile": "fake-claude-code",
    "runner": "claude-code",
    "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
    "tools": {"build": ["Read"]},
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _stand_in(tmp_path: Path, tool: str) -> tuple[Path, Path]:
    """A claude that starts `tool` in the background, records its pid, then hangs."""
    pidfile = tmp_path / "tool.pid"
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\n{tool} &\necho $! > {pidfile}\nsleep 600\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, pidfile


def _gone(pid: int) -> bool:
    """A killed orphan may linger as a zombie until init reaps it; a zombie counts as gone."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    return stat_line.rsplit(")", 1)[1].split()[0] == "Z"


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return _gone(pid)


def _reap_tool(pidfile: Path) -> None:
    if pidfile.exists():
        with suppress(ProcessLookupError):
            os.kill(int(pidfile.read_text(encoding="utf-8")), signal.SIGKILL)


def test_a_timeout_kills_a_tool_that_holds_the_pipes_open(tmp_path: Path) -> None:
    script, pidfile = _stand_in(tmp_path, "sleep 600")
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, timeout=2)

    start = time.monotonic()
    try:
        with pytest.raises(_NodeTimeout, match="did not finish within 2s"):
            runner.run(role="plan", schema=SCHEMA, prompt="go")
        elapsed = time.monotonic() - start
        tool_killed = _gone_within(int(pidfile.read_text(encoding="utf-8")), 2)
    finally:
        _reap_tool(pidfile)

    assert elapsed < 7
    assert tool_killed, "the tool claude started outlived the timeout: the group was not killed"


@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid is Linux-only")
def test_a_tool_that_left_the_group_cannot_hold_the_call_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ccr, "_REAP_SECONDS", 1)
    script, pidfile = _stand_in(tmp_path, "setsid sleep 600")
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, timeout=2)

    start = time.monotonic()
    try:
        with pytest.raises(_NodeTimeout, match="did not finish within 2s"):
            runner.run(role="plan", schema=SCHEMA, prompt="go")
        elapsed = time.monotonic() - start
        tool_survived = not _gone(int(pidfile.read_text(encoding="utf-8")))
    finally:
        _reap_tool(pidfile)

    assert tool_survived, "the tool must outlive the group kill, or the drain fallback was never reached"
    assert elapsed < 7


def test_any_other_error_kills_the_group_closes_the_pipes_and_reraises(tmp_path: Path, monkeypatch) -> None:
    script, pidfile = _stand_in(tmp_path, "sleep 600")
    runner = ClaudeCodeRunner(PROFILE, claude_bin=str(script), cwd=tmp_path, timeout=60)
    real = subprocess.Popen
    started: list[subprocess.Popen[str]] = []

    class Interrupted(real):
        def communicate(self, input=None, timeout=None):
            started.append(self)
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise RuntimeError("interrupted mid-call")

    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, *a, **kw: (Interrupted if argv[0] == str(script) else real)(argv, *a, **kw)
    )

    try:
        with pytest.raises(RuntimeError, match="interrupted mid-call"):
            runner.run(role="plan", schema=SCHEMA, prompt="go")
        tool_killed = _gone_within(int(pidfile.read_text(encoding="utf-8")), 2)
    finally:
        _reap_tool(pidfile)

    (proc,) = started
    assert proc.returncode == -signal.SIGKILL
    assert proc.stdin.closed and proc.stdout.closed and proc.stderr.closed
    assert tool_killed
