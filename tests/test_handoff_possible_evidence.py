"""A missing item that asks for a command the build cannot run is discharged, not looped on."""

from __future__ import annotations

from typing import Any

from graphs.delivery.lifecycle_propose import _handoff, impossible_evidence
from runner import ScriptedRunner

_PYTEST = ["pytest", "python -m pytest"]


def test_a_command_outside_the_permitted_prefixes_is_impossible() -> None:
    assert impossible_evidence("attach `uv run pytest -q` output", _PYTEST) is True


def test_a_command_starting_with_a_permitted_prefix_is_possible() -> None:
    assert impossible_evidence("attach `pytest -q` output", ["pytest"]) is False


def test_a_multi_word_permitted_prefix_matches() -> None:
    assert impossible_evidence("attach `python -m pytest -q`", ["python -m pytest"]) is False


def test_a_span_that_is_not_a_command_word_is_possible() -> None:
    assert impossible_evidence("the `foo()` helper lacks a test", ["pytest"]) is False


_BUILD: dict[str, Any] = {
    "patch": "--- a/x\n+++ b/x\n+line\n",
    "files_touched": ["x"],
    "commands_run": [],
}
_ANSWER = {"complete": False, "blocking": False, "missing": ["attach `uv run pytest -q` output"], "brief": "b"}


class _Permitting(ScriptedRunner):
    def permitted_prefixes(self) -> list[str]:
        return ["pytest"]


def test_handoff_discharges_an_impossible_item_and_comes_back_complete() -> None:
    result = _handoff(_Permitting({"handoff": _ANSWER}), context=[], ticket="T", plan={}, build=_BUILD, facts={})
    assert result["complete"] is True
    assert result["missing"] == []
    assert result["discharged"] == ["attach `uv run pytest -q` output (not runnable in the build's session)"]


def test_handoff_without_permitted_prefixes_keeps_the_item() -> None:
    result = _handoff(ScriptedRunner({"handoff": _ANSWER}), context=[], ticket="T", plan={}, build=_BUILD, facts={})
    assert result["complete"] is False
    assert result["missing"] == ["attach `uv run pytest -q` output"]
    assert result["discharged"] == []
