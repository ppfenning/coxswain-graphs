from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from runner.anthropic_runner import AnthropicRunner
from runner.protocol import RunnerError

PROFILE = {"tiers": {"cheap": "m-cheap", "standard": "m-std", "deep": "m-deep"}}


class _Stub:
    def __init__(self, **response: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
            **response,
        )
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


def _run(stub: _Stub, profile: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    runner = AnthropicRunner({**PROFILE, **(profile or {})}, client=stub)
    return runner.run(role="r", schema={}, prompt="p", **kwargs)


def test_the_decision_carries_the_response_model_id_and_the_tier():
    decision = _run(_Stub(model="claude-x-1"), tier="cheap").decision
    assert (decision.model_id, decision.requested_tier, decision.chosen_tier) == ("claude-x-1", "cheap", "cheap")


def test_the_reason_is_caller_and_there_is_no_claude_code_version():
    decision = _run(_Stub(model="claude-x-1")).decision
    assert (decision.reason, decision.claude_code_version) == ("caller", None)


def test_a_response_without_a_model_falls_back_to_the_profile_model():
    assert _run(_Stub()).decision.model_id == "m-std"


def test_selection_is_unchanged_and_the_decision_is_not_a_key():
    stub = _Stub(model="claude-x-1")
    result = _run(stub, tier="cheap")
    assert stub.calls[0]["model"] == "m-cheap"
    assert result == {"ok": True}


def _chosen(profile: dict[str, Any], **kwargs: Any) -> tuple[str, str, str]:
    stub = _Stub()
    decision = _run(stub, profile, **kwargs).decision
    return decision.chosen_tier, decision.reason, stub.calls[0]["model"]


def test_an_override_beats_the_profile_default_the_caller_tier_and_the_floor():
    profile = {"tier_overrides": {"r": "deep"}, "defaults": {"r": "cheap"}, "floor": "standard"}
    assert _chosen(profile, tier="cheap") == ("deep", "override", "m-deep")


def test_the_profile_default_beats_the_caller_tier_and_the_floor():
    assert _chosen({"defaults": {"r": "deep"}}, tier="cheap") == ("deep", "profile_default", "m-deep")


def test_the_caller_tier_beats_the_floor():
    assert _chosen({}, tier="standard") == ("standard", "caller", "m-std")


def test_a_caller_tier_below_the_floor_is_raised_to_it():
    assert _chosen({"floor": "deep"}, tier="cheap") == ("deep", "caller raised to floor", "m-deep")


def test_a_tier_less_call_is_the_default_tier_requested_by_the_caller():
    stub = _Stub()
    decision = _run(stub, tier=None).decision
    assert (decision.requested_tier, decision.chosen_tier, decision.reason) == ("standard", "standard", "caller")
    assert (stub.calls[0]["model"], decision.effort) == ("m-std", "high")


def test_an_override_naming_the_requested_tier_is_still_reason_override():
    assert _chosen({"tier_overrides": {"r": "standard"}}, tier="standard") == ("standard", "override", "m-std")


def test_no_decision_ever_says_router_because_no_router_runs_here():
    profiles = [{}, {"floor": "deep"}, {"defaults": {"r": "deep"}}, {"tier_overrides": {"r": "cheap"}, "floor": "standard"}]
    assert not any("router" in _chosen(profile, tier="cheap")[1] for profile in profiles)


def test_an_override_below_the_floor_is_raised_to_it():
    got = _chosen({"tier_overrides": {"r": "cheap"}, "floor": "standard"})
    assert got == ("standard", "override raised to floor", "m-std")


def test_an_override_for_another_role_is_ignored():
    assert _chosen({"tier_overrides": {"other": "deep"}}, tier="cheap") == ("cheap", "caller", "m-cheap")


def test_a_profile_default_records_the_caller_tier_as_requested():
    decision = _run(_Stub(), {"defaults": {"r": "deep"}}, tier="cheap").decision
    assert (decision.requested_tier, decision.chosen_tier) == ("cheap", "deep")


def test_an_unknown_tier_is_a_runner_error():
    with pytest.raises(RunnerError):
        _run(_Stub(), tier="bogus")


def test_explicit_model_effort_and_budget_are_used_as_given_and_recorded_unclipped():
    stub = _Stub()
    decision = _run(stub, tier="cheap", model="m-x", effort="max", budget_usd=9.0).decision
    call = stub.calls[0]
    assert (call["model"], call["output_config"]["effort"]) == ("m-x", "max")
    assert (decision.model_id, decision.effort, decision.budget_usd, decision.clipped_by) == ("m-x", "max", 9.0, None)
    assert not any("budget" in key for key in call), "the budget is recorded, never sent: the Messages API cannot enforce it"


@pytest.mark.parametrize("field", ["model", "effort"])
def test_an_empty_explicit_model_or_effort_is_refused_not_read_as_unset(field):
    with pytest.raises(RunnerError, match=field):
        _run(_Stub(), **{field: " "})


def test_a_profile_may_declare_a_tier_outside_the_vocabulary_but_a_call_cannot_ask_for_it():
    profile = {"tiers": {**PROFILE["tiers"], "ultra": "m-ultra"}}
    with pytest.raises(RunnerError, match="ultra"):
        _run(_Stub(), profile, tier="ultra")
    assert _chosen(profile, tier="deep")[2] == "m-deep"


@pytest.mark.parametrize(
    ("block", "key"),
    [
        ({"defaults": {"r": "ultra"}}, "defaults"),
        ({"tier_overrides": {"r": "ultra"}}, "tier_overrides"),
        ({"defaults": ["r"]}, "defaults"),
        ({"floor": "ultra"}, "floor"),
    ],
)
def test_a_malformed_profile_block_is_refused_at_construction_and_names_its_key(block, key):
    with pytest.raises(RunnerError, match=key):
        AnthropicRunner({**PROFILE, **block}, client=_Stub())


def test_without_explicit_values_effort_follows_the_tier_and_budget_is_none():
    decision = _run(_Stub(), tier="cheap").decision
    assert (decision.effort, decision.budget_usd, decision.clipped_by) == ("low", None, None)
