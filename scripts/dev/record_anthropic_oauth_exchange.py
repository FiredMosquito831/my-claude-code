"""Record one real Claude Code request as MCC sees it, redacted, for tests.

Run **by hand, never in CI**. It starts a throwaway HTTP server that speaks
just enough of the Anthropic Messages API to accept one request, writes the
redacted result to ``tests/fixtures/anthropic_oauth/claude_code_session.json``,
and exits. Nothing is forwarded upstream and no credential is used.

    uv run --offline python scripts/dev/record_anthropic_oauth_exchange.py --port 8199

then, in another shell::

    ANTHROPIC_BASE_URL=http://127.0.0.1:8199 ANTHROPIC_AUTH_TOKEN=local \\
        claude -p "say ok"

The fixture is what ``tests/providers/test_anthropic_oauth_wire.py`` replays to
prove the upstream header set, so it has to be a real client's request rather
than one this repo wrote. It is also why redaction is asserted rather than
hoped for: the script refuses to write a file containing ``sk-ant``.
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

REDACTED = "<REDACTED>"

# Header values that are a credential by name, whatever they look like.
_SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "cookie", "proxy-authorization"}
)

# Any long opaque run, and Anthropic's own key shapes.
_LONG_RUN = re.compile(r"[A-Za-z0-9_-]{40,}")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]+")

# Correlation ids that identify the operator's own session rather than the
# client's wire contract.
_SESSION_HEADERS = frozenset(
    {
        "x-claude-code-session-id",
        "x-claude-code-agent-id",
        "x-claude-code-parent-agent-id",
    }
)


def redact_text(value: str) -> str:
    value = _ANTHROPIC_KEY.sub(REDACTED, value)
    return _LONG_RUN.sub(REDACTED, value)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _SECRET_HEADERS or lowered in _SESSION_HEADERS:
            out[lowered] = REDACTED
        else:
            out[lowered] = redact_text(value)
    return out


def _fixture_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "tests" / "fixtures" / "anthropic_oauth" / "claude_code_session.json"


def write_fixture(headers: dict[str, str], body: dict[str, Any]) -> Path:
    payload = {
        "recorded_by": "scripts/dev/record_anthropic_oauth_exchange.py",
        "note": (
            "One real Claude Code request as MCC received it. Credentials, "
            "session ids and every long opaque run are redacted."
        ),
        "headers": redact_headers(headers),
        "body": redact(body),
    }
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    if "sk-ant" in text:
        raise SystemExit("refusing to write: the redacted fixture still says sk-ant")
    path = _fixture_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class _Recorder(BaseHTTPRequestHandler):
    captured: dict[str, Any] | None = None

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        _Recorder.captured = {"headers": dict(self.headers), "body": body}
        payload = json.dumps(
            {
                "id": "msg_recorded",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude-sonnet-4-6"),
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the console quiet; the parameter name is the base class's."""
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8199)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), _Recorder)
    print(f"Waiting for one Claude Code request on http://{args.host}:{args.port} ...")
    while _Recorder.captured is None:
        server.handle_request()
    captured = _Recorder.captured
    path = write_fixture(captured["headers"], captured["body"])
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
