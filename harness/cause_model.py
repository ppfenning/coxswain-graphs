"""Fallback attempt-cause classifier for when `classify_cause` returns None.

`build_prompt`, `cause_evidence`, `one_line` and `parse_answer` are pure. `classify_with_model` is
the edge: it takes the runner as a parameter and raises only `LimitStop`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from harness.cause_rule import CAUSES, Cause
from runner.protocol import LimitStop, NodeRunner

ROLE = "cause_classify"  # unknown to any cartridge until one maps it to a skill
TIER = "cheap"
BUDGET_USD = 0.05  # per-call ceiling, the same `budget_usd` every other node passes
WHY_CHARS = 200  # every returned why is one line of at most this many characters

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cause": {"type": "string", "enum": list(CAUSES)},
        "one_line_why": {"type": "string"},
    },
    "required": ["cause", "one_line_why"],
    "additionalProperties": False,
}

_MEANINGS = (
    ("ticket", "the ticket was unclear, wrong or asked for nothing buildable"),
    ("code", "the change itself was wrong or failed its checks"),
    ("review", "the review or arbitration verdict was the cause"),
    ("harness", "the tooling failed: worktree, patch application or infrastructure"),
    ("unknown", "none of the above can be shown from the text"),
)


def build_prompt(reasoning: str, objections: Sequence[str]) -> str:
    causes = "\n".join(f"- {name}: {meaning}" for name, meaning in _MEANINGS)
    listed = "\n".join(f"{i}. {text}" for i, text in enumerate(objections, start=1)) or "(none)"
    return (
        "A task attempt was quarantined. Name the single cause from this closed set.\n\n"
        f"{causes}\n\n"
        f"Arbitration reasoning:\n{reasoning}\n\n"
        f"Adversary objections:\n{listed}\n\n"
        "Return the cause and a one-line why."
    )


def cause_evidence(reason: str, result: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    """(reasoning, objection claims) a task's result offers the classifier.

    Without arbitration text the quarantine reason is the only account of why, and it is never empty.
    """
    arbitration = (result or {}).get("arbitration")
    said = str(arbitration.get("reasoning") or "").strip() if isinstance(arbitration, Mapping) else ""
    found = ((result or {}).get("adversary") or {}).get("objections") or []
    claims = (str(o.get("claim") or "") if isinstance(o, Mapping) else str(o) for o in found)
    return said or reason, [c.strip() for c in claims if c.strip()]


def one_line(text: str) -> str:
    """First non-blank line, stripped and cut to WHY_CHARS."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:WHY_CHARS]


def parse_answer(answer: Mapping[str, Any]) -> tuple[Cause, str]:
    cause = answer.get("cause") if isinstance(answer, Mapping) else None
    raw_why = answer.get("one_line_why") if isinstance(answer, Mapping) else None
    why = one_line(raw_why) if isinstance(raw_why, str) else ""
    if not isinstance(cause, str) or not why:
        return "unknown", one_line(f"model output malformed: {answer!r}")
    if cause not in CAUSES:
        return "unknown", one_line(f"model returned cause outside the closed set: {cause!r}")
    return cast(Cause, cause), why


def classify_with_model(
    runner: NodeRunner, reasoning: str, objections: Sequence[str], *, task: str | None = None
) -> tuple[Cause, str]:
    try:
        answer = runner.run(
            role=ROLE,
            tier=TIER,
            schema=SCHEMA,
            prompt=build_prompt(reasoning, objections),
            budget_usd=BUDGET_USD,
            task=task,
        )
    except LimitStop:
        raise  # the account's session limit, not this task's; the driver pauses the run on it
    except Exception as error:  # BudgetStop included: it is this call's own small cap, so a task-level unknown
        return "unknown", one_line(f"model call failed: {type(error).__name__}: {error}")
    return parse_answer(answer)
