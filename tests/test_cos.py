"""coxswain: the graph decides, `harness/cos.py` builds the docket and acts.

Two things are under test here, deliberately in one file: the `coxswain`
graph's own refusals (an unrunnable or unknown selection, an incoherent
idle-with-selections answer), and the driver's two functions — `assemble_docket`
reading the intake queue and the ledger into a runnability picture, `run_cos`
invoking exactly what got selected and consuming only what actually ran.
"""

from __future__ import annotations

import pytest
from core.ledger import append as ledger_append

from graphs._contract import ContractViolation
from graphs._spec import GraphSpec
from graphs.ops import coxswain
from harness import cli, cos
from runner import ScriptedRunner


def ledger_row(kind: str, outcome: str) -> dict:
    return {
        "run_id": "r0",
        "ts": "2026-08-01T00:00:00Z",
        "principal": "triage-propose",
        "kind": kind,
        "risk": "low",
        "outcome": outcome,
        "cartridge_sha": "sha-fixture",
        "provider_profile": "anthropic-default",
    }


@pytest.fixture
def cart(cartridge) -> dict:
    cartridge["skills"]["dispatch"] = "acme-skills:dispatch"
    return cartridge


def test_a_successful_dispatch_names_the_graph_and_the_item():
    line = cos.dispatch_line({"graph": "decompose", "item": "queue/001-idea.md"}, {})
    assert line == "dispatched decompose for queue/001-idea.md"


def test_a_failed_dispatch_also_carries_the_reason():
    line = cos.dispatch_line(
        {"graph": "decompose", "item": "queue/001-idea.md"}, {"reason": "error_max_budget_usd"}
    )
    assert line == "dispatched decompose for queue/001-idea.md — failed: error_max_budget_usd"


def test_a_selection_with_no_item_falls_back_to_the_graph_name():
    line = cos.dispatch_line({"graph": "retro"}, {})
    assert line == "dispatched retro for retro"


DOCKET = {
    "registry": [
        {"name": "retro", "summary": "reads the ledger back", "runnable": True, "reason": ""},
        {"name": "decompose", "summary": "idea into a DAG", "runnable": False, "reason": "the intake queue is empty"},
    ],
    "intake": [],
    "ready_tasks": [],
    "ledger": {"rows": 3, "agreement": 1.0},
}


def cos_args(cart, docket=DOCKET, **overrides) -> dict:
    return {"run_id": "r", "date": "2026-08-31", "cartridge": cart, "docket": docket, **overrides}


# ------------------------------------------------------------------ the graph


def test_a_runnable_selection_is_accepted(cart) -> None:
    response = {"selections": [{"graph": "retro", "why": "the ledger has rows"}], "idle": False, "reasoning": "run retro"}
    result = coxswain.run(cos_args(cart), ScriptedRunner({"dispatch": response}))
    assert result["selections"] == response["selections"]
    assert result["idle"] is False
    assert result["proposals"] == [], "the cos graph proposes nothing itself"


def test_selecting_an_unrunnable_registry_entry_is_refused_by_the_graph(cart) -> None:
    response = {"selections": [{"graph": "decompose", "why": "there's an idea"}], "idle": False, "reasoning": "r"}
    with pytest.raises(ContractViolation, match="not runnable"):
        coxswain.run(cos_args(cart), ScriptedRunner({"dispatch": response}))


def test_selecting_a_graph_the_docket_never_named_is_refused(cart) -> None:
    response = {"selections": [{"graph": "ghost", "why": "x"}], "idle": False, "reasoning": "r"}
    with pytest.raises(ContractViolation, match="does not name"):
        coxswain.run(cos_args(cart), ScriptedRunner({"dispatch": response}))


def test_idle_true_with_selections_present_is_refused_as_incoherent(cart) -> None:
    response = {"selections": [{"graph": "retro", "why": "x"}], "idle": True, "reasoning": "r"}
    with pytest.raises(ContractViolation, match="incoherent"):
        coxswain.run(cos_args(cart), ScriptedRunner({"dispatch": response}))


def test_an_empty_docket_can_legitimately_come_back_idle(cart) -> None:
    empty_docket = {"registry": [], "intake": [], "ready_tasks": [], "ledger": {"rows": 0, "agreement": None}}
    response = {"selections": [], "idle": True, "reasoning": "nothing on the docket needs doing"}
    result = coxswain.run(cos_args(cart, docket=empty_docket), ScriptedRunner({"dispatch": response}))
    assert result == {
        "run_id": "r",
        "date": "2026-08-31",
        "selections": [],
        "idle": True,
        "reasoning": "nothing on the docket needs doing",
        "proposals": [],
    }


