"""The exit summary's courier send: additive, best-effort, subprocess-only.

Reuses the driver fixture idiom from `tests/test_epic_driver.py` rather than
importing it, since the tests directory carries no package `__init__.py` and
cross-file test imports are not this suite's convention.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from graphs._spec import GraphSpec
from graphs.delivery import lifecycle_propose, phase_validate
from harness import courier_adapter
from harness.epic import run_epic
from harness.store_migrate import open_store
from harness.store_write import Store
from runner.claude_code_runner import files_touched_from_patch
from runner.protocol import RunnerError

SHA = "sha-fixture"
PROFILE = "anthropic-default"

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
CHUNK_OK = {"satisfied": True, "gaps": [], "reasoning": "the description is satisfied"}
GOAL_MET = {
    "goal_met": True, "partial": False, "missing": [],
    "quarantine_blocks_dependents": False, "reasoning": "the pieces add up",
}

CHECK_SCRIPT = """\
import pathlib
import sys

bad = sorted(p.name for p in pathlib.Path(".").glob("*.txt") if p.read_text().strip() != "ok")
print(f"{len(bad)} failed" if bad else "1 passed")
sys.exit(1 if bad else 0)
"""

FAKE_COX = """\
#!/bin/sh
echo "$@" >> "$COX_LOG"
exit 0
"""


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@invalid", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}"
    return proc.stdout.strip()


def new_file_patch(name: str, content: str = "ok") -> str:
    return (
        f"diff --git a/{name} b/{name}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{name}\n"
        "@@ -0,0 +1 @@\n"
        f"+{content}\n"
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    (root / "check.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "base", cwd=root)
    return root


@pytest.fixture
def cart(tmp_path) -> dict:
    return {
        "team": "acme",
        "cartridge_sha": SHA,
        "context": [],
        "skills": {
            "plan": "acme-skills:plan",
            "build": "acme-skills:build",
            "review_charter": "acme-skills:review",
            "validate_chunk": "acme-skills:validate-chunk",
            "validate_phase": "acme-skills:validate-phase",
            "work_state_arm": "acme-skills:work-state",
        },
        "write_kinds": {
            "draft_pr_create": {"risk": "low", "ramp": "eligible", "apply_arm": "shell"},
            "merge_stack": {"risk": "high", "ramp": "eligible", "apply_arm": "shell"},
            "stack_rebase": {"risk": "high", "ramp": "eligible", "apply_arm": "shell"},
            "state_move": {"risk": "low", "ramp": "deferred", "apply_arm": "work_state_arm"},
            "self_modification": {"risk": "high", "ramp": "never", "apply_arm": "pr"},
            "consolidate": {"risk": "low", "ramp": "deferred"},
        },
        "policy": {"graduation_n": 3, "regraduation_multiplier": 2, "caps": {}},
        "landing_areas": {
            "worktree_root": str(tmp_path / "worktrees"),
            "checks": [{"name": "state", "cmd": f"{sys.executable} check.py"}],
        },
    }


def initiative(*, done: bool = False) -> dict:
    """A single-task initiative: `done=True` puts the task past this run already."""
    return {
        "id": "demo-initiative",
        "title": "demo",
        "body": "make the vendor join measurable end to end",
        "phases": ["p1-foundations"],
        "items": [
            {"id": "t1-probe", "phase": "p1-foundations", "state": "done" if done else "ready",
             "needs": [], "surfaces": [], "title": "schema probe", "body": "read the vendor schema"},
        ],
    }


class Runner:
    def __init__(self, patch: str) -> None:
        self.patch = patch
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def run(self, *, role, tier=None, hints=None, schema, prompt, context=(), thread=None, budget_usd=None, task=None):
        with self.lock:
            self.calls.append({"role": role, "prompt": prompt})
        if role == "plan":
            return {"steps": ["do it"], "files_expected": ["x.txt"], "out_of_scope": []}
        if role == "build":
            return {
                "patch": self.patch,
                "summary": "built t1-probe",
                "files_touched": files_touched_from_patch(self.patch),
                "commands_run": [],
            }
        if role == "review_charter":
            return dict(APPROVE)
        if role == "validate_chunk":
            return dict(CHUNK_OK)
        if role == "validate_phase":
            return dict(GOAL_MET)
        if role == "work_state_arm":
            return {"applied": True, "detail": "state moved"}
        if role == "style_pass":
            return {"patch": ""}
        raise RunnerError(f"no scripted response for role '{role}'")


SPECS = {
    "lifecycle": GraphSpec(name="lifecycle", graph_name="lifecycle-propose", run=lifecycle_propose.run),
    "validate": phase_validate.SPEC,
}


def drive(repo, cart, tmp_path, *, runner, work):
    return run_epic(
        store=Store(open_store("sqlite:///:memory:", datetime.now(UTC).isoformat())),
        initiative=work,
        repo=repo,
        cartridge=cart,
        runner=runner,
        specs=SPECS,
        run_id="epic-1",
        date="2026-09-17",
        max_parallel=3,
        ledger_path=tmp_path / "ledger.jsonl",
        provider_profile=PROFILE,
        runs_dir=tmp_path / "runs",
        worktree_root=cart["landing_areas"]["worktree_root"],
        assume="a",
    )


def _fake_cox(bin_dir: Path, log_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "cox"
    script.write_text(FAKE_COX, encoding="utf-8")
    script.chmod(0o755)
    log_path.write_text("", encoding="utf-8")


def test_one_approved_not_landed_task_sends_one_courier_record(repo, cart, tmp_path, monkeypatch) -> None:
    """A single task that merges clean is still unlanded this run (only `cox runs land` writes `done`)."""
    log_path = tmp_path / "cox.log"
    _fake_cox(tmp_path / "bin", log_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("COX_LOG", str(log_path))

    result = drive(repo, cart, tmp_path, runner=Runner(new_file_patch("t1-probe.txt")), work=initiative())

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_command = f"cox runs land epic-1 --repo {repo} --task t1-probe --apply"
    assert len(lines) == 1
    assert "coxswain://task/t1-probe" in lines[0] and expected_command in lines[0]
    assert f"approved but not landed: t1-probe — {expected_command}" in result["exit_summary"]


def test_a_task_already_landed_sends_no_courier_record(repo, cart, tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "cox.log"
    _fake_cox(tmp_path / "bin", log_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("COX_LOG", str(log_path))

    result = drive(repo, cart, tmp_path, runner=Runner(new_file_patch("t1-probe.txt")), work=initiative(done=True))

    assert log_path.read_text(encoding="utf-8").strip() == ""
    assert result["exit_summary"] == []


def test_missing_cox_on_path_does_not_raise(repo, cart, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(courier_adapter.shutil, "which", lambda name: None)

    result = drive(repo, cart, tmp_path, runner=Runner(new_file_patch("t1-probe.txt")), work=initiative())

    expected_command = f"cox runs land epic-1 --repo {repo} --task t1-probe --apply"
    assert f"approved but not landed: t1-probe — {expected_command}" in result["exit_summary"]
