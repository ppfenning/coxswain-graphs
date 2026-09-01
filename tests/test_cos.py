"""chief-of-staff: the graph decides, `harness/cos.py` builds the docket and acts.

Two things are under test here, deliberately in one file: the `chief_of_staff`
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
from graphs.ops import chief_of_staff
from harness import cos
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
    result = chief_of_staff.run(cos_args(cart), ScriptedRunner({"dispatch": response}))
    assert result["selections"] == response["selections"]
    assert result["idle"] is False
    assert result["proposals"] == [], "the cos graph proposes nothing itself"


def test_selecting_an_unrunnable_registry_entry_is_refused_by_the_graph(cart) -> None:
    response = {"selections": [{"graph": "decompose", "why": "there's an idea"}], "idle": False, "reasoning": "r"}
    with pytest.raises(ContractViolation, match="not runnable"):
        chief_of_staff.run(cos_args(cart), ScriptedRunner({"dispatch": response}))


def test_selecting_a_graph_the_docket_never_named_is_refused(cart) -> None:
    response = {"selections": [{"graph": "ghost", "why": "x"}], "idle": False, "reasoning": "r"}
    with pytest.raises(ContractViolation, match="does not name"):
        chief_of_staff.run(cos_args(cart), ScriptedRunner({"dispatch": response}))


def test_idle_true_with_selections_present_is_refused_as_incoherent(cart) -> None:
    response = {"selections": [{"graph": "retro", "why": "x"}], "idle": True, "reasoning": "r"}
    with pytest.raises(ContractViolation, match="incoherent"):
        chief_of_staff.run(cos_args(cart), ScriptedRunner({"dispatch": response}))


def test_an_empty_docket_can_legitimately_come_back_idle(cart) -> None:
    empty_docket = {"registry": [], "intake": [], "ready_tasks": [], "ledger": {"rows": 0, "agreement": None}}
    response = {"selections": [], "idle": True, "reasoning": "nothing on the docket needs doing"}
    result = chief_of_staff.run(cos_args(cart, docket=empty_docket), ScriptedRunner({"dispatch": response}))
    assert result == {
        "run_id": "r",
        "date": "2026-08-31",
        "selections": [],
        "idle": True,
        "reasoning": "nothing on the docket needs doing",
        "proposals": [],
    }


def test_dispatch_runs_at_standard_tier(cart) -> None:
    scripted = ScriptedRunner({"dispatch": {"selections": [], "idle": True, "reasoning": "r"}})
    chief_of_staff.run(cos_args(cart), scripted)
    assert scripted.calls[0]["role"] == "dispatch"
    assert scripted.calls[0]["tier"] == "standard"


def test_requires_a_docket(cart) -> None:
    with pytest.raises(ContractViolation, match="args.docket is required"):
        chief_of_staff.run({"run_id": "r", "date": "d", "cartridge": cart}, ScriptedRunner({}))


def test_a_team_without_the_dispatch_role_is_told_so(cartridge) -> None:
    with pytest.raises(ContractViolation, match="needs the optional role 'dispatch'"):
        chief_of_staff.run(cos_args(cartridge), ScriptedRunner({}))


def test_refuses_without_a_cartridge() -> None:
    with pytest.raises(ContractViolation, match="cartridge"):
        chief_of_staff.run({"run_id": "r", "date": "d", "docket": DOCKET}, ScriptedRunner({}))


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
    assert result == {"selections": [], "results": [], "proposals": [], "failures": [], "consumed": []}


def test_run_cos_runs_the_cos_graph_itself_when_no_result_is_given(cart) -> None:
    empty_docket = {"registry": [], "intake": [], "ready_tasks": [], "ledger": {"rows": 0, "agreement": None}}
    runner = ScriptedRunner({"dispatch": {"selections": [], "idle": True, "reasoning": "quiet day"}})
    specs = {"cos": chief_of_staff.SPEC}

    result = cos.run_cos(
        docket=empty_docket, specs=specs, runner=runner, cartridge=cart, run_id="parent", date="d", max_parallel=1
    )

    assert result == {"selections": [], "results": [], "proposals": [], "failures": [], "consumed": []}
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
