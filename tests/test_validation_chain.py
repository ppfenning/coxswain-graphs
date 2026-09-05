"""Scrutiny proportional to cost, and a handoff that refuses to pass a gap along.

The conviction under test: nothing is one-shot. Every change gets a reviewer,
how many it gets is decided by what a mistake would cost rather than by the
author, and a step never builds on an unvalidated handoff.
"""

from __future__ import annotations

import pytest

from graphs.delivery import lifecycle_propose
from graphs._contract import ContractViolation, review_tier
from runner import ScriptedRunner
from runner.protocol import RunnerError

REVIEW_TIER_CONFIG = {
    "tier0_patterns": ["docs_only", "rename_only", "config_bump", "param_tweak"],
    "tier1_max_changed_lines": 150,
    "tier1_max_modules": 1,
    "tier2_surfaces": ["schema", "migration", "deletion", "auth", "infrastructure", "cross_repo"],
}

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
ADV_APPROVE = {"verdict": "approve", "objections": [], "strongest_objection": "none that survive"}
ADV_REJECT = {
    "verdict": "reject",
    "objections": [{"claim": "it is safe", "why_wrong": "it drops a column"}],
    "strongest_objection": "it drops a column",
}
ARB = {"verdict": "approve", "sided_with": "charter", "reasoning": "the objection is about style"}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["policy"] = {"review_tier": dict(REVIEW_TIER_CONFIG)}
    return cartridge


def bind(cart, *roles):
    for role in roles:
        cart["skills"][role] = f"acme-skills:{role}"
    return cart


def run(cart, plan_response, build_response, extra=None, **args):
    responses = {
        "plan": plan_response,
        "build": build_response,
        "review_charter": APPROVE,
        **(extra or {}),
    }
    scripted = ScriptedRunner(responses)
    result = lifecycle_propose.run(
        {"run_id": "r", "date": "2026-08-30", "ticket": "T-1", "cartridge": cart, **args}, scripted
    )
    return result, scripted


# ── the tier itself ────────────────────────────────────────────────────────


def test_a_dangerous_surface_is_tier_2_however_small(cart) -> None:
    """A four-line migration outranks a four-hundred-line rename."""
    tier = review_tier(cart, change_facts={"changed_lines": 4, "module_count": 1}, surfaces=["migration"])
    assert tier == 2


def test_a_small_contained_change_is_tier_1(cart) -> None:
    assert review_tier(cart, change_facts={"changed_lines": 20, "module_count": 1}) == 1


def test_a_big_change_is_tier_2_even_on_safe_surfaces(cart) -> None:
    assert review_tier(cart, change_facts={"changed_lines": 900, "module_count": 6}) == 2


def test_a_trivial_pattern_is_tier_0(cart) -> None:
    tier = review_tier(cart, change_facts={"changed_lines": 3, "module_count": 1}, patterns=["docs_only"])
    assert tier == 0


def test_a_trivial_pattern_cannot_talk_down_a_dangerous_surface(cart) -> None:
    """Size and 'it's only docs' must never outvote the surface being touched."""
    tier = review_tier(
        cart, change_facts={"changed_lines": 3, "module_count": 1}, patterns=["docs_only"], surfaces=["auth"]
    )
    assert tier == 2


# ── who reviews, by tier ───────────────────────────────────────────────────


def test_tier_0_still_gets_a_reviewer(cart, plan_response, build_response) -> None:
    """There is no tier that skips review."""
    bind(cart, "review_adversary", "arbitrate")
    result, scripted = run(cart, plan_response, build_response, patterns=["docs_only"])
    roles = [c["role"] for c in scripted.calls]
    assert "review_charter" in roles
    assert "review_adversary" not in roles, "tier 0 is the cheapest review, not two of them"
    assert result["review_tier"] == 0


def test_tier_1_adds_an_adversary(cart, plan_response, build_response) -> None:
    bind(cart, "review_adversary")
    result, scripted = run(cart, plan_response, build_response, extra={"review_adversary": ADV_APPROVE})
    assert result["review_tier"] == 1
    assert "review_adversary" in [c["role"] for c in scripted.calls]


def test_tier_2_arbitrates_even_when_the_reviewers_agree(cart, plan_response, build_response) -> None:
    """At tier 2, two reviewers agreeing is not by itself reason to believe them."""
    bind(cart, "review_adversary", "arbitrate")
    result, scripted = run(
        cart,
        plan_response,
        build_response,
        extra={"review_adversary": ADV_APPROVE, "arbitrate": ARB},
        surfaces=["schema"],
    )
    assert result["review_tier"] == 2
    assert result["arbitration"] is not None
    assert "arbitrate" in [c["role"] for c in scripted.calls]


