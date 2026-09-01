"""The phase driver: many lifecycle runs at once, over a work store's ready set."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from graphs._contract import ContractViolation
from runner.protocol import RunnerError

__all__ = ["run_phase"]


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

    Results are collected into a dict and read back IN TASK-ID ORDER, so two
    runs over the same work produce the same manifest no matter who finished
    first. If wall-clock order could reach the ledger, the record would stop
    being a record and become a race.
    """
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as pool:
        futures = {
            pool.submit(
                lifecycle_run,
                {
                    "run_id": f"{run_id}:{task['id']}",
                    "date": date,
                    "ticket": task["id"],
                    "cartridge": cartridge,
                    "surfaces": task.get("surfaces") or [],
                },
                runner,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                results[task["id"]] = future.result()
            except (ContractViolation, RunnerError) as exc:
                # One task failing must not take the phase with it. The others
                # already ran; their work is still worth gating.
                failures.append(f"{task['id']}: {exc}")

    ordered = [results[key] for key in sorted(results)]
    proposals = [p for result in ordered for p in result.get("proposals", [])]
    return ordered, proposals, sorted(failures)
