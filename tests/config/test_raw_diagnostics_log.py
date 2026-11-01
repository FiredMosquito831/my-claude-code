"""Raw diagnostics land in a bounded file at the default log level (7.113.4).

The user turned on every advanced diagnostic to explain a provider failure and
nothing reached ``server.log``: the Responses reader *printed* its frames (no
console, nothing kept), and the two other writers -- the Anthropic emitter and
the full-payload line -- logged at DEBUG, which ``LOG_LEVEL=INFO`` drops. The
Messages transport had no raw wiring at all.

What these tests hold, per wired path:

* with the setting on, ``raw-diagnostics.log`` receives one line per record,
  starting ``RAW_<KIND> <request_id> ``, where the request id is the one bound
  to the logging context (``RequestCorrelationMiddleware`` binds it in
  production; these tests bind it exactly the same way);
* with the setting off, no file exists, nothing is written and stdout carries
  nothing -- the old ``print`` is gone;
* the file is bounded by the same rotation and retention ``server.log`` has.

Each test drives the real object that writes the record -- the converter, the
stream ledger, ``MessagesTransport.stream`` over a fake upstream,
``MessagesHandler.create`` through the real executor -- rather than a helper,
so a call site that stops emitting its marker fails here.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger

from my_claude_code.api.handlers import MessagesHandler
from my_claude_code.cli.managed.claude import (
    ManagedClaudeParseState,
    parse_managed_claude_stdout_line,
)
from my_claude_code.config import logging_config
from my_claude_code.config.logging_config import (
    RAW_DIAGNOSTICS_LOG_FILENAME,
    configure_logging,
    configure_raw_diagnostics,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import AnthropicStreamLedger
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.messages_transport import MessagesTransport
from my_claude_code.providers.openai_responses import ResponsesStreamConverter
from tests.providers.support import passthrough_rate_limiter

BASE_URL = "https://gateway.example/v1"

#: One minimal Anthropic SSE conversation, the shape a Messages-dialect host
#: answers 200 with. The reader needs nothing more to finish a stream.
MESSAGES_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"type":"message","role":"assistant","model":"m","content":[],'
    '"stop_reason":null,"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"ok"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":'
    '{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _arm(
    tmp_path: Path, *, sse: bool = False, payloads: bool = False, cli: bool = False
) -> Path:
    """Configure logging exactly as ``build_asgi_app`` does, then the raw sink."""

    configure_logging(tmp_path / "logs" / "server.log", force=True)
    path = tmp_path / "logs" / RAW_DIAGNOSTICS_LOG_FILENAME
    assert configure_raw_diagnostics(
        path, sse=sse, payloads=payloads, cli=cli, retain_files=10
    )
    return path


def _lines(path: Path) -> list[str]:
    """Every record enqueued so far, one physical line each."""

    logger.complete()
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# off: nothing at all -- no file, no write, no stdout
# ---------------------------------------------------------------------------


def test_no_file_is_created_while_every_raw_setting_is_off(tmp_path, capsys) -> None:
    path = _arm(tmp_path)
    ledger = AnthropicStreamLedger("msg_1", "m", 1, log_raw_events=False)
    converter = ResponsesStreamConverter(ledger, log_raw_events=False)
    # A completed turn: since 8.0.1 a terminal ``response.incomplete`` that
    # produced nothing is a classified failure, and what this test needs is
    # only that a frame goes through the converter with the sink off.
    list(converter.feed({"type": "response.completed"}))
    ledger.message_start()
    parse_managed_claude_stdout_line(
        "not json", ManagedClaudeParseState(log_raw_cli_diagnostics=False)
    )

    assert _lines(path) == []
    assert not path.exists()
    assert capsys.readouterr().out == ""


def test_a_build_that_never_adopted_a_path_writes_nothing(tmp_path) -> None:
    """An embedded harness or a unit test: no path, no sink, no file."""

    assert configure_raw_diagnostics(sse=True) is False
    assert not list((tmp_path / "logs").glob("raw-diagnostics*.log"))


# ---------------------------------------------------------------------------
# the Responses reader: a print, which reached no file at all
# ---------------------------------------------------------------------------


def test_responses_frames_land_in_the_raw_file_with_the_request_id(
    tmp_path, capsys
) -> None:
    path = _arm(tmp_path, sse=True)
    converter = ResponsesStreamConverter(
        AnthropicStreamLedger("msg_1", "m"), log_raw_events=True
    )
    # The frame that could not be captured live: a Codex stream that stopped
    # early says why only on this event. A truncated answer, not the empty
    # turn 8.0.1 raises for -- the line has to land either way.
    answer = {"type": "response.output_text.delta", "delta": "half an answ"}
    event = {
        "type": "response.incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }

    with logger.contextualize(request_id="req_responses"):
        list(converter.feed(answer))
        list(converter.feed(event))

    lines = _lines(path)
    assert lines == [
        "RAW_SSE req_responses SSE_EVENT: responses_event "
        + json.dumps(answer, default=str),
        "RAW_SSE req_responses SSE_EVENT: responses_event "
        + json.dumps(event, default=str),
    ]
    assert "max_output_tokens" in lines[-1]
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# the Anthropic emitter: DEBUG, which the default level dropped
# ---------------------------------------------------------------------------


def test_anthropic_emitter_events_land_in_the_raw_file(tmp_path) -> None:
    path = _arm(tmp_path, sse=True)

    with logger.contextualize(request_id="req_ledger"):
        AnthropicStreamLedger("msg_1", "m", 1, log_raw_events=True).message_start()

    lines = _lines(path)
    assert len(lines) == 1
    assert lines[0].startswith("RAW_SSE req_ledger SSE_EVENT: message_start ")


# ---------------------------------------------------------------------------
# the Messages transport: no raw wiring at all before 7.113.4
# ---------------------------------------------------------------------------


class _MessagesUpstream:
    """A fake ``{base}/messages`` that answers one Anthropic SSE stream."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=MESSAGES_SSE.encode(),
            request=request,
        )