def test_the_prompt_shows_the_usage_verdict_and_its_one_line_reason(cart) -> None:
    docket = {**DOCKET, "free_slots": 0, "usage": {"verdict": "stop", "reason": "today's budget is spent"}}
    response = {"selections": [], "idle": True, "reasoning": "usage window says stop"}
    runner = ScriptedRunner({"dispatch": response})

    coxswain.run(cos_args(cart, docket=docket), runner)

    prompt = runner.calls[0]["prompt"]
    assert "verdict=stop" in prompt
    assert "today's budget is spent" in prompt


def test_the_prompt_renders_a_missing_reason_as_blank_not_the_word_none(cart) -> None:
    # DOCKET carries no `usage` key at all, so the fallback reads unmeasured
    # with no reason — the prompt must not print the literal word 'None'.
    response = {"selections": [], "idle": True, "reasoning": "r"}
    runner = ScriptedRunner({"dispatch": response})

    coxswain.run(cos_args(cart, docket=DOCKET), runner)

    prompt = runner.calls[0]["prompt"]
    assert "verdict=unmeasured" in prompt
    assert "reason=None" not in prompt


def test_dispatch_runs_at_standard_tier(cart) -> None:
    scripted = ScriptedRunner({"dispatch": {"selections": [], "idle": True, "reasoning": "r"}})
    coxswain.run(cos_args(cart), scripted)
    assert scripted.calls[0]["role"] == "dispatch"
    assert scripted.calls[0]["tier"] == "standard"


def test_requires_a_docket(cart) -> None:
    with pytest.raises(ContractViolation, match="args.docket is required"):
        coxswain.run({"run_id": "r", "date": "d", "cartridge": cart}, ScriptedRunner({}))


def test_a_team_without_the_dispatch_role_is_told_so(cartridge) -> None:
    with pytest.raises(ContractViolation, match="needs the optional role 'dispatch'"):
        coxswain.run(cos_args(cartridge), ScriptedRunner({}))


def test_refuses_without_a_cartridge() -> None:
    with pytest.raises(ContractViolation, match="cartridge"):
        coxswain.run({"run_id": "r", "date": "d", "docket": DOCKET}, ScriptedRunner({}))


# ------------------------------------------------------------ assemble_docket


def _specs(*names: str) -> dict:
    return {name: GraphSpec(name=name, graph_name=f"{name}-graph", run=lambda a, r: {}, summary=f"summary of {name}") for name in names}


def test_assemble_docket_marks_runnability_from_what_is_constructible(tmp_path) -> None:
    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    (intake_root / "001-idea.md").write_text("go arrow-native", encoding="utf-8")

    ledger_path = tmp_path / "ledger.jsonl"
    ledger_append([ledger_row("comment_add", "clean")], ledger_path)

    docket = cos.assemble_docket(
        specs=_specs("retro", "decompose", "triage", "lifecycle"),
        intake_root=intake_root,
        ledger_path=ledger_path,
        alerts_present=False,
    )

    by_name = {row["name"]: row for row in docket["registry"]}
    assert by_name["retro"]["runnable"] is True
    assert by_name["retro"]["reason"] == ""
    assert by_name["decompose"]["runnable"] is True
    assert by_name["triage"]["runnable"] is False
    assert by_name["triage"]["reason"], "an unrunnable entry must name what is missing"
    assert by_name["lifecycle"]["runnable"] is False
    assert by_name["lifecycle"]["reason"]

    assert docket["intake"] == [{"id": "001-idea", "kind": "idea", "title": "001-idea"}]
    assert docket["ledger"] == {"rows": 1, "agreement": 1.0}


def test_assemble_docket_tolerates_a_missing_intake_dir_and_ledger(tmp_path) -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro", "decompose", "triage"),
        intake_root=tmp_path / "no-such-dir",
        ledger_path=tmp_path / "no-such-ledger.jsonl",
        alerts_present=False,
    )
    by_name = {row["name"]: row for row in docket["registry"]}
    assert by_name["retro"]["runnable"] is False
    assert by_name["decompose"]["runnable"] is False
    assert docket["intake"] == []
    assert docket["ledger"] == {"rows": 0, "agreement": None}


