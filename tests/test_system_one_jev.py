from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from runner.protocol import NodeResult
from runner.system_one import Answer, Choice, DecisionRunner, FastPathRunner, Noul, RoleSetting, RoleSpec, Score
from runner.system_one_jev import JevDecider, clip, clip_question, clip_state, to_state


class TypeSafeError(Exception):
    """Shaped like the SDK's error base."""


class TypeSafeRateLimitError(TypeSafeError):
    pass


class TypeSafeAuthenticationError(TypeSafeError):
    pass


class TypeSafeAPITimeoutError(TypeSafeError):
    pass


class FakeClient:
    def __init__(self, answer=None, error=None, **kwargs):
        self.init_kwargs, self.answer, self.error = kwargs, answer, error
        self.calls, self.closed = [], False

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(answers=dict.fromkeys(questions, self.answer))

    def close(self):
        self.closed = True


class FakeType:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


FAKE_TYPES = SimpleNamespace(
    Noul=type("Noul", (FakeType,), {}), Choice=type("Choice", (FakeType,), {}), Score=type("Score", (FakeType,), {})
)


def decider(client):
    return JevDecider("jev-1.13.0", "key", lambda model, key: (client, FAKE_TYPES))


def test_a_noul_request_carries_instructions_and_a_named_question():
    client = FakeClient(SimpleNamespace(noul=0.9))
    decider(client).decide(Noul("Is it done?"), {"plan": "p"})
    state, questions = client.calls[0]
    assert state == {"plan": "p"}
    assert list(questions) == ["q"]
    assert isinstance(questions["q"], FAKE_TYPES.Noul)
    assert questions["q"].kwargs == {"instructions": "Is it done?"}


def test_a_choice_request_uses_the_options_as_criteria_keys():
    client = FakeClient(SimpleNamespace(choice="a", confidence=0.7, probabilities={"a": 0.7, "b": 0.3}))
    decider(client).decide(Choice("Pick", ("a", "b")), {})
    question = client.calls[0][1]["q"]
    assert isinstance(question, FAKE_TYPES.Choice)
    assert question.kwargs == {"criteria": {"a": None, "b": None}, "instructions": "Pick"}


def test_a_score_request_uses_the_levels_as_an_ordered_criteria_list():
    client = FakeClient(SimpleNamespace(score=1.4, confidence=0.6, probabilities={0: 0.1, 1: 0.6, 2: 0.3}))
    decider(client).decide(Score("Rate", ("bad", "ok", "good")), {})
    question = client.calls[0][1]["q"]
    assert isinstance(question, FAKE_TYPES.Score)
    assert question.kwargs == {"criteria": ["bad", "ok", "good"], "instructions": "Rate"}


def test_a_noul_answer_maps_the_probability_of_yes_and_derives_confidence_as_the_picked_side():
    answer = decider(FakeClient(SimpleNamespace(noul=0.9))).decide(Noul("q"), {})
    assert answer == Answer("noul", "yes", {"yes": 0.9, "no": pytest.approx(0.1)}, 0.9)
    assert decider(FakeClient(SimpleNamespace(noul=0.25))).decide(Noul("q"), {}).value == "no"


def test_a_choice_answer_maps_the_choice_probabilities_and_confidence():
    sdk = SimpleNamespace(choice="b", confidence=0.8, probabilities={"a": 0.2, "b": 0.8})
    assert decider(FakeClient(sdk)).decide(Choice("q", ("a", "b")), {}) == Answer(
        "choice", "b", {"a": 0.2, "b": 0.8}, 0.8
    )


def test_a_score_answer_carries_the_weighted_score_as_text_not_a_level():
    sdk = SimpleNamespace(score=1.5, confidence=0.5, probabilities={1: 0.5, 2: 0.5})
    assert decider(FakeClient(sdk)).decide(Score("q", ("x", "y", "z")), {}) == Answer(
        "score", "1.5", {"1": 0.5, "2": 0.5}, 0.5
    )


def test_the_client_is_built_once_and_kept_open_across_decisions_until_closed():
    built = []
    client = FakeClient(SimpleNamespace(noul=0.5))

    def factory(model, key):
        built.append(model)
        return client, FAKE_TYPES

    jev = JevDecider("jev-1.13.0", "key", factory)
    jev.decide(Noul("q"), {})
    jev.decide(Noul("q"), {})
    assert (built, len(client.calls), client.closed) == (["jev-1.13.0"], 2, False)
    jev.close()
    assert client.closed


