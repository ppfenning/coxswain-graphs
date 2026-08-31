"""triage-propose — morning triage of an alert queue. Strictly read-only.

    fetch -> classify -> verify -> emit

Alerts arrive as an argument rather than being fetched here. A graph that reads
a queue reads the world, and the contract puts both the filesystem and the clock
on the far side of the graph boundary for the same reason: a graph that cannot
be replayed cannot be debugged after the fact.

The fetch cap must exceed the verify cap comfortably. A busy queue otherwise
blows the structured-output limit and the run dies mid-flight — so overflow is
counted and deferred to the next run, never dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphs._contract import ContractViolation, proposal, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["run", "GRAPH_NAME"]

GRAPH_NAME = "triage-propose"

DEFAULT_MAX_ALERTS = 15
DEFAULT_VERIFY_CAP = 5

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "symptom_key": {"type": "string"},
        "runbook_entry": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["symptom_key", "runbook_entry", "confidence"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string"},
                    "output": {"type": "string"},
                    "supports_symptom": {"type": "boolean"},
                },
                "required": ["check", "output", "supports_symptom"],
                "additionalProperties": False,
            },
        },
        "trap_considered": {"type": "string", "description": "the known wrong belief for this symptom"},
        "conclusion": {"type": "string"},
        "suggested_action": {"type": "string"},
        "actionable": {"type": "boolean"},
    },
    "required": ["checks", "trap_considered", "conclusion", "suggested_action", "actionable"],
    "additionalProperties": False,
}


def _fetch(alerts: Sequence[Mapping[str, Any]], max_alerts: int) -> tuple[list[Mapping[str, Any]], int]:
    """Cap the queue and COUNT what did not fit. Never silently truncate.

    A graph that drops nine of ten alerts and reports success on the tenth is
    worse than one that fails.
    """
    taken = list(alerts[:max_alerts])
    return taken, max(0, len(alerts) - len(taken))


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. Read-only from end to end — it emits, it never writes."""
    cartridge = require_cartridge(args)
    run_id, date = require(args, "run_id", "date")

    alerts = args.get("alerts")
    if alerts is None:
        raise ContractViolation(
            "args.alerts is required. This graph does not read the queue itself — "
            "a node that fetches cannot be replayed."
        )

    max_alerts = int(args.get("max_alerts") or DEFAULT_MAX_ALERTS)
    verify_cap = int(args.get("verify_cap") or DEFAULT_VERIFY_CAP)
    if verify_cap > max_alerts:
        raise ContractViolation(
            f"verify_cap ({verify_cap}) exceeds max_alerts ({max_alerts}); the fetch cap must "
            "comfortably exceed the verify cap or a busy queue kills the run mid-flight"
        )

    # The runbook index is a cartridge-provided path, not a skill-layout guess.
    runbook_index = (cartridge.get("landing_areas") or {}).get("runbook_index")
    context = list(cartridge.get("context") or [])
    if runbook_index:
        context.append(str(runbook_index))

    fetched, overflow = _fetch(alerts, max_alerts)

    triaged: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    deferred_for_capacity = 0

    for index, alert in enumerate(fetched):
        classification = runner.run(
            role="triage_classify",
            tier="cheap",
            schema=CLASSIFY_SCHEMA,
            context=context,
            prompt=(
                f"Classify this alert against the runbook index.\n\nAlert: {alert}\n"
                f"Date: {date}\n\nReturn the symptom key and the runbook entry it matches."
            ),
        )

        if index >= verify_cap:
            # Classified but not verified. Counted, and it comes back next run.
            deferred_for_capacity += 1
            triaged.append({"alert": dict(alert), "classification": dict(classification), "verified": False})
            continue

        verification = runner.run(
            role="evidence_verify",
            tier="deep",
            schema=VERIFY_SCHEMA,
            context=context,
            prompt=(
                "Follow the runbook entry for this symptom and run its deterministic "
                f"checks verbatim.\n\nAlert: {alert}\nClassification: {classification}\n\n"
                "State the trap — the known wrong belief for this symptom — and say "
                "whether your checks actually rule it out."
            ),
        )
        triaged.append(
            {"alert": dict(alert), "classification": dict(classification), "verification": dict(verification), "verified": True}
        )

        if verification.get("actionable") and verification.get("checks"):
            proposals.append(
                proposal(
                    cartridge,
                    kind="comment_add",
                    target=str(alert.get("id", f"alert-{index}")),
                    evidence=[
                        {"check": c["check"], "output": c["output"]}
                        for c in verification["checks"]
                        if isinstance(c, Mapping)
                    ],
                    rationale=str(verification.get("conclusion", "")),
                    suggested_action=str(verification.get("suggested_action", "")),
                )
            )

    return {
        "run_id": run_id,
        "date": date,
        "triaged": triaged,
        "proposals": proposals,
        "totals": {
            "received": len(alerts),
            "fetched": len(fetched),
            "verified": sum(1 for t in triaged if t["verified"]),
            "deferred_overflow": overflow,
            "deferred_for_capacity": deferred_for_capacity,
        },
    }
