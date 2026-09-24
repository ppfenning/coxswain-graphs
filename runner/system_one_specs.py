"""The per-role specs the fast path knows. Later tasks add entries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runner.system_one import Answer, Noul, RoleSpec

__all__ = ["role_specs"]

_HANDOFF_CRITERIA = (
    "Do the change facts satisfy the plan? Answer yes when the facts show the work the plan "
    "asked for was done, and no when they show it was not."
)


def _between(text: str, start: str, end: str) -> str:
    _, found, rest = text.partition(start)
    body, closed, _ = rest.partition(end)
    if not (found and closed):
        raise ValueError(f"prompt lacks the {start.strip()!r} section")
    return body


def _handoff_build(request: Mapping[str, Any]) -> tuple[Noul, Mapping[str, str]]:
    """The handoff request carries plan and facts only inside `prompt`; slice them out unchanged."""
    prompt = str(request["prompt"])
    plan = _between(prompt, "\nPlan: ", "\nSummary: ")
    facts = _between(prompt, "\nChange facts: ", "\nThe facts listed under Change facts")
    return Noul(_HANDOFF_CRITERIA), {"plan": plan, "change_facts": facts}


def _handoff_render(answer: Answer) -> Mapping[str, Any]:
    return {"complete": answer.probabilities.get("yes", 0.0) >= 0.5}


def _handoff_agrees(answer: Answer, result: Mapping[str, Any]) -> bool:
    return _handoff_render(answer)["complete"] == bool(result.get("complete"))


def role_specs() -> Mapping[str, RoleSpec]:
    return {"handoff": RoleSpec(build=_handoff_build, render=_handoff_render, agrees=_handoff_agrees)}
