"""A runner that returns canned answers, so a whole graph is testable offline.

This is not a mock in the apologetic sense — it is the reason the graphs can be
tested at all. Every node in this system is a model call, and a test suite that
needs a network and a key to run is a test suite nobody runs in CI.

It is also deliberately strict. A scripted runner that invented a plausible
answer for a node the test forgot to script would let a graph change shape
without any test noticing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from runner.protocol import NodeResult, RunnerError

__all__ = ["ScriptedRunner"]


class ScriptedRunner:
    """Replays `{role: response}` (or `{role: [response, ...]}`) by role.

    Records every call on `.calls` so a test can assert on what the graph asked
    for — the tier it requested, the context it passed — and not merely on what
    came back.
    """

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self._responses = {
            role: list(value) if isinstance(value, list) else [value] for role, value in responses.items()
        }
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        *,
        role: str,
        tier: str,
        schema: Mapping[str, Any],
        prompt: str,
        context: Sequence[str] = (),
    ) -> NodeResult:
        self.calls.append({"role": role, "tier": tier, "prompt": prompt, "context": list(context)})
        queued = self._responses.get(role)
        if not queued:
            raise RunnerError(
                f"no scripted response for role '{role}'"
                + (f" (call {len(self.calls)})" if role in self._responses else "")
                + "; scripting every node a graph runs is how a test notices the graph changed shape"
            )
        return NodeResult(queued.pop(0) if len(queued) > 1 else queued[0])
