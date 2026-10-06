"""A decompose that ends on a ticket-lint refusal stores the first problem line as the run's outcome."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from graphs._contract import ContractViolation
from graphs.delivery import initiative_decompose
from graphs.delivery.initiative_decompose import GRAPH_NAME, LintRefusal, first_problem_line
from harness import cli
from harness.store_dialect import json_load
from harness.store_write import Store
from runner import ScriptedRunner

RUN = {"run_id": "r1", "ts": "2026-10-06T00:00:00Z", "principal": "initiative-decompose"}
REACH = {
    "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["~/scratch/notes.md"]}],
    "rationale": "r",
}
OK = {
    "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": ["graphs/notes.py"]}],
    "rationale": "r",
}


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["decompose"] = "acme-skills:decompose"
    cartridge["work_routing"] = {"states": {"active": "work", "planned": "work", "future": "backlog"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "deferred"}
    cartridge["write_kinds"]["state_move"] = {"risk": "low", "ramp": "deferred"}
    return cartridge


@pytest.fixture
def store(store_conn):
    s = Store(store_conn)
    s.record_run(RUN)
    return s


def stored(store: Store) -> dict:
    mark = store.conn.dialect.placeholder
    return json_load(store.conn.query_one(f"SELECT record_json FROM runs WHERE run_id = {mark}", ("r1",))[0])


def exit_code(store: Store | None, run, monkeypatch) -> int:
    """Drive the real `_run_graph` arm that every non-epic, non-cos graph shares, with `run` as the graph."""
    monkeypatch.setattr(cli, "_materialise", lambda spec, args, parser: {})
    spec = SimpleNamespace(graph_name=GRAPH_NAME, run=lambda graph_args, runner: run())
    args = SimpleNamespace(graph=GRAPH_NAME, date="2026-10-06")
    return cli._run_graph(
        specs={GRAPH_NAME: spec}, parser=None, args=args, cartridge={}, runner=None, run_id="r1", store=store
    )


def refuse(text: str):
    def run():
        raise LintRefusal(text)

    return run


def test_first_problem_line_is_the_first_line_with_text_stripped() -> None:
    assert first_problem_line(["  t1: reach — a (fix a)  ", "t2: coupling — b (fix b)"]) == "t1: reach — a (fix a)"
    assert first_problem_line(["", "  ", " t2: x "]) == "t2: x"
    assert first_problem_line([]) is None
    assert first_problem_line(["", "   "]) is None


def test_a_refusal_with_several_problem_lines_stores_only_the_first(store, monkeypatch) -> None:
    text = "\n  t1: reach — one (fix one)  \nt2: coupling — two (fix two)\nt3: reach — three (fix three)"
    assert exit_code(store, refuse(text), monkeypatch) == 1
    assert stored(store) == {**RUN, "outcome": {"refused": "t1: reach — one (fix one)"}}


def test_the_exit_code_is_unchanged_when_the_store_write_raises(store, monkeypatch, capsys) -> None:
    def boom(self, run_id, outcome):
        raise RuntimeError("store down")

    monkeypatch.setattr(Store, "record_outcome", boom)
    assert exit_code(store, refuse("t1: reach — one (fix one)"), monkeypatch) == 1
    err = capsys.readouterr().err
    assert "store down" in err and f"{GRAPH_NAME} failed: t1: reach" in err


def test_a_run_with_no_store_still_exits_non_zero(monkeypatch) -> None:
    assert exit_code(None, refuse("t1: reach — one (fix one)"), monkeypatch) == 1


def test_a_refusal_that_is_not_lint_stores_no_outcome(store, monkeypatch) -> None:
    def run():
        raise ContractViolation("decompose returned no tasks")

    assert exit_code(store, run, monkeypatch) == 1
    assert "outcome" not in stored(store)


def test_a_successful_decompose_stores_no_refused(cart, store) -> None:
    result = initiative_decompose.run(
        {"run_id": "r1", "date": "d", "cartridge": cart, "idea": "x"}, ScriptedRunner({"decompose": OK})
    )
    assert result["tasks"][0]["id"] == "t1"
    assert "outcome" not in stored(store)


def test_the_real_lint_step_refuses_a_reach_problem_and_the_run_gains_refused(cart, store, monkeypatch) -> None:
    """No stub of `lint_tickets`: the decompose graph's own lint step raises, and the cli handler stores its first line."""
    runner = ScriptedRunner({"decompose": REACH})
    args = {"run_id": "r1", "date": "d", "cartridge": cart, "idea": "x"}
    with pytest.raises(LintRefusal) as caught:
        initiative_decompose.run(args, runner)
    first = str(caught.value).splitlines()[0].strip()
    assert first.startswith("t1: reach")

    assert (
        exit_code(store, lambda: initiative_decompose.run(args, ScriptedRunner({"decompose": REACH})), monkeypatch) == 1
    )
    assert stored(store) == {**RUN, "outcome": {"refused": first}}
