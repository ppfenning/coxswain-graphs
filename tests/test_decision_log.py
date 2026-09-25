from dataclasses import FrozenInstanceError

import pytest

from runner.decision_log import (
    CallDecision,
    RouterDecision,
    from_row,
    from_wire,
    joined_reasons,
    to_row,
)

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
    router_reason="hard task; long context",
    router_model="anthropic/claude-deep",
    router_effort="high",
    router_budget_usd=1.5,
    router_clipped_by=("node_budget_cap",),
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
    assert len(row) == 23


def test_router_tier_defaults_to_none():
    assert CallDecision(**REQUIRED).router_tier is None


def test_router_reason_defaults_to_none():
    assert CallDecision(**REQUIRED).router_reason is None


def test_router_model_defaults_to_none():
    assert CallDecision(**REQUIRED).router_model is None


def test_router_effort_defaults_to_none():
    assert CallDecision(**REQUIRED).router_effort is None


def test_router_budget_usd_defaults_to_none():
    assert CallDecision(**REQUIRED).router_budget_usd is None


def test_router_clipped_by_defaults_to_none():
    assert CallDecision(**REQUIRED).router_clipped_by is None


def test_a_json_row_with_a_list_clipped_by_loads_as_a_tuple():
    row = {**REQUIRED, "router_clipped_by": ["node_budget_cap"]}
    assert from_row(row).router_clipped_by == ("node_budget_cap",)


def test_a_row_with_a_string_clipped_by_is_refused_not_split_into_characters():
    with pytest.raises(ValueError):
        from_row({**REQUIRED, "router_clipped_by": "node_budget_cap"})


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


WIRE = {
    "schema": 1,
    "chosen_class": "deep",
    "model": "anthropic/claude-deep",
    "effort": "high",
    "budget_usd": 1.5,
    "reasons": ["hard task", "long context"],
    "clipped_by": ["node_budget_cap"],
}


def test_from_wire_turns_a_good_dict_into_a_router_decision():
    assert from_wire(WIRE) == RouterDecision(
        chosen_class="deep",
        model="anthropic/claude-deep",
        effort="high",
        budget_usd=1.5,
        reasons=("hard task", "long context"),
        clipped_by=("node_budget_cap",),
    )


def _without(key):
    return {k: v for k, v in WIRE.items() if k != key}


@pytest.mark.parametrize(
    "wire",
    [
        pytest.param({**WIRE, "schema": 2}, id="unknown-schema"),
        pytest.param(_without("schema"), id="missing-schema"),
        pytest.param({**WIRE, "schema": True}, id="bool-schema"),
        pytest.param(_without("model"), id="missing-key"),
        pytest.param({**WIRE, "reasons": "hard task"}, id="string-reasons"),
        pytest.param({**WIRE, "clipped_by": ["node_budget_cap", 3]}, id="non-str-clipped-by"),
        pytest.param({**WIRE, "budget_usd": "1.5"}, id="string-budget"),
        pytest.param({**WIRE, "budget_usd": True}, id="bool-budget"),
        pytest.param({**WIRE, "budget_usd": float("nan")}, id="nan-budget"),
        pytest.param({**WIRE, "budget_usd": float("inf")}, id="infinite-budget"),
        pytest.param(["not", "a", "dict"], id="not-a-mapping"),
    ],
)
def test_from_wire_returns_none_on_a_bad_shape(wire):
    assert from_wire(wire) is None


def test_router_reason_is_the_reasons_joined_by_a_semicolon():
    assert joined_reasons(from_wire(WIRE)) == "hard task; long context" == FULL.router_reason
