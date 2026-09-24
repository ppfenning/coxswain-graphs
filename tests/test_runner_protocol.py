import pytest

from runner.decision_log import CallDecision
from runner.protocol import Capability, NodeResult, ProviderProfile, RunnerError, resolve_profile

CHEAP = ProviderProfile(
    capabilities={
        "structured_output": True,
        "tool_use": True,
        "resume": False,
        "streaming": False,
        "max_context": 32000,
    },
    tiers={
        "cheap": "local/mixtral-8x7b",
        "standard": "local/mixtral-standard",
        "deep": "local/mixtral-deep",
    },
)
STANDARD = ProviderProfile(
    capabilities={
        "structured_output": True,
        "tool_use": True,
        "resume": True,
        "streaming": True,
        "max_context": 200000,
    },
    tiers={
        "cheap": "anthropic/claude-cheap",
        "standard": "anthropic/claude-standard",
        "deep": "anthropic/claude-deep",
    },
)
DEEP_NO_RESUME = ProviderProfile(
    capabilities={
        "structured_output": True,
        "tool_use": True,
        "resume": False,
        "streaming": True,
        "max_context": 500000,
    },
    tiers={"cheap": "x/cheap", "standard": "x/standard", "deep": "x/deep"},
)
BY_TIER = {"cheap": CHEAP, "standard": STANDARD, "deep": DEEP_NO_RESUME}


def test_a_tier_with_every_required_capability_resolves_to_itself():
    profile, fallback = resolve_profile(
        BY_TIER, role="plan", tier="cheap", required=[Capability.TOOL_USE]
    )
    assert profile is CHEAP
    assert fallback is None
    assert profile.tiers["cheap"] == "local/mixtral-8x7b"


def test_a_tier_missing_a_capability_falls_up_one_tier_and_records_capability_fallback():
    profile, fallback = resolve_profile(
        BY_TIER, role="build", tier="cheap", required=[Capability.RESUME]
    )
    assert profile is STANDARD
    assert fallback == {
        "event": "capability_fallback",
        "role": "build",
        "requested_tier": "cheap",
        "resolved_tier": "standard",
        "missing_capability": "resume",
    }
    assert profile.tiers[fallback["resolved_tier"]] == "anthropic/claude-standard"


def test_a_bare_single_profile_applies_to_every_tier():
    for tier in ("cheap", "standard", "deep"):
        profile, fallback = resolve_profile(
            CHEAP, role="plan", tier=tier, required=[Capability.TOOL_USE]
        )
        assert profile is CHEAP
        assert fallback is None
        assert profile.tiers[tier] == CHEAP.tiers[tier]


def test_a_role_needing_two_capabilities_only_falls_up_when_the_destination_has_both():
    profile, fallback = resolve_profile(
        BY_TIER,
        role="build",
        tier="cheap",
        required=[Capability.TOOL_USE, Capability.RESUME],
    )
    assert profile is STANDARD
    assert fallback["missing_capability"] == "resume"


def test_a_fallback_destination_still_missing_the_capability_refuses_rather_than_claim_success():
    deficient = {"cheap": CHEAP, "standard": DEEP_NO_RESUME, "deep": DEEP_NO_RESUME}
    with pytest.raises(RunnerError):
        resolve_profile(
            deficient, role="build", tier="cheap", required=[Capability.RESUME]
        )


def test_the_top_tier_missing_a_capability_refuses_instead_of_indexing_past_it():
    with pytest.raises(RunnerError):
        resolve_profile(
            BY_TIER, role="build", tier="deep", required=[Capability.RESUME]
        )


def test_a_fresh_node_result_has_no_decision():
    assert NodeResult({"a": 1}).decision is None


def test_a_decision_rides_on_the_result_without_becoming_a_key():
    decision = CallDecision(
        role="plan",
        requested_tier="cheap",
        chosen_tier="cheap",
        model_id="m",
        reason="r",
        ticket_key="T-1",
        outcome_key="o-1",
    )
    result = NodeResult({"a": 1})
    result.decision = decision
    assert result == {"a": 1}
    assert result.decision is decision
