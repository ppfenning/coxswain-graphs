"""A scripted OpenAI-compatible server on 127.0.0.1, for tests. Standard library only."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Scripted = str | tuple[int, dict]


def _chat_body(text: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


class FakeOpenAIServer:
    """Serves `replies` in order. A str is a 200 chat reply; a `(status, body)` pair is sent as given.

    `requests` records `{"path": ..., "body": ...}` for every request received.
    """

    def __init__(self, replies: list[Scripted]) -> None:
        self._replies = list(replies)
        self.requests: list[dict] = []
        lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with lock:
                    outer.requests.append({"path": self.path, "body": json.loads(raw)})
                    reply = outer._replies.pop(0) if outer._replies else (500, {"error": "no scripted reply left"})
                status, body = (200, _chat_body(reply)) if isinstance(reply, str) else reply
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> FakeOpenAIServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