def test_a_decider_used_as_a_context_manager_closes_its_client():
    client = FakeClient(SimpleNamespace(noul=0.5))
    with decider(client) as jev:
        jev.decide(Noul("q"), {})
    assert client.closed


def test_a_pinned_model_is_accepted():
    assert isinstance(decider(FakeClient()), DecisionRunner)


@pytest.mark.parametrize("model", ["jev-latest", "jev", "", "JEV-LATEST"])
def test_an_unpinned_model_is_refused(model):
    with pytest.raises(ValueError):
        JevDecider(model, "key", lambda m, k: (FakeClient(), FAKE_TYPES))


@pytest.mark.parametrize("error", [TypeSafeRateLimitError, TypeSafeAuthenticationError, TypeSafeAPITimeoutError])
def test_an_sdk_error_propagates_from_decide_and_the_fast_path_runs_the_inner_runner(error):
    class Inner:
        def run(self, **kwargs):
            return NodeResult({"complete": False})

    jev = decider(FakeClient(error=error("down")))
    with pytest.raises(TypeSafeError):
        jev.decide(Noul("q"), {})
    spec = RoleSpec(build=lambda r: (Noul("q"), {}), render=lambda a: {"complete": True}, agrees=lambda a, r: True)
    runner = FastPathRunner(Inner(), jev, {"r": RoleSetting("on", 0.5)}, {"r": spec})
    assert runner.run(role="r", schema={}, prompt="p") == {"complete": False}


def test_the_default_factory_passes_keyword_args_to_the_sdk_client(monkeypatch):
    made = []

    class TypeSafeClient(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(SimpleNamespace(noul=1.0), **kwargs)
            made.append(self)

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(TypeSafeClient=TypeSafeClient, **vars(FAKE_TYPES)))
    JevDecider("jev-1.13.0", "secret").decide(Noul("q"), {"a": "b"})
    assert made[0].init_kwargs == {"api_key": "secret", "model": "jev-1.13.0"}
    assert made[0].calls[0][0] == {"a": "b"}


def test_a_missing_sdk_fails_loudly_at_construction_naming_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    with pytest.raises(ImportError, match="system-one-jev"):
        JevDecider("jev-1.13.0", "key")


def test_clip_cuts_to_the_character_estimate_of_the_token_budget():
    assert clip("x" * 100, 10) == "x" * 40


def test_the_last_state_field_is_clipped_first():
    state = {"first": "a" * 30, "second": "b" * 30, "third": "c" * 30}
    assert clip_state(state, 10) == {"first": "a" * 30, "second": "b" * 10}


def test_a_state_within_budget_is_kept_whole_and_in_order():
    state = {"plan": "p", "facts": "f"}
    assert list(to_state(state).items()) == list(state.items())


def test_an_oversize_noul_question_is_clipped_in_the_request():
    client = FakeClient(SimpleNamespace(noul=0.5))
    decider(client).decide(Noul("x" * 200_000), {})
    assert len(client.calls[0][1]["q"].kwargs["instructions"]) == 128_000


def test_an_oversize_choice_is_clipped_across_options_and_instructions_within_the_budget():
    options = ("a" * 200_000, "b" * 200_000)
    instructions, labels = clip_question(Choice("i" * 200_000, options))
    assert [len(label) for label in labels] == [42_666, 42_666]
    assert len(instructions) + sum(map(len, labels)) == 128_000


def test_an_oversize_score_is_clipped_across_levels_and_instructions_within_the_budget():
    instructions, labels = clip_question(Score("i" * 200_000, ("x" * 200_000, "y" * 5)))
    assert [len(label) for label in labels] == [42_666, 5]
    assert len(instructions) + sum(map(len, labels)) == 128_000


def test_a_clipped_choice_label_is_mapped_back_to_the_original_option():
    options = ("a" * 200_000, "b" * 200_000)
    _, labels = clip_question(Choice("q", options))
    sdk = SimpleNamespace(choice=labels[1], confidence=0.9, probabilities={labels[0]: 0.1, labels[1]: 0.9})
    answer = decider(FakeClient(sdk)).decide(Choice("q", options), {})
    assert (answer.value, dict(answer.probabilities)) == (options[1], {options[0]: 0.1, options[1]: 0.9})


def test_choice_options_that_collide_after_clipping_are_refused_so_the_llm_runs():
    with pytest.raises(ValueError, match="collide"):
        decider(FakeClient()).decide(Choice("q", ("s" * 200_000 + "1", "s" * 200_000 + "2")), {})
