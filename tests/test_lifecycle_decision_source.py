"""lifecycle-propose asks the DecisionSource on every runner call and passes the answer as router_decision."""

from __future__ import annotations

from graphs.delivery import lifecycle_propose
from runner import ScriptedRunner
from runner.decision_log import RouterDecision
from runner.decision_source import NoDecisionSource
from runner.tier_resolution import Hints


class RecordingRunner(ScriptedRunner):
    """A ScriptedRunner that also accepts and records `router_decision`."""

    def run(self, *, router_decision=None, **kwargs):
        self.decisions.append(router_decision)
        return super().run(**kwargs)

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.decisions: list[RouterDecision | None] = []


def decision_for(role: str) -> RouterDecision:
    return RouterDecision(
        chosen_class="standard", model=f"model-{role}", effort="medium",
        budget_usd=1.0, reasons=(role,), clipped_by=(),
    )


class RoleSource:
    def __init__(self) -> None:
        self.asked: list[tuple[str, Hints | None]] = []

    def __call__(self, role, hints):
        self.asked.append((role, hints))
        return decision_for(role)


def args(cartridge, **overrides):
    return {"run_id": "run-1", "date": "2026-08-30", "ticket": "TICKET-1", "cartridge": cartridge, **overrides}


def scripted(plan_response, build_response, review_response) -> RecordingRunner:
    return RecordingRunner({"plan": plan_response, "build": build_response, "review_charter": review_response})


def test_every_call_carries_the_sources_decision_for_its_role(
    cartridge, plan_response, build_response, review_response
) -> None:
    fake = scripted(plan_response, build_response, review_response)
    source = RoleSource()
    lifecycle_propose.run(args(cartridge), fake, source)
    assert fake.calls
    assert fake.decisions == [decision_for(c["role"]) for c in fake.calls]


def test_the_source_is_asked_with_the_calls_own_role_and_hints(
    cartridge, plan_response, build_response, review_response
) -> None:
    fake = scripted(plan_response, build_response, review_response)
    source = RoleSource()
    lifecycle_propose.run(args(cartridge), fake, source)
    assert source.asked == [(c["role"], c.get("hints")) for c in fake.calls]
    assert any(hints is not None for _, hints in source.asked)


def test_no_source_and_the_default_source_both_pass_none(
    cartridge, plan_response, build_response, review_response
) -> None:
    bare = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge), bare)
    default = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(args(cartridge), default, NoDecisionSource())
    assert bare.decisions and set(bare.decisions) == {None}
    assert default.decisions and set(default.decisions) == {None}


def test_a_ticket_tier_and_the_literal_tiers_are_unchanged_by_the_source(
    cartridge, plan_response, build_response, review_response
) -> None:
    tiered = args(cartridge, tier={"build": "deep"})
    without = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(tiered, without)
    with_source = scripted(plan_response, build_response, review_response)
    lifecycle_propose.run(tiered, with_source, RoleSource())
    assert [(c["role"], c["tier"]) for c in with_source.calls] == [(c["role"], c["tier"]) for c in without.calls]
    assert ("build", "deep") in [(c["role"], c["tier"]) for c in with_source.calls]
    assert with_source.decisions == [decision_for(c["role"]) for c in with_source.calls]


def test_a_source_that_raises_never_fails_the_run(
    cartridge, plan_response, build_response, review_response
) -> None:
    def boom(role, hints):
        raise RuntimeError("source down")

    fake = scripted(plan_response, build_response, review_response)
    result = lifecycle_propose.run(args(cartridge), fake, boom)
    assert result["proposals"]
    assert set(fake.decisions) == {None}
