"""A runner for models served by an OpenAI-compatible endpoint, such as a local server.

The profile names the env var holding the endpoint's base URL. It never carries the URL.
This task serves `local/` models only. A local call is free: it records a cost of 0.0, never
draws on `budget_usd`, and cannot stop a budget.
"""

from __future__ import annotations

import itertools
import os
import uuid
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from runner.anthropic_runner import (
    DEFAULT_TIER,
    ROUTER_ON_WARNING,
    AnthropicRunner,
    _as_caller,
    _class_model,
    _classes_map,
    _decision,
    _router_mode,
    _tier_map,
)
from runner.decision_log import CallDecision, RouterDecision, to_row
from runner.openai_chat import (
    ChatError,
    ChatTransportError,
    Reply,
    build_request,
    parse_response,
    post_chat,
    wire_model,
)
from runner.protocol import NodeResult, RunnerError
from runner.schema_answer import retry_prompt, schema_instruction, shape_answer
from runner.tier_resolution import CLASSES, TIERS, Hints, resolve, to_class

if TYPE_CHECKING:
    from harness.store_write import Store

__all__ = ["OpenAICompatibleRunner"]

LOCAL_PREFIX = "local/"
MAX_TOKENS = 4096
TEMPERATURE = 0.0


@dataclass(frozen=True)
class _Ask:
    """What one node call sends, fixed across the first attempt and the retry."""

    role: str
    task: str | None
    tier: str
    model_id: str
    system: str | None


