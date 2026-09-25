"""A system-one decider that asks the wrapped runner's cheap tier, one call per question."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from runner.system_one import Answer, Choice, Noul, Question

__all__ = ["TierDecider"]

_ASK = "Give your answer and a confidence from 0 to 1."


def _kind(question: Question) -> str:
    return "noul" if isinstance(question, Noul) else "choice" if isinstance(question, Choice) else "score"


def _values(question: Question) -> tuple[str, ...]:
    if isinstance(question, Noul):
        return ("yes", "no")
    return question.options if isinstance(question, Choice) else question.levels


def _prompt(question: Question, state: Mapping[str, str]) -> str:
    fields = "".join(f"\n\n## {name}\n{text}" for name, text in state.items())
    return f"{question.criteria}{fields}\n\n{_ASK}"


def _schema(question: Question) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "enum": list(_values(question))},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["answer", "confidence"],
    }


def _answer(kind: str, value: str, confidence: float, values: tuple[str, ...]) -> Answer:
    """The picked value carries `confidence` (clamped to 0 to 1); the rest share what is left evenly."""
    c = min(1.0, max(0.0, confidence))
    others = [v for v in dict.fromkeys(values) if v != value]
    share = (1.0 - c) / len(others) if others else 0.0
    return Answer(kind, value, {value: c, **dict.fromkeys(others, share)}, c)


class TierDecider:
    """Answers a typed question with one `runner.run` call at `tier`. Any error raises."""

    def __init__(self, runner: Any, tier: str = "cheap") -> None:
        self.runner, self.tier = runner, tier

    def decide(self, question: Question, state: Mapping[str, str]) -> Answer:
        values = _values(question)
        result = self.runner.run(
            role="system_one", tier=self.tier, schema=_schema(question), prompt=_prompt(question, state)
        )
        value, confidence = result["answer"], float(result["confidence"])
        if value not in values:
            raise ValueError(f"answer {value!r} is not one of {list(values)}")
        if not math.isfinite(confidence):
            raise ValueError(f"confidence {confidence!r} is not a finite number")
        return _answer(_kind(question), value, confidence, values)