def _messages_transport(*, log_raw_sse_events: bool) -> MessagesTransport:
    return MessagesTransport(
        ProviderConfig(
            api_key="sk-test",
            base_url=BASE_URL,
            log_raw_sse_events=log_raw_sse_events,
        ),
        base_url=BASE_URL,
        provider_name="GATEWAY",
        provider_id="gateway",
        identity=None,
        api_key="sk-test",
        rate_limiter=passthrough_rate_limiter(),
    )


@pytest.mark.asyncio
async def test_the_messages_transport_streams_land_in_the_raw_file(tmp_path) -> None:
    path = _arm(tmp_path, sse=True)
    transport = _messages_transport(log_raw_sse_events=True)
    upstream = _MessagesUpstream()
    # Reaching for the inner provider's client is the narrowest available seam;
    # the same one ``test_opencode_messages_surface.py`` uses.
    transport._provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream)
    )
    request = MessagesRequest(
        model="m", max_tokens=16, messages=[Message(role="user", content="hi")]
    )

    with logger.contextualize(request_id="req_messages"):
        frames = [
            frame
            async for frame in transport.stream(
                request,
                input_tokens=3,
                reasoning=DEFAULT_REASONING_POLICY,
                request_id="req_messages",
            )
        ]

    # The client still gets exactly the six frames the upstream sent.
    assert len(frames) == 6
    lines = _lines(path)
    raw = [line for line in lines if line.startswith("RAW_SSE ")]
    assert len(raw) == 6
    assert all(
        line.startswith("RAW_SSE req_messages SSE_EVENT: GATEWAY ") for line in raw
    )
    assert "message_stop" in raw[-1]
    # The transport's own DEBUG lines are not raw diagnostics and stay out.
    assert not any("_STREAM:" in line for line in lines)


@pytest.mark.asyncio
async def test_the_messages_transport_writes_nothing_when_the_setting_is_off(
    tmp_path,
) -> None:
    path = _arm(tmp_path)
    transport = _messages_transport(log_raw_sse_events=False)
    transport._provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_MessagesUpstream())
    )
    request = MessagesRequest(
        model="m", max_tokens=16, messages=[Message(role="user", content="hi")]
    )

    frames = [
        frame
        async for frame in transport.stream(
            request, input_tokens=3, reasoning=DEFAULT_REASONING_POLICY
        )
    ]

    assert len(frames) == 6
    assert _lines(path) == []
    assert not path.exists()


