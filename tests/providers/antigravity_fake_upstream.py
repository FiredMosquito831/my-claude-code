"""A loopback ``v1internal`` fake upstream, shaped like the captured references.

**A pytest fixture, not a dev tool** (NQ10): a dev tool drifts from the tests
that use it; a fixture imported by the tests is the only version exercised on
every run. It is never reachable from production code — the base URL is a
setting only a test sets.

Every frame here is byte-shaped like a capture quoted in
``specs/IMPLEMENTATION-ANTIGRAVITY-PROVIDER.md`` §4, and the behaviours carry
that spec's F-numbers so a test reads as a sentence:

* **F1** a text frame in the host's own envelope
  ``data: {"response":{"candidates":[…]}}`` — no ``event:`` line exists.
* **F2** a terminal frame, and a variant with **no ``finishReason`` at all**
  (a real tool-call stream ends that way).
* **F5** the six-counter ``usageMetadata`` on a tool turn, whose arithmetic
  closes only if the counters are disjoint (153,609 + 17 + 94 = 153,720).
* **F8** the byte-exact ``403 PERMISSION_DENIED`` + ``ErrorInfo``
  ``TOS_VIOLATION`` body with its ``appeal_url``.
* **F9** ``loadCodeAssist`` answering **200** with no ``currentTier`` and
  ``UNSUPPORTED_CLIENT``.
* **F13** ``loadCodeAssist`` answering a project.
* **F14** the ``fetchAvailableModels`` roster (nine models: one ``-disabled``,
  old Gemini families, one ``-high``, one with no published cap, one absent
  from quota).
* **F15** the header assertion: ``Client-Metadata``, ``X-Goog-Api-Client``,
  ``x-goog-user-project`` or an ``Accept`` header is a test-visible refusal.

F3/F4/F6/F7/F10/F11/F12 are represented here too (the thinking stream, the
tool frame with its captured ``call_524881`` id, the 401-then-200, the 429 with
``Retry-After``, the in-stream error, the empty stream and the wrong content
type) so P3 extends the same fixture rather than inventing a second one.

No token value exists anywhere in this file: every credential is a fake string
of the right *shape* (never a ``demo-`` prefix, which this product once read as
"configured").
"""

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: A fake access token with the right shape. Never a real credential.
FAKE_ACCESS_TOKEN = "ya29.fake-antigravity-access-token-for-tests"
FAKE_REFRESH_TOKEN = "1//fake-antigravity-refresh-token-for-tests"
FAKE_ACCESS_TOKEN_2 = "ya29.fake-antigravity-access-token-two"
FAKE_REFRESH_TOKEN_2 = "1//fake-antigravity-refresh-token-two"

#: The TOS body, byte-exact per the capture
#: (``CLIProxyAPI antigravity_executor_tos_test.go:17-35``).
TOS_BODY: dict[str, Any] = {
    "error": {
        "code": 403,
        "message": (
            "This service has been disabled in this account for violation of "
            "Terms of Service. Please submit an appeal to continue using this "
            "product."
        ),
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "TOS_VIOLATION",
                "domain": "cloudcode-pa.googleapis.com",
                "metadata": {
                    "uiMessage": "true",
                    "appeal_url": "https://forms.gle/fake-appeal-url",
                    "appeal_url_link_text": "Submit Appeal",
                },
            }
        ],
    }
}

#: The 200-that-means-refused (PROTOCOL §5.4).
UNSUPPORTED_CLIENT_BODY: dict[str, Any] = {
    "allowedTiers": [{"id": "free-tier", "isDefault": True}],
    "ineligibleTiers": [
        {
            "reasonCode": "UNSUPPORTED_CLIENT",
            "reasonMessage": (
                "This client is no longer supported for Gemini Code Assist "
                "for individuals. To continue using Gemini, please migrate to "
                "the Antigravity suite of products."
            ),
        }
    ],
}

#: A healthy ``loadCodeAssist`` (F13).
PROJECT_OK_BODY: dict[str, Any] = {
    "currentTier": {"id": "free-tier"},
    "cloudaicompanionProject": "default-cli-project",
}

