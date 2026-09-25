"""One literal case per rule of the chat completions client."""

from __future__ import annotations

import socket

import pytest
from fake_openai_server import FakeOpenAIServer

from runner.openai_chat import (
    ChatError,
    ChatTransportError,
    Reply,
    build_request,
    parse_response,
    post_chat,
    wire_model,
)


def test_wire_model_strips_the_vendor_prefix():
    assert wire_model("local/qwen2.5-7b-instruct") == "qwen2.5-7b-instruct"


def test_wire_model_without_a_slash_is_unchanged():
    assert wire_model("qwen2.5-7b-instruct") == "qwen2.5-7b-instruct"


def test_build_request_with_a_system_message():
    assert build_request("m", "be brief", "hi", 64, 0.0) == {
        "model": "m",
        "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        "max_tokens": 64,
        "temperature": 0.0,
    }


def test_build_request_without_a_system_message():
    assert build_request("m", None, "hi", 64, 0.5)["messages"] == [{"role": "user", "content": "hi"}]


def test_parse_response_on_a_good_body():
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2},
    }
    assert parse_response(body) == Reply(text="ok", prompt_tokens=7, completion_tokens=2, finish_reason="stop")


def test_parse_response_with_no_choices_is_a_chat_error():
    assert parse_response({"choices": []}) == ChatError("response has no choices")


def test_parse_response_with_no_message_content_is_a_chat_error():
    assert parse_response({"choices": [{"message": {}}]}) == ChatError("first choice has no message content")


def test_parse_response_with_no_usage_reads_zero_tokens():
    body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "length"}]}
    assert parse_response(body) == Reply(text="ok", prompt_tokens=0, completion_tokens=0, finish_reason="length")


def test_post_chat_returns_the_body_and_hits_the_completions_path():
    with FakeOpenAIServer(["hello"]) as server:
        body = post_chat(server.base_url + "/", {"model": "m"}, timeout=5)
    assert body["choices"][0]["message"]["content"] == "hello"
    assert server.requests == [{"path": "/v1/chat/completions", "body": {"model": "m"}}]


def test_post_chat_raises_naming_a_500_status():
    with FakeOpenAIServer([(500, {"error": "boom"})]) as server, pytest.raises(ChatTransportError, match="500"):
        post_chat(server.base_url, {"model": "m"}, timeout=5)


def test_post_chat_raises_on_a_closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(ChatTransportError):
        post_chat(f"http://127.0.0.1:{port}", {"model": "m"}, timeout=2)
