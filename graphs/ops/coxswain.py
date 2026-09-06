"""coxswain — dispatch is a graph; invocation is the harness's.

    dispatch

One node, one role. Given a docket describing what is on hand right now — the
graph registry, the intake queue, the ready set, the ledger's shape — decide
which graphs to run next, and say why. That decision is judgment, exactly like
every other decision this codebase hands to a model, so it lives in a graph
like every other one: pure, replayable, no disk, no clock.

Actually RUNNING what got selected is a different kind of thing entirely — it
blocks on futures, and it is where the world gets touched. So it does not
happen here. `harness/cos.py` assembles the docket this graph reads, calls
this graph, and then invokes whatever it selected through
`harness.invoke.invoke_graphs`, under one run id, the same primitive
`harness/phase.py` already runs a whole phase through. This graph's own
`proposals` field is always empty: the work it dispatches carries its own
proposals through the ordinary policy/gate/ledger path once the driver has run
it, and attaching them here would just be the driver's job done twice, in the
wrong place.

The docket names each registry entry's runnability, and this graph enforces
it: a selection naming an entry the docket marks `runnable: false`, or one the
docket does not mention at all, is refused rather than proposed. The skill
that binds `dispatch` can say "never select past absent inputs" as often as it
likes; the graph is what makes that true regardless of what the model does.

An empty docket, or an `idle: true` answer with no selections, is not a
degenerate case to special-case around — it is what "there is nothing that
needs doing right now" looks like, and it is exactly as valid an answer as any
list of selections.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from graphs._contract import ContractViolation, require, require_cartridge
from runner.protocol import NodeRunner

__all__ = ["GRAPH_NAME", "run"]

GRAPH_NAME = "coxswain"

DISPATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "selections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "graph": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["graph", "why"],
                "additionalProperties": False,
            },
        },
        "idle": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["selections", "idle", "reasoning"],
    "additionalProperties": False,
}


def run(args: Mapping[str, Any], runner: NodeRunner) -> dict[str, Any]:
    """Run the graph. The docket arrives as an argument; the cos driver built it."""
    cartridge = require_cartridge(args)
    run_id, date = require(args, "run_id", "date")

    docket = args.get("docket")
    if docket is None:
        raise ContractViolation(
            "args.docket is required. This graph does not assemble system state itself — "
            "the cos driver reads the registry, the intake queue and the ledger and hands "
            "them in, because a node that gathers its own inputs cannot be replayed."
        )

    bound = cartridge.get("skills") or {}
    if "dispatch" not in bound:
        raise ContractViolation(
            "this graph needs the optional role 'dispatch' bound in the cartridge; "
            "a team that has not bound it cannot run coxswain dispatch"
        )

    registry = list(docket.get("registry") or [])
    runnable_by_name = {str(item.get("name")): bool(item.get("runnable")) for item in registry}
    context = list(cartridge.get("context") or [])

    dispatch = dict(
        runner.run(
            role="dispatch",
            tier="standard",
            schema=DISPATCH_SCHEMA,
            context=context,
            prompt=(
                f"Decide what to run next.\n\nDate: {date}\n\n"
                f"Registry (name, summary, runnable, reason when not): {registry}\n"
                f"Intake queue: {docket.get('intake') or []}\n"
                f"Ready tasks: {docket.get('ready_tasks') or []}\n"
                f"Ledger: {docket.get('ledger') or {}}\n"
                f"In flight: {docket.get('in_flight') or []}, bound max_in_flight="
                f"{docket.get('max_in_flight')}, free_slots={docket.get('free_slots')} — a "
                "selection list longer than the free slots will be truncated.\n"
                f"Usage window: verdict={(docket.get('usage') or {}).get('verdict', 'unmeasured')}, "
                f"reason={(docket.get('usage') or {}).get('reason') or ''}\n\n"
                "Select only graphs the registry marks runnable — never select past an "
                "absent input. If nothing on the docket needs doing, say so with idle=true "
                "and no selections; that is a complete and correct answer, not a fallback."
            ),
        )
    )

    selections = list(dispatch.get("selections") or [])
    idle = bool(dispatch.get("idle"))

    if idle and selections:
        raise ContractViolation(
            "dispatch returned idle=true together with selections; that is incoherent — "
            "either nothing needs running or something does, not both"
        )

    for item in selections:
        name = str(item.get("graph"))
        if name not in runnable_by_name:
            known = ", ".join(sorted(runnable_by_name)) or "none"
            raise ContractViolation(
                f"dispatch selected '{name}', which the docket's registry does not name "
                f"(it offers: {known}); the driver never has to trust a model's own belief "
                "about what exists"
            )
        if not runnable_by_name[name]:
            raise ContractViolation(
                f"dispatch selected '{name}', which the docket marks not runnable; the "
                "graph enforces 'never dispatch past absent inputs' so the driver never has to"
            )

    return {
        "run_id": run_id,
        "date": date,
        "selections": selections,
        "idle": idle,
        "reasoning": str(dispatch.get("reasoning", "")),
        "proposals": [],
    }


from graphs._spec import GraphSpec, Need  # noqa: E402

SPEC = GraphSpec(
    name="cos",  # registry key and CLI subcommand stay `cos` this release; other repos launch `shell.py cos`
    graph_name=GRAPH_NAME,
    run=run,
    summary="coxswain dispatch: judgment over a docket the driver assembled",
    needs=(
        Need(
            "docket",
            flag="--docket",
            kind="json_file",
            required=False,
            help="system state for dispatch; the cos driver assembles it when omitted",
        ),
    ),
)