#: The nine-model roster (F14). Four are listable; the other five — one
#: ``-disabled`` placeholder and four old-Gemini families — stay routable.
ROSTER: dict[str, Any] = {
    "gemini-3.8-flash": {
        "displayName": "Gemini 3.8 Flash",
        "modelId": "gemini-3.8-flash",
        "maxOutputTokens": 65536,
        "modelTier": "MODEL_TIER_PRO",
        "isInternal": False,
        "quotaInfo": {"remainingFraction": 0.5, "resetTime": "2026-11-20T00:00:00Z"},
        "recommendedUpgradeModel": "",
    },
    "gemini-3.7-flash-high": {
        "displayName": "Gemini 3.7 Flash (High)",
        "modelId": "gemini-3.7-flash-high",
        "maxOutputTokens": 65536,
        "modelTier": "MODEL_TIER_FLASH",
        "isInternal": False,
    },
    "claude-sonnet-4-6": {
        "displayName": "Claude Sonnet 4.6",
        "modelId": "claude-sonnet-4-6",
        "maxOutputTokens": 65535,
        "modelTier": "MODEL_TIER_PRO",
        "isInternal": False,
        "quotaInfo": {"remainingFraction": 0.25},
        "recommendedUpgradeModel": "claude-opus-4-6",
    },
    "gpt-oss-120b-medium": {
        "displayName": "GPT-OSS 120B (Medium)",
        "modelId": "gpt-oss-120b-medium",
        "modelTier": "MODEL_TIER_FLASH",
        "isInternal": False,
        "quotaInfo": {"remainingFraction": 0.75},
    },
    "gemini-3.8-flash-disabled": {
        "displayName": "Gemini 3.8 Flash (disabled)",
        "modelId": "gemini-3.8-flash-disabled",
        "maxOutputTokens": 65536,
        "modelTier": "MODEL_TIER_PRO",
        "isInternal": True,
    },
    "gemini-2.5-pro": {
        "displayName": "Gemini 2.5 Pro",
        "modelId": "gemini-2.5-pro",
        "maxOutputTokens": 65536,
        "modelTier": "MODEL_TIER_PRO",
    },
    "gemini-2.5-flash": {
        "displayName": "Gemini 2.5 Flash",
        "modelId": "gemini-2.5-flash",
        "maxOutputTokens": 65536,
        "modelTier": "MODEL_TIER_FLASH",
    },
    "gemini-2.5-flash-lite": {
        "displayName": "Gemini 2.5 Flash Lite",
        "modelId": "gemini-2.5-flash-lite",
        "maxOutputTokens": 32768,
        "modelTier": "MODEL_TIER_FLASH_LITE",
    },
    "gemini-3.0-flash-preview": {
        "displayName": "Gemini 3.0 Flash Preview",
        "modelId": "gemini-3.0-flash-preview",
        "maxOutputTokens": 65536,
        "modelTier": "MODEL_TIER_FLASH",
    },
}

#: The six-counter usage block from the captured frame, plus the two counters
#: the capture carries beside them. The arithmetic closes only if the counters
#: are disjoint: 153,609 + 17 + 94 = 153,720.
SIX_COUNTER_USAGE: dict[str, Any] = {
    "promptTokenCount": 153609,
    "candidatesTokenCount": 17,
    "thoughtsTokenCount": 94,
    "cachedContentTokenCount": 1000,
    "toolUsePromptTokenCount": 500,
    "totalTokenCount": 153720,
}

MINIMAL_USAGE: dict[str, Any] = {
    "promptTokenCount": 12,
    "candidatesTokenCount": 3,
    "totalTokenCount": 15,
}

#: A signature long enough to be real-shaped (>= 48 chars). It is NOT a real
#: signature and nothing in P1 replays it.
FAKE_THOUGHT_SIGNATURE = "F" * 64


def frame(payload: dict[str, Any]) -> bytes:
    """One SSE frame, exactly as the host writes it: ``data:`` + JSON + blank."""

    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def text_frame(text: str, *, finish_reason: str | None = None) -> bytes:
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": text}]}
    }
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    return frame({"response": {"candidates": [candidate]}})


