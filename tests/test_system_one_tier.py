import pytest
import yaml

import runner.anthropic_runner as anthropic_runner
from harness import runners
from harness.runners import build_runner
from runner.protocol import NodeResult
from runner.system_one import Answer, Choice, FastPathRunner, Noul, RoleSetting, Score
from runner.system_one_specs import role_specs
from runner.system_one_tier import TierDecider, _answer, _prompt, _schema


class _Fake:
    def __init__(self, result):
        self.result, self.calls = result, []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_it_asks_the_cheap_tier_once_as_system_one_with_criteria_then_fields():
    fake = _Fake({"answer": "yes", "confidence": 0.75})
    answer = TierDecider(fake).decide(Noul("Is it done?"), {"plan": "add x", "summary": "did x"})
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert (call["role"], call["tier"]) == ("system_one", "cheap")
    assert call["prompt"] == (
        "Is it done?\n\n## plan\nadd x\n\n## summary\ndid x\n\nGive your answer and a confidence from 0 to 1."
    )
    assert call["schema"] == _schema(Noul("Is it done?"))
    assert answer == Answer("noul", "yes", {"yes": 0.75, "no": 0.25}, 0.75)


def test_the_tier_is_configurable():
    fake = _Fake({"answer": "no", "confidence": 1})
    TierDecider(fake, tier="reason").decide(Noul("q"), {})
    assert fake.calls[0]["tier"] == "reason"


@pytest.mark.parametrize(
    ("question", "enum"),
    [
        (Noul("q"), ["yes", "no"]),
        (Choice("q", ("approve", "revise", "reject")), ["approve", "revise", "reject"]),
        (Score("q", ("low", "high")), ["low", "high"]),
    ],
)
def test_the_schema_enum_follows_the_question_kind(question, enum):
    schema = _schema(question)
    assert schema["properties"]["answer"]["enum"] == enum
    assert schema["properties"]["confidence"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert schema["required"] == ["answer", "confidence"]


def test_kind_follows_the_question():
    assert TierDecider(_Fake({"answer": "a", "confidence": 1})).decide(Choice("q", ("a", "b")), {}).kind == "choice"
    assert TierDecider(_Fake({"answer": "a", "confidence": 1})).decide(Score("q", ("a", "b")), {}).kind == "score"


def test_the_remainder_is_split_evenly_over_the_other_values():
    assert _answer("choice", "b", 0.8, ("a", "b", "c")).probabilities == {
        "b": 0.8,
        "a": pytest.approx(0.1),
        "c": pytest.approx(0.1),
    }


def test_confidence_is_clamped_to_the_unit_interval():
    high, low = _answer("noul", "yes", 1.4, ("yes", "no")), _answer("noul", "yes", -0.2, ("yes", "no"))
    assert (high.confidence, high.probabilities) == (1.0, {"yes": 1.0, "no": 0.0})
    assert (low.confidence, low.probabilities) == (0.0, {"yes": 0.0, "no": 1.0})


def test_a_single_value_does_not_divide_by_zero():
    assert _answer("choice", "a", 0.6, ("a",)).probabilities == {"a": 0.6}


@pytest.mark.parametrize(
    "result",
    [
        {"answer": "maybe", "confidence": 0.5},
        {"confidence": 0.5},
        {"answer": "yes"},
        {"answer": "yes", "confidence": "x"},
        {"answer": "yes", "confidence": float("nan")},
    ],
)
def test_a_bad_result_raises(result):
    with pytest.raises((KeyError, ValueError)):
        TierDecider(_Fake(result)).decide(Noul("q"), {})


def test_a_runner_error_propagates():
    class Boom:
        def run(self, **kwargs):
            raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        TierDecider(Boom()).decide(Noul("q"), {})


def test_the_prompt_with_no_state_is_the_criteria_and_the_ask():
    assert _prompt(Noul("q"), {}) == "q\n\nGive your answer and a confidence from 0 to 1."


def test_backend_finds_the_built_in_with_no_entry_points(monkeypatch):
    monkeypatch.setattr(runners.importlib.metadata, "entry_points", lambda **kw: [])
    constructor = runners._backend("model-tier")
    assert constructor is runners._model_tier and constructor.wants_runner is True
    assert runners._backend("nope") is None


class _Real:
    def __init__(self, profile=None, **kwargs):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["role"] == "system_one":
            return NodeResult({"answer": "no", "confidence": 0.9})
        return NodeResult({"complete": True})


def _profile(tmp_path, **over):
    block = {
        "backend": "model-tier",
        "model": "cheap-tier-1",
        "roles": {"handoff": {"mode": "shadow", "threshold": 0.9}},
    }
    body = {
        "profile": "p",
        "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
        "system_one": block | over,
    }
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def test_decider_passes_the_real_runner_and_the_block_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic_runner, "AnthropicRunner", _Real)
    wrapped = build_runner(scripted=None, provider_profile=_profile(tmp_path, tier="reason"))
    assert isinstance(wrapped, FastPathRunner)
    assert wrapped._decider.runner is wrapped._inner
    assert wrapped._decider.tier == "reason"


def test_the_tier_defaults_to_cheap(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic_runner, "AnthropicRunner", _Real)
    assert build_runner(scripted=None, provider_profile=_profile(tmp_path))._decider.tier == "cheap"


def test_a_shadow_role_wrapped_with_model_tier_leaves_the_real_result_untouched():
    real = _Real()
    fast = FastPathRunner(
        real, TierDecider(real), {"handoff": RoleSetting("shadow", 0.9)}, role_specs(), backend="cheap-tier-1"
    )
    prompt = "\nPlan: p\nSummary: s\nChange facts: f\nThe facts listed under Change facts are measured."
    result = fast.run(role="handoff", schema={"type": "object"}, prompt=prompt)
    assert dict(result) == {"complete": True}
    assert sorted(c["role"] for c in real.calls) == ["handoff", "system_one"]
    assert {c["role"]: c.get("tier") for c in real.calls}["system_one"] == "cheap"
