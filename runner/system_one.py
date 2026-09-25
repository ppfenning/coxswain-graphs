"""System-one fast path: a cheap decider that may answer a node before, or beside, the LLM.

The core is pure: question and answer types, the per-role setting, and the parse of a
provider profile's `system_one` block. `FastPathRunner` is the edge. It wraps a
NodeRunner and consults a DecisionRunner per role, in shadow or on mode. A decider
that raises never fails the node: the inner runner runs as if the fast path were absent.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from runner.protocol import NodeResult, NodeRunner
from runner.tier_resolution import Hints

_log = logging.getLogger(__name__)

__all__ = [
    "Answer",
    "Choice",
    "ConfigError",
    "DecisionRunner",
    "FastPathRunner",
    "Noul",
    "Question",
    "RoleSetting",
    "RoleSpec",
    "Score",
    "SystemOneConfig",
    "parse_system_one_block",
]

MODES = ("off", "shadow", "on")
MAX_OPTIONS = 255
MIN_LEVELS, MAX_LEVELS = 2, 10


@dataclass(frozen=True)
class Noul:
    """An open question with no fixed answer set."""

    criteria: str


@dataclass(frozen=True)
class Choice:
    """Pick one of `options`, at most 255."""

    criteria: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.options) > MAX_OPTIONS:
            raise ValueError(f"Choice carries at most {MAX_OPTIONS} options, got {len(self.options)}")


@dataclass(frozen=True)
class Score:
    """Rate on one of `levels`, ordered, 2 to 10 of them."""

    criteria: str
    levels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not MIN_LEVELS <= len(self.levels) <= MAX_LEVELS:
            raise ValueError(f"Score carries {MIN_LEVELS} to {MAX_LEVELS} levels, got {len(self.levels)}")


Question = Noul | Choice | Score


@dataclass(frozen=True)
class Answer:
    """What a decider returned. `value` is the picked option or level; confidence is in 0 to 1."""

    kind: str
    value: str
    probabilities: Mapping[str, float]
    confidence: float


@runtime_checkable
class DecisionRunner(Protocol):
    """Answers one question from a state of field name to text."""

    def decide(self, question: Question, state: Mapping[str, str]) -> Answer: ...


def _setting_error(mode: object, threshold: object) -> str | None:
    if mode not in MODES:
        return f"mode must be one of {', '.join(MODES)}, got {mode!r}"
    if isinstance(threshold, bool) or not isinstance(threshold, int | float) or not 0 <= threshold <= 1:
        return f"threshold must be a number from 0 to 1, got {threshold!r}"
    return None


@dataclass(frozen=True)
class RoleSetting:
    mode: str
    threshold: float

    def __post_init__(self) -> None:
        error = _setting_error(self.mode, self.threshold)
        if error is not None:
            raise ValueError(error)


@dataclass(frozen=True)
class RoleSpec:
    """Three pure functions for one role.

    `build` maps the node request (the keyword arguments of `run`) to a question and state.
    `render` maps an Answer to the output the LLM node would have returned.
    `agrees` says whether an Answer matches an LLM result.
    """

    build: Callable[[Mapping[str, Any]], tuple[Question, Mapping[str, str]]]
    render: Callable[[Answer], Mapping[str, Any]]
    agrees: Callable[[Answer, Mapping[str, Any]], bool]


@dataclass(frozen=True)
class SystemOneConfig:
    backend: str
    model: str
    roles: Mapping[str, RoleSetting]


@dataclass(frozen=True)
class ConfigError:
    error: str


_VERSION = re.compile(r"\d+(\.\d+)*")


def _model_error(model: object) -> str | None:
    if not isinstance(model, str) or not model:
        return f"model must be a non-empty string, got {model!r}"
    if model.lower().endswith("latest"):
        return f"model {model!r} ends in latest; pin a version"
    if _VERSION.search(model) is None:
        return f"model {model!r} carries no version"
    return None


def _parse_role(role: object, entry: object) -> RoleSetting | ConfigError:
    if not isinstance(entry, Mapping):
        return ConfigError(f"roles.{role} must be a mapping of mode and threshold")
    error = _setting_error(entry.get("mode"), entry.get("threshold"))
    if error is not None:
        return ConfigError(f"roles.{role}: {error}")
    return RoleSetting(mode=entry["mode"], threshold=entry["threshold"])


def parse_system_one_block(block: Mapping[str, Any]) -> SystemOneConfig | ConfigError:
    """Validate a provider profile's `system_one` block: backend, model, roles."""
    if not isinstance(block, Mapping):
        return ConfigError("system_one block must be a mapping")
    backend, model, roles = block.get("backend"), block.get("model"), block.get("roles")
    if not isinstance(backend, str) or not backend:
        return ConfigError(f"backend must be a non-empty string, got {backend!r}")
    model_error = _model_error(model)
    if model_error is not None:
        return ConfigError(model_error)
    if not isinstance(roles, Mapping):
        return ConfigError("roles must be a mapping of role to mode and threshold")
    parsed = {role: _parse_role(role, entry) for role, entry in roles.items()}
    failed = next((p for p in parsed.values() if isinstance(p, ConfigError)), None)
    if failed is not None:
        return failed
    return SystemOneConfig(backend=backend, model=model, roles=parsed)


