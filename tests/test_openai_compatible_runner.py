"""OpenAICompatibleRunner serves `local/` tiers against a scripted server: text, schema answers, one retry, free cost."""

from __future__ import annotations

import pytest

from harness.store_migrate import open_store
from harness.store_write import Store
from runner.openai_compatible_runner import OpenAICompatibleRunner
from runner.protocol import RunnerError
from tests.fake_openai_server import FakeOpenAIServer

ENV_VAR = "LOCAL_LLM_URL"
PROFILE = {
    "endpoint_env": ENV_VAR,
    "capabilities": {"structured_output": False},
    "tiers": {"cheap": "local/qwen-small", "standard": "local/qwen-big", "deep": "remote/opus"},
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
RUN = "run-1:p3-write"


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
