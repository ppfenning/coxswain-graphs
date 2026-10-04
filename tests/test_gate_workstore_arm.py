"""The built-in `workstore` apply arm: applied in code, model arm only as the fallback."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from harness.gate import auto_apply, workstore_route

TASK = """---
id: t1-probe
phase: p1-foundations
state: ready
needs: []
surfaces: [schema]
title: Probe the schema
---

Read the schema and list the fields in use.
"""

ITEM = {
    "id": "t2-new",
    "phase": "p1-foundations",
    "state": "ready",
    "needs": [],
    "surfaces": ["schema"],
    "title": "A brand new task",
    "body": "Write the thing.",
}


class NoRunner:
    def run(self, **kwargs):
        pytest.fail(f"the model arm must not run: {kwargs.get('role')}")


class RecordingRunner:
    capabilities: ClassVar[dict[str, bool]] = {"tool_use": True}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {"applied": True, "detail": "by the model"}


def cartridge(kind: str) -> dict:
    return {"context": [], "write_kinds": {kind: {"risk": "low", "ramp": "gated", "apply_arm": "workstore"}}}


def proposal(kind: str, **apply) -> dict:
    return {
        "kind": kind,
        "risk": "low",
        "target": "t1-probe",
        "evidence": [{"check": "c", "output": "o"}],
        "rationale": "because",
        "suggested_action": "do it",
        **({"apply": apply} if apply else {}),
    }


def test_state_move_sets_the_state_in_code(tmp_path: Path) -> None:
    path = tmp_path / "t1-probe.md"
    path.write_text(TASK, encoding="utf-8")
    got = auto_apply(
        proposal("state_move", path=str(path), state="done"),
        cartridge=cartridge("state_move"),
        runner=NoRunner(),
    )
    assert got == (True, f"{path} moved to state 'done'")
    assert "state: done" in path.read_text(encoding="utf-8")


def test_item_create_writes_a_new_item(tmp_path: Path) -> None:
    path = tmp_path / "t2-new.md"
    got = auto_apply(
        proposal("item_create", path=str(path), item=ITEM),
        cartridge=cartridge("item_create"),
        runner=NoRunner(),
    )
    assert got == (True, f"{path} created")
    text = path.read_text(encoding="utf-8")
    assert "title: A brand new task" in text
    assert "Write the thing." in text


def test_item_update_rewrites_an_existing_item(tmp_path: Path) -> None:
    path = tmp_path / "t1-probe.md"
    path.write_text(TASK, encoding="utf-8")
    got = auto_apply(
        proposal("item_update", path=str(path), item={**ITEM, "id": "t1-probe", "title": "Retitled"}),
        cartridge=cartridge("item_update"),
        runner=NoRunner(),
    )
    assert got == (True, f"{path} updated")
    assert "title: Retitled" in path.read_text(encoding="utf-8")


def test_item_create_onto_an_existing_path_is_refused_and_leaves_it_alone(tmp_path: Path) -> None:
    path = tmp_path / "t1-probe.md"
    path.write_text(TASK, encoding="utf-8")
    got = auto_apply(
        proposal("item_create", path=str(path), item=ITEM),
        cartridge=cartridge("item_create"),
        runner=NoRunner(),
    )
    assert got == (False, f"refused: {path} already exists")
    assert path.read_text(encoding="utf-8") == TASK


def test_a_missing_apply_falls_back_to_the_work_state_arm_once() -> None:
    runner = RecordingRunner()
    got = auto_apply(proposal("state_move"), cartridge=cartridge("state_move"), runner=runner)
    assert got == (True, "by the model")
    assert [call["role"] for call in runner.calls] == ["work_state_arm"]


@pytest.mark.parametrize(
    ("kind", "apply", "route"),
    [
        ("state_move", {"path": "p", "state": "done"}, "state_move"),
        ("state_move", {"path": "p"}, "fallback"),
        ("state_move", {"path": 3, "state": "done"}, "fallback"),
        ("item_create", {"path": "p", "item": {"id": "x"}}, "item_create"),
        ("item_update", {"path": "p", "item": "not a dict"}, "fallback"),
        ("item_update", None, "fallback"),
        ("comment_add", {"path": "p", "state": "done"}, "fallback"),
    ],
)
def test_the_route_is_chosen_from_kind_and_apply_alone(kind: str, apply: object, route: str) -> None:
    assert workstore_route(kind, apply)[0] == route


def test_model_arm_refuses_when_no_tier_has_tool_use() -> None:
    from runner.claude_code_runner import ClaudeCodeRunner

    class ToolUseRunner(ClaudeCodeRunner):
        run = RecordingRunner.run

    class AnthropicShaped(RecordingRunner):
        capabilities = None  # AnthropicRunner has no such attribute; its profile dict declares none either

    message = "work_state_arm needs tool_use; 'deep' lacks it too"
    refused = RecordingRunner()
    refused.capabilities = {"structured_output": True}
    assert auto_apply(proposal("state_move"), cartridge=cartridge("state_move"), runner=refused) == (False, message)
    assert refused.calls == []
    undeclared = AnthropicShaped()
    undeclared.profile = {"tiers": {"standard": "sonnet"}}
    assert auto_apply(proposal("state_move"), cartridge=cartridge("state_move"), runner=undeclared) == (False, message)
    assert undeclared.calls == []
    # A real ClaudeCodeRunner: no `capabilities` in its profile dict, tool use on its attribute.
    tooled = ToolUseRunner({"tiers": {"standard": "sonnet"}})
    tooled.calls = []
    assert auto_apply(proposal("state_move"), cartridge=cartridge("state_move"), runner=tooled) == (True, "by the model")
    assert tooled.calls[0]["tier"] == "standard"