def test_assemble_docket_marks_triage_runnable_when_alerts_are_in_hand(tmp_path) -> None:
    docket = cos.assemble_docket(
        specs=_specs("triage"), intake_root=None, ledger_path=None, alerts_present=True
    )
    assert docket["registry"] == [{"name": "triage", "summary": "summary of triage", "runnable": True, "reason": ""}]


# -------------------------------------------------------------------- run_cos


def _stub_spec(name: str, graph_name: str, *, fail: bool = False):
    calls: list[dict] = []

    def _run(args, runner):
        calls.append(dict(args))
        if fail:
            raise ContractViolation(f"{name} refused")
        return {"run_id": args["run_id"], "proposals": [{"kind": "comment_add", "target": name}]}

    return GraphSpec(name=name, graph_name=graph_name, run=_run, summary=name), calls


def test_run_cos_invokes_exactly_the_selected_graphs_and_constructs_their_args(tmp_path, cart) -> None:
    retro_spec, retro_calls = _stub_spec("retro", "retro-propose")
    triage_spec, triage_calls = _stub_spec("triage", "triage-propose")
    decompose_spec, decompose_calls = _stub_spec("decompose", "initiative-decompose")
    specs = {"retro": retro_spec, "triage": triage_spec, "decompose": decompose_spec}

    ledger_path = tmp_path / "ledger.jsonl"
    ledger_append([ledger_row("comment_add", "clean")], ledger_path)

    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    (intake_root / "001-idea.md").write_text("build the thing", encoding="utf-8")

    cos_result = {
        "selections": [
            {"graph": "retro", "why": "the ledger has rows"},
            {"graph": "decompose", "why": "the queue has an idea"},
        ],
        "idle": False,
        "reasoning": "r",
    }

    result = cos.run_cos(
        docket=DOCKET,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="2026-08-31",
        max_parallel=2,
        intake_root=intake_root,
        ledger_path=ledger_path,
        cos_result=cos_result,
    )

    assert triage_calls == [], "triage was never selected; it must never be invoked"
    assert len(retro_calls) == 1
    assert retro_calls[0]["ledger_rows"][0]["outcome"] == "clean"
    assert retro_calls[0]["run_id"] == "parent:retro-0"
    assert len(decompose_calls) == 1
    assert decompose_calls[0]["idea"] == "build the thing"
    assert decompose_calls[0]["run_id"] == "parent:decompose-1"

    assert {p["target"] for p in result["proposals"]} == {"retro", "decompose"}
    assert result["consumed"] == ["001-idea"]
    assert not (intake_root / "001-idea.md").exists()
    assert (intake_root / "consumed" / "001-idea.md").exists()


def test_consumes_only_after_a_successful_decompose_never_a_failed_one(tmp_path, cart) -> None:
    decompose_spec, decompose_calls = _stub_spec("decompose", "initiative-decompose", fail=True)
    specs = {"decompose": decompose_spec}

    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    (intake_root / "001-idea.md").write_text("body", encoding="utf-8")

    cos_result = {"selections": [{"graph": "decompose", "why": "x"}], "idle": False, "reasoning": "r"}

    result = cos.run_cos(
        docket=DOCKET,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        intake_root=intake_root,
        cos_result=cos_result,
    )

    assert len(decompose_calls) == 1, "the invocation still ran; it just did not succeed"
    assert result["consumed"] == []
    assert len(result["failures"]) == 1
    assert (intake_root / "001-idea.md").exists(), "a failed decompose must not consume the item it worked on"
    assert not (intake_root / "consumed").exists() or list((intake_root / "consumed").iterdir()) == []


def test_a_successful_invocation_is_recorded_with_its_item_and_graph(tmp_path, cart) -> None:
    decompose_spec, _ = _stub_spec("decompose", "initiative-decompose")
    specs = {"decompose": decompose_spec}

    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    (intake_root / "001-idea.md").write_text("body", encoding="utf-8")

    cos_result = {"selections": [{"graph": "decompose", "why": "x"}], "idle": False, "reasoning": "r"}

    result = cos.run_cos(
        docket=DOCKET,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        intake_root=intake_root,
        cos_result=cos_result,
    )

    item = str(intake_root / "001-idea.md")
    assert result["invoked"] == [
        {"graph": "decompose", "why": "x", "item": item, "line": f"dispatched decompose for {item}"}
    ]


