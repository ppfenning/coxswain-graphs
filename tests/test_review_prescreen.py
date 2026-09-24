"""The review pre-screen picks a depth for the charter review. It never skips a review."""

from __future__ import annotations

import pytest

from graphs.delivery import lifecycle_propose
from graphs.delivery.lifecycle_propose import review_depth
from runner import ScriptedRunner
from runner.decision_log import CallDecision
from runner.system_one import Answer, Choice, FastPathRunner, RoleSetting
from runner.system_one_specs import role_specs

SPEC = role_specs()["review_charter"]
PATCH = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
PROMPT = (
    "Review this change against the team's own written charter in your context.\n\n"
    "Task: fix the bug\nSummary: did it\nChange facts: {}\n"
    f"Patch:\n{PATCH}\n\nCite the charter principle behind every finding."
)
APPROVE = {"verdict": "approve", "findings": [], "rationale": "ok"}
ADV_APPROVES = {"verdict": "approve", "objections": [], "strongest_objection": "none"}
ADV_OBJECTS = {"verdict": "revise", "objections": [], "strongest_objection": "the fixture leaks state"}
ARBITRATION = {"verdict": "revise", "sided_with": "adversary", "reasoning": "the objection holds"}


def _answer(value: str, confidence: float) -> Answer:
    return Answer("choice", value, {value: confidence}, confidence)


@pytest.mark.parametrize(
    "answer,depth",
    [
        (_answer("approve", 0.9), "cheap"),
        (_answer("approve", 0.8), "cheap"),
        (_answer("approve", 0.5), "deep"),
        (_answer("revise", 0.99), "deep"),
        (_answer("reject", 0.99), "deep"),
        (None, "deep"),
    ],
)
def test_review_depth_is_cheap_only_for_a_confident_approve(answer: Answer | None, depth: str) -> None:
    assert review_depth(answer, 0.8) == depth


def test_the_spec_asks_a_choice_of_approve_revise_reject_over_patch_and_plan() -> None:
    question, state = SPEC.build({"prompt": PROMPT})
    assert isinstance(question, Choice)
    assert question.options == ("approve", "revise", "reject")
    assert state == {"patch": PATCH, "plan": "fix the bug"}


def test_agrees_compares_the_predicted_choice_with_the_review_verdict() -> None:
    assert SPEC.agrees(_answer("approve", 0.9), {"verdict": "approve"}) is True
    assert SPEC.agrees(_answer("approve", 0.9), {"verdict": "revise"}) is False


def test_render_never_produces_a_review() -> None:
    with pytest.raises(ValueError):
        SPEC.render(_answer("approve", 0.99))


class _Decider:
    def __init__(self, answer: Answer | None) -> None:
        self.answer, self.calls = answer, 0

    def decide(self, question, state) -> Answer:
        self.calls += 1
        if self.answer is None:
            raise RuntimeError("backend down")
        return self.answer


class _Decisioned(ScriptedRunner):
    """A ScriptedRunner whose results carry the decision record a real runner attaches."""

    def run(self, **kwargs):
        result = super().run(**kwargs)
        result.decision = CallDecision(
            role=kwargs["role"],
            requested_tier="cheap",
            chosen_tier="cheap",
            model_id="m",
            reason="x",
            ticket_key="t",
            outcome_key="o",
        )
        return result


class _Tap:
    """Records what the graph saw come back from the fast-path runner, decision included."""

    def __init__(self, fast: FastPathRunner) -> None:
        self.fast, self.results = fast, {}

    def consult(self, role, request):
        return self.fast.consult(role, request)

    def run(self, **kwargs):
        result = self.fast.run(**kwargs)
        self.results[kwargs["role"]] = result
        return result


def _fast(mode: str, answer: Answer | None, adversary=ADV_APPROVES):
    inner = _Decisioned(
        {
            "plan": None,
            "build": None,
            "review_charter": APPROVE,
            "review_adversary": adversary,
            "arbitrate": ARBITRATION,
        }
    )
    decider = _Decider(answer)
    fast = FastPathRunner(inner, decider, {"review_charter": RoleSetting(mode, 0.8)}, role_specs())
    return inner, decider, fast


def _run(cartridge, plan_response, build_response, mode: str, answer: Answer | None, adversary=ADV_APPROVES):
    inner, decider, fast = _fast(mode, answer, adversary)
    inner._responses["plan"], inner._responses["build"] = [plan_response], [build_response]
    cartridge["skills"]["review_adversary"] = "acme-skills:review_adversary"
    cartridge["skills"]["arbitrate"] = "acme-skills:arbitrate"
    tap = _Tap(fast)
    call_args = {"run_id": "run-1", "date": "2026-08-30", "ticket": "TICKET-1", "cartridge": cartridge}
    result = lifecycle_propose.run(call_args, tap)
    return result, inner, decider, tap


def _judgments(inner: ScriptedRunner, role: str) -> list[str | None]:
    return [call["hints"].judgment for call in inner.calls if call["role"] == role]


