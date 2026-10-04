"""The live runner: role + tier in, structured output out.

This is the only module in either repo that imports a vendor SDK, and the only
one that reads an environment variable or a file. Everything else — every graph,
every policy decision — is pure and testable without it. That is not an accident
of layering; it is the reason the substrate can change providers by editing one
YAML file.

Two indirections are resolved here and nowhere else:

    tier  -> model       from the provider profile
    role  -> skill body  from the cartridge's bindings, handed in by the harness

The provider profile names an env var for the key. It never carries the value,
and neither does any cartridge, graph, or test fixture.
"""

from __future__ import annotations

import itertools
import json
import os
import uuid
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from runner.decision_log import CallDecision, RouterDecision, joined_reasons, to_row
from runner.pricing import max_output_tokens_for_budget, price_call
from runner.protocol import BudgetStop, LimitStop, NodeResult, RunnerError
from runner.schema_answer import schema_instruction, shape_answer
from runner.tier_resolution import CLASSES, TIERS, Hints, Resolution, resolve, to_class
from runner.tool_loop import AdapterStep, FinalAnswer, ToolCall, run_tool_loop

if TYPE_CHECKING:
    # Type only: importing harness at module load is circular, since harness imports the runners.
    from harness.store_write import Store

__all__ = ["AnthropicRunner", "AnthropicToolLoopAdapter", "load_provider_profile"]

# unknown: the right turn cap for a build; this is a guess, not a measurement.
_BUILD_TURN_CAP = 40

# Roles doing verification, adversarial review, or arbitration are worth more
# capability than roles doing bulk enumeration. The profile decides which model
# each tier means; this is only the fallback when a node names no tier.
DEFAULT_TIER = "standard"

# Effort is the first quality/cost lever, and it belongs to the tier rather than
# the node: a `cheap` node is cheap because the work is bulk, not because we
# want it to think less about a hard case it happens to hit.
TIER_EFFORT = {"cheap": "low", "standard": "high", "deep": "xhigh"}

ROUTER_MODES = ("off", "shadow", "on")
ROUTER_ON_WARNING = "provider profile router 'on' is not implemented in this runner; acting as shadow"


def load_provider_profile(path: Path | str) -> dict[str, Any]:
    """Read a provider profile. The vendor axis, isolated to one file."""
    path = Path(path).expanduser()
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunnerError(f"{path}: cannot read provider profile: {exc}") from exc
    if not isinstance(profile, Mapping) or "tiers" not in profile:
        raise RunnerError(f"{path}: provider profile must be a mapping with a 'tiers' block")
    return dict(profile)


def _router_mode(profile: Mapping[str, Any]) -> str:
    """The profile `router` key; absent means off. Refuses an unknown value rather than falling back to off.

    YAML 1.1 loads an unquoted `off` as False and `on` as True, so a bool reads as that mode.
    """
    raw = profile.get("router", "off")
    mode = ("on" if raw else "off") if isinstance(raw, bool) else raw
    if mode not in ROUTER_MODES:
        raise RunnerError(f"provider profile 'router' must be one of {', '.join(ROUTER_MODES)}, not '{mode}'")
    return str(mode)


def _tier_map(profile: Mapping[str, Any], key: str) -> dict[str, str]:
    """A role -> tier block of the profile. A tier outside TIERS is a profile fault, refused at construction."""
    raw = profile.get(key) or {}
    if not isinstance(raw, Mapping):
        raise RunnerError(f"provider profile '{key}' must map a role to a tier")
    bad = {str(role): str(tier) for role, tier in raw.items() if to_class(str(tier)) is None}
    if bad:
        raise RunnerError(f"provider profile '{key}' names tiers outside {', '.join(TIERS)} or the capability classes: {bad}")
    return {str(role): str(tier) for role, tier in raw.items()}


def _as_caller(resolution: Resolution) -> Resolution:
    """No router runs here: the caller's tier fills the resolver's `router_tier` slot, so that source is named `caller`."""
    reason = resolution.reason
    if not reason.startswith("router"):
        return resolution
    return Resolution(resolution.tier, "caller" + reason[len("router") :], resolution.chosen_class)