def test_a_failed_invocation_is_recorded_with_its_item_and_reason(tmp_path, cart) -> None:
    decompose_spec, _ = _stub_spec("decompose", "initiative-decompose", fail=True)
    specs = {"decompose": decompose_spec}

    intake_root = tmp_path / "intake"
    intake_root.mkdir()
    (intake_root / "001-idea.md").write_text("body", encoding="utf-8")

    cos_result = {"selections": [{"graph": "decompose", "why": "x"}], "idle": False, "reasoning": "r"}

    result = cos.run_cos(
        docket=DOCKET,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        intake_root=intake_root,
        cos_result=cos_result,
    )

    item = str(intake_root / "001-idea.md")
    assert result["invoked"] == [
        {
            "graph": "decompose",
            "why": "x",
            "item": item,
            "reason": "decompose refused",
            "line": f"dispatched decompose for {item} — failed: decompose refused",
        }
    ]


def test_dispatch_line_accepts_the_two_shapes_run_cos_now_calls_it_with() -> None:
    entry = {"graph": "decompose", "item": "queue/001-idea.md"}
    assert cos.dispatch_line(entry, {"reason": None}) == "dispatched decompose for queue/001-idea.md"
    assert (
        cos.dispatch_line(entry, {"reason": "decompose refused"})
        == "dispatched decompose for queue/001-idea.md — failed: decompose refused"
    )


def test_an_idle_decision_invokes_nothing(cart) -> None:
    result = cos.run_cos(
        docket=DOCKET,
        specs={},
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        cos_result={"selections": [], "idle": True, "reasoning": "nothing to do"},
    )
    assert result == {
        "selections": [],
        "invoked": [],
        "results": [],
        "proposals": [],
        "failures": [],
        "consumed": [],
        "deferred": [],
    }


def test_run_cos_runs_the_cos_graph_itself_when_no_result_is_given(cart) -> None:
    empty_docket = {"registry": [], "intake": [], "ready_tasks": [], "ledger": {"rows": 0, "agreement": None}}
    runner = ScriptedRunner({"dispatch": {"selections": [], "idle": True, "reasoning": "quiet day"}})
    specs = {"cos": coxswain.SPEC}

    result = cos.run_cos(
        docket=empty_docket, specs=specs, runner=runner, cartridge=cart, run_id="parent", date="d", max_parallel=1
    )

    assert result == {
        "selections": [],
        "invoked": [],
        "results": [],
        "proposals": [],
        "failures": [],
        "consumed": [],
        "deferred": [],
    }
    assert runner.calls[0]["role"] == "dispatch"


def test_run_cos_raises_a_cos_error_for_a_selection_it_has_no_recipe_for(cart) -> None:
    weird_spec, _ = _stub_spec("lifecycle", "lifecycle-propose")
    specs = {"lifecycle": weird_spec}
    cos_result = {"selections": [{"graph": "lifecycle", "why": "x"}], "idle": False, "reasoning": "r"}
    with pytest.raises(cos.CosError, match="no argument recipe"):
        cos.run_cos(
            docket=DOCKET,
            specs=specs,
            runner=None,
            cartridge=cart,
            run_id="p",
            date="d",
            max_parallel=1,
            cos_result=cos_result,
        )


def test_proposals_aggregate_under_the_parent_run_id_in_invocation_order(tmp_path, cart) -> None:
    retro_spec, _ = _stub_spec("retro", "retro-propose")
    triage_spec, _ = _stub_spec("triage", "triage-propose")
    specs = {"retro": retro_spec, "triage": triage_spec}

    cos_result = {
        "selections": [
            {"graph": "triage", "why": "x"},
            {"graph": "retro", "why": "y"},
        ],
        "idle": False,
        "reasoning": "r",
    }
    result = cos.run_cos(
        docket=DOCKET,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=2,
        cos_result=cos_result,
    )
    # Invocation ids are "retro-1" and "triage-0"; results/proposals come back
    # sorted by invocation id, never by selection or finish order.
    assert [r["run_id"] for r in result["results"]] == ["parent:retro-1", "parent:triage-0"]


# --------------------------------------------------------- in-flight / free_slots


def _pidfile(runs_dir, run_id: str, pid: int) -> None:
    runs_dir.mkdir(exist_ok=True)
    (runs_dir / f"{run_id}.pid").write_text(str(pid), encoding="utf-8")


