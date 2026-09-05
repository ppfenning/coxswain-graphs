"""Nested invocation: running graphs from a driver, and waiting on the results.

This is NOT a graph, and it must never acquire a `SPEC`. The contract already
ruled on the shape: *sequence belongs to a graph; concurrency belongs to the
I/O edge that already owns every side effect.* A graph is
`run(args, runner) -> dict` — pure, replayable, no clock, no disk. Something
that blocks on futures is none of those, so it lives here beside `phase.py`
rather than in `graphs/`, where `test_portability.py` would hold it to purity
rules it cannot satisfy and the honest fix would be to weaken the test.

One primitive, three consumers. `run_phase` is the first (it is now a thin
wrapper over this); the epic-swarm driver, the coxswain dispatcher, and
the bounded fix loop are the ones still to arrive. Building it once is the
point, so the API stays general — an id, a registry name, some args — and
small.

Two properties are load-bearing, and both are about the record rather than the
running:

**One run_id, derived here.** Children run under `f"{parent}:{invocation.id}"`,
so every proposal a fan-out produces flows into the same policy, gate and
ledger as everything else instead of into a side channel. The driver derives
that id and refuses to accept one from the caller: two sources of truth for a
run id is how a manifest lies.

**Invocation-id order, never wall-clock order.** Results are collected into a
dict and read back sorted, so two runs over the same invocations produce the
same manifest no matter who finished first.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from graphs._contract import ContractViolation
from graphs._spec import GraphSpec
from runner.protocol import RunnerError

__all__ = ["Invocation", "InvokeError", "invoke_graphs"]


class InvokeError(Exception):
    """The set of invocations is malformed — a caller bug, not a run failure.

    Distinct from `ContractViolation` and `RunnerError` on purpose: those are
    things a child run does, and they are quarantined. This is something the
    driver's caller did, it is true before any work starts, and it fails the
    whole call.
    """


@dataclass(frozen=True)
class Invocation:
    """One graph to run: which graph, on what args, under what id.

    `id` does double duty — it is the deterministic ordering key for results
    and failures, and it is the suffix of the child's run id. That is one name
    for one thing, so a failure string, a result's position and a manifest row
    all point at the same invocation.

    `args` carries the graph's args WITHOUT a run id; the driver derives that.
    """

    id: str
    graph: str
    args: Mapping[str, Any]


def invoke_graphs(
    invocations: Sequence[Invocation],
    *,
    specs: Mapping[str, GraphSpec],
    runner: Any,
    run_id: str,
    max_parallel: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Invoke each named graph with its args, at the same time, under one run id.

    `specs` is what `harness.registry.discover()` returns — the driver selects
    from the registry by name rather than holding entrypoints, so a driver
    never has to know every graph by name either.

    The failure policy is **continue-and-quarantine**, and naming it is the
    only new thing about it: `harness/phase.py` shipped this behaviour with the
    argument that "one task failing must not take the phase with it", and the
    epic-swarm spec then named it as the `continue_independent` default. A
    child's `ContractViolation` or `RunnerError` becomes a per-invocation
    failure string and the siblings run on, because their work already happened
    and is still worth gating.

    Every OTHER exception propagates with its traceback intact. A bug in a
    driver is not a failed invocation, and flattening one into a string in a
    list is how it goes unnoticed for a month.

    The whole call is refused, before anything runs, when the invocations
    cannot produce an honest record: an unknown graph name, a duplicate id
    (two results under one key silently drops one), or a caller-supplied
    `run_id` (see the module docstring). Failing one future at a time would
    mean half the fan-out had already executed by the time anyone found out.

    Returns `(results in id order, their proposals flattened in id order,
    sorted failure strings)` — the shape `run_phase` has always returned.
    """
    _refuse_malformed(invocations, specs=specs)

    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = {
            pool.submit(
                specs[invocation.graph].run,
                {**invocation.args, "run_id": f"{run_id}:{invocation.id}"},
                runner,
            ): invocation
            for invocation in invocations
        }
        for future in as_completed(futures):
            invocation = futures[future]
            try:
                results[invocation.id] = future.result()
            except (ContractViolation, RunnerError) as exc:
                # continue-and-quarantine: this one is set aside with its
                # diagnosis, the rest of the fan-out finishes.
                failures.append(f"{invocation.id}: {exc}")

    ordered = [results[key] for key in sorted(results)]
    proposals = [item for result in ordered for item in result.get("proposals", [])]
    return ordered, proposals, sorted(failures)


def _refuse_malformed(invocations: Sequence[Invocation], *, specs: Mapping[str, GraphSpec]) -> None:
    """Raise on anything that would make the fan-out's record untrustworthy.

    Every offender of a kind is named at once, for the same reason
    `_contract.require` names every missing arg at once: fixing a caller one
    error message at a time is a worse loop than reading three.
    """
    counts = Counter(inv.id for inv in invocations)
    duplicates = sorted(name for name, times in counts.items() if times > 1)
    if duplicates:
        raise InvokeError(
            f"duplicate invocation id(s): {', '.join(duplicates)}. Ids key the results, "
            "so a repeat would silently drop one of the runs it named."
        )

    unknown = sorted({inv.graph for inv in invocations if inv.graph not in specs})
    if unknown:
        known = ", ".join(sorted(specs)) or "none"
        raise InvokeError(f"no graph registers the subcommand(s): {', '.join(unknown)}; the registry offers: {known}")

    presupplied = sorted(inv.id for inv in invocations if "run_id" in inv.args)
    if presupplied:
        raise InvokeError(
            f"invocation(s) {', '.join(presupplied)} supply their own run_id. The driver derives "
            "it from the parent run so the children record under one scope; two sources of truth "
            "for a run id is how a manifest lies."
        )
