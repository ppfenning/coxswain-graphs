"""The rules in docs/GRAPH-CONTRACT.md, as code both graphs call.

Kept beside the graphs rather than in the substrate because these are the
graph's obligations, not the cartridge's: require a cartridge, refuse a write
kind the cartridge never declared, and never let a node invent a risk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "ContractViolation",
    "epic_shape",
    "landing_for",
    "proposal",
    "require",
    "require_cartridge",
    "review_tier",
]

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
    """Where work in this state lands, per `work_routing`.

    Route by the state of the work, not by who filed it. Unscoped work must not
    reach the active board — that is how a board fills with work nobody has
    thought about and stops meaning anything.
    """
    routing = cartridge.get("work_routing") or {}
    states = routing.get("states") or {}
    landing = states.get(state)
    if landing is None:
        known = ", ".join(sorted(states)) or "none"
        raise ContractViolation(f"cartridge routes no state '{state}'; it declares: {known}")
    # The routing names an ABSTRACT landing (`planned_work`); the cartridge's
    # `landing_areas` binds it to a real place (a directory, a board). A
    # proposal must name the bound place — the first live decompose run named
    # the abstract one, and the arm rightly refused to invent a directory for
    # it. Unbound, the abstract name stands, which is what a base cartridge
    # with no bindings means.
    areas = cartridge.get("landing_areas") or {}
    return str(areas.get(str(landing), landing))


def review_tier(
    cartridge: Mapping[str, Any],
    *,
    change_facts: Mapping[str, Any],
    surfaces: Sequence[str] = (),
    patterns: Sequence[str] = (),
) -> int:
    """How much review this change has to survive. Read off `review_tier`.

    Scrutiny is proportional to what a mistake would cost, not to how large the
    diff happens to be — a four-line migration outranks a four-hundred-line
    rename. So the dangerous-surface check runs FIRST and cannot be talked down
    by size.

    0  trivial and self-evident   -> the charter reviewer alone
    1  small and contained        -> charter + an adversary
    2  touches something dangerous, or is simply big -> charter + adversary,
                                     and arbitration whether or not they disagree

    There is no tier that skips review. "Never one-shot" is the whole point:
    tier 0 is the cheapest review, not the absence of one.
    """
    config = (cartridge.get("policy") or {}).get("review_tier") or {}

    dangerous = set(config.get("tier2_surfaces") or [])
    if dangerous & set(surfaces):
        return 2

    trivial = set(config.get("tier0_patterns") or [])
    if patterns and set(patterns) <= trivial:
        return 0

    # Size alone can also make a change self-evident — a dozen lines in one
    # module, on no dangerous surface, is a charter read, not a debate. Off
    # unless the cartridge sets it: live epics reached tier 0 by no path at
    # all, because work items rarely carry patterns, so every task bought an
    # adversary and, on any disagreement, an arbiter.
    tier0_lines = int(config.get("tier0_max_changed_lines", 0))
    if tier0_lines and int(change_facts.get("changed_lines", 0)) <= tier0_lines and int(change_facts.get("module_count", 0)) <= 1:
        return 0

    max_lines = int(config.get("tier1_max_changed_lines", 150))
    max_modules = int(config.get("tier1_max_modules", 1))
    if int(change_facts.get("changed_lines", 0)) <= max_lines and int(change_facts.get("module_count", 0)) <= max_modules:
        return 1
    return 2


def proposal(
    cartridge: Mapping[str, Any],
    *,
    kind: str,
    target: str,
    evidence: Sequence[Mapping[str, Any]],
    rationale: str,
    suggested_action: str,
    subject: str | None = None,
    subject_new: bool = False,
    attempts: int | None = None,
) -> dict[str, Any]:
    """Build one proposal, refusing anything the cartridge did not authorise.

    `risk` is read off the taxonomy rather than accepted from the caller. A node
    that could name its own risk could downgrade a destructive write to `low`
    and walk it straight past the policy that exists to stop it.

    Three optional fields, each present only when it says something. A field
    that is always there and usually empty stops being read.

    `subject` is the finer-grained principal inside a kind — the runbook entry,
    not the `doc_update` category. What earns trust was never the *category* of
    write; it was the encoded judgment that produced it, and a kind's streak is
    the average of forty entries of wildly different quality.

    `subject_new` marks a proposal that CREATES its subject. It always gates: a
    brand-new entry has no track record by definition, and a streak inherited
    from the kind it happens to belong to is a track record that was never
    earned.

    `attempts` is the fix loop's count, carried so the ledger can tell a
    third-try pass from a first-try one. A pass that took three rounds is not
    the same evidence as a pass that took one, and the difference must survive
    the trip downstream.
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
        **({"subject": subject} if subject is not None else {}),
        **({"subject_new": True} if subject_new else {}),
        **({"attempts": attempts} if attempts is not None else {}),
    }