def _classes_map(profile: Mapping[str, Any]) -> dict[str, list[Any]]:
    """The optional profile `classes` block: class name -> models, first entry preferred."""
    raw = profile.get("classes") or {}
    if not isinstance(raw, Mapping) or not all(isinstance(models, list) for models in raw.values()):
        raise RunnerError("provider profile 'classes' must map a capability class to a list of models")
    return {str(name): list(models) for name, models in raw.items()}


def _class_model(classes: Mapping[str, Sequence[Any]], chosen_class: str | None) -> str | None:
    """First model listed for the class; None when the class has no entry, so the tiers map answers."""
    models = classes.get(chosen_class) if chosen_class else None
    return str(models[0]) if models else None


def _limit_exception_types() -> tuple[type[BaseException], ...]:
    """The SDK's rate-limit exception class; empty when the optional `anthropic` extra is absent.

    Imported here and nowhere else, so this module still imports, and a stub client still runs, without the SDK.
    `InternalServerError` is left out on purpose: it covers every 5xx, and a 500 is a failure, not a limit. An overloaded
    response is HTTP 529, which `run` maps by status code whichever class carries it. Unchecked: `anthropic` is not
    installed in the build sandbox, so which class the SDK raises for 529 was not confirmed by importing it.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover - depends on install extras
        return ()
    return (anthropic.RateLimitError,)


def _decision(
    *,
    role: str,
    requested_tier: str,
    resolution: Resolution,
    model_id: str,
    effort: str,
    budget_usd: float | None,
    task: str | None,
    router_decision: RouterDecision | None = None,
) -> CallDecision:
    """Requested tier is the caller's; chosen tier and reason are the resolver's. Nothing is clipped here.

    A supplied `router_decision` is copied onto the `router_*` fields as given; it never changes what was chosen.

    `budget_usd` is the ceiling the caller granted, recorded as given. `run` enforces it, by capping `max_tokens` and raising `BudgetStop`.
    """
    return CallDecision(
        role=role,
        requested_tier=requested_tier,
        chosen_tier=resolution.tier,
        model_id=model_id,
        reason=resolution.reason,
        ticket_key=task or "",
        outcome_key=task or "",
        claude_code_version=None,
        effort=effort,
        budget_usd=budget_usd,
        clipped_by=None,
        router_tier=router_decision.chosen_class if router_decision else None,
        router_reason=joined_reasons(router_decision) if router_decision else None,
        router_model=router_decision.model if router_decision else None,
        router_effort=router_decision.effort if router_decision else None,
        router_budget_usd=router_decision.budget_usd if router_decision else None,
        router_clipped_by=router_decision.clipped_by if router_decision else None,
    )


def _wire_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """One tool_loop message as one Messages API message; a tool result is a user turn of `tool_result` blocks."""
    if message["role"] == "assistant":
        calls = message["tool_calls"]
        return {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in calls],
        }
    if message["role"] == "tool":
        block = {
            "type": "tool_result",
            "tool_use_id": message["tool_call_id"],
            "content": message["content"],
            "is_error": message["is_error"],
        }
        return {"role": "user", "content": [block]}
    return {"role": "user", "content": message["content"]}


def _merge_results(acc: list[dict[str, Any]], message: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold consecutive block-list user turns into one: the API wants every result for a turn's calls in one message."""
    prev = acc[-1] if acc else None
    if prev and prev["role"] == message["role"] == "user" and isinstance(prev["content"], list) and isinstance(message["content"], list):
        return [*acc[:-1], {"role": "user", "content": [*prev["content"], *message["content"]]}]
    return [*acc, message]