class OpenAICompatibleRunner:
    """Runs nodes against a chat-completions endpoint and shapes the reply to the node's schema."""

    def __init__(
        self,
        profile: Mapping[str, Any],
        *,
        role_skills: Mapping[str, str],
        env: Mapping[str, str] | None = None,
        timeout: float = 120.0,
        max_tokens: int = MAX_TOKENS,
        store: Store | None = None,
        run_id: str | None = None,
    ) -> None:
        self.profile = dict(profile)
        self.role_skills = dict(role_skills)
        endpoint_env = self.profile.get("endpoint_env")
        if not endpoint_env:
            raise RunnerError("provider profile names no 'endpoint_env'")
        base_url = (os.environ if env is None else env).get(str(endpoint_env))
        if not base_url:
            raise RunnerError(
                f"${endpoint_env} is not set. The provider profile names the variable; it holds the endpoint's base URL."
            )
        self.base_url = base_url
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.store = store
        self.run_id = run_id
        self.capabilities = dict(self.profile.get("capabilities") or {})
        self.tiers = dict(self.profile.get("tiers") or {})
        if not self.tiers:
            raise RunnerError("provider profile declares no tiers")
        self.classes = _classes_map(self.profile)
        self.tier_overrides = _tier_map(self.profile, "tier_overrides")
        self.profile_defaults = _tier_map(self.profile, "defaults")
        self.floor = str(self.profile.get("floor") or TIERS[0])
        if to_class(self.floor) is None:
            raise RunnerError(
                f"provider profile 'floor' must be one of {', '.join(TIERS)} or {', '.join(CLASSES)}, not '{self.floor}'"
            )
        self.router_mode = _router_mode(self.profile)
        self.calls: list[dict[str, Any]] = []
        self._store_seq = itertools.count(1)

    def close(self) -> None:
        """No connection is held, so there is nothing to release."""

    def _model_for(self, tier: str) -> str:
        model = self.tiers.get(tier)
        if not model:
            raise RunnerError(
                f"provider profile has no model for tier '{tier}'; it declares: {', '.join(sorted(self.tiers))}"
            )
        return str(model)

    def _record_to_store(self, call: Mapping[str, Any], decision: CallDecision | None) -> None:
        """Write one finished call to the store; no decision means the call did not stand. A store error is warned about, never raised."""
        if self.store is None or not self.run_id:
            return
        from harness.store_write import split_phase_id  # here, not at the top: harness imports the runners

        run_id, phase_id = split_phase_id(self.run_id)
        try:
            self.store.record_call(
                {**call, "ok": decision is not None},
                None if decision is None else to_row(decision),
                run_id=run_id,
                seq=next(self._store_seq),
                phase_id=phase_id or None,
            )
        except Exception as exc:
            warnings.warn(f"store write failed for call {call['id']}: {exc}", RuntimeWarning, stacklevel=2)

    def _post(self, ask: _Ask, prompt: str) -> tuple[dict[str, Any], Reply]:
        """One HTTP call and its record. A transport or chat failure is recorded, stored as failed, and raised."""
        record = {
            "id": str(uuid.uuid4()),
            "role": ask.role,
            "task_id": ask.task,
            "tier": ask.tier,
            "model": ask.model_id,
            "ts": datetime.now(UTC).isoformat(),
            "ok": False,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        payload = build_request(wire_model(ask.model_id), ask.system, prompt, self.max_tokens, TEMPERATURE)
        try:
            reply = parse_response(post_chat(self.base_url, payload, timeout=self.timeout))
        except ChatTransportError as exc:
            self.calls.append(record)
            self._record_to_store(record, None)
            raise RunnerError(f"node '{ask.role}' could not reach the endpoint: {exc}") from exc
        if isinstance(reply, ChatError):
            self.calls.append(record)
            self._record_to_store(record, None)
            raise RunnerError(f"node '{ask.role}' got an unusable reply: {reply.reason}")
        counted = {**record, "ok": True, "input_tokens": reply.prompt_tokens, "output_tokens": reply.completion_tokens}
        self.calls.append(counted)
        return counted, reply

    def _shaped(
        self, ask: _Ask, prompt: str, schema: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Reply, dict[str, Any] | None, list[str]]:
        record, reply = self._post(ask, prompt)
        data, errors = shape_answer(reply.text, dict(schema))
        problems = errors or ([] if isinstance(data, dict) else [f"$: expected an object, got {type(data).__name__}"])
        return record, reply, data if isinstance(data, dict) else None, problems

    def run(
        self,
        *,
        role: str,
        tier: str | None = None,
        hints: Hints | None = None,
        schema: Mapping[str, Any] | None = None,
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
        router_decision: RouterDecision | None = None,
    ) -> NodeResult:
        # `thread` is accepted for the protocol and ignored: each call is one stateless request.
        # `budget_usd` is recorded on the decision as given and never spent against: a local call is free.
        requested_tier = tier or DEFAULT_TIER
        if to_class(requested_tier) is None:
            raise RunnerError(
                f"node '{role}' asked for tier '{requested_tier}'; the tiers are {', '.join(TIERS)} or a capability class"
            )
        resolution = _as_caller(
            resolve(role, hints, self.tier_overrides, self.profile_defaults, requested_tier, self.floor)
        )
        class_model = _class_model(self.classes, resolution.chosen_class)
        model_id = class_model or self._model_for(resolution.tier)
        recorded = replace(resolution, tier=resolution.chosen_class) if class_model else resolution
        # The hybrid task replaces this branch.
        if not model_id.startswith(LOCAL_PREFIX):
            raise RunnerError(
                f"node '{role}' resolved tier '{recorded.tier}' to model '{model_id}'; this runner serves only '{LOCAL_PREFIX}' models"
            )
        if self.router_mode == "on" and router_decision is not None:
            warnings.warn(ROUTER_ON_WARNING, RuntimeWarning, stacklevel=1)
        shadow = router_decision if self.router_mode in ("shadow", "on") else None
        body = self.role_skills.get(role)
        packs = [body, *context] if body else list(context)
        ask = _Ask(role, task, recorded.tier, model_id, AnthropicRunner._read_context(packs) or None)
        decision = replace(
            _decision(
                role=role,
                requested_tier=requested_tier,
                resolution=recorded,
                model_id=model_id,
                effort="",
                budget_usd=budget_usd,
                task=task,
                router_decision=shadow,
            ),
            effort=None,
        )
        if not schema:
            record, reply = self._post(ask, prompt)
            self._record_to_store(record, decision)
            result = NodeResult({"text": reply.text})
            result.decision = decision
            return result
        asked = f"{prompt}\n\n{schema_instruction(dict(schema))}"
        first_record, first, data, errors = self._shaped(ask, asked, schema)
        if not errors:
            return self._answer(first_record, data, decision)
        self._record_to_store(first_record, None)
        second_record, _, data, errors = self._shaped(ask, retry_prompt(asked, first.text, errors), schema)
        if errors:
            self._record_to_store(second_record, None)
            raise RunnerError(f"node '{role}' returned no valid answer after one retry: {'; '.join(errors)}")
        return self._answer(second_record, data, decision)

    def _answer(self, record: Mapping[str, Any], data: dict[str, Any] | None, decision: CallDecision) -> NodeResult:
        self._record_to_store(record, decision)
        result = NodeResult(data or {})
        result.decision = decision
        return result
