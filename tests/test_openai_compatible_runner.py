"""OpenAICompatibleRunner serves `local/` tiers against a scripted server: text, schema answers, one retry, free cost."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harness.store_migrate import open_store
from harness.store_write import Store
from runner.anthropic_runner import AnthropicRunner
from runner.openai_compatible_runner import TEXT_SCHEMA, OpenAICompatibleRunner
from runner.protocol import NodeResult, RunnerError
from tests.fake_openai_server import FakeOpenAIServer

ENV_VAR = "LOCAL_LLM_URL"
PROFILE = {
    "endpoint_env": ENV_VAR,
    "capabilities": {"structured_output": False},
    "tiers": {"cheap": "local/qwen-small", "standard": "local/qwen-big", "deep": "remote/opus"},
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
RUN = "run-1:p3-write"
HYBRID = {
    **PROFILE,
    "tiers": {"cheap": "local/qwen-small", "standard": "anthropic/claude-sonnet-x", "deep": "remote/opus"},
}


class _FakeDelegate:
    def __init__(self, fail: bool = False) -> None:
        self.runs: list[dict[str, Any]] = []
        self.closed = 0
        self.fail = fail

    def run(self, **kwargs: Any) -> NodeResult:
        self.runs.append(kwargs)
        if self.fail:
            raise RunnerError("refused")
        return NodeResult({"ok": True})

    def close(self) -> None:
        self.closed += 1


class _Factory:
    def __init__(self, fail: bool = False) -> None:
        self.built: list[tuple[Any, dict[str, Any]]] = []
        self.delegate = _FakeDelegate(fail)

    def __call__(self, profile: Any, **kwargs: Any) -> _FakeDelegate:
        self.built.append((profile, kwargs))
        return self.delegate


class _Client:
    """Stands in for the anthropic SDK client that the real AnthropicRunner calls."""

    def __init__(self, text: str = '{"ok": true}') -> None:
        self.sent: list[dict[str, Any]] = []
        self._response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return self._response


def _hybrid(server: FakeOpenAIServer, factory: Any, **kwargs: Any) -> OpenAICompatibleRunner:
    return OpenAICompatibleRunner(
        HYBRID, role_skills={"other": "skill.md"}, env={ENV_VAR: server.base_url}, delegate_factory=factory, **kwargs
    )


def _real(client: _Client) -> Any:
    return lambda profile, **kwargs: AnthropicRunner(profile, client=client, **kwargs)


def _runner(server: FakeOpenAIServer, **kwargs) -> OpenAICompatibleRunner:
    return OpenAICompatibleRunner(PROFILE, role_skills={}, env={ENV_VAR: server.base_url}, **kwargs)


def test_a_plain_text_call_returns_the_text_at_zero_cost_with_the_reported_tokens() -> None:
    with FakeOpenAIServer(["hello"]) as server:
        runner = _runner(server)
        out = runner.run(role="r", tier="cheap", schema=None, prompt="hi", budget_usd=0.0001)
    assert out["text"] == "hello"
    assert out.decision.budget_usd == 0.0001
    (call,) = runner.calls
    assert (call["cost_usd"], call["input_tokens"], call["output_tokens"]) == (0.0, 3, 2)
    assert server.requests[0]["path"] == "/v1/chat/completions"
    assert server.requests[0]["body"]["model"] == "qwen-small"


def test_a_valid_schema_answer_on_the_first_reply_makes_one_request() -> None:
    with FakeOpenAIServer(['{"ok": true}']) as server:
        out = _runner(server).run(role="r", tier="cheap", schema=SCHEMA, prompt="go")
    assert dict(out) == {"ok": True}
    assert len(server.requests) == 1
    assert "JSON schema" in server.requests[0]["body"]["messages"][-1]["content"]


def test_an_invalid_first_reply_then_a_valid_second_makes_two_requests_and_the_second_carries_the_errors() -> None:
    with FakeOpenAIServer(['{"ok": "yes"}', '{"ok": true}']) as server:
        runner = _runner(server)
        out = runner.run(role="r", tier="cheap", schema=SCHEMA, prompt="go")
    assert dict(out) == {"ok": True}
    assert len(server.requests) == 2 == len(runner.calls)
    assert "$.ok: expected boolean, got string" in server.requests[1]["body"]["messages"][-1]["content"]


def test_two_invalid_replies_raise_after_exactly_two_requests() -> None:
    with (
        FakeOpenAIServer(["prose", "more prose", '{"ok": true}']) as server,
        pytest.raises(RunnerError, match="no JSON object found"),
    ):
        _runner(server).run(role="r", tier="cheap", schema=SCHEMA, prompt="go")
    assert len(server.requests) == 2


def test_a_missing_or_empty_env_var_raises_at_construction_and_names_it() -> None:
    for env in ({}, {ENV_VAR: ""}):
        with pytest.raises(RunnerError, match=ENV_VAR):
            OpenAICompatibleRunner(PROFILE, role_skills={}, env=env)


def test_a_tier_that_resolves_to_a_non_local_model_raises_naming_the_tier() -> None:
    with FakeOpenAIServer([]) as server, pytest.raises(RunnerError, match="deep"):
        _runner(server).run(role="r", tier="deep", schema=None, prompt="go")
    assert server.requests == []


def test_capabilities_come_from_the_profile_and_close_is_a_no_op() -> None:
    with FakeOpenAIServer([]) as server:
        runner = _runner(server)
    assert runner.capabilities == {"structured_output": False}
    assert runner.close() is None


def test_the_store_write_for_a_local_call_has_the_shape_the_anthropic_runner_writes() -> None:
    conn = open_store("sqlite:///:memory:", "2026-09-24T00:00:00Z")
    with FakeOpenAIServer(["bad", '{"ok": true}']) as server:
        out = _runner(server, store=Store(conn), run_id=RUN).run(role="r", tier="cheap", schema=SCHEMA, prompt="p")
    rows = conn.query_all(
        "SELECT run_id, phase_id, role, ok, model_id, chosen_tier, cost_usd, input_tokens, output_tokens FROM node_calls ORDER BY seq"
    )
    conn.close()
    assert rows[0][:4] == ("run-1", "p3-write", "r", 0)
    assert rows[1][:4] == ("run-1", "p3-write", "r", 1)
    assert (rows[1][4], rows[1][5]) == (out.decision.model_id, out.decision.chosen_tier)
    assert rows[1][6:] == (0.0, 3, 2)


def test_a_standard_tier_goes_to_the_delegate_and_never_touches_the_server() -> None:
    factory = _Factory()
    with FakeOpenAIServer([]) as server:
        out = _hybrid(server, factory).run(role="r", tier="standard", schema=SCHEMA, prompt="go")
    assert dict(out) == {"ok": True}
    (sent,) = factory.delegate.runs
    assert (sent["role"], sent["schema"], sent["prompt"]) == ("r", SCHEMA, "go")
    assert (sent["tier"], sent["model"]) == ("standard", "claude-sonnet-x")
    assert factory.built[0][1]["role_skills"] == {"other": "skill.md"}
    assert server.requests == []


def test_a_cheap_tier_never_builds_the_delegate() -> None:
    factory = _Factory()
    with FakeOpenAIServer(["hello"]) as server:
        _hybrid(server, factory).run(role="r", tier="cheap", schema=None, prompt="hi")
    assert factory.built == []


def test_the_delegates_record_follows_a_local_record_in_calls() -> None:
    factory = _Factory()
    with FakeOpenAIServer(["hello"]) as server:
        runner = _hybrid(server, factory)
        runner.run(role="r", tier="cheap", schema=None, prompt="hi")
        runner.run(role="r", tier="standard", schema=SCHEMA, prompt="go")
        runner.run(role="r", tier="standard", schema=SCHEMA, prompt="again")
    assert [(c["model"], c["ok"]) for c in runner.calls] == [
        ("local/qwen-small", True),
        ("anthropic/claude-sonnet-x", True),
        ("anthropic/claude-sonnet-x", True),
    ]
    assert len(factory.built) == 1


def test_a_failed_delegate_call_is_recorded_as_not_ok_and_reraised() -> None:
    with FakeOpenAIServer([]) as server, pytest.raises(RunnerError, match="refused"):
        runner = _hybrid(server, _Factory(fail=True))
        runner.run(role="r", tier="standard", schema=SCHEMA, prompt="go")
    assert [c["ok"] for c in runner.calls] == [False]


def test_the_real_delegate_joins_calls_shares_one_store_sequence_and_closes_twice() -> None:
    conn = open_store("sqlite:///:memory:", "2026-09-25T00:00:00Z")
    client = _Client()
    with FakeOpenAIServer(["hello"]) as server:
        runner = _hybrid(server, _real(client), store=Store(conn), run_id=RUN)
        runner.run(role="r", tier="cheap", schema=None, prompt="hi")
        runner.run(role="r", tier="standard", schema=SCHEMA, prompt="go")
    runner.close()
    runner.close()
    rows = conn.query_all("SELECT seq, model_id FROM node_calls ORDER BY seq")
    conn.close()
    assert rows == [(1, "local/qwen-small"), (2, "claude-sonnet-x")]
    assert [c["model"] for c in runner.calls] == ["local/qwen-small", "anthropic/claude-sonnet-x"]
    assert client.sent[0]["model"] == "claude-sonnet-x"


def test_a_schemaless_anthropic_call_returns_text_like_the_local_path() -> None:
    client = _Client('{"text": "hello"}')
    with FakeOpenAIServer([]) as server:
        out = _hybrid(server, _real(client)).run(role="r", tier="standard", schema=None, prompt="hi")
    assert out["text"] == "hello"
    assert client.sent[0]["output_config"]["format"]["schema"] == TEXT_SCHEMA


def test_the_default_factory_builds_the_real_anthropic_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    monkeypatch.setattr(AnthropicRunner, "_build_client", lambda self: client)
    with FakeOpenAIServer([]) as server:
        runner = OpenAICompatibleRunner(HYBRID, role_skills={}, env={ENV_VAR: server.base_url})
        runner.run(role="r", tier="standard", schema=SCHEMA, prompt="go")
    assert client.sent[0]["model"] == "claude-sonnet-x"
    runner.close()


def test_close_closes_the_delegate_once() -> None:
    factory = _Factory()
    with FakeOpenAIServer([]) as server:
        runner = _hybrid(server, factory)
        runner.run(role="r", tier="standard", schema=SCHEMA, prompt="go")
        runner.close()
        runner.close()
    assert factory.delegate.closed == 1


def test_an_unknown_prefix_raises_without_building_the_delegate() -> None:
    factory = _Factory()
    with FakeOpenAIServer([]) as server, pytest.raises(RunnerError, match="deep"):
        _hybrid(server, factory).run(role="r", tier="deep", schema=SCHEMA, prompt="go")
    assert factory.built == []
