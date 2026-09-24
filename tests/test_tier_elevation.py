"""A ticket's per-role tier reaches the calls, and the second build after a revise asks a tier up.

Every test drives the lifecycle graph with `ScriptedRunner`, whose `run` has the
protocol's real keyword-only signature. A fake that accepts any keyword let a
`reason=` argument through once, and no live runner takes one.
"""

from __future__ import annotations

import pytest

from graphs.delivery import lifecycle_propose
from graphs.delivery.lifecycle_propose import _escalate, _tier_for
from runner import ScriptedRunner
from runner.protocol import BudgetStop

REVISE = {"verdict": "revise", "findings": [], "rationale": "the error path is untested"}
PATCH_2 = "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+new line, error path now tested\n+assert covered()\n"
PATCH_3 = "--- a/src/a.py\n+++ b/src/a.py\n-old line\n+third try, wholly different\n+assert other_thing()\n+more\n"
ESCALATED = "escalated: attempt 2 after revise (standard -> deep)"


def args(cartridge, **overrides):
    return {"run_id": "run-1", "date": "2026-09-24", "ticket": "TICKET-1", "cartridge": cartridge, **overrides}


def scripted(plan_response, build, review) -> ScriptedRunner:
    return ScriptedRunner({"plan": plan_response, "build": build, "review_charter": review})


def rebuilt(build_response, patch) -> dict:
    return {**build_response, "patch": patch, "summary": "next attempt"}


def build_tiers(runner: ScriptedRunner) -> list[str | None]:
    return [c["tier"] for c in runner.calls if c["role"] == "build"]


def escalation_rows(result) -> list[dict]:
    return [e for e in result["proposals"][0]["evidence"] if e["check"] == "tier escalation"]


@pytest.mark.parametrize(
    ("role", "literal", "tiers", "expected"),
    [
        ("adversary", "cheap", {"adversary": "deep"}, "deep"),
        ("adversary", "deep", {"adversary": "deep"}, "deep"),
        ("adversary", "deep", {"adversary": "cheap"}, "deep"),
        ("build", None, {"build": "cheap"}, "cheap"),
        ("build", "standard", {"plan": "deep"}, "standard"),
    ],
)
def test_a_ticket_tier_raises_a_literal_and_never_lowers_it(role, literal, tiers, expected) -> None:
    assert _tier_for(role, literal, tiers) == expected


def test_escalate_goes_one_tier_up_and_stops_at_the_top() -> None:
    assert _escalate("standard") == "deep"
    assert _escalate("deep") == "deep"


def test_a_ticket_build_tier_makes_the_build_call_pass_it(cartridge, plan_response, build_response, review_response) -> None:
    runner = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge, tier={"build": "deep"}), runner)
    assert build_tiers(runner) == ["deep"]
    assert [c["tier"] for c in runner.calls if c["role"] == "plan"] == ["standard"]


def test_a_ticket_value_lower_than_a_literal_does_not_lower_it(cartridge, plan_response, build_response, review_response) -> None:
    runner = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge, tier={"plan": "cheap", "build": "cheap", "review_charter": "deep"}), runner)
    assert [(c["role"], c["tier"]) for c in runner.calls] == [
        ("plan", "standard"),
        ("build", "standard"),
        ("review_charter", "deep"),
    ]


def test_a_ticket_without_a_tier_map_leaves_every_call_as_it_was(cartridge, plan_response, build_response, review_response) -> None:
    runner = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge), runner)
    assert [(c["role"], c["tier"]) for c in runner.calls] == [("plan", "standard"), ("build", "standard"), ("review_charter", None)]


def test_attempt_two_after_a_revise_asks_deep_and_the_record_says_so(
    cartridge, plan_response, build_response, review_response
) -> None:
    runner = scripted(plan_response, [build_response, rebuilt(build_response, PATCH_2)], [REVISE, review_response])
    result = lifecycle_propose.run(args(cartridge), runner)
    assert build_tiers(runner) == ["standard", "deep"]
    assert escalation_rows(result) == [{"check": "tier escalation", "output": ESCALATED}]


def test_a_ticket_build_tier_overrides_the_escalation(cartridge, plan_response, build_response, review_response) -> None:
    for ticket_tier, expected in (("deep", ["deep", "deep"]), ("cheap", ["standard", "standard"])):
        runner = scripted(plan_response, [build_response, rebuilt(build_response, PATCH_2)], [REVISE, review_response])
        result = lifecycle_propose.run(args(cartridge, tier={"build": ticket_tier}), runner)
        assert build_tiers(runner) == expected
        assert escalation_rows(result) == []


def test_a_third_attempt_is_never_escalated(cartridge, plan_response, build_response, review_response) -> None:
    runner = scripted(
        plan_response,
        [build_response, rebuilt(build_response, PATCH_2), rebuilt(build_response, PATCH_3)],
        [REVISE, REVISE, review_response],
    )
    result = lifecycle_propose.run(args(cartridge, fix_attempts=2), runner)
    assert build_tiers(runner) == ["standard", "deep", "standard"]
    assert len(escalation_rows(result)) == 1


def test_a_check_failure_retry_enters_as_a_fresh_run_and_does_not_escalate(
    cartridge, plan_response, build_response, review_response
) -> None:
    """The driver re-enters the graph with the failing check's output in the body and fewer attempts left."""
    runner = scripted(plan_response, build_response, review_response)
    body = "approved, then failed its configured checks:\nruff: E501"
    result = lifecycle_propose.run(args(cartridge, ticket_body=body, fix_attempts=1), runner)
    assert build_tiers(runner) == ["standard"]
    assert escalation_rows(result) == []


def test_an_escalated_build_that_hits_its_budget_resumes_at_the_escalated_tier(
    cartridge, plan_response, build_response, review_response
) -> None:
    """Catches a `reason=` keyword reaching the runner from `_resume_build`: ScriptedRunner refuses it with TypeError."""
    stop = BudgetStop(role="build", thread="TICKET-1", session="s1", spent_usd=1.0, detail="error_max_budget_usd")
    runner = scripted(plan_response, [build_response, stop, rebuilt(build_response, PATCH_2)], [REVISE, review_response])
    result = lifecycle_propose.run(args(cartridge), runner)
    assert build_tiers(runner) == ["standard", "deep", "deep"]
    assert result["fix_loop"] == {"attempts": 2, "stopped": None, "continuations": 1}
    assert escalation_rows(result) == [{"check": "tier escalation", "output": ESCALATED}]
