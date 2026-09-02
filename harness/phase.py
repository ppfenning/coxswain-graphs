"""The phase driver: many lifecycle runs at once, over a work store's ready set."""

from __future__ import annotations

from typing import Any

from graphs._spec import GraphSpec
from harness.invoke import Invocation, invoke_graphs

__all__ = ["run_phase"]

# The phase driver runs exactly one graph, and it is handed the entrypoint
# rather than a registry name — the caller already selected `lifecycle` from
# the registry. So it builds a one-entry registry of its own to invoke through,
# which keeps `invoke_graphs` with a single way to name a graph instead of two.
_LIFECYCLE = "lifecycle"


def run_phase(
    *,
    lifecycle_run,
    tasks: list[dict[str, Any]],
    cartridge: dict[str, Any],
    runner: Any,
    run_id: str,
    date: str,
    max_parallel: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Run the lifecycle graph once per ready task, at the same time.

    Not a graph — a driver. Concurrency lives here and nowhere else: the graphs
    stay pure and single-task, and this is the I/O edge, which is exactly where
    running several HTTP-bound node sequences at once belongs.

    The concurrency, the ordering and the failure policy are `harness/invoke.py`
    now; a phase is one fan-out of many, and it was the first. What this adds is
    the only thing specific to a phase: turning a ready task into the lifecycle
    graph's args.

    Results are collected into a dict and read back IN TASK-ID ORDER, so two
    runs over the same work produce the same manifest no matter who finished
    first. If wall-clock order could reach the ledger, the record would stop
    being a record and become a race.

    A failing task is quarantined, not fatal — the policy `invoke_graphs` names
    **continue-and-quarantine**. The others already ran; their work is still
    worth gating.
    """
    invocations = [
        Invocation(
            id=task["id"],
            graph=_LIFECYCLE,
            args={
                "date": date,
                "ticket": task["id"],
                "ticket_title": task.get("title") or "",
                "ticket_body": task.get("body") or "",
                "cartridge": cartridge,
                "surfaces": task.get("surfaces") or [],
                "patterns": task.get("patterns") or [],
            },
        )
        for task in tasks
    ]
    specs = {_LIFECYCLE: GraphSpec(name=_LIFECYCLE, graph_name="lifecycle-propose", run=lifecycle_run)}
    return invoke_graphs(
        invocations,
        specs=specs,
        runner=runner,
        run_id=run_id,
        max_parallel=max_parallel,
    )