class FastPathRunner:
    """A NodeRunner that consults a decider per role before or beside the inner runner.

    `backend` labels the decider in the decision record. Only the decider side is guarded:
    an exception from the inner runner propagates as it would without this wrapper.
    """

    def __init__(
        self,
        inner: NodeRunner,
        decider: DecisionRunner,
        settings: Mapping[str, RoleSetting],
        specs: Mapping[str, RoleSpec],
        *,
        backend: str = "system_one",
    ) -> None:
        self._inner = inner
        self._decider = decider
        self._settings = settings
        self._specs = specs
        self._backend = backend
        self._consulted: dict[str, Answer] = {}
        self._warned_unpersisted = False

    def consult(self, role: str, request: Mapping[str, Any]) -> tuple[Answer, RoleSetting] | None:
        """Ask the decider ahead of the call, and remember the answer for that role's next `run`.

        A call site that needs the answer before it declares its hints consults here. The next
        `run` for the role reuses this answer, so the logged answer is the one the site acted on
        and the decider is asked once. None when the role is off, has no spec, or the decider fails.
        """
        setting, spec = self._settings.get(role), self._specs.get(role)
        if setting is None or spec is None or setting.mode not in ("shadow", "on"):
            return None
        answer = self._ask(spec, MappingProxyType(dict(request)))
        if answer is None:
            return None
        self._consulted[role] = answer
        return answer, setting

    def _ask(self, spec: RoleSpec, request: Mapping[str, Any]) -> Answer | None:
        try:
            question, state = spec.build(request)
            return self._decider.decide(question, state)
        except Exception:
            return None

    def _rendered(self, spec: RoleSpec, answer: Answer) -> NodeResult | None:
        try:
            return NodeResult(spec.render(answer))
        except Exception:
            return None

    def _tagged(self, result: NodeResult, spec: RoleSpec, setting: RoleSetting, answer: Answer) -> NodeResult | None:
        decision = getattr(result, "decision", None)
        if decision is None:
            return None
        try:
            tagged = NodeResult(result)
            tagged.decision = replace(
                decision,
                system_one_backend=self._backend,
                system_one_mode=setting.mode,
                system_one_answer=answer.value,
                system_one_confidence=answer.confidence,
                system_one_threshold=setting.threshold,
                system_one_agreed=spec.agrees(answer, result),
            )
            return tagged
        except Exception:
            return None

    def _run_tagging(self, kwargs: Mapping[str, Any], spec: RoleSpec, setting: RoleSetting, answer: Answer) -> NodeResult:
        """Run the inner runner with its `tag_decision` hook set, so the tag lands before its ledger write."""
        self._inner.tag_decision = lambda result: getattr(self._tagged(result, spec, setting, answer), "decision", None)
        try:
            return self._inner.run(**kwargs)
        finally:
            self._inner.tag_decision = None

    def _warn_unpersisted(self) -> None:
        """Say once that this inner runner cannot persist a shadow decision. Never guess a call entry for it."""
        if not self._warned_unpersisted:
            _log.warning("system_one: %s has no tag_decision hook, so shadow decisions are not persisted", type(self._inner).__name__)
            self._warned_unpersisted = True

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
    ) -> NodeResult:
        kwargs: dict[str, Any] = {
            "role": role,
            "tier": tier,
            "hints": hints,
            "schema": schema,
            "prompt": prompt,
            "context": context,
            "thread": thread,
            "budget_usd": budget_usd,
            "task": task,
        }
        setting, spec = self._settings.get(role), self._specs.get(role)
        if setting is None or spec is None or setting.mode not in ("shadow", "on"):
            return self._inner.run(**kwargs)
        consulted = self._consulted.pop(role, None)
        answer = consulted if consulted is not None else self._ask(spec, MappingProxyType(kwargs))
        if answer is None:
            return self._inner.run(**kwargs)
        if setting.mode == "on" and answer.confidence >= setting.threshold:
            fast = self._rendered(spec, answer)
            if fast is not None:
                return fast
        if setting.mode == "on":
            return self._inner.run(**kwargs)
        if hasattr(self._inner, "tag_decision"):
            return self._run_tagging(kwargs, spec, setting, answer)
        result = self._inner.run(**kwargs)
        tagged = self._tagged(result, spec, setting, answer)
        if tagged is None:
            return result
        self._warn_unpersisted()
        return tagged
