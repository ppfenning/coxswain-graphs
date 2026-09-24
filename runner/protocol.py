"""The seam between a graph and a model.

A graph is an ordered set of agent nodes. It owns *sequence* — which node runs,
in what order, on what. It does not own *execution*, and it must not: a graph
that constructs its own API client cannot be replayed, cannot be tested without
a network, and cannot be pointed at a different provider without an edit.

So execution arrives as an argument. `graph(args, runner)` is the whole shape.
Tests pass a `ScriptedRunner` holding canned dicts; production passes an
`AnthropicRunner`. Neither the graph nor its tests change between them.

A node asks for a ROLE and a TIER, never a skill and never a model:

    role   what the node needs done      -> cartridge maps role -> skill name
    tier   how much capability it wants  -> provider profile maps tier -> model

Both indirections exist so the same graph runs for any team, on any provider.
The runner is the only object in the system that gets to know either mapping.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from runner.decision_log import CallDecision

__all__ = ["BudgetStop", "Capability", "LimitStop", "NodeResult", "NodeRunner", "ProviderProfile", "RunnerError", "resolve_profile"]

_TIERS = ("cheap", "standard", "deep")


class RunnerError(Exception):
    """A node could not be run, or came back with something unusable."""


class BudgetStop(RunnerError):
    """The CLI stopped a node on a budget limit, not a real failure.

    `error_max_budget_usd` names the shape ceiling (split the task);
    `error_spend_cap` names the operator's `--node-cap-usd` (the operator's
    limit), per docs/design/cost-bounds.md §1 — the effective limit sent to
    the provider is `min(shape_ceiling, node_cap)`. The session named here
    still exists on disk with its whole context; a later phase resumes it
    rather than starting the node over.
    """

    def __init__(
        self,
        *,
        role: str,
        thread: str | None,
        session: str | None,
        spent_usd: float,
        detail: str,
        partial_patch: str = "",
    ) -> None:
        self.role = role
        self.thread = thread
        self.session = session
        self.spent_usd = spent_usd
        self.partial_patch = partial_patch
        self.detail = detail
        super().__init__(detail)


class LimitStop(RunnerError):
    """The account's session limit stopped the CLI. `detail` is the raw banner text."""

    def __init__(self, *, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class NodeResult(dict):
    """A node's structured return. A plain dict, deliberately.

    Return shapes stay small. A node that hands the next node a large blob has
    moved the reasoning into the wrong place, and blows structured-output limits
    on a busy day.

    `decision` is an attribute, not a key, so it never reaches structured output.
    """

    decision: CallDecision | None = None


class Capability(StrEnum):
    """A thing a provider profile may declare, per tier."""

    STRUCTURED_OUTPUT = "structured_output"
    TOOL_USE = "tool_use"
    RESUME = "resume"
    STREAMING = "streaming"
    MAX_CONTEXT = "max_context"


@dataclass(frozen=True)
class ProviderProfile:
    """A tier's capabilities and per-tier model, per docs/design/vendor-axis.md §3."""

    capabilities: Mapping[str, Any]
    tiers: Mapping[str, str]

    def has(self, capability: Capability) -> bool:
        return bool(self.capabilities.get(capability.value))


def resolve_profile(
    provider_profile: ProviderProfile | Mapping[str, ProviderProfile],
    *,
    role: str,
    tier: str,
    required: Sequence[Capability] = (),
) -> tuple[ProviderProfile, dict[str, str] | None]:
    """Resolve `tier`, retrying one tier up per §5 if `required` is unmet."""
    by_tier = provider_profile if isinstance(provider_profile, Mapping) else dict.fromkeys(_TIERS, provider_profile)
    profile = by_tier[tier]
    missing = next((c for c in required if not profile.has(c)), None)
    if missing is None:
        return profile, None
    index = _TIERS.index(tier)
    if index + 1 == len(_TIERS):
        raise RunnerError(f"{role} needs {missing.value}; no tier above {tier!r}")
    resolved_tier = _TIERS[index + 1]
    resolved = by_tier[resolved_tier]
    still_missing = next((c for c in required if not resolved.has(c)), None)
    if still_missing is not None:
        raise RunnerError(f"{role} needs {still_missing.value}; {resolved_tier!r} lacks it too")
    return resolved, {
        "event": "capability_fallback",
        "role": role,
        "requested_tier": tier,
        "resolved_tier": resolved_tier,
        "missing_capability": missing.value,
    }


@runtime_checkable
class NodeRunner(Protocol):
    """Runs one agent node and returns its structured output."""

    def run(
        self,
        *,
        role: str,
        tier: str,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
        thread: str | None = None,
        budget_usd: float | None = None,
        task: str | None = None,
    ) -> NodeResult:
        """Execute one node.

        `context` is a list of absolute paths to context packs, resolved by the
        cartridge. The runner reads them; the graph never does. That is the
        contract rule about scripts having no filesystem access, enforced by
        putting the filesystem on the other side of this boundary.

        `thread` is a continuity hint: nodes that share one may share what
        earlier nodes in it learned — the same session, the same scratch tree.
        A graph uses it to keep plan, build and a fix-loop retry on one
        instance, and withholds it from review, which must never inherit the
        builder's reasoning. A runner may ignore it entirely; the scripted
        runner does, which is why the graphs stay replayable.

        `budget_usd` is a per-call dollar ceiling that overrides the role or
        tier one when given.

        `task` names the task id this call belongs to, for a runner that keeps
        a call ledger to stamp onto its own record. A runner may ignore it.
        """
        ...
