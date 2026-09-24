from dataclasses import FrozenInstanceError

import pytest

from runner.decision_log import CallDecision, from_row, to_row

REQUIRED = {
    "role": "plan",
    "requested_tier": "standard",
    "chosen_tier": "cheap",
    "model_id": "anthropic/claude-cheap",
    "reason": "node cap",
    "ticket_key": "T-1",
    "outcome_key": "o-1",
}
FULL = CallDecision(
    **REQUIRED,
    router_tier="deep",
    router_reason="hard task",
    claude_code_version="2.1.0",
    effort="high",
    budget_usd=0.5,
    clipped_by="node_budget_cap",
)


def test_a_full_record_round_trips_through_a_row():
    assert from_row(to_row(FULL)) == FULL


def test_to_row_returns_a_plain_dict_of_every_field():
    row = to_row(FULL)
    assert type(row) is dict
    assert row["router_tier"] == "deep"
    assert len(row) == 13


def test_router_tier_defaults_to_none():
    assert CallDecision(**REQUIRED).router_tier is None


def test_router_reason_defaults_to_none():
    assert CallDecision(**REQUIRED).router_reason is None


def test_claude_code_version_defaults_to_none():
    assert CallDecision(**REQUIRED).claude_code_version is None


def test_effort_defaults_to_none():
    assert CallDecision(**REQUIRED).effort is None


def test_budget_usd_defaults_to_none():
    assert CallDecision(**REQUIRED).budget_usd is None


def test_clipped_by_defaults_to_none():
    assert CallDecision(**REQUIRED).clipped_by is None


def test_a_row_without_the_optional_keys_loads_with_them_none():
    decision = from_row(REQUIRED)
    assert decision == CallDecision(**REQUIRED)
    assert decision.clipped_by is None


def test_a_row_missing_a_required_key_is_refused():
    with pytest.raises(KeyError):
        from_row({"role": "plan"})


def test_the_record_is_frozen():
    with pytest.raises(FrozenInstanceError):
        FULL.role = "build"  # type: ignore[misc]
