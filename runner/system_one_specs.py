"""The per-role specs the fast path knows. Later tasks add entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runner.system_one import Answer, Choice, Noul, RoleSpec

__all__ = ["role_specs"]

_HANDOFF_CRITERIA = (
    "Do the change facts satisfy the plan? Answer yes when the facts show the work the plan "
    "asked for was done, and no when they show it was not."
)

_REVIEW_PRESCREEN_CRITERIA = (
    "Review this change against the team's written charter. Answer approve when the patch does the work "
    "the plan asked for and breaks no charter principle, revise when it needs changes, and reject when "
    "the approach is wrong."
)


def _between(text: str, start: str, end: str) -> str:
    _, found, rest = text.partition(start)
    body, closed, _ = rest.partition(end)
    if not (found and closed):
        raise ValueError(f"prompt lacks the {start.strip()!r} section")
    return body


def _handoff_build(request: Mapping[str, Any]) -> tuple[Noul, Mapping[str, str]]:
    """The handoff request carries plan, summary and facts only inside `prompt`; slice them out unchanged."""
    prompt = str(request["prompt"])
    plan = _between(prompt, "\nPlan: ", "\nSummary: ")
    summary = _between(prompt, "\nSummary: ", "\nChange facts: ")
    facts = _between(prompt, "\nChange facts: ", "\nThe facts listed under Change facts")
    return Noul(_HANDOFF_CRITERIA), {"plan": plan, "summary": summary, "change_facts": facts}


def _handoff_render(answer: Answer) -> Mapping[str, Any]:
    return {"complete": answer.probabilities.get("yes", 0.0) >= 0.5}


def _handoff_agrees(answer: Answer, result: Mapping[str, Any]) -> bool:
    return _handoff_render(answer)["complete"] == bool(result.get("complete"))


def _review_prescreen_build(request: Mapping[str, Any]) -> tuple[Choice, Mapping[str, str]]:
    """The charter review request carries plan and patch only inside `prompt`; slice them out unchanged."""
    prompt = str(request["prompt"])
    plan = _between(prompt, "\nTask: ", "\nSummary: ")
    patch = _between(prompt, "\nPatch:\n", "\n\nCite the charter")
    return Choice(_REVIEW_PRESCREEN_CRITERIA, ("approve", "revise", "reject")), {"patch": patch, "plan": plan}


def _review_prescreen_render(answer: Answer) -> Mapping[str, Any]:
    raise ValueError("a pre-screen picks review depth and never stands in for a review")


def _review_prescreen_agrees(answer: Answer, result: Mapping[str, Any]) -> bool:
    return answer.value == result.get("verdict")


def role_specs() -> Mapping[str, RoleSpec]:
    return {
        "handoff": RoleSpec(build=_handoff_build, render=_handoff_render, agrees=_handoff_agrees),
        "review_charter": RoleSpec(
            build=_review_prescreen_build, render=_review_prescreen_render, agrees=_review_prescreen_agrees
        ),
    }
