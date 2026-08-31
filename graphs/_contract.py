"""The rules in docs/GRAPH-CONTRACT.md, as code both graphs call.

Kept beside the graphs rather than in the substrate because these are the
graph's obligations, not the cartridge's: require a cartridge, refuse a write
kind the cartridge never declared, and never let a node invent a risk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["ContractViolation", "require", "require_cartridge", "proposal"]

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
