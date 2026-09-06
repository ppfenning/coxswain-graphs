"""review-diff: a standalone review entry for an arbitrary diff, repo and ref."""

from __future__ import annotations

import subprocess
from pathlib import Path

from graphs.delivery import review_entry
from harness.cli import _build_parser
from harness.registry import discover
from runner import ScriptedRunner

CHARTER_APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}


def _seed_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _diff_adding(path: Path, name: str, text: str) -> str:
    """A real unified diff for adding `name`, produced by git itself, same recipe as test_worktree.py."""
    (path / name).write_text(text)
    diff = subprocess.run(
        ["git", "diff", "--no-index", "--", "/dev/null", name], cwd=path, capture_output=True, text=True
    ).stdout
    (path / name).unlink()
    return diff


def test_review_request_shapes_a_diff_repo_and_ref() -> None:
    assert review_entry.review_request("diff-text", "org/repo", "main") == {
        "diff": "diff-text",
        "repo": "org/repo",
        "ref": "main",
    }


def test_run_returns_a_verdict_and_postable_findings_from_a_stub_runner(cartridge) -> None:
    charter = {
        "verdict": "revise",
        "findings": [{"charter_principle": "A3", "detail": "mutates the argument", "file": "src/a.py:42"}],
        "rationale": "revise: src/a.py mutates its argument",
    }
    adversary = {"verdict": "revise", "objections": [{"claim": "x", "why_wrong": "y"}], "strongest_objection": "y"}
    scripted = ScriptedRunner({"review_charter": charter, "review_adversary": adversary})
    args = {"run_id": "r1", "date": "2026-09-06", "cartridge": cartridge, "diff": "diff-text", "repo": "/tmp/repo", "ref": "main"}

    result = review_entry.run(args, scripted)

    assert result["verdict"] == "revise"
    assert result["findings"] == [{"file": "src/a.py", "line": 42, "detail": "mutates the argument", "charter_principle": "A3"}]
    assert result["checks"] == []
    assert {c["role"] for c in scripted.calls} == {"review_charter", "review_adversary"}


def test_disagreement_calls_arbitration(cartridge) -> None:
    scripted = ScriptedRunner(
        {
            "review_charter": CHARTER_APPROVE,
            "review_adversary": {"verdict": "revise", "objections": [{"claim": "x", "why_wrong": "y"}], "strongest_objection": "y"},
            "arbitrate": {"verdict": "revise", "sided_with": "adversary", "reasoning": "the objection holds"},
        }
    )
    args = {"run_id": "r1", "date": "2026-09-06", "cartridge": cartridge, "diff": "diff-text", "repo": "/tmp/repo", "ref": "main"}

    result = review_entry.run(args, scripted)

    assert result["verdict"] == "revise"
    assert result["rationale"] == "the objection holds"
    assert "arbitrate" in {c["role"] for c in scripted.calls}


def test_an_abstained_charter_never_forwards_its_placeholder_as_a_verdict(cartridge) -> None:
    placeholder = {"verdict": "revise", "findings": [], "rationale": ""}
    scripted = ScriptedRunner({"review_charter": [placeholder, placeholder], "review_adversary": CHARTER_APPROVE})
    args = {"run_id": "r1", "date": "2026-09-06", "cartridge": cartridge, "diff": "diff-text", "repo": "/tmp/repo", "ref": "main"}

    result = review_entry.run(args, scripted)

    assert result["verdict"] == "revise"
    assert result["rationale"] == "review_charter did not produce a judgment after a second attempt"
    assert result["findings"] == []
    assert "arbitrate" not in {c["role"] for c in scripted.calls}


def test_an_abstained_adversary_never_forwards_its_placeholder_as_a_verdict(cartridge) -> None:
    placeholder = {"verdict": "revise", "objections": [], "strongest_objection": ""}
    scripted = ScriptedRunner({"review_charter": CHARTER_APPROVE, "review_adversary": [placeholder, placeholder]})
    args = {"run_id": "r1", "date": "2026-09-06", "cartridge": cartridge, "diff": "diff-text", "repo": "/tmp/repo", "ref": "main"}

    result = review_entry.run(args, scripted)

    assert result["verdict"] == "revise"
    assert result["rationale"] == "matches the charter"
    assert "arbitrate" not in {c["role"] for c in scripted.calls}


def test_verify_evidence_applies_the_diff_and_runs_the_repos_own_checks_in_a_fresh_worktree(tmp_path: Path) -> None:
    _seed_repo(tmp_path)
    diff = _diff_adding(tmp_path, "new.txt", "hello\n")

    passed, evidence = review_entry._verify_evidence(
        str(tmp_path), "HEAD", diff, [{"name": "exists", "cmd": "test -f new.txt"}]
    )

    assert passed, evidence
    assert evidence == [{"check": "checks:exists", "output": "pass — (exit 0)"}]


def test_shell_registers_review_diff_with_no_flag_collision() -> None:
    specs = discover()
    assert "review-diff" in specs
    parser = _build_parser(specs)
    assert parser.prog == "shell.py"
