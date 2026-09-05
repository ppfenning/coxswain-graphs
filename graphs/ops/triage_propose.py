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

__all__ = ["GRAPH_NAME", "run"]

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
        "trap_held": {"type": "boolean", "description": "did the runbook's stated trap actually apply here"},
        "runbook_correction": {
            "type": "string",
            "description": "what the runbook entry gets wrong, or empty if it held up",
        },
        "conclusion": {"type": "string"},
        "suggested_action": {"type": "string"},
        "actionable": {"type": "boolean"},
    },
    "required": [
        "checks",
        "trap_considered",
        "trap_held",
        "runbook_correction",
        "conclusion",
        "suggested_action",
        "actionable",
    ],
    "additionalProperties": False,
}


def _fetch(alerts: Sequence[Mapping[str, Any]], max_alerts: int) -> tuple[list[Mapping[str, Any]], int]:
    """Cap the queue and COUNT what did not fit. Never silently truncate.

    A graph that drops nine of ten alerts and reports success on the tenth is
    worse than one that fails.
    """
    taken = list(alerts[:max_alerts])
    return taken, max(0, len(alerts) - len(taken))


def _runbook_gap(
    classification: Mapping[str, Any],
    verification: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Did this run learn something the runbook does not know?

    Two cases, and only two — both established by the run rather than guessed:

    - Nothing matched, or the match was a guess. That is a missing entry.
    - The entry's trap did not hold. That is the worse one: a runbook that only
      states the right answer lets the next person re-derive the wrong one, and
      a trap that is itself wrong actively points them at it.
    """
    entry = str(classification.get("runbook_entry") or "").strip()
    confidence = str(classification.get("confidence") or "").lower()
    correction = str(verification.get("runbook_correction") or "").strip()

    if not entry:
        return (
            f"no runbook entry matched symptom '{classification.get('symptom_key')}'",
            f"add a runbook entry for '{classification.get('symptom_key')}', with its trap",
        )
    if correction:
        return (f"the runbook entry needs correcting: {correction}", f"amend '{entry}': {correction}")
    if verification.get("trap_held") is False:
        return (
            f"the trap stated in '{entry}' did not apply to this alert",
            f"amend the trap in '{entry}' — as written it points at the wrong belief",
        )
    if confidence == "low":
        return (
            f"'{entry}' matched only weakly (confidence: low)",
            f"sharpen the match criteria on '{entry}', or add an entry that fits better",
        )
    return None


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
    runbook_gaps = 0

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

        # The runbook improves itself, or it rots. A symptom nothing matched and
        # a trap that turned out to be wrong are the two facts a runbook can only
        # learn from a run — nobody goes back to amend one from memory.
        #
        # Still propose-only: a doc_update is a proposal like any other, so this
        # does not put a write into a read-only graph. `doc_update` is `deferred`
        # in the base taxonomy, so it cannot auto-apply until the basics have
        # earned their ramp anyway.
        gap = _runbook_gap(classification, verification)
        if gap is not None:
            reason, correction = gap
            runbook_gaps += 1
            # The proposal names the entry it is about, because the entry — not
            # the `doc_update` category — is the thing with a track record. An
            # amendment carries the entry it amends; a gap nothing matched
            # carries the symptom it would be filed under, and says it is new,
            # because an entry that does not exist yet cannot have earned
            # anything from the streak of the forty around it.
            entry = str(classification.get("runbook_entry") or "").strip()
            symptom = str(classification.get("symptom_key") or "").strip()
            subject = entry or symptom or None
            proposals.append(
                proposal(
                    cartridge,
                    kind="doc_update",
                    target=str(classification.get("runbook_entry") or classification.get("symptom_key") or "runbook"),
                    subject=subject,
                    subject_new=subject is not None and not entry,
                    evidence=[
                        {"check": "classification confidence", "output": str(classification.get("confidence"))},
                        {"check": "matched runbook entry", "output": str(classification.get("runbook_entry") or "none")},
                        *(
                            {"check": c["check"], "output": c["output"]}
                            for c in (verification.get("checks") or [])
                            if isinstance(c, Mapping)
                        ),
                    ],
                    rationale=reason,
                    suggested_action=correction,
                )
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
            "runbook_gaps": runbook_gaps,
        },
    }


# How a harness offers this graph as a subcommand. See graphs/_spec.py for
# why the spec is declarative and why the types live with the graphs.
from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="triage",
    graph_name=GRAPH_NAME,
    run=run,
    summary="morning triage of an alert queue; read-only, proposes runbook corrections",
    needs=(
        Need("alerts", flag="--alerts", kind="json_file",
             help="path to a JSON list of alerts; this graph does not read the queue itself"),
        Need("max_alerts", flag="--max-alerts", kind="int", required=False,
             help="fetch cap (default 15)"),
    ),
)
