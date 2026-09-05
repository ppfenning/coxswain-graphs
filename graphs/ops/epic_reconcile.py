"""epic-reconcile — declared state vs actual state. The feedback half of epics.

    compare -> emit

`lifecycle-propose` scopes work *into* an epic. Nothing until now looked at
whether the epic still describes reality afterwards, and epics drift: tickets
get closed on the board and not in the epic, phases finish out of order, work
gets added that nobody attached. An epic model that is only ever written and
never checked degrades into a diagram of what someone once intended.

Both sides arrive as ARGUMENTS. The graph does not read the tracker, for the
same reason it does not read the clock: a reconcile that fetches its own
"actual" cannot be replayed, and a drift report you cannot replay is a drift
report you cannot argue with.

Strictly propose-only. Correcting drift means touching a system of record, which
is the single most obvious place to want autonomy and the last place to grant it
cheaply — every correction goes out as a proposal and earns its ramp like
anything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphs._contract import ContractViolation, landing_for, proposal, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "run"]

GRAPH_NAME = "epic-reconcile"

RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "drifts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticket": {"type": "string"},
                    "declared": {"type": "string"},
                    "actual": {"type": "string"},
                    "correction": {"type": "string", "enum": ["item_update", "state_move", "none"]},
                    "detail": {"type": "string"},
                },
                "required": ["ticket", "declared", "actual", "correction", "detail"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["drifts", "summary"],
    "additionalProperties": False,
}


def _divergences(declared: Mapping[str, Any], observed: Mapping[str, Any]) -> list[dict[str, str]]:
    """Deterministic set comparison, computed here rather than asked of a model.

    A model asked "what drifted?" will produce a plausible answer whether or not
    anything did. Set arithmetic will not, and the reconcile node then reasons
    about differences that are already established fact.
    """
    declared_states = {str(t.get("id")): str(t.get("state")) for t in declared.get("tickets") or []}
    observed_states = {str(t.get("id")): str(t.get("state")) for t in observed.get("tickets") or []}

    facts: list[dict[str, str]] = []
    for ticket in sorted(set(declared_states) | set(observed_states)):
        here, there = declared_states.get(ticket), observed_states.get(ticket)
        if here == there:
            continue
        facts.append(
            {
                "ticket": ticket,
                "declared": here or "absent from the epic",
                "actual": there or "absent from the board",
            }
        )
    return facts


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. Declared and observed both arrive as arguments."""
    cartridge = require_cartridge(args)
    run_id, date, epic = require(args, "run_id", "date", "epic")

    observed = args.get("observed")
    if observed is None:
        raise ContractViolation(
            "args.observed is required. This graph does not read the tracker itself — "
            "a reconcile that fetches its own 'actual' cannot be replayed."
        )

    if "reconcile" not in (cartridge.get("skills") or {}):
        raise ContractViolation(
            "this graph needs the optional role 'reconcile' bound in the cartridge; "
            "a team that has not bound it cannot run epic reconciliation"
        )

    facts = _divergences(epic, observed)
    context = list(cartridge.get("context") or [])

    if not facts:
        return {
            "run_id": run_id,
            "date": date,
            "epic": epic.get("id"),
            "divergences": [],
            "reconcile": None,
            "proposals": [],
            "totals": {"declared": len(epic.get("tickets") or []), "drifted": 0},
        }

    reconcile = dict(
        runner.run(
            role="reconcile",
            tier="standard",
            schema=RECONCILE_SCHEMA,
            context=context,
            prompt=(
                f"An epic's declared state disagrees with the board.\n\nEpic: {epic.get('id')}\n"
                f"Date: {date}\nDivergences (already established by set comparison):\n{facts}\n\n"
                "For each, say which correction the epic model calls for, and why. "
                "Answer 'none' where the difference is legitimate rather than drift."
            ),
        )
    )

    proposals: list[dict[str, Any]] = []
    for drift in reconcile.get("drifts") or []:
        kind = drift.get("correction")
        if kind in (None, "none"):
            continue
        matching = next((f for f in facts if f["ticket"] == drift.get("ticket")), None)
        if matching is None:
            # The node named a ticket the set comparison never flagged. Refuse it
            # rather than propose a correction to something that did not drift.
            continue
        evidence = [
            {"check": f"declared state of {matching['ticket']}", "output": matching["declared"]},
            {"check": f"observed state of {matching['ticket']}", "output": matching["actual"]},
        ]
        if kind == "state_move":
            evidence.append(
                {"check": "work_routing", "output": f"active work lands in {landing_for(cartridge, 'active')}"}
            )
        proposals.append(
            proposal(
                cartridge,
                kind=kind,
                target=str(matching["ticket"]),
                evidence=evidence,
                rationale=str(drift.get("detail", "")),
                suggested_action=f"reconcile {matching['ticket']}: {matching['declared']} -> {matching['actual']}",
            )
        )

    return {
        "run_id": run_id,
        "date": date,
        "epic": epic.get("id"),
        "divergences": facts,
        "reconcile": reconcile,
        "proposals": proposals,
        "totals": {
            "declared": len(epic.get("tickets") or []),
            "drifted": len(facts),
            "corrections_proposed": len(proposals),
        },
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="reconcile",
    graph_name=GRAPH_NAME,
    run=run,
    summary="declared state vs actual: set arithmetic first, judgment second",
    needs=(
        Need("epic", flag="--epic", kind="json_file",
             help="the epic's DECLARED state, as JSON; this graph does not read the tracker itself"),
        Need("observed", flag="--observed", kind="json_file",
             help="the board's ACTUAL state, as JSON"),
    ),
)