def usage_frame(usage: dict[str, Any], *, finish_reason: str | None = "STOP") -> bytes:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": []}}
    if finish_reason is not None:
        candidate["finishReason"] = finish_reason
    return frame({"response": {"candidates": [candidate], "usageMetadata": usage}})


def tool_frame(
    *,
    name: str = "get_weather",
    args: dict[str, Any] | None = None,
    call_id: str = "call_524881",
    signature: str = FAKE_THOUGHT_SIGNATURE,
    usage: dict[str, Any] | None = None,
) -> bytes:
    """The captured tool chunk: one ``functionCall`` with a sibling signature,
    plus ``usageMetadata`` — and **no** ``finishReason``."""

    part: dict[str, Any] = {
        "functionCall": {
            "name": name,
            "args": args if args is not None else {"city": "Paris"},
            "id": call_id,
        }
    }
    if signature:
        part["thoughtSignature"] = signature
    response: dict[str, Any] = {
        "candidates": [{"content": {"role": "model", "parts": [part]}}]
    }
    if usage is not None:
        response["usageMetadata"] = usage
    return frame({"response": response})


def thinking_frame(text: str, *, signature: str = FAKE_THOUGHT_SIGNATURE) -> bytes:
    return frame(
        {
            "response": {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "thought": True,
                                    "text": text,
                                    "thoughtSignature": signature,
                                }
                            ],
                        }
                    }
                ]
            }
        }
    )