def test_disagreement_arbitrates_at_any_tier(cart, plan_response, build_response) -> None:
    bind(cart, "review_adversary", "arbitrate")
    result, _ = run(cart, plan_response, build_response, extra={"review_adversary": ADV_REJECT, "arbitrate": ARB})
    assert result["review_tier"] == 1
    assert result["arbitration"]["sided_with"] == "charter"


def test_an_unarbitrated_disagreement_blocks_the_proposal(cart, plan_response, build_response) -> None:
    """No arbitrator bound, reviewers split: the change does not go out."""
    bind(cart, "review_adversary")
    result, _ = run(cart, plan_response, build_response, extra={"review_adversary": ADV_REJECT})
    assert result["arbitration"] is None
    assert [p["kind"] for p in result["proposals"]] == []


def test_arbitration_has_the_last_word(cart, plan_response, build_response) -> None:
    """At tier 2 the arbitrator runs on agreement — and can still overrule it.

    This is the whole reason tier 2 arbitrates unconditionally: two reviewers
    nodding at a migration is not evidence the migration is safe.
    """
    bind(cart, "review_adversary", "arbitrate")
    overruled = {"verdict": "reject", "sided_with": "adversary", "reasoning": "the objection holds"}
    result, _ = run(
        cart,
        plan_response,
        build_response,
        extra={"review_adversary": ADV_APPROVE, "arbitrate": overruled},
        surfaces=["migration"],
    )
    assert result["review_tier"] == 2
    assert result["proposals"] == [], "both reviewers approved; the arbitrator still said no"


def test_the_adversary_reasoning_becomes_evidence(cart, plan_response, build_response) -> None:
    bind(cart, "review_adversary", "arbitrate")
    result, _ = run(
        cart, plan_response, build_response, extra={"review_adversary": ADV_APPROVE, "arbitrate": ARB},
        surfaces=["schema"],
    )
    checks = {e["check"] for e in result["proposals"][0]["evidence"]}
    assert {"review tier", "adversary verdict", "strongest objection", "arbitration"} <= checks


def test_binding_nothing_leaves_the_original_single_reviewer_loop(cart, plan_response, build_response) -> None:
    """Optional means optional. An unbound adversary is not a silent objection."""
    result, scripted = run(cart, plan_response, build_response)
    assert result["adversary"] is None and result["arbitration"] is None
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]


# ── the handoff ────────────────────────────────────────────────────────────


def test_a_complete_handoff_passes_its_brief_to_review(cart, plan_response, build_response) -> None:
    bind(cart, "handoff")
    complete = {"complete": True, "blocking": False, "missing": [], "brief": "one file, one behaviour, tests green"}
    result, scripted = run(cart, plan_response, build_response, extra={"handoff": complete})
    assert result["handoff"]["brief"].startswith("one file")
    review_prompt = next(c["prompt"] for c in scripted.calls if c["role"] == "review_charter")
    assert "one file, one behaviour" in review_prompt


def test_a_blocking_handoff_stops_the_graph(cart, plan_response, build_response) -> None:
    """A reviewer handed half a change produces a confident opinion about the wrong thing."""
    bind(cart, "handoff")
    incomplete = {"complete": False, "blocking": True, "missing": ["the migration script", "any test"], "brief": ""}
    with pytest.raises(ContractViolation, match="handoff from build to review is incomplete"):
        run(cart, plan_response, build_response, extra={"handoff": incomplete})


def test_the_blocking_handoff_names_what_is_missing(cart, plan_response, build_response) -> None:
    bind(cart, "handoff")
    incomplete = {"complete": False, "blocking": True, "missing": ["any test"], "brief": ""}
    with pytest.raises(ContractViolation, match="any test"):
        run(cart, plan_response, build_response, extra={"handoff": incomplete})


def test_review_never_runs_after_a_blocking_handoff(cart, plan_response, build_response) -> None:
    bind(cart, "handoff")
    incomplete = {"complete": False, "blocking": True, "missing": ["x"], "brief": ""}
    scripted = ScriptedRunner({"plan": plan_response, "build": build_response, "handoff": incomplete})
    with pytest.raises(ContractViolation):
        lifecycle_propose.run(
            {"run_id": "r", "date": "d", "ticket": "T", "cartridge": cart}, scripted
        )
    assert "review_charter" not in [c["role"] for c in scripted.calls]


# ── the split: a missing artifact stops, missing evidence revises ───────────


def test_an_absent_artifact_is_blocking_whatever_the_node_flagged(cart, plan_response) -> None:
    """The graph decides this from the patch, not from the shuttle's opinion.

    A handoff holding no patch cannot truthfully be told it is only short of
    evidence: there is no change for a builder to add evidence about.
    """
    bind(cart, "handoff")
    empty = {"patch": "   ", "summary": "nothing", "files_touched": [], "commands_run": []}
    incomplete = {"complete": False, "blocking": False, "missing": ["the patch"], "brief": ""}
    with pytest.raises(ContractViolation, match="artifact or an input is missing"):
        run(cart, plan_response, empty, extra={"handoff": incomplete})


