from __future__ import annotations

import pytest

from runner.decision_log import CallDecision
from runner.protocol import NodeResult
from runner.system_one import (
    Answer,
    Choice,
    ConfigError,
    DecisionRunner,
    FastPathRunner,
    Noul,
    RoleSetting,
    RoleSpec,
    Score,
    SystemOneConfig,
    parse_system_one_block,
)
from runner.system_one_specs import role_specs

DECISION = CallDecision(
    role="r", requested_tier="cheap", chosen_tier="cheap", model_id="m", reason="x", ticket_key="t", outcome_key="o"
)
SPEC = RoleSpec(
    build=lambda req: (Noul("is it ok"), {"prompt": req["prompt"]}),
    render=lambda a: {"verdict": a.value},
    agrees=lambda a, r: r["verdict"] == a.value,
)
CALL = {"role": "r", "schema": {"type": "object"}, "prompt": "p", "context": ("c",)}


class Decider:
    def __init__(self, confidence: float = 0.9, boom: bool = False) -> None:
        self.calls: list[tuple] = []
        self.confidence, self.boom = confidence, boom

    def decide(self, question, state):
        self.calls.append((question, dict(state)))
        if self.boom:
            raise RuntimeError("backend down")
        return Answer("noul", "yes", {"yes": self.confidence}, self.confidence)


class Inner:
    def __init__(self, decision: CallDecision | None = DECISION) -> None:
        self.calls: list[dict] = []
        self.result = NodeResult({"verdict": "yes"})
        self.result.decision = decision

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def runner(mode: str, decider: Decider, inner: Inner, threshold: float = 0.8, specs=None) -> FastPathRunner:
    return FastPathRunner(
        inner,
        decider,
        {"r": RoleSetting(mode, threshold)},
        {"r": SPEC} if specs is None else specs,
        backend="jev-1.13.0",
    )


def test_choice_and_score_bounds_hold_on_construction():
    assert len(Choice("c", ("x",) * 255).options) == 255
    assert Score("c", ("a", "b")).levels == ("a", "b")
    assert len(Score("c", ("a",) * 10).levels) == 10
    for bad in (lambda: Choice("c", ("x",) * 256), lambda: Score("c", ("a",)), lambda: Score("c", ("a",) * 11)):
        with pytest.raises(ValueError):
            bad()


def test_a_stub_satisfies_the_decision_runner_protocol():
    assert isinstance(Decider(), DecisionRunner)


def test_role_setting_refuses_a_bad_mode_or_threshold():
    assert RoleSetting("shadow", 0.8).threshold == 0.8
    for mode, threshold in (("maybe", 0.5), ("on", 1.5), ("on", -0.1)):
        with pytest.raises(ValueError):
            RoleSetting(mode, threshold)


def test_parse_accepts_a_pinned_version():
    block = {"backend": "jev", "model": "jev-1.13.0", "roles": {"r": {"mode": "shadow", "threshold": 0.9}}}
    assert parse_system_one_block(block) == SystemOneConfig("jev", "jev-1.13.0", {"r": RoleSetting("shadow", 0.9)})


@pytest.mark.parametrize("model", ["jev-latest", "jev", "JEV-Latest", ""])
def test_parse_rejects_a_floating_or_unversioned_model(model):
    assert isinstance(parse_system_one_block({"backend": "jev", "model": model, "roles": {}}), ConfigError)


@pytest.mark.parametrize(
    "entry", [{"mode": "maybe", "threshold": 0.5}, {"mode": "on", "threshold": 2}, {"mode": "on"}, "on"]
)
def test_parse_rejects_a_bad_role_entry(entry):
    result = parse_system_one_block({"backend": "jev", "model": "jev-1.13.0", "roles": {"r": entry}})
    assert isinstance(result, ConfigError)
    assert "roles.r" in result.error


def test_off_returns_the_inner_result_object_without_asking():
    decider, inner = Decider(), Inner()
    assert runner("off", decider, inner).run(**CALL) is inner.result
    assert decider.calls == []


def test_a_role_with_no_spec_returns_the_inner_result():
    decider, inner = Decider(), Inner()
    assert runner("on", decider, inner, specs={}).run(**CALL) is inner.result
    assert decider.calls == []


def test_an_absent_role_returns_the_inner_result():
    decider, inner = Decider(), Inner()
    assert FastPathRunner(inner, decider, {}, {"r": SPEC}).run(**CALL) is inner.result
    assert decider.calls == []


def test_shadow_calls_both_and_the_decision_carries_the_six_fields():
    decider, inner = Decider(0.9), Inner()
    result = runner("shadow", decider, inner).run(**CALL)
    assert len(decider.calls) == 1 and len(inner.calls) == 1
    assert decider.calls[0] == (Noul("is it ok"), {"prompt": "p"})
    assert dict(result) == {"verdict": "yes"}
    d = result.decision
    assert (d.system_one_backend, d.system_one_mode, d.system_one_answer) == ("jev-1.13.0", "shadow", "yes")
    assert (d.system_one_confidence, d.system_one_threshold, d.system_one_agreed) == (0.9, 0.8, True)
    assert inner.result.decision is DECISION and DECISION.system_one_mode is None


def test_shadow_records_disagreement():
    inner = Inner()
    inner.result["verdict"] = "no"
    assert runner("shadow", Decider(), inner).run(**CALL).decision.system_one_agreed is False


def test_shadow_without_an_inner_decision_returns_the_inner_result():
    inner = Inner(decision=None)
    assert runner("shadow", Decider(), inner).run(**CALL) is inner.result


def test_on_above_threshold_does_not_call_inner():
    decider, inner = Decider(0.9), Inner()
    result = runner("on", decider, inner).run(**CALL)
    assert dict(result) == {"verdict": "yes"} and result is not inner.result
    assert inner.calls == []


def test_on_at_the_threshold_takes_the_fast_path():
    inner = Inner()
    runner("on", Decider(0.8), inner).run(**CALL)
    assert inner.calls == []


def test_on_below_threshold_calls_inner():
    decider, inner = Decider(0.5), Inner()
    assert runner("on", decider, inner).run(**CALL) is inner.result
    assert len(decider.calls) == 1 and len(inner.calls) == 1


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_a_raising_decider_falls_back_to_inner_and_mutates_nothing(mode):
    inner = Inner()
    settings, specs = {"r": RoleSetting(mode, 0.8)}, {"r": SPEC}
    fast = FastPathRunner(inner, Decider(boom=True), settings, specs)
    call = {**CALL, "context": ["c"]}
    before = (dict(settings), dict(specs), dict(call["schema"]), list(call["context"]))
    assert fast.run(**call) is inner.result
    assert len(inner.calls) == 1 and inner.calls[0]["prompt"] == "p"
    assert (dict(settings), dict(specs), dict(call["schema"]), list(call["context"])) == before


def test_inner_receives_every_argument_unchanged():
    inner = Inner()
    runner("off", Decider(), inner).run(**CALL, tier="deep", thread="th", budget_usd=1.5, task="t1")
    assert inner.calls[0] == {**CALL, "tier": "deep", "hints": None, "thread": "th", "budget_usd": 1.5, "task": "t1"}


def test_role_specs_is_a_fresh_mapping():
    assert role_specs() is not role_specs()