@dataclass
class RecordedRequest:
    """One request the fake received, in the shape a test can assert on."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Any

    def header_names(self) -> set[str]:
        return set(self.headers)


@dataclass
class FakeTurn:
    """One scripted answer.

    ``frames`` present -> an SSE response; ``payload`` present -> JSON. The
    two are mutually exclusive, which makes a scenario read as what it is.
    """

    status: int = 200
    frames: tuple[bytes, ...] = ()
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    delay_first_frame: float = 0.0

    def is_sse(self) -> bool:
        return bool(self.frames) or self.content_type == "text/event-stream"


def sse(*frames: bytes, status: int = 200, delay_first_frame: float = 0.0) -> FakeTurn:
    return FakeTurn(
        status=status,
        frames=frames,
        content_type="text/event-stream",
        delay_first_frame=delay_first_frame,
    )


def json_turn(
    payload: Any, *, status: int = 200, headers: dict[str, str] | None = None
) -> FakeTurn:
    return FakeTurn(
        status=status,
        payload=payload,
        content_type="application/json",
        headers=dict(headers or {}),
    )


#: The default text turn: one text frame, then a terminal frame (F1, F2).
DEFAULT_TEXT_TURN = sse(
    text_frame("Hello from the fake upstream."),
    usage_frame(MINIMAL_USAGE),
)


class AntigravityFakeUpstream:
    """A real HTTP server on loopback that speaks ``v1internal``.

    Every knob is a constructor argument so a test reads as a sentence:

    ``turns``                 scripted stream answers, consumed in order
    ``token_turns``           scripted token-endpoint answers, in order
    ``identity_mode``         ``cli`` / ``ide`` / ``incomplete`` (F15's refusal)
    ``holdback_probe``        delay the first frame past the 0.75 s commit holdback
    ``sse_terminator``        ``finishReason`` / ``done`` / ``eof`` / ``none``
    ``usage_variant``         ``six_counter_tool`` / ``minimal`` / ``absent``
    ``fail_on_header``        header names whose presence is a refusal
    ``project_discovery``     ``present`` / ``empty`` / ``unsupported_client``
    ``stream_error_after``    ``None`` or an int (frames before an error frame)
    ``roster``                the ``fetchAvailableModels`` body
    """

    def __init__(
        self,
        *,
        turns: list[FakeTurn] | None = None,
        token_turns: list[FakeTurn] | None = None,
        identity_mode: str = "cli",
        holdback_probe: bool = False,
        sse_terminator: str = "finishReason",
        usage_variant: str = "minimal",
        fail_on_header: tuple[str, ...] = (
            "client-metadata",
            "x-goog-api-client",
            "x-goog-user-project",
            "accept",
        ),
        project_discovery: str = "present",
        stream_error_after: int | None = None,
        roster: dict[str, Any] | None = None,
    ) -> None:
        self.turns = list(turns) if turns is not None else [DEFAULT_TEXT_TURN]
        self.token_turns = (
            list(token_turns)
            if token_turns is not None
            else [json_turn({"access_token": FAKE_ACCESS_TOKEN, "expires_in": 3599})]
        )
        self.identity_mode = identity_mode
        self.holdback_probe = holdback_probe
        self.sse_terminator = sse_terminator
        self.usage_variant = usage_variant
        self.fail_on_header = tuple(name.lower() for name in fail_on_header)
        self.project_discovery = project_discovery
        self.stream_error_after = stream_error_after
        self.roster = roster if roster is not None else ROSTER
        self.requests: list[RecordedRequest] = []
        self.violations: list[str] = []
        self._lock = threading.Lock()
        self._turn_index = 0
        self._token_index = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="antigravity-fake-upstream"
        )
        self._thread.daemon = True
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> AntigravityFakeUpstream:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- assertions --------------------------------------------------------

    def requests_for(self, fragment: str) -> list[RecordedRequest]:
        return [request for request in self.requests if fragment in request.path]

    def last_request(self, fragment: str = "") -> RecordedRequest:
        matching = self.requests_for(fragment)
        assert matching, f"no request matching {fragment!r} was received"
        return matching[-1]

    # -- scripted turns ----------------------------------------------------

    def _next_turn(self) -> FakeTurn:
        with self._lock:
            if self._turn_index >= len(self.turns):
                return DEFAULT_TEXT_TURN
            turn = self.turns[self._turn_index]
            self._turn_index += 1
            return turn

    def _next_token_turn(self) -> FakeTurn:
        with self._lock:
            if self._token_index >= len(self.token_turns):
                return json_turn(
                    {"access_token": FAKE_ACCESS_TOKEN, "expires_in": 3599}
                )
            turn = self.token_turns[self._token_index]
            self._token_index += 1
            return turn

    def _stream_turn(self) -> FakeTurn:
        turn = self._next_turn()
        # The terminal shape is applied to a default text turn only: a scripted
        # multi-frame turn already decided how it ends.
        if turn is DEFAULT_TEXT_TURN:
            return self._with_terminator(turn)
        return turn

    def _with_terminator(self, turn: FakeTurn) -> FakeTurn:
        if self.sse_terminator == "finishReason":
            return turn
        frames = tuple(frame for frame in turn.frames if b'"finishReason"' not in frame)
        if self.sse_terminator == "done":
            frames = (*frames, b"data: [DONE]\n\n")
        return FakeTurn(
            status=turn.status,
            frames=frames,
            content_type=turn.content_type,
            delay_first_frame=turn.delay_first_frame,
        )

    # -- the handler -------------------------------------------------------

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # stdlib signature
                return

            def do_POST(self) -> None:  # the stdlib's own method name
                upstream._handle(self)

        return Handler

    # -- request handling --------------------------------------------------

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        length = int(handler.headers.get("content-length") or 0)
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        headers = {key.lower(): value for key, value in handler.headers.items()}
        request = RecordedRequest(
            method=handler.command,
            path=parsed.path,
            query=parse_qs(parsed.query),
            headers=headers,
            body=body,
        )
        with self._lock:
            self.requests.append(request)

        # F15 is an assertion about the IDENTITY calls, not about the token
        # endpoint: httpx's own ``Accept: */*`` on a token POST says nothing
        # about the client the model door claims to be.
        violation = (
            self._header_violation(headers)
            if parsed.path.startswith("/v1internal")
            else ""
        )
        if violation:
            self._write_json(
                handler,
                400,
                {
                    "error": {
                        "code": 400,
                        "message": f"forbidden header(s) on the wire: {violation}",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )
            return

        if parsed.path == "/token":
            self._write_turn(handler, self._next_token_turn())
            return
        if parsed.path.endswith(":streamGenerateContent"):
            self._write_stream(handler, request)
            return
        if parsed.path.endswith(":fetchAvailableModels"):
            self._write_json(handler, 200, {"models": self.roster})
            return
        if parsed.path.endswith(":loadCodeAssist"):
            self._write_load_code_assist(handler)
            return
        if parsed.path.endswith(":onboardUser"):
            self._write_json(
                handler,
                200,
                {
                    "done": True,
                    "response": {
                        "cloudaicompanionProject": {"id": "default-cli-project"}
                    },
                },
            )
            return
        self._write_json(
            handler,
            404,
            {"error": {"code": 404, "message": f"no fake for {parsed.path}"}},
        )

    def _header_violation(self, headers: dict[str, str]) -> str:
        """F15: the identity is proven by absence as much as by presence."""

        if self.identity_mode == "incomplete":
            # The measured 403 for a metadata/UA set the backend does not
            # recognise (OmniRoute's controlled A/B).
            self.violations.append("identity_mode=incomplete")
            return "identity profile is incomplete"
        found = [name for name in self.fail_on_header if name in headers]
        if found:
            self.violations.extend(found)
            return ", ".join(sorted(found))
        return ""

    def _write_load_code_assist(self, handler: BaseHTTPRequestHandler) -> None:
        if self.project_discovery == "unsupported_client":
            self._write_json(handler, 200, UNSUPPORTED_CLIENT_BODY)
            return
        if self.project_discovery == "empty":
            self._write_json(handler, 200, {"currentTier": {"id": "free-tier"}})
            return
        self._write_json(handler, 200, PROJECT_OK_BODY)

    def _write_stream(
        self, handler: BaseHTTPRequestHandler, request: RecordedRequest
    ) -> None:
        turn = self._stream_turn()
        if turn.status >= 400:
            self._write_turn(handler, turn)
            return
        default_turn = turn.frames == DEFAULT_TEXT_TURN.frames
        frames = list(turn.frames)
        if default_turn and self.usage_variant == "six_counter_tool":
            # F5 by name: the captured tool chunk replaces the plain text turn.
            frames = [tool_frame(usage=SIX_COUNTER_USAGE)]
        elif self.usage_variant == "absent":
            frames = [chunk for chunk in frames if b"usageMetadata" not in chunk]
        if self.stream_error_after is not None:
            error_frame = frame(
                {
                    "error": {
                        "code": 503,
                        "message": "The service is currently unavailable.",
                        "status": "UNAVAILABLE",
                    }
                }
            )
            frames = [*frames[: self.stream_error_after], error_frame]
        content_type = turn.content_type or "text/event-stream"
        handler.send_response(turn.status)
        handler.send_header("Content-Type", content_type)
        if turn.delay_first_frame:
            # The commit holdback is 0.75 s; a probe that wants a commit-order
            # test to be honest delays past it.
            time.sleep(turn.delay_first_frame)
        for key, value in turn.headers.items():
            handler.send_header(key, value)
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        for frame_bytes in frames:
            handler.wfile.write(frame_bytes)
            handler.wfile.flush()
            if self.holdback_probe:
                time.sleep(0.01)
        if self.sse_terminator == "eof":
            return

    def _write_turn(self, handler: BaseHTTPRequestHandler, turn: FakeTurn) -> None:
        if turn.frames:
            handler.send_response(turn.status)
            handler.send_header(
                "Content-Type", turn.content_type or "text/event-stream"
            )
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            for frame_bytes in turn.frames:
                handler.wfile.write(frame_bytes)
                handler.wfile.flush()
            return
        self._write_json(handler, turn.status, turn.payload, headers=turn.headers)

    def _write_json(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()


__all__ = [
    "DEFAULT_TEXT_TURN",
    "FAKE_ACCESS_TOKEN",
    "FAKE_ACCESS_TOKEN_2",
    "FAKE_REFRESH_TOKEN",
    "FAKE_REFRESH_TOKEN_2",
    "FAKE_THOUGHT_SIGNATURE",
    "MINIMAL_USAGE",
    "PROJECT_OK_BODY",
    "ROSTER",
    "SIX_COUNTER_USAGE",
    "TOS_BODY",
    "UNSUPPORTED_CLIENT_BODY",
    "AntigravityFakeUpstream",
    "FakeTurn",
    "RecordedRequest",
    "frame",
    "json_turn",
    "sse",
    "text_frame",
    "thinking_frame",
    "tool_frame",
    "usage_frame",
]
