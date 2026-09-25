"""Chat completions client for OpenAI-compatible servers. Standard library only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class ChatTransportError(Exception):
    """The request never produced a usable JSON body: refused, timed out, non-2xx, or not JSON."""


@dataclass(frozen=True)
class Reply:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None


@dataclass(frozen=True)
class ChatError:
    reason: str


def wire_model(tier_model: str) -> str:
    """Strip the vendor prefix up to the first slash."""
    return tier_model.split("/", 1)[-1]


def build_request(model: str, system: str | None, prompt: str, max_tokens: int, temperature: float) -> dict:
    system_messages = [] if system is None else [{"role": "system", "content": system}]
    return {
        "model": model,
        "messages": [*system_messages, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def parse_response(body: dict) -> Reply | ChatError:
    """Local servers sometimes omit `usage`; that reads as zero tokens."""
    choices = body.get("choices")
    if not choices:
        return ChatError("response has no choices")
    choice = choices[0]
    content = (choice.get("message") or {}).get("content")
    if not content:
        return ChatError("first choice has no message content")
    usage = body.get("usage") or {}
    return Reply(
        text=content,
        prompt_tokens=usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("completion_tokens") or 0,
        finish_reason=choice.get("finish_reason"),
    )


def post_chat(base_url: str, payload: dict, *, timeout: float) -> dict:
    """POST to `{base_url}/v1/chat/completions`. The base is the server root, not a path ending in /v1."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ChatTransportError(f"HTTP {exc.code} from {request.full_url}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ChatTransportError(f"transport failure: {exc}") from exc
    except ValueError as exc:
        raise ChatTransportError(f"response body is not JSON: {exc}") from exc
