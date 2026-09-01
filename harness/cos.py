"""The chief-of-staff driver: assembles the docket, runs dispatch, invokes what it picked.

NOT a graph, and it must never acquire a `SPEC` — the same rule
`harness/invoke.py` states for itself, and for the same reason.
`graphs/ops/chief_of_staff.py` is the graph: one node, the `dispatch` role,
judgment over a docket it is handed. This module is the other half — reading
enough of the system to build that docket, and then acting on whatever the
graph decided, through the nested-invocation primitive `harness/invoke.py`
already provides. The split is the one `harness/phase.py` draws for a phase:
sequence and judgment belong to a graph; concurrency and reading the world
belong to the I/O edge that already owns every side effect.

Two functions, two moments:

    assemble_docket   BEFORE the graph runs. Reads the intake queue and the
                       ledger, and marks each graph the registry offers
                       runnable or not from what is actually constructible
                       right now — never from what a graph merely exists to
                       do. Registry entries come from `specs` alone, so a new
                       graph appears in the docket the moment it declares a
                       SPEC, the same argument `harness.registry.discover`
                       makes for the CLI.

    run_cos           AFTER the graph runs (or runs it itself, when handed no
                       `cos_result`). Turns each selection into constructed
                       args, invokes the selected graphs under the parent run
                       id via `invoke_graphs`, and consumes the intake item a
                       `decompose` selection worked on — but only once that
                       invocation is known to have actually succeeded.
                       Consumption means "processed", not "approved": approval
                       is the gate's business, downstream of the proposals
                       this produces, not the queue's.

An empty docket producing `idle: true` and no selections is not treated as a
degenerate case anywhere in here: `run_cos` given no selections invokes
nothing and returns an empty record, honestly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.intake import consume as consume_intake_item
from core.intake import read_queue
from core.ledger import read as read_ledger
from graphs._spec import GraphSpec
from harness.invoke import Invocation, invoke_graphs

__all__ = ["CosError", "assemble_docket", "run_cos"]


class CosError(Exception):
    """This driver has no argument recipe for a graph a selection named.

    Distinct from `graphs._contract.ContractViolation`: that is the dispatch
    graph refusing a selection against the docket it was handed. This is the
    same shape of problem one layer down — a selection the graph already
    approved as runnable, but that names a graph `run_cos` does not yet know
    how to build invocation args for. Extending `_KNOWN_GRAPHS` (and the
    branch in `run_cos` below) is how a new graph joins chief-of-staff
    dispatch for real, not just in the registry listing.
    """


# Which graphs this driver can build invocation args for. `assemble_docket`
# marks everything else not runnable, so `run_cos` should never see a
# selection outside this set — `CosError` exists for the day something drifts.
_KNOWN_GRAPHS = ("retro", "decompose", "triage")


def assemble_docket(
    *,
    specs: Mapping[str, GraphSpec],
    intake_root: Path | str | None,
    ledger_path: Path | str | None,
    alerts_present: bool,
) -> dict[str, Any]:
    """Read what is on hand, and mark each registered graph runnable or not.

    `retro` is runnable iff the ledger holds rows to retro over. `decompose`
    is runnable iff the intake queue holds a queued idea. `triage` is runnable
    iff the caller says it has alerts in hand this run — this driver never
    fetches an alert queue itself, the same rule `triage-propose` holds for
    its own `alerts` argument. Every other registered graph — `lifecycle`,
    `reconcile`, `cos` itself, anything needing inputs this driver cannot
    construct (a work store, a declared-vs-observed pair) — is marked not
    runnable, with the reason naming what is missing, rather than left off
    the registry where a model could not tell "not needed" from "not built
    yet".

    Both reads tolerate their source being absent: `read_queue` returns `[]`
    for a missing directory, `read_ledger` returns `()` for a missing file —
    a docket assembled on day one, before either exists, is not an error.
    """
    intake_items = read_queue(intake_root) if intake_root is not None else []
    ledger_rows = read_ledger(ledger_path) if ledger_path is not None else ()

    clean = sum(1 for row in ledger_rows if row.get("outcome") == "clean")
    reversal = sum(1 for row in ledger_rows if row.get("outcome") == "reversal")
    agreement = clean / (clean + reversal) if (clean + reversal) else None

    def _runnable(name: str) -> tuple[bool, str]:
        if name == "retro":
            return (bool(ledger_rows), "" if ledger_rows else "no ledger rows to run a retro over")
        if name == "decompose":
            return (bool(intake_items), "" if intake_items else "the intake queue is empty")
        if name == "triage":
            return (alerts_present, "" if alerts_present else "no alerts were provided this run")
        return (False, f"this driver cannot construct '{name}'s required inputs")

    registry = []
    for name in sorted(specs):
        spec = specs[name]
        runnable, reason = _runnable(name)
        registry.append({"name": name, "summary": spec.summary, "runnable": runnable, "reason": reason})

    return {
        "registry": registry,
        "intake": [{"id": item["id"], "kind": item["kind"], "title": item["title"]} for item in intake_items],
        "ready_tasks": [],
        "ledger": {"rows": len(ledger_rows), "agreement": agreement},
    }


def run_cos(
    *,
    docket: Mapping[str, Any],
    specs: Mapping[str, GraphSpec],
    runner: Any,
    cartridge: Mapping[str, Any],
    run_id: str,
    date: str,
    max_parallel: int,
    intake_root: Path | str | None = None,
    ledger_path: Path | str | None = None,
    alerts: Sequence[Mapping[str, Any]] | None = None,
    cos_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run (or accept) the dispatch decision, then invoke whatever it selected.

    `cos_result` lets a caller that already ran the `cos` graph — or, in
    tests, scripted its decision directly without a model — hand the decision
    straight in. Omitted, this calls `specs["cos"].run` itself, so a caller
    with nothing special to do about the decision can drive the whole thing
    through this one function.

    Reads the intake queue and the ledger itself, independently of whatever
    `assemble_docket` read to build the docket: the docket carries only what
    a model needs to decide (row counts, item titles), never a queued item's
    full body or the ledger's actual rows, and constructing a `decompose`
    invocation's `idea` — or a `retro` invocation's `ledger_rows` — needs
    exactly that.
    """
    intake_items = read_queue(intake_root) if intake_root is not None else []
    ledger_rows = read_ledger(ledger_path) if ledger_path is not None else ()

    if cos_result is None:
        cos_result = specs["cos"].run(
            {"run_id": run_id, "date": date, "cartridge": cartridge, "docket": docket}, runner
        )

    selections = list(cos_result.get("selections") or [])

    invocations: list[Invocation] = []
    decompose_queue = list(intake_items)
    # Only decompose invocations consume anything, so only they need their
    # queue item remembered alongside the invocation id that will tell us
    # whether it actually ran.
    decompose_by_invocation: list[tuple[str, Mapping[str, Any]]] = []

    for index, selection in enumerate(selections):
        graph = str(selection.get("graph"))
        invocation_id = f"{graph}-{index}"

        if graph == "retro":
            args: dict[str, Any] = {"ledger_rows": list(ledger_rows), "cartridge": cartridge, "date": date}
        elif graph == "decompose":
            if not decompose_queue:
                raise CosError(
                    f"selection {index} dispatches 'decompose' but the intake queue has no "
                    "(more) items to construct an idea from"
                )
            item = decompose_queue.pop(0)
            args = {"idea": item["body"], "cartridge": cartridge, "date": date}
            decompose_by_invocation.append((invocation_id, item))
        elif graph == "triage":
            args = {"alerts": list(alerts or []), "cartridge": cartridge, "date": date}
        else:
            raise CosError(
                f"selection {index} dispatches '{graph}', which this driver has no argument "
                f"recipe for (known: {', '.join(_KNOWN_GRAPHS)})"
            )

        invocations.append(Invocation(id=invocation_id, graph=graph, args=args))

    results, proposals, failures = invoke_graphs(
        invocations, specs=specs, runner=runner, run_id=run_id, max_parallel=max_parallel
    )

    failed_ids = {failure.split(":", 1)[0] for failure in failures}

    consumed: list[str] = []
    for invocation_id, item in decompose_by_invocation:
        if invocation_id in failed_ids:
            continue
        # Consumption means "processed", not "approved" — approval is the
        # gate's business, downstream of the proposals this invocation just
        # produced. This only marks the item handled once its invocation is
        # known to have actually run.
        consume_intake_item(item["path"])
        consumed.append(item["id"])

    return {
        "selections": selections,
        "results": results,
        "proposals": proposals,
        "failures": failures,
        "consumed": consumed,
    }