def test_pid_alive_reads_liveness_off_a_faked_kill_never_the_real_process_table(tmp_path, monkeypatch) -> None:
    # `os.kill(pid, 0)` against a real pid depends on what the host happens to
    # have running (and a recycled or high pid_max can make an "obviously
    # dead" number answer alive) — faking it makes every branch deterministic.
    def fake_kill(pid: int, _sig: int) -> None:
        if pid == 222:
            raise PermissionError
        if pid != 111:
            raise ProcessLookupError

    monkeypatch.setattr(cos.os, "kill", fake_kill)

    live = tmp_path / "live.pid"
    live.write_text("111", encoding="utf-8")
    perm = tmp_path / "perm.pid"
    perm.write_text("222", encoding="utf-8")
    dead = tmp_path / "dead.pid"
    dead.write_text("333", encoding="utf-8")
    bad = tmp_path / "bad.pid"
    bad.write_text("not-a-pid", encoding="utf-8")

    assert cos._pid_alive(live) is True
    assert cos._pid_alive(perm) is True, "PermissionError still means the process exists"
    assert cos._pid_alive(dead) is False
    assert cos._pid_alive(bad) is False, "unparsable content reads as dead"


def test_free_slots_is_a_pure_function_over_plain_rows() -> None:
    rows = [{"run_id": "a", "alive": True}, {"run_id": "b", "alive": False}, {"run_id": "c", "alive": True}]
    assert cos._free_slots(3, rows) == 1
    assert cos._free_slots(2, rows) == 0, "never negative"
    assert cos._free_slots(5, []) == 5


def test_max_in_flight_defaults_to_three_off_plain_cartridge_data() -> None:
    assert cos._max_in_flight({"policy": {"dispatch": {"max_in_flight": 7}}}) == 7
    assert cos._max_in_flight({}) == 3
    assert cos._max_in_flight(None) == 3


def test_assemble_docket_reports_in_flight_runs_and_the_free_slots_left(tmp_path, monkeypatch) -> None:
    alive_pids = {111, 222}

    def fake_kill(pid: int, _sig: int) -> None:
        if pid not in alive_pids:
            raise ProcessLookupError

    monkeypatch.setattr(cos.os, "kill", fake_kill)

    runs_dir = tmp_path / "runs"
    _pidfile(runs_dir, "run-a", 111)
    _pidfile(runs_dir, "run-b", 222)
    _pidfile(runs_dir, "run-c", 333)  # not a live pid

    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
        runs_dir=runs_dir,
    )

    assert docket["in_flight"] == [
        {"run_id": "run-a", "alive": True},
        {"run_id": "run-b", "alive": True},
        {"run_id": "run-c", "alive": False},
    ]
    assert docket["max_in_flight"] == 3
    assert docket["free_slots"] == 1


def test_assemble_docket_without_a_runs_dir_leaves_every_slot_free() -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
    )
    assert docket["in_flight"] == []
    assert docket["free_slots"] == docket["max_in_flight"] == 3


def test_assemble_docket_defaults_max_in_flight_to_three_without_a_dispatch_policy() -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro"), intake_root=None, ledger_path=None, alerts_present=False, cartridge={}
    )
    assert docket["max_in_flight"] == 3
    assert docket["free_slots"] == 3


def test_assemble_docket_ignores_everything_in_runs_dir_that_is_not_a_live_pidfile(tmp_path, monkeypatch) -> None:
    # The default `--runs-dir` points at the real runs directory, which also
    # holds logs, usage files, and pidfiles left behind by runs that already
    # ended. None of that should ever count as in flight — only a `*.pid`
    # file naming a pid that is still alive does.
    monkeypatch.setattr(cos.os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(ProcessLookupError))

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "run-a.pid").write_text("111", encoding="utf-8")  # a real but dead pid
    (runs_dir / "run-b.pid").write_text("not-a-pid", encoding="utf-8")  # garbage content
    (runs_dir / "run-a.log").write_text("hello", encoding="utf-8")
    (runs_dir / "run-a.usage.json").write_text("{}", encoding="utf-8")

    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
        runs_dir=runs_dir,
    )

    assert docket["in_flight"] == [
        {"run_id": "run-a", "alive": False},
        {"run_id": "run-b", "alive": False},
    ]
    assert docket["free_slots"] == docket["max_in_flight"] == 3


def test_assemble_docket_without_usage_says_unmeasured_and_behaves_as_today() -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
    )
    assert docket["usage"] == {"verdict": "unmeasured"}
    assert docket["free_slots"] == docket["max_in_flight"] == 3


