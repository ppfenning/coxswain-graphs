"""rescue-review: the review half of the loop, from a patch that already exists."""

from __future__ import annotations

from graphs.delivery import rescue_review
from runner import ScriptedRunner

PATCH = (
    "diff --git a/src/a.py b/src/a.py\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,1 +1,2 @@\n"
    "-old\n"
    "+new\n"
    "+another\n"
)
EVIDENCE = [{"command": "pytest -q", "output": "3 passed\n(exit 0)", "source": "harness_verify"}]

HANDOFF_OK = {"complete": True, "blocking": False, "missing": [], "brief": "ready"}
CHARTER_APPROVES = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
CHARTER_REVISES = {"verdict": "revise", "findings": [], "rationale": "the error path is untested"}
ADVERSARY_APPROVES = {"verdict": "approve", "objections": [], "strongest_objection": "none that survive"}
ARBITER_REVISES = {"verdict": "revise", "sided_with": "charter", "reasoning": "the error path is untested"}


def bound(cartridge) -> dict:
    cartridge["skills"].update(
        {role: f"acme-skills:{role}" for role in ("handoff", "review_charter", "review_adversary", "arbitrate")}
    )
    return cartridge


def args(cartridge, **overrides) -> dict:
    return {
        "run_id": "run-1", "date": "2026-09-26", "ticket": "TICKET-1", "ticket_body": "Fix the off-by-one.",
        "patch": PATCH, "evidence": EVIDENCE, "cartridge": bound(cartridge), **overrides,
    }


def roles(scripted: ScriptedRunner) -> list[str]:
    return [call["role"] for call in scripted.calls]


def prompt(scripted: ScriptedRunner, role: str) -> str:
    return next(call["prompt"] for call in scripted.calls if call["role"] == role)


def test_all_approve_gives_a_verdict_proposals_and_the_evidence_in_the_reviewer_prompt(cartridge) -> None:
    scripted = ScriptedRunner(
        {"handoff": HANDOFF_OK, "review_charter": CHARTER_APPROVES, "review_adversary": ADVERSARY_APPROVES}
    )
    result = rescue_review.run(args(cartridge), scripted)

    assert result["verdict"] == "approve"
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]
    assert {"check": "pytest -q", "output": "3 passed\n(exit 0)"} in result["proposals"][0]["evidence"]
    assert result["build"]["patch"] == PATCH
    assert result["build"]["commands_run"] == EVIDENCE
    assert result["adversary"] == ADVERSARY_APPROVES
    assert result["fix_loop"]["stopped"] is None

    assert "Rows with source harness_verify were run by the harness" in prompt(scripted, "handoff")
    assert "'command': 'pytest -q'" in prompt(scripted, "handoff")
    for role in ("review_charter", "review_adversary"):
        assert "$ pytest -q\n3 passed\n(exit 0)" in prompt(scripted, role), role
        assert "do not ask for a rerun" in prompt(scripted, role), role


def test_the_result_carries_every_key_a_lifecycle_result_does(cartridge) -> None:
    scripted = ScriptedRunner(
        {"handoff": HANDOFF_OK, "review_charter": CHARTER_APPROVES, "review_adversary": ADVERSARY_APPROVES}
    )
    result = rescue_review.run(args(cartridge), scripted)
    assert set(result) == {
        "run_id", "date", "ticket", "scope", "review_tier", "handoff", "adversary",
        "arbitration", "plan", "plan_competition", "plan_attack", "plan_gate", "build", "review",
        "change_facts", "fix_loop", "proposals", "verdict",
    }


def test_a_charter_revise_gives_revise_and_no_proposals(cartridge) -> None:
    scripted = ScriptedRunner(
        {
            "handoff": HANDOFF_OK,
            "review_charter": CHARTER_REVISES,
            "review_adversary": ADVERSARY_APPROVES,
            "arbitrate": ARBITER_REVISES,
        }
    )
    result = rescue_review.run(args(cartridge), scripted)

    assert result["verdict"] == "revise"
    assert result["proposals"] == []
    assert result["arbitration"]["reasoning"] == "the error path is untested"
    assert result["review"]["rationale"] == "the error path is untested"
    assert result["fix_loop"]["stopped"] == "rescue_revise"


def test_only_review_roles_are_called_never_plan_or_build(cartridge) -> None:
    approving = ScriptedRunner(
        {"handoff": HANDOFF_OK, "review_charter": CHARTER_APPROVES, "review_adversary": ADVERSARY_APPROVES}
    )
    rescue_review.run(args(cartridge), approving)
    assert roles(approving) == ["handoff", "review_charter", "review_adversary"]

    revising = ScriptedRunner(
        {
            "handoff": HANDOFF_OK,
            "review_charter": CHARTER_REVISES,
            "review_adversary": ADVERSARY_APPROVES,
            "arbitrate": ARBITER_REVISES,
        }
    )
    rescue_review.run(args(cartridge), revising)
    assert roles(revising) == ["handoff", "review_charter", "review_adversary", "arbitrate"]


def test_a_patch_that_does_not_parse_is_revised_without_asking_a_model(cartridge) -> None:
    scripted = ScriptedRunner({})
    result = rescue_review.run(args(cartridge, patch=PATCH[:-10]), scripted)
    assert scripted.calls == []
    assert result["verdict"] == "revise"
    assert result["proposals"] == []
    assert "cut off" in result["review"]["findings"][0]["detail"]


def test_an_incomplete_handoff_is_revised_with_its_gaps_and_no_review(cartridge) -> None:
    gap = {"complete": False, "blocking": False, "missing": ["no test covers the error path"], "brief": ""}
    scripted = ScriptedRunner({"handoff": gap})
    result = rescue_review.run(args(cartridge), scripted)
    assert roles(scripted) == ["handoff"]
    assert result["verdict"] == "revise"
    assert result["proposals"] == []
    assert result["review"]["findings"][0]["detail"] == "no test covers the error path"