def _to_anthropic(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The `system`, `messages` and `tools` request fields for a tool_loop message list and tool set."""
    head = messages[0]
    schema = head.get("schema")
    system = "\n\n".join(part for part in (head["content"], schema_instruction(dict(schema)) if schema else "") if part)
    return {
        "system": system or None,
        "messages": reduce(_merge_results, map(_wire_message, messages[1:]), []),
        "tools": [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
        ],
    }


class AnthropicToolLoopAdapter:
    """`ToolLoopAdapter` over the Messages API; `answer` and `billed` outlive the loop, which returns neither."""

    def __init__(self, client: Any, *, model: str, max_tokens: int, effort: str, prices: Mapping[str, Any]) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._prices = prices
        self.answer: dict[str, Any] | None = None
        # Every billed turn, including one whose reply then raises, so a failed loop can still be charged.
        self.billed: dict[str, float] = {}

    def send(self, messages: list[dict], tools: list[dict]) -> AdapterStep:
        schema = dict(messages[0].get("schema") or {})
        try:
            # No `thinking`: a thinking block must be echoed back unchanged, and the neutral message list cannot hold one.
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                output_config={"effort": self._effort},
                **_to_anthropic(messages, tools),
            )
        except Exception as exc:
            if isinstance(exc, _limit_exception_types()) or getattr(exc, "status_code", None) in (429, 529):
                raise LimitStop(detail=str(exc)) from exc
            raise RunnerError(f"tool loop: {exc}") from exc
        billed = getattr(response, "model", None) or self._model
        raw = getattr(response, "usage", None)
        input_tokens = getattr(raw, "input_tokens", 0) or 0
        output_tokens = getattr(raw, "output_tokens", 0) or 0
        cost = price_call(self._prices, billed, input_tokens, output_tokens)
        if cost is None:
            cost = price_call(self._prices, self._model, input_tokens, output_tokens)
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, **({} if cost is None else {"cost_usd": cost})}
        self.billed = {k: self.billed.get(k, 0) + usage.get(k, 0) for k in {*self.billed, *usage}}
        if getattr(response, "stop_reason", None) == "refusal":
            raise RunnerError("tool loop: the model refused the request")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"]
        if calls:
            return AdapterStep(calls, None, usage)
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise RunnerError("tool loop: the reply had no tool call and no text block")
        data, errors = shape_answer(text, schema)
        if not isinstance(data, dict):
            raise RunnerError(f"tool loop: the final reply does not match the schema: {'; '.join(errors) or 'not an object'}")
        self.answer = data
        return AdapterStep([], FinalAnswer(data), usage)


class AnthropicRunner:
    """Runs nodes against the Messages API with structured outputs."""

    def __init__(
        self,
        profile: Mapping[str, Any],
        *,
        client: Any = None,
        max_tokens: int = 16000,
        extra_system: str = "",
        role_skills: Mapping[str, str] | None = None,
        store: Store | None = None,
        run_id: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.cwd = cwd
        # Also record every finished call in the run-record store. This runner receives no run id today,
        # so a new optional `run_id` (`run:phase`) is the smallest way to give the store its context.
        self.store = store
        self.run_id = run_id
        self._store_seq = itertools.count(1)
        # Cumulative cost_usd per thread, in process memory only: what "the thread's spend so far" means for the budget.
        self._thread_spend: dict[str, float] = {}
        # role -> path of the skill body the cartridge bound to it, resolved by
        # the harness. Prepended to the node's system below — the moment a
        # binding stops being a validated name and becomes what the node knows.
        self.role_skills = dict(role_skills or {})
        self.profile = dict(profile)
        self.tiers = dict(self.profile.get("tiers") or {})
        if not self.tiers:
            raise RunnerError("provider profile declares no tiers")
        self.classes = _classes_map(self.profile)
        self.tier_overrides = _tier_map(self.profile, "tier_overrides")
        # unknown: the profile key for role -> tier defaults; assumed to be `defaults`. Nothing in the repo names it.
        self.profile_defaults = _tier_map(self.profile, "defaults")
        # unknown: where the floor comes from; assumed to be an optional profile `floor`, else the lowest tier.
        self.floor = str(self.profile.get("floor") or TIERS[0])
        if to_class(self.floor) is None:
            raise RunnerError(f"provider profile 'floor' must be one of {', '.join(TIERS)} or {', '.join(CLASSES)}, not '{self.floor}'")
        self.router_mode = _router_mode(self.profile)
        self.max_tokens = max_tokens
        self.extra_system = extra_system
        self._client = client or self._build_client()

    def _build_client(self) -> Any:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RunnerError("the anthropic SDK is not installed; `pip install anthropic`") from exc

        # The profile names the variable. Reading the value is this line's job
        # and nothing else's; it is never written to a cartridge or a manifest.
        env_var = self.profile.get("auth_env", "ANTHROPIC_API_KEY")
        api_key = os.environ.get(env_var)
        if not api_key:
            raise RunnerError(
                f"${env_var} is not set. The provider profile names the variable; "
                "the value belongs in your environment, never in a file here."
            )
        return anthropic.Anthropic(api_key=api_key)

    def _model_for(self, tier: str) -> str:
        model = self.tiers.get(tier)
        if not model:
            known = ", ".join(sorted(self.tiers))
            raise RunnerError(f"provider profile has no model for tier '{tier}'; it declares: {known}")
        return str(model)

    @staticmethod
    def _read_context(context: Sequence[str]) -> str:
        """Context packs are read HERE, at the edge — never inside a graph."""
        chunks = []
        for entry in context:
            path = Path(entry)
            try:
                chunks.append(f"<context path=\"{path.name}\">\n{path.read_text(encoding='utf-8')}\n</context>")
            except OSError as exc:
                raise RunnerError(f"cannot read context pack {path}: {exc}") from exc
        return "\n\n".join(chunks)

    def _record_to_store(
        self,
        role: str,
        task: str | None,
        tier: str,
        model: str,
        decision: CallDecision | None,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Write one finished call to the store; no decision means the call failed. A store error is warned about, never raised."""
        if self.store is None or not self.run_id:
            return
        from harness.store_write import split_phase_id  # here, not at the top: harness imports this module

        run_id, phase_id = split_phase_id(self.run_id)
        call = {
            "id": str(uuid.uuid4()),
            "role": role,
            "task_id": task,
            "tier": tier,
            "model": model,
            "ts": datetime.now(UTC).isoformat(),
            "ok": decision is not None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
        try:
            self.store.record_call(
                call,
                None if decision is None else to_row(decision),
                run_id=run_id,
                seq=next(self._store_seq),
                phase_id=phase_id or None,
            )
        except Exception as exc:
            warnings.warn(f"store write failed for call {call['id']}: {exc}", RuntimeWarning, stacklevel=2)

    def _charge_loop(self, thread: str | None, spent_before: float, billed: Mapping[str, float]) -> dict[str, Any]:
        """Charge a tool loop's billed turns to `thread`; returns the store row's usage fields."""
        cost_usd = billed.get("cost_usd")
        if thread is not None:
            self._thread_spend[thread] = spent_before + (cost_usd or 0.0)
        return {
            "input_tokens": int(billed.get("input_tokens", 0)),
            "output_tokens": int(billed.get("output_tokens", 0)),
            "cost_usd": cost_usd,
        }

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
        # unknown: protocol.py declares no model or effort; optional keyword-only args stay compatible with it.
        model: str | None = None,
        effort: str | None = None,
        # unknown: protocol.py does not declare this either. The caller's shadow decision is recorded, never computed here.
        router_decision: RouterDecision | None = None,
        wait_if_paused: Callable[[], None] | None = None,
        # The commands `run_check` may run in the build's worktree. The stateless path has no use for them.
        checks: Sequence[str] = (),
    ) -> NodeResult:
        # `thread` carries no history here: each call is one stateless Messages
        # request. It is the key `_thread_spend` accumulates cost under, so
        # `budget_usd` holds across a caller's calls on one thread. Before the
        # call `budget_usd` caps `max_tokens`; after it, a thread over budget
        # raises BudgetStop, once the call's own row is written. `model` and
        # `effort` arrive finished from above the runner and are used as given.
        # `task` is accepted for the protocol and ignored: this runner keeps
        # no call ledger for `_trace_evidence` to read, so there is nothing to
        # stamp it onto.
        for name, value in (("model", model), ("effort", effort)):
            if value is not None and not value.strip():
                raise RunnerError(f"node '{role}' passed an empty {name}; pass None to leave it to the tier")
        # A tier-less call takes DEFAULT_TIER as its own tier, as the Claude Code runner does.
        requested_tier = tier or DEFAULT_TIER
        # A call may name a legacy tier or a capability class; a profile may declare other tiers but a call cannot ask for them.
        if to_class(requested_tier) is None:
            raise RunnerError(f"node '{role}' asked for tier '{requested_tier}'; the tiers are {', '.join(TIERS)} or a capability class")
        resolution = _as_caller(resolve(role, hints, self.tier_overrides, self.profile_defaults, requested_tier, self.floor))
        # The resolver's class picks the model from the profile `classes` map; no class entry falls back to the tiers map.
        class_model = _class_model(self.classes, resolution.chosen_class)
        model_id = model if model is not None else class_model or self._model_for(resolution.tier)
        # When a class chose the model, the class is what the decision records as the chosen tier.
        recorded = replace(resolution, tier=resolution.chosen_class) if class_model and model is None else resolution
        effort_used = effort if effort is not None else TIER_EFFORT.get(resolution.tier, "high")
        # The default warnings filter shows this once per process, not once per runner.
        if self.router_mode == "on" and router_decision is not None:
            warnings.warn(ROUTER_ON_WARNING, RuntimeWarning, stacklevel=1)
        shadow = router_decision if self.router_mode in ("shadow", "on") else None
        # The bound skill body leads the system prompt: it is the role's craft,
        # and the context packs are the team's rules it applies them under.
        body = self.role_skills.get(role)
        packs = [body, *context] if body else list(context)
        system = "\n\n".join(part for part in (self._read_context(packs), self.extra_system) if part)

        # A paused run waits here, before the request, as it does before a Claude Code call.
        if wait_if_paused is not None:
            wait_if_paused()
        profile_prices = self.profile.get("prices") or {}
        spent_before = self._thread_spend.get(thread, 0.0) if thread is not None else 0.0
        request_max_tokens = self.max_tokens
        if budget_usd is not None and thread is not None and spent_before >= budget_usd:
            # Nothing is left to spend: stop before sending a call that could only overspend. No call, so no row.
            raise BudgetStop(
                role=role,
                thread=thread,
                session=None,
                spent_usd=spent_before,
                detail=f"node '{role}' has spent ${spent_before:.4f} of its ${budget_usd:.4f} budget",
            )
        if budget_usd is not None:
            affordable = max_output_tokens_for_budget(profile_prices, model_id, budget_usd - spent_before)
            if affordable is not None:
                # The API refuses max_tokens below 1, so an exhausted budget still sends the smallest request.
                request_max_tokens = max(1, min(request_max_tokens, affordable))
        if role == "build" and self.cwd is not None:
            if budget_usd is not None and model_id not in profile_prices:
                # The loop stops on summed `cost_usd`; an unpriced model never reports one, so the budget could never trip.
                raise RunnerError(
                    f"node '{role}' has a ${budget_usd:.4f} budget but model '{model_id}' has no price in the provider "
                    "profile, so the tool loop could not enforce it"
                )
            adapter = AnthropicToolLoopAdapter(
                self._client, model=model_id, max_tokens=request_max_tokens, effort=effort_used, prices=profile_prices
            )
            try:
                loop = run_tool_loop(
                    adapter,
                    worktree=self.cwd,
                    system_prompt=system,
                    user_prompt=prompt,
                    schema=dict(schema),
                    checks=checks,
                    turn_cap=_BUILD_TURN_CAP,
                    budget_usd=None if budget_usd is None else budget_usd - spent_before,
                    wait_if_paused=wait_if_paused,
                )
            except RunnerError as exc:
                # BudgetStop and LimitStop are RunnerErrors: every turn billed before the raise is charged and recorded.
                paid = self._charge_loop(thread, spent_before, adapter.billed)
                self._record_to_store(role, task, recorded.tier, model_id, None, **paid)
                if isinstance(exc, BudgetStop):
                    raise BudgetStop(
                        role=role,
                        thread=thread,
                        session=None,
                        spent_usd=spent_before + (paid["cost_usd"] or 0.0),
                        detail=exc.detail,
                        partial_patch=exc.partial_patch,
                        num_turns=exc.num_turns,
                    ) from exc
                raise
            paid = self._charge_loop(thread, spent_before, adapter.billed)
            if loop.stop_reason != "final":
                self._record_to_store(role, task, recorded.tier, model_id, None, **paid)
                raise RunnerError(
                    f"node '{role}' used all {_BUILD_TURN_CAP} tool-loop turns without a final answer; "
                    f"the worktree {self.cwd} holds its unfinished change ({len(loop.patch)} characters of diff)"
                )
            result = NodeResult(
                {**(adapter.answer or {}), "patch": loop.patch, **({"patch_error": loop.patch_error} if loop.patch_error else {})}
            )
            result.decision = _decision(
                role=role,
                requested_tier=requested_tier,
                resolution=recorded,
                model_id=model_id,
                effort=effort_used,
                budget_usd=budget_usd,
                task=task,
                router_decision=shadow,
            )
            self._record_to_store(
                role,
                task,
                recorded.tier,
                model_id,
                result.decision,
                **paid,
            )
            return result
        try:
            response = self._client.messages.create(
                model=model_id,
                max_tokens=request_max_tokens,
                system=system or None,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": effort_used,
                    "format": {"type": "json_schema", "schema": dict(schema)},
                },
            )
        except Exception as exc:
            # Rate limit (429) and overloaded (529) are limits, not failures; anything else is a call that did not complete.
            if isinstance(exc, _limit_exception_types()) or getattr(exc, "status_code", None) in (429, 529):
                raise LimitStop(detail=str(exc)) from exc
            raise RunnerError(f"node '{role}': {exc}") from exc

        # The vendor has billed the call once `create` returns, whatever the content turns out to be:
        # price it, charge the thread and carry the usage onto every row below, failures included.
        billed_model = getattr(response, "model", None) or model_id
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cost_usd = price_call(profile_prices, billed_model, input_tokens, output_tokens)
        if cost_usd is None:
            cost_usd = price_call(profile_prices, model_id, input_tokens, output_tokens)
        spent_usd = spent_before + (cost_usd or 0.0)
        if thread is not None:
            self._thread_spend[thread] = spent_usd
        paid = {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost_usd}

        # A refusal is an HTTP 200 with no usable content. Checking stop_reason
        # before reading content is the difference between a clear error and a
        # confusing one three frames further up.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            self._record_to_store(role, task, recorded.tier, billed_model, None, **paid)
            raise RunnerError(f"node '{role}' was refused by the model (category: {getattr(details, 'category', None)})")

        try:
            text = next(block.text for block in response.content if block.type == "text")
        except StopIteration as exc:
            self._record_to_store(role, task, recorded.tier, billed_model, None, **paid)
            raise RunnerError(f"node '{role}' returned no text block") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._record_to_store(role, task, recorded.tier, billed_model, None, **paid)
            raise RunnerError(f"node '{role}' returned text that is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            self._record_to_store(role, task, recorded.tier, billed_model, None, **paid)
            raise RunnerError(f"node '{role}' returned {type(data).__name__}, expected an object")
        result = NodeResult(data)
        result.decision = _decision(
            role=role,
            requested_tier=requested_tier,
            resolution=recorded,
            model_id=billed_model,
            effort=effort_used,
            budget_usd=budget_usd,
            task=task,
            router_decision=shadow,
        )
        # The row is written before any stop: a call the thread paid for always leaves its row.
        self._record_to_store(
            role,
            task,
            recorded.tier,
            result.decision.model_id,
            result.decision,
            **paid,
        )
        if budget_usd is not None and spent_usd > budget_usd:
            raise BudgetStop(
                role=role,
                thread=thread,
                session=None,
                spent_usd=spent_usd,
                detail=f"node '{role}' spent ${spent_usd:.4f} past its ${budget_usd:.4f} budget",
            )
        return result
