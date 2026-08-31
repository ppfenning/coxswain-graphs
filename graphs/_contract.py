"""The rules in docs/GRAPH-CONTRACT.md, as code both graphs call.

Kept beside the graphs rather than in the substrate because these are the
graph's obligations, not the cartridge's: require a cartridge, refuse a write
kind the cartridge never declared, and never let a node invent a risk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["ContractViolation", "require", "require_cartridge", "proposal", "epic_shape", "landing_for"]

PROPOSAL_FIELDS = ("kind", "risk", "target", "evidence", "rationale", "suggested_action")


class ContractViolation(Exception):
    """A graph was asked to do something the contract forbids."""


def require_cartridge(args: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the resolved cartridge, or raise. There is NO fallback.

    Not "defaults to the last known config" — required. A fallback means the
    seam is never exercised, so it rots silently while the cartridge drifts,
    and the first symptom is a production run against year-old values.
    """
    cartridge = args.get("cartridge")
    if cartridge is None:
        raise ContractViolation(
            "args.cartridge is required and has no fallback. Resolve one with "
            "`python -m core.cartridge --team <team> --json` and pass it in."
        )
    if not isinstance(cartridge, Mapping) or "cartridge_sha" not in cartridge:
        raise ContractViolation(
            "args.cartridge must be a RESOLVED cartridge (it needs a cartridge_sha); "
            "pass the loader's output, not a raw cartridge.yaml."
        )
    return cartridge


def require(args: Mapping[str, Any], *names: str) -> tuple[Any, ...]:
    """Fetch required args, naming every missing one at once."""
    missing = [name for name in names if args.get(name) is None]
    if missing:
        raise ContractViolation(f"missing required arg(s): {', '.join(missing)}")
    return tuple(args[name] for name in names)


def epic_shape(cartridge: Mapping[str, Any], *, phases: int, tickets: int, repos: int) -> str:
    """Decide epic / parent+subtasks / single ticket, from the cartridge's threshold.

    Read off `epic_threshold`, never hardcoded: a team that thinks two tickets is
    an epic and a team that thinks five is are both right about their own board,
    and neither belongs in a graph.

    Most work is not an epic. Making everything an epic is how a board becomes
    unreadable, so the threshold is a bar to clear, not a default.
    """
    threshold = cartridge.get("epic_threshold") or {}
    if (
        phases >= int(threshold.get("phases_min", 2))
        or tickets >= int(threshold.get("tickets_min", 3))
        or (bool(threshold.get("multi_repo", True)) and repos > 1)
    ):
        return "epic"
    return "parent_with_subtasks" if tickets > 1 else "ticket"


def landing_for(cartridge: Mapping[str, Any], state: str) -> str:
    """Where work in this state lands, per `ticket_routing`.

    Route by the state of the work, not by who filed it. Unscoped work must not
    reach the active board — that is how a board fills with work nobody has
    thought about and stops meaning anything.
    """
    routing = cartridge.get("ticket_routing") or {}
    states = routing.get("states") or {}
    landing = states.get(state)
    if landing is None:
        known = ", ".join(sorted(states)) or "none"
        raise ContractViolation(f"cartridge routes no state '{state}'; it declares: {known}")
    return str(landing)


def proposal(
    cartridge: Mapping[str, Any],
    *,
    kind: str,
    target: str,
    evidence: Sequence[Mapping[str, Any]],
    rationale: str,
    suggested_action: str,
) -> dict[str, Any]:
    """Build one proposal, refusing anything the cartridge did not authorise.

    `risk` is read off the taxonomy rather than accepted from the caller. A node
    that could name its own risk could downgrade a destructive write to `low`
    and walk it straight past the policy that exists to stop it.
    """
    write_kinds = cartridge.get("write_kinds") or {}
    spec = write_kinds.get(kind)
    if not isinstance(spec, Mapping):
        known = ", ".join(sorted(write_kinds)) or "none"
        raise ContractViolation(f"unknown write kind '{kind}'; the cartridge declares: {known}")

    risk = spec.get("risk")
    if risk is None:
        raise ContractViolation(f"write kind '{kind}' declares no risk; the taxonomy is incomplete")

    if not evidence:
        raise ContractViolation(
            f"proposal for '{kind}' carries no evidence. A claim without evidence is not a "
            "proposal, it is a guess with formatting."
        )

    return {
        "kind": kind,
        "risk": risk,
        "target": target,
        "evidence": [dict(item) for item in evidence],
        "rationale": rationale,
        "suggested_action": suggested_action,
    }