# ---------------------------------------------------------------------------
# the payload setting: DEBUG in the frozen executor, captured as a sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_payloads_land_in_the_raw_file_with_the_request_id(
    tmp_path,
) -> None:
    path = _arm(tmp_path, payloads=True)
    settings = Settings()
    settings.log_raw_api_payloads = True

    async def fake_stream(*_args, **_kwargs):
        yield "event: ping\ndata: {}\n\n"

    provider = MagicMock()
    provider.stream_response = fake_stream
    handler = MessagesHandler(settings, provider_resolver=lambda _: provider)
    request = MessagesRequest(
        model="claude-3-haiku-20240307",
        max_tokens=10,
        messages=[Message(role="user", content="visible")],
    )

    with logger.contextualize(request_id="req_payload"):
        await handler.create(request, request_id="req_payload")

    lines = _lines(path)
    payloads = [line for line in lines if line.startswith("RAW_PAYLOAD ")]
    assert payloads, lines
    assert payloads[0].startswith(
        "RAW_PAYLOAD req_payload FULL_PAYLOAD [req_payload]: "
    )
    assert "'visible'" in payloads[0]


# ---------------------------------------------------------------------------
# the managed CLI's non-JSON lines: DEBUG in the frozen cli/ package
# ---------------------------------------------------------------------------


def test_managed_cli_non_json_lines_land_in_the_raw_file(tmp_path) -> None:
    path = _arm(tmp_path, cli=True)

    with logger.contextualize(request_id="req_cli"):
        events = list(
            parse_managed_claude_stdout_line(
                "these are the CLI's own words",
                ManagedClaudeParseState(log_raw_cli_diagnostics=True),
            )
        )

    assert events == [{"type": "raw", "content": "these are the CLI's own words"}]
    lines = _lines(path)
    assert lines == ["RAW_CLI req_cli Non-JSON output: these are the CLI's own words"]


# ---------------------------------------------------------------------------
# one record is one physical line, however the message was written
# ---------------------------------------------------------------------------


def test_a_multi_line_message_is_written_as_one_physical_line(tmp_path) -> None:
    path = _arm(tmp_path, sse=True)

    with logger.contextualize(request_id="req_multiline"):
        logger.debug("SSE_EVENT: {} {}", "GATEWAY", "event: x\ndata: {}\n")

    lines = _lines(path)
    assert lines == ["RAW_SSE req_multiline SSE_EVENT: GATEWAY event: x\\ndata: {}\\n"]


def test_a_record_with_no_request_behind_it_is_tagged_with_a_dash(tmp_path) -> None:
    path = _arm(tmp_path, sse=True)

    logger.debug("SSE_EVENT: {} - {}", "message_start", "event: message_start")

    assert _lines(path) == ["RAW_SSE - SSE_EVENT: message_start - event: message_start"]


# ---------------------------------------------------------------------------
# bounded: the same rotation and retention server.log has
# ---------------------------------------------------------------------------


def test_the_raw_file_rotates_and_prunes_like_the_server_log(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(logging_config, "_MAX_LOG_BYTES", 200)
    path = _arm(tmp_path, sse=True)
    configure_raw_diagnostics(path, sse=True, retain_files=2)  # same file, retention 2

    with logger.contextualize(request_id="req_rotate"):
        for index in range(6):
            logger.debug("SSE_EVENT: {}", "x" * 60 + str(index))

    logger.complete()
    sink = logging_config._raw_sink
    assert sink is not None
    active = sink.active_path
    rotated = sorted(
        candidate
        for candidate in path.parent.glob("raw-diagnostics.*.log")
        if candidate != active
    )
    # Six ~70-byte lines against a 200-byte cap: more than one rotation, and
    # the cap keeps exactly the two newest rotated files.
    assert rotated, list(path.parent.iterdir())
    assert len(rotated) <= 2
    assert active.is_file()
