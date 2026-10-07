"""A check can carry its own `timeout`; `run_checks` honours it over the argument.

The command runner is faked: nothing here launches a real command or sleeps.
"""

from __future__ import annotations

import subprocess

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


def _fake_run(seen: list):
    def fake(cmd, **kwargs):
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout="1 passed\n", stderr="")

    return fake


def test_a_per_check_timeout_overrides_the_argument(tmp_path, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr("harness.checks.subprocess.run", _fake_run(seen))
    run_checks(tmp_path, [{"name": "slow", "cmd": "x", "timeout": 7}], timeout=600)
    assert seen == [7]


def test_a_check_with_no_timeout_falls_back_to_the_argument(tmp_path, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr("harness.checks.subprocess.run", _fake_run(seen))
    run_checks(tmp_path, [{"name": "quick", "cmd": "x"}], timeout=45)
    assert seen == [45]


@pytest.mark.parametrize("bad", [0, -5, "30", 2.5])
def test_a_bad_per_check_timeout_is_refused_naming_check_and_value(tmp_path, monkeypatch, bad) -> None:
    seen: list = []
    monkeypatch.setattr("harness.checks.subprocess.run", _fake_run(seen))
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
