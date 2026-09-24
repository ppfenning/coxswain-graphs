"""Jev backend for the system-one fast path, over the typesafe-sdk.

Jev reads text only: questions must not depend on counting, arithmetic or parsing dates.
The edge reads the key and passes the string in; this module reads no environment.
A client factory takes `(model, api_key)` and returns `(client, types)`: the SDK client and its `Noul`, `Choice`, `Score`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from itertools import accumulate
from typing import Any

from runner.system_one import Answer, Choice, Noul, Question, Score, _model_error

__all__ = [
    "QUESTION_MAX_TOKENS",
    "STATE_MAX_TOKENS",
    "JevDecider",
    "clip",
    "clip_question",
    "clip_state",
    "to_answer",
    "to_sdk_question",
    "to_state",
]

QUESTION_MAX_TOKENS = 32_000
STATE_MAX_TOKENS = 64_000
CHARS_PER_TOKEN = 4  # an estimate, not a tokenizer
QUESTION_NAME = "q"

ClientFactory = Callable[[str, str], tuple[Any, Any]]


def clip(text: str, max_tokens: int) -> str:
    """Cut `text` to the character estimate of `max_tokens`."""
    return text[: max_tokens * CHARS_PER_TOKEN]


def clip_state(state: Mapping[str, str], max_tokens: int) -> dict[str, str]:
    """Fit `state` to the budget. Order is most important first, so the last field is cut first."""
    budget = max_tokens * CHARS_PER_TOKEN
    ends = list(accumulate(len(text) for text in state.values()))
    starts = [end - len(text) for end, text in zip(ends, state.values())]
    return {name: text[: budget - start] for (name, text), start in zip(state.items(), starts) if start < budget}


def to_state(state: Mapping[str, str]) -> dict[str, str]:
    """Each field sits under its own name, so criteria can cite it as a backticked path such as `plan`."""
    return clip_state(state, STATE_MAX_TOKENS)


def _labels(question: Question) -> tuple[str, ...]:
    if isinstance(question, Choice):
        return question.options
    if isinstance(question, Score):
        return question.levels
    return ()


def clip_question(question: Question) -> tuple[str, tuple[str, ...]]:
    """Fit instructions and labels into one question budget. Labels share equal caps; instructions take the rest."""
    budget = QUESTION_MAX_TOKENS * CHARS_PER_TOKEN
    labels = _labels(question)
    cap = budget // (len(labels) + 1)
    clipped = tuple(label[:cap] for label in labels)
    return question.criteria[: budget - sum(map(len, clipped))], clipped


def to_sdk_question(question: Question, types: Any) -> Any:
    """Map a runner question onto the SDK type. The criteria text rides in `instructions`."""
    instructions, labels = clip_question(question)
    if isinstance(question, Noul):
        return types.Noul(instructions=instructions)
    if isinstance(question, Choice):
        if len(set(labels)) != len(labels):
            raise ValueError("choice options collide after clipping")
        return types.Choice(criteria=dict.fromkeys(labels), instructions=instructions)
    if isinstance(question, Score):
        return types.Score(criteria=list(labels), instructions=instructions)
    raise TypeError(f"unsupported question {type(question).__name__}")


def _named(probabilities: Mapping[Any, float]) -> dict[str, float]:
    return {str(key): value for key, value in probabilities.items()}


def to_answer(question: Question, sdk_answer: Any) -> Answer:
    """Map an SDK answer back. Noul confidence is derived, since the SDK gives none. Score value is the weighted score."""
    if isinstance(question, Noul):
        yes = float(sdk_answer.noul)
        return Answer(
            kind="noul",
            value="yes" if yes >= 0.5 else "no",
            probabilities={"yes": yes, "no": 1 - yes},
            confidence=max(yes, 1 - yes),
        )
    if isinstance(question, Choice):
        original = dict(zip(clip_question(question)[1], question.options))
        return Answer(
            kind="choice",
            value=original.get(sdk_answer.choice, sdk_answer.choice),
            probabilities={original.get(k, k): v for k, v in _named(sdk_answer.probabilities).items()},
            confidence=float(sdk_answer.confidence),
        )
    if isinstance(question, Score):
        return Answer(
            kind="score",
            value=str(sdk_answer.score),
            probabilities=_named(sdk_answer.probabilities),
            confidence=float(sdk_answer.confidence),
        )
    raise TypeError(f"unsupported question {type(question).__name__}")


def _default_factory(model: str, api_key: str) -> tuple[Any, Any]:
    try:
        import typesafe_sdk  # optional extra: system-one-jev
    except ImportError as error:
        raise ImportError("typesafe-sdk is not installed; install the system-one-jev extra") from error
    return typesafe_sdk.TypeSafeClient(api_key=api_key, model=model), typesafe_sdk


class JevDecider:
    """A DecisionRunner over a pinned Jev model. One client for its life; `close()` or `with` releases it.

    The client is built here, so a missing SDK fails at startup, not silently on every node.
    """

    def __init__(self, model: str, api_key: str, client_factory: ClientFactory | None = None) -> None:
        error = _model_error(model)
        if error is not None:
            raise ValueError(error)
        self._client, self._types = (client_factory or _default_factory)(model, api_key)

    def decide(self, question: Question, state: Mapping[str, str]) -> Answer:
        response = self._client.system_one(to_state(state), {QUESTION_NAME: to_sdk_question(question, self._types)})
        return to_answer(question, response.answers[QUESTION_NAME])

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JevDecider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
