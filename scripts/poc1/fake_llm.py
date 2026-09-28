"""PoC 1: fake OpenAI-compatible LLM server (stdlib only).

Serves POST /v1/chat/completions with deterministic streamed or plain
responses. Every request body is appended as one JSON line to --log so the
orchestrator can assert, server-side, that a later process's turn carried the
earlier process's messages (cross-process checkpoint round-trip evidence).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_log_lock = threading.Lock()


def _log_request(path: str, body: dict) -> None:
    if not ARGS.log:
        return
    messages = body.get("messages") or []
    entry = {
        "ts": time.time(),
        "path": path,
        "stream": body.get("stream", False),
        "model": body.get("model"),
        "n_messages": len(messages),
        "messages": [
            {"role": m.get("role"), "content": str(m.get("content"))[:500]}
            for m in messages
        ],
    }
    with _log_lock, open(ARGS.log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _reply_text(body: dict) -> str:
    """Deterministic reply: echo the last user message (truncated)."""
    last_user = ""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            last_user = str(m.get("content"))
            break
    return f"FAKE-REPLY :: {last_user[:200]}"


def _chunk(completion_id: str, model: str, content: str, finish: str | None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": ({"role": "assistant"} if finish is None else {})
                if content == "" and finish is None
                else {"content": content},
                "finish_reason": finish,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # silence
        return

    def _respond_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._respond_json(400, {"error": {"message": "bad json"}})
            return

        _log_request(self.path, body)

        if not self.path.startswith("/v1/chat/completions"):
            self._respond_json(404, {"error": {"message": "not found"}})
            return

        model = body.get("model") or "fake-model"
        completion_id = f"chatcmpl-poc-{int(time.time() * 1000)}"
        text = _reply_text(body)

        if body.get("stream"):
            if ARGS.token_delay_ms > 0:
                text = (text + " ") * 30  # slow mode: long stream for cancel tests
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # role frame, then a few token frames, then final stop frame
            self.wfile.write(_chunk(completion_id, model, "", None).encode())
            for i in range(0, len(text), 16):
                piece = text[i : i + 16]
                self.wfile.write(_chunk(completion_id, model, piece, None).encode())
                self.wfile.flush()
                if ARGS.token_delay_ms > 0:
                    time.sleep(ARGS.token_delay_ms / 1000)
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self._respond_json(
                200,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )


ARGS: argparse.Namespace


def main() -> None:
    global ARGS
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", default="")
    parser.add_argument("--token-delay-ms", type=int, default=0, help="slow-mode: delay per token piece")
    ARGS = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    print(f"READY port={ARGS.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
