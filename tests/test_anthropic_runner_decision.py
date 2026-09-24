from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from runner.anthropic_runner import AnthropicRunner

PROFILE = {"tiers": {"cheap": "m-cheap", "standard": "m-std"}}


class _Stub:
    def __init__(self, **response: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
            **response,
        )
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


def _run(stub: _Stub, **kwargs: Any) -> Any:
    runner = AnthropicRunner(PROFILE, client=stub)
    return runner.run(role="r", schema={}, prompt="p", **kwargs)


def test_the_decision_carries_the_response_model_id_and_the_tier():
    decision = _run(_Stub(model="claude-x-1"), tier="cheap").decision
    assert (decision.model_id, decision.requested_tier, decision.chosen_tier) == ("claude-x-1", "cheap", "cheap")


def test_the_reason_is_caller_and_there_is_no_claude_code_version():
    decision = _run(_Stub(model="claude-x-1")).decision
    assert (decision.reason, decision.claude_code_version) == ("caller", None)


def test_a_response_without_a_model_falls_back_to_the_profile_model():
    assert _run(_Stub()).decision.model_id == "m-std"


def test_selection_is_unchanged_and_the_decision_is_not_a_key():
    stub = _Stub(model="claude-x-1")
    result = _run(stub, tier="cheap")
    assert stub.calls[0]["model"] == "m-cheap"
    assert result == {"ok": True}
