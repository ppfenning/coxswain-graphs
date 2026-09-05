"""Running a phase: several tasks at once, one record every time.

Concurrency is the point and also the risk. If wall-clock order could reach the
manifest, the record would stop being a record and become a race, so these
check both halves — that the tasks really do overlap, and that the output does
not notice.
"""

from __future__ import annotations

import threading
import time
from functools import partial

from graphs.delivery import lifecycle_propose
from harness import run_phase

_run_phase = partial(run_phase, lifecycle_run=lifecycle_propose.run)

TASKS = [
    {"id": "t2-second", "surfaces": []},
    {"id": "t1-first", "surfaces": ["schema"]},
    {"id": "t3-third", "surfaces": []},
]

RESPONSES = {
    "plan": {"steps": ["s"], "files_expected": ["a.py"], "out_of_scope": []},
    "build": {
        "patch": "--- a/a.py\n+++ b/a.py\n+one\n",
        "summary": "s",
        "files_touched": ["a.py"],
        "commands_run": [{"command": "pytest", "output": "ok"}],
    },
    "review_charter": {"verdict": "approve", "findings": [], "rationale": "fine"},
}


class SlowRunner:
    """Records overlap. Each call sleeps, so sequential execution is visible."""

    def __init__(self, responses, delay=0.05):
        self.responses = responses
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return dict(self.responses[role])
        finally:
            with self.lock:
                self.active -= 1


def phase(cartridge, tasks=TASKS, max_parallel=3, responses=None):
    runner = SlowRunner(responses or RESPONSES)
    results, proposals, failures = _run_phase(
        tasks=list(tasks),
        cartridge=cartridge,
        runner=runner,
        run_id="run-1",
        date="2026-08-30",
        max_parallel=max_parallel,
    )
    return results, proposals, failures, runner


def test_independent_tasks_really_do_overlap(cartridge) -> None:
    """The whole reason for a DAG: unblocked work runs at the same time."""
    _, _, _, runner = phase(cartridge)
    assert runner.peak > 1, "tasks ran one after another; the parallelism is not real"


def test_max_parallel_is_respected(cartridge) -> None:
    _, _, _, runner = phase(cartridge, max_parallel=1)
    assert runner.peak == 1


def test_results_come_back_in_task_id_order_not_finish_order(cartridge) -> None:
    """Given deliberately unsorted input, the record is still sorted."""
    results, _, _, _ = phase(cartridge)
    assert [r["ticket"] for r in results] == ["t1-first", "t2-second", "t3-third"]


def test_two_runs_produce_the_same_record(cartridge) -> None:
    first, _, _, _ = phase(cartridge)
    second, _, _, _ = phase(cartridge)
    assert [r["ticket"] for r in first] == [r["ticket"] for r in second]
    assert [p["target"] for p in _flatten(first)] == [p["target"] for p in _flatten(second)]


def _flatten(results):
    return [p for r in results for p in r.get("proposals", [])]


def test_every_task_contributes_its_proposals(cartridge) -> None:
    _, proposals, _, _ = phase(cartridge)
    assert {p["target"] for p in proposals} == {"t1-first", "t2-second", "t3-third"}


def test_one_failing_task_does_not_take_the_phase_with_it(cartridge) -> None:
    """The others already did their work; it is still worth gating."""

    class Flaky(SlowRunner):
        def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None):
            if role == "build" and "t2-second" in prompt:
                from runner.protocol import RunnerError

                raise RunnerError("the build node fell over")
            return super().run(role=role, tier=tier, schema=schema, prompt=prompt, context=context, budget_usd=budget_usd)

    runner = Flaky(RESPONSES)
    results, proposals, failures = _run_phase(
        tasks=list(TASKS),
        cartridge=cartridge,
        runner=runner,
        run_id="r",
        date="d",
        max_parallel=3,
    )
    assert [r["ticket"] for r in results] == ["t1-first", "t3-third"]
    assert len(failures) == 1 and "t2-second" in failures[0]


def test_failures_are_reported_in_a_stable_order(cartridge) -> None:
    class AllBroken(SlowRunner):
        def run(self, *, role, tier, schema, prompt, context=(), thread=None, budget_usd=None):
            from runner.protocol import RunnerError

            raise RunnerError("down")

    _, _, failures = _run_phase(
        tasks=list(TASKS),
        cartridge=cartridge,
        runner=AllBroken(RESPONSES),
        run_id="r",
        date="d",
        max_parallel=3,
    )
    assert failures == sorted(failures)
    assert len(failures) == 3


def test_each_task_carries_its_own_surfaces_into_the_tier(cartridge) -> None:
    """t1 touches schema and must be reviewed harder than the others."""
    cartridge["policy"] = {
        "review_tier": {"tier2_surfaces": ["schema"], "tier1_max_changed_lines": 150, "tier1_max_modules": 1}
    }
    results, _, _, _ = phase(cartridge)
    tiers = {r["ticket"]: r["review_tier"] for r in results}
    assert tiers["t1-first"] == 2
    assert tiers["t2-second"] == 1
