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

import json
import os
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from runner.decision_log import CallDecision, RouterDecision, joined_reasons
from runner.protocol import NodeResult, RunnerError
from runner.tier_resolution import CLASSES, TIERS, Hints, Resolution, resolve, to_class

__all__ = ["AnthropicRunner", "load_provider_profile"]

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

    `budget_usd` is the ceiling the caller granted. It is recorded, not enforced: the Messages API has no per-call spend ceiling.
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
    ) -> None:
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
    ) -> NodeResult:
        # `thread` is accepted for the protocol and ignored: each call here is
        # one stateless Messages request. Carrying history would be this
        # runner's own feature, and nothing in it is needed for correctness.
        # `budget_usd` is recorded on the decision and otherwise ignored: the
        # Messages API has no per-call spend ceiling to hand it to. `model`,
        # `effort` and `budget_usd` arrive finished from above the runner and
        # are used as given, never clipped here.
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

        response = self._client.messages.create(
            model=model_id,
            max_tokens=self.max_tokens,
            system=system or None,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort_used,
                "format": {"type": "json_schema", "schema": dict(schema)},
            },
        )

        # A refusal is an HTTP 200 with no usable content. Checking stop_reason
        # before reading content is the difference between a clear error and a
        # confusing one three frames further up.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise RunnerError(f"node '{role}' was refused by the model (category: {getattr(details, 'category', None)})")

        try:
            text = next(block.text for block in response.content if block.type == "text")
        except StopIteration as exc:
            raise RunnerError(f"node '{role}' returned no text block") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"node '{role}' returned text that is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise RunnerError(f"node '{role}' returned {type(data).__name__}, expected an object")
        result = NodeResult(data)
        result.decision = _decision(
            role=role,
            requested_tier=requested_tier,
            resolution=recorded,
            model_id=getattr(response, "model", None) or model_id,
            effort=effort_used,
            budget_usd=budget_usd,
            task=task,
            router_decision=shadow,
        )
        return result
