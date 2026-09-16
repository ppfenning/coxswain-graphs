"""sweep: plan, apply, verify, then the ordinary review pair over one diff."""

from __future__ import annotations

import subprocess
from pathlib import Path

from graphs.ops import sweep
from runner import ScriptedRunner

CHARTER_APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
ADVERSARY_APPROVE = {"verdict": "approve", "objections": [], "strongest_objection": ""}


def _seed_repo(path: Path, name: str = "old.txt", text: str = "old_name\n") -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / name).write_text(text)
    subprocess.run(["git", "add", name], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _diff_modifying(path: Path, name: str, new_text: str) -> str:
    """A real unified diff for changing `name`'s already-committed content, applied against the same HEAD."""
    (path / name).write_text(new_text)
    diff = subprocess.run(["git", "diff", "--", name], cwd=path, capture_output=True, text=True).stdout
    subprocess.run(["git", "checkout", "--", name], cwd=path, check=True)
    return diff


def test_sweep_plan_schema_has_the_fields_section_1_names() -> None:
    assert sweep.SWEEP_PLAN_SCHEMA["required"] == ["map", "exceptions", "application", "postcondition"]
    assert sweep.SWEEP_PLAN_SCHEMA["properties"]["application"]["properties"]["kind"]["enum"] == ["script", "build"]


def test_sweep_verify_passes_when_every_pattern_greps_to_zero_and_checks_pass() -> None:
    applied = {"applied": True, "postcondition": [{"pattern": "old_name", "matches": []}], "checks_passed": True}
    assert sweep.sweep_verify(applied) == {"verdict": "pass", "failures": []}


def test_sweep_verify_fails_and_names_the_pattern_and_first_three_matches() -> None:
    applied = {
        "applied": True,
        "postcondition": [{"pattern": "old_name", "matches": ["a.py:1", "b.py:2", "c.py:3", "d.py:4"]}],
        "checks_passed": True,
    }
    result = sweep.sweep_verify(applied)
    assert result["verdict"] == "fail"
    assert result["failures"] == ["old_name: a.py:1, b.py:2, c.py:3"]


def test_sweep_verify_fails_when_the_configured_checks_do_not_pass() -> None:
    applied = {
        "applied": True,
        "postcondition": [{"pattern": "old_name", "matches": []}],
        "checks_passed": False,
        "checks": [{"check": "checks:pytest", "output": "fail — (exit 1)"}],
    }
    result = sweep.sweep_verify(applied)
    assert result["verdict"] == "fail"
    assert result["failures"] == ["configured checks did not pass"]


def test_ceilings_come_from_the_cartridge_not_a_hardcoded_number(tmp_path: Path, cartridge: dict) -> None:
    plan_scripted = ScriptedRunner({"sweep_plan": {"map": [], "exceptions": [], "application": {}, "postcondition": []}})
    sweep.sweep_plan(cartridge, plan_scripted, "rename old_name to new_name everywhere")
    assert plan_scripted.calls[0]["role"] == "sweep_plan"
    assert plan_scripted.calls[0]["budget_usd"] is None

    _seed_repo(tmp_path)
    diff = _diff_modifying(tmp_path, "old.txt", "new_name\n")
    build_response = {"patch": diff, "summary": "s", "files_touched": ["old.txt"], "commands_run": []}
    build_scripted = ScriptedRunner({"sweep_build": build_response})
    plan = {"application": {"kind": "build", "body": "brief"}, "map": [], "exceptions": [], "postcondition": []}

    applied = sweep.sweep_apply(cartridge, build_scripted, str(tmp_path), plan)

    assert applied["applied"], applied
    assert build_scripted.calls[0]["role"] == "sweep_build"
    assert build_scripted.calls[0]["budget_usd"] is None
    source = Path(sweep.__file__).read_text(encoding="utf-8")
    assert "budget_usd=" not in source


def test_sweep_apply_runs_the_script_with_no_model_call(tmp_path: Path, cartridge: dict) -> None:
    _seed_repo(tmp_path)
    plan = {
        "application": {"kind": "script", "body": "printf 'new_name\\n' > old.txt"},
        "postcondition": ["old_name"],
        "exceptions": [],
    }
    scripted = ScriptedRunner({})

    applied = sweep.sweep_apply(cartridge, scripted, str(tmp_path), plan)

    assert applied["applied"], applied
    assert scripted.calls == []
    assert "new_name" in applied["diff"]
    assert applied["postcondition"] == [{"pattern": "old_name", "matches": []}]


def test_run_stops_before_review_when_verify_fails(tmp_path: Path, cartridge: dict) -> None:
    _seed_repo(tmp_path)
    plan = {"map": [], "exceptions": [], "application": {"kind": "script", "body": "true"}, "postcondition": ["old_name"]}
    scripted = ScriptedRunner({"sweep_plan": plan})
    args = {"run_id": "r1", "date": "2026-09-15", "cartridge": cartridge, "repo": str(tmp_path), "idea": "rename old_name"}

    result = sweep.run(args, scripted)

    assert result["verify"]["verdict"] == "fail"
    assert result["verify"]["failures"] == ["old_name: old.txt:1"]
    assert result["review"] is None
    assert {c["role"] for c in scripted.calls} == {"sweep_plan"}


def test_run_delegates_the_review_pair_to_review_entry(tmp_path: Path, cartridge: dict) -> None:
    _seed_repo(tmp_path)
    plan = {
        "map": [],
        "exceptions": [],
        "application": {"kind": "script", "body": "printf 'new_name\\n' > old.txt"},
        "postcondition": ["old_name"],
    }
    scripted = ScriptedRunner({"sweep_plan": plan, "review_charter": CHARTER_APPROVE, "review_adversary": ADVERSARY_APPROVE})
    args = {"run_id": "r1", "date": "2026-09-15", "cartridge": cartridge, "repo": str(tmp_path), "idea": "rename old_name"}

    result = sweep.run(args, scripted)

    assert result["verify"]["verdict"] == "pass"
    assert result["review"]["verdict"] == "approve"
    assert {c["role"] for c in scripted.calls} == {"sweep_plan", "review_charter", "review_adversary"}
