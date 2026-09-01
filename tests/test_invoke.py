"""Nested invocation: graphs invoking graphs, under one run id and one order.

The primitive `run_phase` now sits on, and that the swarm driver, the
chief-of-staff dispatcher and the bounded fix loop will sit on next. Three
things can go wrong here and none of them are loud: the fan-out quietly
serialises, wall-clock order leaks into the record, or a malformed invocation
set half-runs before anyone finds out. These check all three, plus the line
between a child that failed and a driver that is broken.
"""

from __future__ import annotations

import threading
import time

import pytest

from graphs._contract import ContractViolation
from graphs._spec import GraphSpec
from harness import Invocation, invoke_graphs
from harness.invoke import InvokeError
from runner.protocol import RunnerError


class SlowGraph:
    """A stub graph that sleeps, so sequential execution is visible.

    Records overlap the way `tests/test_phase_runner.py`'s SlowRunner does —
    but one level up, because what is concurrent here is whole graph runs, not
    individual nodes.
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.args_seen: list[dict] = []

    def run(self, args, runner):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.args_seen.append(dict(args))
        try:
            time.sleep(self.delay)
            return {
                "run_id": args["run_id"],
                "ticket": args.get("ticket"),
                "proposals": [{"kind": "comment_add", "target": args.get("ticket")}],
            }
        finally:
            with self.lock:
                self.active -= 1


def spec_for(run, name="stub"):
    return {name: GraphSpec(name=name, graph_name=f"{name}-propose", run=run)}


def invocations(*ids, graph="stub"):
    return [Invocation(id=i, graph=graph, args={"ticket": i}) for i in ids]


# Deliberately unsorted, and deliberately slowest-first is not assumed: what
# matters is that submission order is not id order.
IDS = ("t2-second", "t1-first", "t3-third")


def test_invocations_really_do_overlap() -> None:
    """The whole reason a driver exists: independent runs go at the same time."""
    graph = SlowGraph()
    invoke_graphs(list(invocations(*IDS)), specs=spec_for(graph.run), runner=None, run_id="r", max_parallel=3)
    assert graph.peak > 1, "the invocations ran one after another; the parallelism is not real"


def test_max_parallel_of_one_serialises() -> None:
    """`max_parallel` is a bound the caller sets, not a hint the driver may ignore."""
    graph = SlowGraph()
    invoke_graphs(list(invocations(*IDS)), specs=spec_for(graph.run), runner=None, run_id="r", max_parallel=1)
    assert graph.peak == 1


def test_results_come_back_in_invocation_id_order_not_finish_order() -> None:
    """Wall-clock order must never reach a manifest, so it must not reach here."""

    class Staggered(SlowGraph):
        """Finishes in the reverse of id order, on purpose."""

        def run(self, args, runner):
            time.sleep({"t1-first": 0.09, "t2-second": 0.05, "t3-third": 0.01}[args["ticket"]])
            return {"ticket": args["ticket"], "proposals": []}

    results, _, _ = invoke_graphs(
        list(invocations(*IDS)), specs=spec_for(Staggered().run), runner=None, run_id="r", max_parallel=3
    )
    assert [r["ticket"] for r in results] == ["t1-first", "t2-second", "t3-third"]


def test_proposals_flatten_in_invocation_id_order() -> None:
    """Two runs over the same invocations must produce the same proposal list."""
    graph = SlowGraph(delay=0.01)
    _, proposals, _ = invoke_graphs(
        list(invocations(*IDS)), specs=spec_for(graph.run), runner=None, run_id="r", max_parallel=3
    )
    assert [p["target"] for p in proposals] == ["t1-first", "t2-second", "t3-third"]


def test_children_run_under_a_run_id_derived_from_the_parent() -> None:
    """One run id for the fan-out, so every proposal lands in one scope.

    `_require_single_scope` on the manifest side is only satisfiable if the
    children are demonstrably branches of the parent run, not runs of their own.
    """
    graph = SlowGraph(delay=0.0)
    results, _, _ = invoke_graphs(
        list(invocations("b", "a")), specs=spec_for(graph.run), runner=None, run_id="parent-run", max_parallel=2
    )
    assert [r["run_id"] for r in results] == ["parent-run:a", "parent-run:b"]
    assert {a["run_id"] for a in graph.args_seen} == {"parent-run:a", "parent-run:b"}


def test_the_child_args_are_otherwise_the_callers() -> None:
    """The driver adds a run id and nothing else; it is not a second arg builder."""
    graph = SlowGraph(delay=0.0)
    invoke_graphs(
        [Invocation(id="a", graph="stub", args={"ticket": "a", "date": "2026-08-30", "cartridge": {"team": "acme"}})],
        specs=spec_for(graph.run),
        runner=None,
        run_id="r",
        max_parallel=1,
    )
    assert graph.args_seen[0] == {"ticket": "a", "date": "2026-08-30", "cartridge": {"team": "acme"}, "run_id": "r:a"}


def test_a_contract_violation_quarantines_only_its_own_invocation() -> None:
    """continue-and-quarantine: the siblings already did their work."""

    class Picky(SlowGraph):
        def run(self, args, runner):
            if args["ticket"] == "t2-second":
                raise ContractViolation("cartridge routes no state 'invented'")
            return super().run(args, runner)

    results, proposals, failures = invoke_graphs(
        list(invocations(*IDS)), specs=spec_for(Picky(delay=0.01).run), runner=None, run_id="r", max_parallel=3
    )
    assert [r["ticket"] for r in results] == ["t1-first", "t3-third"]
    assert [p["target"] for p in proposals] == ["t1-first", "t3-third"]
    assert len(failures) == 1
    assert failures[0].startswith("t2-second: "), "a failure that does not name its invocation is not a diagnosis"
    assert "invented" in failures[0], "the child's own message is the diagnosis; do not swallow it"


def test_a_runner_error_is_quarantined_the_same_way() -> None:
    """A node that fell over is a failed run, not a failed driver."""

    class Down(SlowGraph):
        def run(self, args, runner):
            raise RunnerError("the build node fell over")

    results, _, failures = invoke_graphs(
        list(invocations(*IDS)), specs=spec_for(Down().run), runner=None, run_id="r", max_parallel=3
    )
    assert results == []
    assert failures == sorted(failures) and len(failures) == 3


def test_a_non_contract_exception_propagates() -> None:
    """A bug in this code must not be flattened into a string in a list.

    ContractViolation and RunnerError are things a child DOES. A KeyError is
    something that is WRONG, and a driver that reports it as "invocation failed"
    hides it behind a plausible-looking failure line for as long as anyone can
    stand to read them.
    """

    class Buggy(SlowGraph):
        def run(self, args, runner):
            return {"proposals": args["a-key-nobody-set"]}

    with pytest.raises(KeyError, match="a-key-nobody-set"):
        invoke_graphs(
            list(invocations("only")), specs=spec_for(Buggy().run), runner=None, run_id="r", max_parallel=2
        )


def test_an_unknown_graph_is_refused_before_anything_runs() -> None:
    """Fail the whole call at submission, not one future at a time.

    Half a fan-out having already executed by the time the caller hears about
    their typo is a worse outcome than the typo.
    """
    graph = SlowGraph(delay=0.0)
    with pytest.raises(InvokeError, match="lifecycel"):
        invoke_graphs(
            [*invocations("a"), Invocation(id="b", graph="lifecycel", args={})],
            specs=spec_for(graph.run),
            runner=None,
            run_id="r",
            max_parallel=2,
        )
    assert graph.args_seen == [], "the valid invocation ran anyway; the refusal came too late"


def test_duplicate_invocation_ids_are_refused() -> None:
    """Ids key the results, so a repeat would silently drop one of the runs."""
    graph = SlowGraph(delay=0.0)
    with pytest.raises(InvokeError, match="duplicate"):
        invoke_graphs(
            list(invocations("a", "b", "a")), specs=spec_for(graph.run), runner=None, run_id="r", max_parallel=2
        )
    assert graph.args_seen == []


def test_a_caller_supplied_run_id_is_refused() -> None:
    """Two sources of truth for a run id is how a manifest lies.

    Overwriting it silently would be worse: the caller's id vanishes and the
    record looks fine.
    """
    graph = SlowGraph(delay=0.0)
    with pytest.raises(InvokeError, match="run_id"):
        invoke_graphs(
            [Invocation(id="a", graph="stub", args={"run_id": "some-other-run", "ticket": "a"})],
            specs=spec_for(graph.run),
            runner=None,
            run_id="r",
            max_parallel=1,
        )
    assert graph.args_seen == []


def test_no_invocations_is_an_empty_record_not_an_error() -> None:
    """A ready set can legitimately be empty; that is a fact, not a fault."""
    assert invoke_graphs([], specs={}, runner=None, run_id="r", max_parallel=4) == ([], [], [])
