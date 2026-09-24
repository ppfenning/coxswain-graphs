"""The arbiter is skipped only when charter and adversary both approve."""

from __future__ import annotations

from graphs.delivery import lifecycle_propose
from graphs.delivery.lifecycle_propose import ARBITER_SKIPPED, should_skip_arbiter
from runner import ScriptedRunner

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
REVISE = {"verdict": "revise", "findings": [], "rationale": "too loose"}
ADV_APPROVE = {"verdict": "approve", "objections": [], "strongest_objection": "none that survive"}
ADV_REVISE = {"verdict": "revise", "objections": [], "strongest_objection": "too loose"}
ARB_APPROVE = {"verdict": "approve", "sided_with": "neither", "reasoning": "over-strict pair"}


def test_pair_approve_approve_skips() -> None:
    assert should_skip_arbiter("approve", "approve") is True


def test_pair_approve_revise_does_not_skip() -> None:
    assert should_skip_arbiter("approve", "revise") is False


def test_pair_revise_approve_does_not_skip() -> None:
    assert should_skip_arbiter("revise", "approve") is False


def test_pair_revise_revise_does_not_skip() -> None:
    assert should_skip_arbiter("revise", "revise") is False


def test_pair_approve_reject_does_not_skip() -> None:
    assert should_skip_arbiter("approve", "reject") is False


def test_pair_reject_approve_does_not_skip() -> None:
    assert should_skip_arbiter("reject", "approve") is False


def test_pair_reject_reject_does_not_skip() -> None:
    assert should_skip_arbiter("reject", "reject") is False


def _tier_2_run(cartridge, plan_response, build_response, charter, adversary):
    cartridge["policy"] = {"review_tier": {"tier2_surfaces": ["schema"]}}
    for role in ("review_adversary", "arbitrate"):
        cartridge["skills"][role] = f"acme-skills:{role}"
    scripted = ScriptedRunner(
        {
            "plan": plan_response,
            "build": build_response,
            "review_charter": charter,
            "review_adversary": adversary,
            "arbitrate": ARB_APPROVE,
        }
    )
    result = lifecycle_propose.run(
        {"run_id": "r", "date": "2026-09-24", "ticket": "T-1", "cartridge": cartridge, "surfaces": ["schema"]},
        scripted,
    )
    return result, [call["role"] for call in scripted.calls]


def test_tier_2_approve_approve_never_invokes_the_arbiter(cartridge, plan_response, build_response) -> None:
    result, roles = _tier_2_run(cartridge, plan_response, build_response, APPROVE, ADV_APPROVE)
    assert result["review_tier"] == 2
    assert "arbitrate" not in roles
    assert result["arbitration"] == "arbiter: skipped (both approved)" == ARBITER_SKIPPED
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]


def test_tier_2_revise_revise_still_invokes_the_arbiter(cartridge, plan_response, build_response) -> None:
    result, roles = _tier_2_run(cartridge, plan_response, build_response, REVISE, ADV_REVISE)
    assert roles.count("arbitrate") == 1
    assert result["arbitration"] == ARB_APPROVE
