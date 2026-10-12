"""A check can carry its own `timeout`; `run_checks` honours it over the argument.

The command runner is faked, except the one test that proves a timeout kills a backgrounded grandchild.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from pathlib import Path

import pytest

from harness.checks import fixable_checks, run_checks
from harness.epic import _Ctx


class _ExplodingRunner:
    """A model call that fails the test if anything touches it."""

    def __getattr__(self, name):
        pytest.fail(f"model runner touched: {name}")

    def __call__(self, *args, **kwargs):
        pytest.fail("model call made")


def _ctx(tmp_path, cartridge_checks=(), repo_checks=()) -> _Ctx:
    return _Ctx(
        repo=tmp_path,
        cartridge={"landing_areas": {"checks": list(cartridge_checks)}},
        runner=_ExplodingRunner(),
        specs={},
        run_id="r1",
        date="2026-10-07",
        max_parallel=1,
        ledger_path=tmp_path / "ledger",
        provider_profile="p",
        runs_dir=tmp_path / "runs",
        worktree_root=tmp_path / "wt",
        assume=None,
        fix_attempts=None,
        initiative_id="i",
        default_ref="main",
        repo_checks=list(repo_checks),
    )


def _fake_popen(seen: list):
    class Fake:
        returncode = 0
        pid = 1

        def __init__(self, cmd, **kwargs):
            pass

        def communicate(self, timeout=None):
            seen.append(timeout)
            return "1 passed\n", ""

    return Fake


def test_a_per_check_timeout_overrides_the_argument(tmp_path, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr("harness.checks.subprocess.Popen", _fake_popen(seen))
    run_checks(tmp_path, [{"name": "slow", "cmd": "x", "timeout": 7}], timeout=600)
    assert seen == [7]


def test_a_check_with_no_timeout_falls_back_to_the_argument(tmp_path, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr("harness.checks.subprocess.Popen", _fake_popen(seen))
    run_checks(tmp_path, [{"name": "quick", "cmd": "x"}], timeout=45)
    assert seen == [45]


def _is_dead(pid: int) -> bool:
    """Gone, or a zombie waiting for its reaper: either way it no longer holds anything open."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] == "Z"


def test_a_timeout_kills_a_backgrounded_grandchild_holding_the_pipes(tmp_path) -> None:
    cmd = "echo 3 passed\nsleep 600 &\necho $! > grandchild.pid\nwait"
    started = time.monotonic()
    [result] = run_checks(tmp_path, [{"name": "bg", "cmd": cmd}], timeout=1)
    elapsed = time.monotonic() - started
    pid = int((tmp_path / "grandchild.pid").read_text())
    try:
        deadline = time.monotonic() + 5
        while not _is_dead(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _is_dead(pid), "the backgrounded sleep outlived the timeout"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert elapsed < 1 + 5
    assert result["outcome"] == "timed_out"
    assert result["output_tail"].count("3 passed") == 1
    assert result["passed"] is False
    assert result["exit_code"] is None


@pytest.mark.parametrize("bad", [0, -5, "30", 2.5])
def test_a_bad_per_check_timeout_is_refused_naming_check_and_value(tmp_path, monkeypatch, bad) -> None:
    seen: list = []
    monkeypatch.setattr("harness.checks.subprocess.Popen", _fake_popen(seen))
    with pytest.raises(ValueError, match=rf"'slow'.*{bad!r}"):
        run_checks(tmp_path, [{"name": "slow", "cmd": "x", "timeout": bad}])
    assert seen == []


def test_fixable_checks_keeps_the_timeout_in_the_fix_entry() -> None:
    checks = [
        {"name": "fmt", "cmd": "x", "fix": "y", "timeout": 90},
        {"name": "lint", "cmd": "x", "fix": "z"},
    ]
    assert fixable_checks(checks) == [
        {"name": "fmt", "cmd": "y", "timeout": 90},
        {"name": "lint", "cmd": "z"},
    ]


@pytest.mark.parametrize("bad", [0, -5, "soon", 2.5])
def test_a_ctx_with_a_bad_check_timeout_is_refused_before_any_model_call(tmp_path, bad) -> None:
    with pytest.raises(ValueError, match=r"'slow'"):
        _ctx(tmp_path, cartridge_checks=[{"name": "slow", "cmd": "x", "timeout": bad}])


def test_a_ctx_with_a_bad_timeout_in_a_repo_check_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match=r"'repo-slow'"):
        _ctx(tmp_path, repo_checks=[{"name": "repo-slow", "cmd": "y", "timeout": 0}])


def test_a_ctx_with_valid_timeouts_builds(tmp_path) -> None:
    checks = [{"name": "slow", "cmd": "x", "timeout": 7}, {"name": "quick", "cmd": "y"}]
    assert _ctx(tmp_path, cartridge_checks=checks).checks == checks