def test_assemble_docket_with_a_go_verdict_leaves_the_slots_alone() -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
        usage={
            "verdict": "go",
            "headroom_usd": 12.5,
            "tier_ceiling": "deep",
            "effort_ceiling": "high",
            "reason": "plenty of headroom left",
        },
    )
    assert docket["usage"] == {
        "verdict": "go",
        "headroom_usd": 12.5,
        "tier_ceiling": "deep",
        "effort_ceiling": "high",
        "reason": "plenty of headroom left",
    }
    assert docket["free_slots"] == docket["max_in_flight"] == 3


def test_assemble_docket_with_a_stop_verdict_zeroes_the_slots_and_carries_the_reason() -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
        usage={
            "verdict": "stop",
            "headroom_usd": 0.0,
            "tier_ceiling": "cheap",
            "effort_ceiling": "low",
            "reason": "today's budget is spent",
        },
    )
    assert docket["free_slots"] == 0
    assert docket["usage"] == {
        "verdict": "stop",
        "headroom_usd": 0.0,
        "tier_ceiling": "cheap",
        "effort_ceiling": "low",
        "reason": "today's budget is spent",
    }


def test_assemble_docket_folds_the_verdicts_case_before_matching_stop() -> None:
    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
        usage={"verdict": "Stop", "reason": "today's budget is spent"},
    )
    assert docket["usage"]["verdict"] == "stop"
    assert docket["free_slots"] == 0


def test_assemble_docket_treats_a_verdictless_usage_payload_as_unmeasured() -> None:
    # A payload from the cross-repository tool that is missing its own verdict
    # (or misspells it) must not silently read as measured with a wall of
    # `None`s standing in for a real assessment.
    docket = cos.assemble_docket(
        specs=_specs("retro"),
        intake_root=None,
        ledger_path=None,
        alerts_present=False,
        cartridge={"policy": {"dispatch": {"max_in_flight": 3}}},
        usage={"headroom_usd": 5.0},
    )
    assert docket["usage"] == {"verdict": "unmeasured"}
    assert docket["free_slots"] == docket["max_in_flight"] == 3


def test_run_cos_invokes_only_the_free_slots_and_defers_the_rest(cart) -> None:
    retro_spec, retro_calls = _stub_spec("retro", "retro-propose")
    triage_spec, triage_calls = _stub_spec("triage", "triage-propose")
    specs = {"retro": retro_spec, "triage": triage_spec}

    docket = {**DOCKET, "in_flight": [{"run_id": "run-a", "alive": True}], "max_in_flight": 2, "free_slots": 1}
    cos_result = {
        "selections": [
            {"graph": "retro", "why": "a"},
            {"graph": "triage", "why": "b"},
            {"graph": "retro", "why": "c"},
        ],
        "idle": False,
        "reasoning": "r",
    }

    result = cos.run_cos(
        docket=docket,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=2,
        cos_result=cos_result,
    )

    assert len(retro_calls) == 1
    assert triage_calls == []
    assert result["deferred"] == [
        {"graph": "triage", "reason": "at capacity: 1 in flight of 2"},
        {"graph": "retro", "reason": "at capacity: 1 in flight of 2"},
    ]


def test_run_cos_with_zero_free_slots_invokes_nothing(cart) -> None:
    retro_spec, retro_calls = _stub_spec("retro", "retro-propose")
    specs = {"retro": retro_spec}

    docket = {**DOCKET, "in_flight": [{"run_id": "run-a", "alive": True}], "max_in_flight": 1, "free_slots": 0}
    cos_result = {"selections": [{"graph": "retro", "why": "a"}], "idle": False, "reasoning": "r"}

    result = cos.run_cos(
        docket=docket,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        cos_result=cos_result,
    )

    assert retro_calls == []
    assert result["results"] == []
    assert result["deferred"] == [{"graph": "retro", "reason": "at capacity: 1 in flight of 1"}]


def test_run_cos_with_a_stop_usage_verdict_defers_with_the_usage_reason_not_a_manufactured_capacity(cart) -> None:
    retro_spec, retro_calls = _stub_spec("retro", "retro-propose")
    specs = {"retro": retro_spec}

    docket = {
        **DOCKET,
        "in_flight": [],
        "max_in_flight": 3,
        "free_slots": 0,
        "usage": {"verdict": "stop", "reason": "today's budget is spent"},
    }
    cos_result = {"selections": [{"graph": "retro", "why": "a"}], "idle": False, "reasoning": "r"}

    result = cos.run_cos(
        docket=docket,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        cos_result=cos_result,
    )

    assert retro_calls == []
    assert result["deferred"] == [
        {"graph": "retro", "reason": "usage window stopped dispatch: today's budget is spent"}
    ]