def test_an_under_evidenced_handoff_revises_instead_of_quarantining(cart, plan_response, build_response) -> None:
    """The artifact is present; what is missing is a check nobody ran.

    That is a gap one more build attempt closes. Stopping the run here throws
    away a finished change and buys a whole replan to return to where the run
    already was — which is what three of four handoff stops in one day did.
    """
    bind(cart, "handoff")
    second = {**build_response, "patch": build_response["patch"] + "+evidence attached\n"}
    result, scripted = run(
        cart, plan_response, [build_response, second],
        extra={
            "handoff": [
                {"complete": False, "blocking": False, "missing": ["output of the cleanliness check"], "brief": ""},
                {"complete": True, "blocking": False, "missing": [], "brief": "checks attached"},
            ]
        },
    )
    assert [p["kind"] for p in result["proposals"]] == ["draft_pr_create"]
    assert result["fix_loop"] == {"attempts": 2, "stopped": None}
    assert result["handoff"]["complete"] is True

    # No reviewer was bought for the change the shuttle had already refused.
    roles = [c["role"] for c in scripted.calls]
    assert roles.index("build") < roles.index("handoff") < roles.index("review_charter")
    assert roles.count("review_charter") == 1, "review runs once, on the change that was handed over"


def test_the_builder_is_sent_back_the_handoff_s_own_words(cart, plan_response, build_response) -> None:
    """Verbatim, like any other critique. 'The handoff refused' fixes nothing."""
    bind(cart, "handoff")
    second = {**build_response, "patch": build_response["patch"] + "+evidence\n"}
    _, scripted = run(
        cart, plan_response, [build_response, second],
        extra={
            "handoff": [
                {"complete": False, "blocking": False, "missing": ["output of the cleanliness check"],
                 "brief": "attach the check output"},
                {"complete": True, "blocking": False, "missing": [], "brief": "ok"},
            ]
        },
    )
    retry = [c["prompt"] for c in scripted.calls if c["role"] == "build"][1]
    assert "output of the cleanliness check" in retry
    assert "attach the check output" in retry


def test_an_under_evidenced_handoff_costs_one_attempt_not_the_run(cart, plan_response, build_response) -> None:
    """It buys a build attempt out of the same bounded budget as any revise.

    With the fix loop disabled there is no attempt to spend, so the run ends
    unapproved — but it ends with the loop's accounting, not with an exception,
    and the record says which.
    """
    bind(cart, "handoff")
    result, scripted = run(
        cart, plan_response, build_response,
        extra={"handoff": {"complete": False, "blocking": False, "missing": ["a test"], "brief": ""}},
        fix_attempts=0,
    )
    assert result["proposals"] == []
    assert result["fix_loop"] == {"attempts": 1, "stopped": "attempts_exhausted"}
    assert result["review"]["verdict"] == "revise"
    assert result["review"]["findings"][0]["detail"] == "a test"
    # No second opinion was invented out of the shuttle's objection.
    assert result["adversary"] is None and result["arbitration"] is None
    assert "review_charter" not in [c["role"] for c in scripted.calls]


# ── a retry that hits the build node's dollar ceiling ───────────────────────

REVISE = {"verdict": "revise",
          "findings": [{"charter_principle": "x", "detail": "needs a test", "file": "src/a.py"}],
          "rationale": "needs a test"}
BUDGET_STOP = RunnerError("node 'build' failed in claude: {\"subtype\": \"error_max_budget_usd\"}")


def test_a_budget_stop_on_a_retry_keeps_the_last_reviewed_build(cart, plan_response, build_response) -> None:
    """The exception is swallowed; the record keeps the build that was reviewed."""
    result, scripted = run(
        cart, plan_response, build_response,
        extra={"review_charter": REVISE, "build": [build_response, BUDGET_STOP]},
    )
    assert result["fix_loop"] == {"attempts": 2, "stopped": "budget"}
    assert result["build"]["patch"] == build_response["patch"]
    assert result["review"]["verdict"] == "revise"
    assert result["proposals"] == []


def test_a_non_budget_runner_error_on_a_retry_still_propagates(cart, plan_response, build_response) -> None:
    """Only the dollar ceiling is swallowed. Any other failure is still a failure."""
    other = RunnerError("node 'build' failed in claude: {\"subtype\": \"error_something_else\"}")
    with pytest.raises(RunnerError):
        run(cart, plan_response, build_response, extra={"review_charter": REVISE, "build": [build_response, other]})


def test_a_budget_stop_on_the_first_build_still_propagates(cart, plan_response) -> None:
    """There is no earlier build to keep, so the honest behaviour is to raise."""
    with pytest.raises(RunnerError):
        run(cart, plan_response, build_response=BUDGET_STOP)
