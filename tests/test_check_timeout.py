"""A check can carry its own `timeout`; `run_checks` honours it over the argument.

The command runner is faked: nothing here launches a real command or sleeps.
"""

from __future__ import annotations

import subprocess

import pytest

from harness.checks import fixable_checks, run_checks


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