def test_run_cos_enforces_a_stop_verdict_even_on_a_docket_with_no_free_slots_key(cart) -> None:
    # DOCKET (module-level fixture) carries no in_flight/max_in_flight/free_slots
    # at all, the same shape `test_a_docket_missing_free_slots_...` below covers
    # for the ordinary capacity fallback. A `usage` verdict of `stop` must win
    # over that fallback too, not just over an explicit `free_slots` the docket
    # happened to carry.
    retro_spec, retro_calls = _stub_spec("retro", "retro-propose")
    specs = {"retro": retro_spec}

    docket = {**DOCKET, "usage": {"verdict": "stop", "reason": "today's budget is spent"}}
    cos_result = {"selections": [{"graph": "retro", "why": "a"}], "idle": False, "reasoning": "r"}

    result = cos.run_cos(
        docket=docket,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=1,
        cos_result=cos_result,
    )

    assert retro_calls == []
    assert result["deferred"] == [
        {"graph": "retro", "reason": "usage window stopped dispatch: today's budget is spent"}
    ]


def test_a_docket_missing_free_slots_falls_back_to_the_cartridges_bound_not_uncapped(cart) -> None:
    # DOCKET (module-level fixture) predates in-flight bookkeeping and carries
    # none of in_flight/max_in_flight/free_slots. A missing key must still
    # cap at the cartridge's own bound, not dispatch every selection through.
    cart["policy"] = {"dispatch": {"max_in_flight": 1}}
    retro_spec, retro_calls = _stub_spec("retro", "retro-propose")
    triage_spec, triage_calls = _stub_spec("triage", "triage-propose")
    specs = {"retro": retro_spec, "triage": triage_spec}
    cos_result = {
        "selections": [{"graph": "retro", "why": "a"}, {"graph": "triage", "why": "b"}],
        "idle": False,
        "reasoning": "r",
    }

    result = cos.run_cos(
        docket=DOCKET,
        specs=specs,
        runner=None,
        cartridge=cart,
        run_id="parent",
        date="d",
        max_parallel=2,
        cos_result=cos_result,
    )

    assert len(retro_calls) == 1
    assert triage_calls == [], "the bound is 1 and nothing is reported in flight, so only one may run"
    assert result["deferred"] == [{"graph": "triage", "reason": "at capacity: 0 in flight of 1"}]


def test_the_cos_cli_arm_resolves_runs_dir_off_the_existing_global_flag() -> None:
    # `--runs-dir` is not new: it already exists as a global flag (used
    # elsewhere for run manifests). The cos arm reuses that same flag as
    # assemble_docket's pidfile directory rather than declaring a second,
    # colliding `--runs-dir` of its own — argparse forbids two options
    # sharing one string. Parsing "cos" here proves `args.runs_dir` resolves
    # for the cos arm with no AttributeError, not just that the name is
    # present somewhere in the source.
    parser = cli._build_parser({"cos": coxswain.SPEC})
    args = parser.parse_args(["cos", "--team", "acme"])
    assert args.runs_dir is not None


def test_the_cos_arm_wires_runs_dir_and_the_cartridge_into_the_docket_call() -> None:
    # The check above only proves the parser resolves `--runs-dir`; it holds
    # whether or not the cos arm ever passes that value on. This exercises
    # the actual kwargs the arm hands `assemble_docket`, so it goes red if
    # the wire from `--runs-dir` (or the cartridge) to the docket ever breaks.
    parser = cli._build_parser({"cos": coxswain.SPEC})
    args = parser.parse_args(["cos", "--team", "acme", "--runs-dir", "/tmp/some-runs"])
    cart = {"intake": [{"source": "queue_dir", "path": "/tmp/intake"}]}

    kwargs = cli._cos_docket_args(cartridge=cart, args=args)

    assert kwargs["runs_dir"] == "/tmp/some-runs"
    assert kwargs["cartridge"] is cart
    assert kwargs["intake_root"] == "/tmp/intake"
    assert kwargs["ledger_path"] == args.ledger
