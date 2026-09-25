from __future__ import annotations

import warnings
from types import SimpleNamespace
from typing import Any

import pytest

from runner.anthropic_runner import ROUTER_ON_WARNING, AnthropicRunner, load_provider_profile
from runner.decision_log import RouterDecision
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


CLASSES_PROFILE = {"classes": {"reason": ["c-first", "c-second"], "frontier": ["c-front"]}}


def test_a_class_resolves_to_the_first_entry_of_its_list_and_is_recorded_as_the_chosen_tier():
    stub = _Stub()
    decision = _run(stub, CLASSES_PROFILE, tier="reason").decision
    assert stub.calls[0]["model"] == "c-first"
    assert (decision.chosen_tier, decision.reason) == ("reason", "caller")


def test_frontier_keeps_its_class_and_takes_the_deep_effort():
    stub = _Stub()
    decision = _run(stub, CLASSES_PROFILE, tier="frontier").decision
    assert (stub.calls[0]["model"], decision.chosen_tier, decision.effort) == ("c-front", "frontier", "xhigh")


def test_a_legacy_tier_resolves_through_its_class_when_the_profile_lists_it():
    stub = _Stub()
    decision = _run(stub, CLASSES_PROFILE, tier="standard").decision
    assert (decision.chosen_tier, stub.calls[0]["model"]) == ("reason", "c-first")


def test_a_profile_without_classes_uses_the_tiers_map_for_a_class_name():
    assert _chosen({}, tier="reason") == ("standard", "caller", "m-std")


def test_a_class_missing_from_the_classes_map_falls_back_to_the_tiers_map():
    assert _chosen(CLASSES_PROFILE, tier="extract") == ("cheap", "caller", "m-cheap")


def test_an_explicit_model_beats_the_class_entry_and_the_tier_is_not_rewritten():
    stub = _Stub()
    decision = _run(stub, CLASSES_PROFILE, tier="reason", model="m-x").decision
    assert (stub.calls[0]["model"], decision.chosen_tier) == ("m-x", "standard")


def test_a_class_name_is_accepted_as_a_profile_default():
    assert _chosen({"defaults": {"r": "judge"}}, tier="cheap")[:2] == ("deep", "profile_default")


@pytest.mark.parametrize("classes", ["x", {"reason": "c-first"}])
def test_a_malformed_classes_block_is_refused_at_construction(classes):
    with pytest.raises(RunnerError, match="classes"):
        AnthropicRunner({**PROFILE, "classes": classes}, client=_Stub())


def test_the_runner_imports_neither_cartridges_nor_the_core_router():
    import ast

    import runner.anthropic_runner as module

    with open(module.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [name for name in names if name.split(".")[0] == "cartridges" or name.startswith("core.router")]


ROUTED = RouterDecision(
    chosen_class="judge", model="m-routed", effort="max", budget_usd=2.5, reasons=("a", "b"), clipped_by=("chair",)
)
ROUTER_FIELDS = ("router_tier", "router_reason", "router_model", "router_effort", "router_budget_usd", "router_clipped_by")
RECORDED = ("judge", "a; b", "m-routed", "max", 2.5, ("chair",))


def _router_fields(decision: Any) -> tuple[Any, ...]:
    return tuple(getattr(decision, name) for name in ROUTER_FIELDS)


def test_shadow_copies_the_supplied_decision_and_still_runs_the_resolvers_model():
    stub = _Stub()
    decision = _run(stub, {"router": "shadow"}, tier="cheap", router_decision=ROUTED).decision
    assert _router_fields(decision) == RECORDED
    assert (stub.calls[0]["model"], decision.chosen_tier, decision.clipped_by) == ("m-cheap", "cheap", None)


@pytest.mark.parametrize("profile", [{}, {"router": "off"}])
def test_off_ignores_a_supplied_decision(profile):
    assert _router_fields(_run(_Stub(), profile, router_decision=ROUTED).decision) == (None,) * 6


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_no_decision_leaves_the_router_fields_none(mode):
    assert _router_fields(_run(_Stub(), {"router": mode}).decision) == (None,) * 6


def test_on_acts_as_shadow_and_warns_once_per_process_across_runners():
    stub = _Stub()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        runners = [AnthropicRunner({**PROFILE, "router": "on"}, client=stub) for _ in range(2)]
        decisions = [r.run(role="r", schema={}, prompt="p", tier="cheap", router_decision=ROUTED).decision for r in runners * 2]
    assert [_router_fields(d) for d in decisions] == [RECORDED] * 4
    assert {(d.chosen_tier, d.reason) for d in decisions} == {("cheap", "caller")}
    assert {call["model"] for call in stub.calls} == {"m-cheap"}
    assert [str(w.message) for w in caught] == [ROUTER_ON_WARNING]


def test_on_without_a_decision_does_not_warn():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _run(_Stub(), {"router": "on"})
    assert caught == []


def test_a_supplied_decision_never_changes_the_class_model_the_runner_calls():
    stub = _Stub()
    decision = _run(stub, {**CLASSES_PROFILE, "router": "shadow"}, tier="reason", router_decision=ROUTED).decision
    assert (stub.calls[0]["model"], decision.chosen_tier, decision.reason, decision.router_model) == (
        "c-first",
        "reason",
        "caller",
        "m-routed",
    )


@pytest.mark.parametrize(("line", "mode"), [("router: off", "off"), ("router: on", "on"), ("router: shadow", "shadow"), ("", "off")])
def test_a_yaml_profile_with_an_unquoted_mode_loads(tmp_path, line, mode):
    path = tmp_path / "profile.yaml"
    path.write_text(f"tiers:\n  standard: m-std\n{line}\n", encoding="utf-8")
    assert AnthropicRunner(load_provider_profile(path), client=_Stub()).router_mode == mode


def test_an_unknown_router_mode_is_refused_at_construction():
    with pytest.raises(RunnerError, match="router"):
        AnthropicRunner({**PROFILE, "router": "bogus"}, client=_Stub())