def test_a_live_confident_approve_lowers_the_charter_hints_and_still_runs_both_reviewers(
    cartridge, plan_response, build_response
) -> None:
    result, inner, decider, _ = _run(cartridge, plan_response, build_response, "on", _answer("approve", 0.9))
    assert _judgments(inner, "review_charter") == ["low"]
    assert _judgments(inner, "review_adversary") == [None]
    assert result["review"]["verdict"] == "approve"
    assert decider.calls == 1


@pytest.mark.parametrize(
    "answer",
    [_answer("approve", 0.5), _answer("revise", 0.99), _answer("reject", 0.99), None],
    ids=["unconfident", "revise", "reject", "decider_down"],
)
def test_a_live_deep_depth_runs_both_reviewers_with_todays_hints(
    cartridge, plan_response, build_response, answer
) -> None:
    _, inner, _, _ = _run(cartridge, plan_response, build_response, "on", answer)
    assert _judgments(inner, "review_charter") == [None]
    assert _judgments(inner, "review_adversary") == [None]


def test_shadow_runs_the_reviews_as_today_and_logs_the_answer_the_depth_came_from(
    cartridge, plan_response, build_response
) -> None:
    _, inner, decider, tap = _run(cartridge, plan_response, build_response, "shadow", _answer("approve", 0.9))
    assert _judgments(inner, "review_charter") == [None]
    assert _judgments(inner, "review_adversary") == [None]
    assert decider.calls == 1, "consulted once; the run reused that answer"
    logged = tap.results["review_charter"].decision
    assert (logged.system_one_mode, logged.system_one_answer) == ("shadow", "approve")
    assert (logged.system_one_confidence, logged.system_one_threshold, logged.system_one_agreed) == (0.9, 0.8, True)
    assert (
        review_depth(_answer(logged.system_one_answer, logged.system_one_confidence), logged.system_one_threshold)
        == "cheap"
    )


def test_a_role_that_is_off_is_never_consulted(cartridge, plan_response, build_response) -> None:
    _, inner, decider, _ = _run(cartridge, plan_response, build_response, "off", _answer("approve", 0.9))
    assert decider.calls == 0
    assert _judgments(inner, "review_charter") == [None]


def test_a_runner_with_no_pre_screen_gets_todays_hints(cartridge, plan_response, build_response) -> None:
    scripted = ScriptedRunner(
        {"plan": plan_response, "build": build_response, "review_charter": APPROVE, "review_adversary": ADV_APPROVES}
    )
    cartridge["skills"]["review_adversary"] = "acme-skills:review_adversary"
    lifecycle_propose.run({"run_id": "r", "date": "2026-08-30", "ticket": "T-1", "cartridge": cartridge}, scripted)
    assert _judgments(scripted, "review_charter") == [None]
    assert _judgments(scripted, "review_adversary") == [None]


def test_a_cheap_depth_leaves_the_arbiter_decision_alone(cartridge, plan_response, build_response) -> None:
    _, inner, _, _ = _run(
        cartridge, plan_response, build_response, "on", _answer("approve", 0.9), adversary=ADV_OBJECTS
    )
    assert [c["role"] for c in inner.calls if c["role"].startswith(("review_", "arbitrate"))] == [
        "review_charter",
        "review_adversary",
        "arbitrate",
    ]
    assert _judgments(inner, "arbitrate") == ["high"]


def test_a_confident_approve_in_mode_on_still_runs_the_real_review() -> None:
    inner = ScriptedRunner({"review_charter": APPROVE})
    fast = FastPathRunner(
        inner, _Decider(_answer("approve", 0.99)), {"review_charter": RoleSetting("on", 0.8)}, role_specs()
    )
    assert dict(fast.run(role="review_charter", schema={}, prompt=PROMPT)) == APPROVE
    assert [call["role"] for call in inner.calls] == ["review_charter"]


def test_consult_is_none_without_a_setting_and_a_failing_decider_is_none() -> None:
    inner = ScriptedRunner({})
    request = {"prompt": PROMPT}
    assert (
        FastPathRunner(inner, _Decider(_answer("approve", 0.9)), {}, role_specs()).consult("review_charter", request)
        is None
    )
    down = FastPathRunner(inner, _Decider(None), {"review_charter": RoleSetting("on", 0.8)}, role_specs())
    assert down.consult("review_charter", request) is None


def test_a_consulted_answer_is_used_by_one_run_and_not_the_next() -> None:
    decider = _Decider(_answer("approve", 0.9))
    fast = FastPathRunner(
        _Decisioned({"review_charter": APPROVE}), decider, {"review_charter": RoleSetting("shadow", 0.8)}, role_specs()
    )
    fast.consult("review_charter", {"prompt": PROMPT})
    fast.run(role="review_charter", schema={}, prompt=PROMPT)
    assert decider.calls == 1
    fast.run(role="review_charter", schema={}, prompt=PROMPT)
    assert decider.calls == 2
