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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from runner.protocol import NodeResult, RunnerError

__all__ = ["AnthropicRunner", "load_provider_profile"]

# Roles doing verification, adversarial review, or arbitration are worth more
# capability than roles doing bulk enumeration. The profile decides which model
# each tier means; this is only the fallback when a node names no tier.
DEFAULT_TIER = "standard"

# Effort is the first quality/cost lever, and it belongs to the tier rather than
# the node: a `cheap` node is cheap because the work is bulk, not because we
# want it to think less about a hard case it happens to hit.
TIER_EFFORT = {"cheap": "low", "standard": "high", "deep": "xhigh"}


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
        tier: str = DEFAULT_TIER,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
    ) -> NodeResult:
        model = self._model_for(tier)
        # The bound skill body leads the system prompt: it is the role's craft,
        # and the context packs are the team's rules it applies them under.
        body = self.role_skills.get(role)
        packs = [body, *context] if body else list(context)
        system = "\n\n".join(part for part in (self._read_context(packs), self.extra_system) if part)

        response = self._client.messages.create(
            model=model,
            max_tokens=self.max_tokens,
            system=system or None,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": TIER_EFFORT.get(tier, "high"),
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
        return NodeResult(data)
