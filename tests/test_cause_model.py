from typing import Any

import pytest

from harness.cause_model import WHY_CHARS, build_prompt, classify_with_model
from runner.protocol import BudgetStop, LimitStop, RunnerError


class StubRunner:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def test_a_valid_response_returns_the_cause_and_why_on_a_cheap_budgeted_call():
    runner = StubRunner({"cause": "code", "one_line_why": "the check failed"})
    assert classify_with_model(runner, "r", ["o1"], task="t1") == ("code", "the check failed")
    call = runner.calls[0]
    assert (call["tier"], call["budget_usd"], call["task"]) == ("cheap", 0.05, "t1")
    schema = call["schema"]
    assert schema["required"] == ["cause", "one_line_why"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["cause"]["enum"] == ["ticket", "code", "review", "harness", "unknown"]
    assert schema["properties"]["one_line_why"] == {"type": "string"}


def test_the_prompt_carries_the_reasoning_each_objection_and_every_cause():
    prompt = build_prompt("because X", ["first", "second"])
    assert all(part in prompt for part in ("because X", "1. first", "2. second", "ticket", "code", "review", "harness"))


def test_a_multi_line_or_long_why_comes_back_as_one_bounded_line():
    long_line = "a" * (WHY_CHARS + 50)
    runner = StubRunner({"cause": "ticket", "one_line_why": f"\n  {long_line}\nsecond line"})
    assert classify_with_model(runner, "r", []) == ("ticket", "a" * WHY_CHARS)


def test_a_cause_outside_the_set_is_unknown_and_names_the_value():
    cause, why = classify_with_model(StubRunner({"cause": "gremlins", "one_line_why": "x"}), "r", [])
    assert (cause, why) == ("unknown", "model returned cause outside the closed set: 'gremlins'")


def test_a_malformed_response_is_unknown():
    for answer in (
        {"cause": "code"},
        {"cause": ["code"], "one_line_why": "x"},
        {"cause": "code", "one_line_why": " \n"},
        {},
    ):
        cause, why = classify_with_model(StubRunner(answer), "r", [])
        assert (cause, why.startswith("model output malformed:")) == ("unknown", True)


@pytest.mark.parametrize(
    "error",
    [
        RunnerError("boom"),
        BudgetStop(role="cause_classify", thread=None, session=None, spent_usd=0.05, detail="cap hit"),
        KeyError("k"),
    ],
)
def test_a_runner_failure_is_unknown_and_does_not_raise(error: Exception):
    cause, why = classify_with_model(StubRunner(error), "r", [])
    assert (cause, why.startswith(f"model call failed: {type(error).__name__}:")) == ("unknown", True)


def test_a_session_limit_propagates_and_is_never_a_task_cause():
    with pytest.raises(LimitStop):
        classify_with_model(StubRunner(LimitStop(detail="session limit reached")), "r", [])
