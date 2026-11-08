"""8.15.0: ``parallel_tool_calls`` is one adapter-level setting, on by default.

Before 8.15.0 both Responses senders hard-coded ``False`` -- the ChatGPT
subscription's own builder and the shared transport OpenCode Zen/Go are served
on. ``RESPONSES_PARALLEL_TOOL_CALLS`` replaces both literals with one value
that ships **on** (user decision, 2026-11-08: *"parallel tool calls should be
full adapter level setting or option we can toggle on or off (default on)"*).

What this file proves, and nothing else:

* **off-equality** -- with the setting ``false``, each door's body is exactly
  the body v8.14.3 built, compared cell by cell against a snapshot recorded on
  a detached worktree at ``179c19aa`` (v8.14.3). Zero differing lines.
* **on-difference** -- with it ``true``, exactly one cell moves per body,
  ``"parallel_tool_calls": false`` -> ``true``, and nothing else does. The
  assertion is written as a whole-body comparison of the two builds with the
  one cell normalised, so a second moved cell fails it.
* **both doors, through their real senders** -- a fake upstream records the
  JSON each door actually posts, so the assertion is about the wire and not
  about a builder called in isolation.
* **the builder's own keyword default is untouched** -- a caller that says
  nothing still gets ``false``, which is what keeps every pre-8.15.0 caller
  (and every historical equality contract) on its own bytes.
* **the setting is what moves it** -- the environment variable, through
  ``get_settings``, which is the same path an admin save clears.

The historical equality contracts pin the switch off rather than being
re-recorded (``tests/support/parallel_tool_calls_pin.py`` says why); this file
is what covers the shipped, switched-on state.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config import settings as config_settings
from my_claude_code.config.constants import RESPONSES_PARALLEL_TOOL_CALLS_DEFAULT
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)
from tests.providers.support import passthrough_rate_limiter
from tests.support.parallel_tool_calls_pin import PARALLEL_TOOL_CALLS_ENV

BASELINE = Path(__file__).with_name("responses_parallel_tool_calls_baseline.json")

#: The one cell this release is allowed to move.
CELL = "parallel_tool_calls"

REASONING = ReasoningPolicy.on()

#: A request with tools, a replayed tool call and a system prompt -- the shape
#: that carries every key the builder can emit, so "nothing else moved" is a
#: statement about a full body rather than a bare one.
FIXTURE: dict[str, Any] = {
    "model": "gpt-5.6-terra",
    "max_tokens": 512,
    "system": "You are a test harness.",
    "messages": [
        {"role": "user", "content": "list the files"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading."},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "a.txt"}
            ],
        },
    ],
    "tools": [
        {
            "name": "Bash",
            "description": "Run a shell command.",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }
    ],
    "tool_choice": {"type": "auto"},
}


def _request() -> MessagesRequest:
    return MessagesRequest.model_validate(FIXTURE)


def _set(value: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the setting where the server reads it, and rebuild the cache.

    The environment variable plus ``get_settings.cache_clear()`` is exactly
    what an admin save does (``runtime/application._commit_admin_update``), so
    a value set here reaches the body builder the same way a saved one does.
    """

    monkeypatch.setenv(PARALLEL_TOOL_CALLS_ENV, "true" if value else "false")
    config_settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    """Never hand a settings object built under this file's env to another."""

    config_settings.get_settings.cache_clear()
    yield
    config_settings.get_settings.cache_clear()


# --------------------------------------------------------------- both doors ---


def _chatgpt_oauth_body() -> dict[str, Any]:
    """The ChatGPT subscription door, through its own sender."""

    return build_chatgpt_oauth_request_body(_request(), reasoning=REASONING)


def _opencode_provider() -> Any:
    return create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["opencode"],
    )


def _shared_transport_body() -> dict[str, Any]:
    """The shared Responses transport (OpenCode Zen/Go), through its sender."""

    provider = _opencode_provider()
    body, _headers = provider._responses.build_body(
        provider.with_stand_ins(_request()),
        reasoning=REASONING,
        max_output_tokens=512,
    )
    # The session id behind the cache key is a per-process fingerprint, not a
    # property of the conversion (the same exclusion the reasoning-replay
    # contract makes). Everything else is compared.
    body.pop("prompt_cache_key", None)
    return body


DOORS = {
    "chatgpt_oauth": _chatgpt_oauth_body,
    "shared_responses_transport": _shared_transport_body,
}


# ------------------------------------------------------------ off-equality ---


def _baseline() -> dict[str, dict[str, Any]]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["bodies"]


@pytest.mark.parametrize("door", sorted(DOORS))
def test_with_the_setting_off_each_door_sends_v8_14_3_bytes(
    door: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off is not "a degraded mode": it is every byte v8.14.3 sent.

    The baseline was recorded on a detached worktree at ``179c19aa``
    (v8.14.3) with the same fixture and the same two senders. A diff here is
    a body that changed for a reason other than this setting.
    """

    _set(False, monkeypatch)
    assert DOORS[door]() == _baseline()[door]


@pytest.mark.parametrize("door", sorted(DOORS))
def test_with_the_setting_off_the_cell_is_false(
    door: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cell itself, named, so the baseline cannot hide a missing field."""

    _set(False, monkeypatch)
    assert DOORS[door]()[CELL] is False


# ----------------------------------------------------------- on-difference ---


def _without_the_cell(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != CELL}


@pytest.mark.parametrize("door", sorted(DOORS))
def test_with_the_setting_on_exactly_one_cell_moves(
    door: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``false`` -> ``true``, and not one other byte.

    Written as a whole-body comparison with the one cell removed from both
    sides, so a second moved cell -- a reordered key, a dropped ``include``, a
    changed ``store`` -- fails here rather than being lost in a diff nobody
    reads.
    """

    _set(False, monkeypatch)
    off = DOORS[door]()
    _set(True, monkeypatch)
    on = DOORS[door]()

    assert off[CELL] is False
    assert on[CELL] is True
    assert _without_the_cell(on) == _without_the_cell(off), (
        "8.15.0 may move parallel_tool_calls and nothing else"
    )
    # Key order is part of the body's identity (the request log compares the
    # serialised bytes), and the cell keeps its position: the builder emits it
    # in the same place whatever its value.
    assert list(on) == list(off)


@pytest.mark.parametrize("door", sorted(DOORS))
def test_the_on_body_is_the_off_body_with_one_cell_changed(
    door: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serialised diff names one cell, in the same position.

    This is the byte-level form of the assertion above: the two strings differ
    only where ``false`` became ``true``.
    """

    _set(False, monkeypatch)
    off = json.dumps(DOORS[door]())
    _set(True, monkeypatch)
    on = json.dumps(DOORS[door]())

    assert off != on
    assert on == off.replace(f'"{CELL}": false', f'"{CELL}": true'), (
        "the serialised bodies differ somewhere other than the one cell"
    )


# ------------------------------------------------- the builder's own default ---


def test_a_caller_that_says_nothing_still_gets_false() -> None:
    """The keyword default is a host choice and stays ``False``.

    Every caller that predates 8.15.0 -- and every equality baseline recorded
    against one -- depends on it. The setting is applied by the two senders,
    not by the builder.
    """

    body = build_responses_request_body(_request(), reasoning=REASONING)
    assert body[CELL] is False


def test_the_shipped_default_is_on() -> None:
    """The constant, the field and the accessor agree, and all three say on."""

    from my_claude_code.config.settings import (
        Settings,
        configured_responses_parallel_tool_calls,
    )

    assert RESPONSES_PARALLEL_TOOL_CALLS_DEFAULT is True
    field = Settings.model_fields["responses_parallel_tool_calls"]
    assert field.default is True
    assert field.validation_alias == PARALLEL_TOOL_CALLS_ENV
    assert configured_responses_parallel_tool_calls() is True


# ------------------------------------------------------ both doors, on wire ---


def _sse(payload: str) -> bytes:
    return payload.encode()


_RESPONSES_SSE = _sse(
    'event: response.created\ndata: {"type":"response.created","response":'
    '{"id":"resp_1","model":"gpt-5.6-terra","output":[]}}\n\n'
    'event: response.completed\ndata: {"type":"response.completed","response":'
    '{"id":"resp_1","model":"gpt-5.6-terra","output":[],"usage":'
    '{"input_tokens":3,"output_tokens":1}}}\n\n'
)


def _fake_upstream(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> list[dict[str, Any]]:
    """Record the JSON body a provider posts, and answer with one SSE stream.

    Patched at the client's own two seams -- ``build_request`` (which carries
    the body) and ``send`` (which returns the response) -- rather than through
    ``httpx.MockTransport``: this is the pattern
    ``tests/providers/test_chatgpt_oauth.py`` already uses for this provider,
    and it makes the assertion about the exact object that went to the wire.
    """

    from unittest.mock import AsyncMock, MagicMock

    posted: list[dict[str, Any]] = []

    def build_request(_method: str, _url: str, **kwargs: Any) -> Any:
        posted.append(json.loads(json.dumps(kwargs["json"])))
        return MagicMock()

    async def _raw_stream() -> Any:
        yield _RESPONSES_SSE

    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    response.aiter_raw = _raw_stream
    response.aclose = AsyncMock()

    monkeypatch.setattr(client, "build_request", build_request)
    monkeypatch.setattr(client, "send", AsyncMock(return_value=response))
    return posted


@pytest.mark.asyncio
async def test_the_chatgpt_oauth_wire_carries_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the ChatGPT subscription door actually posts.

    The body is read off the request the provider built, so this is the wire
    and not a builder called beside it.
    """

    from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider

    for expected in (True, False):
        _set(expected, monkeypatch)
        provider = ChatGPTOAuthProvider(
            ProviderConfig(
                api_key="test_token",
                base_url="https://chatgpt.example/backend-api",
                rate_limit=10,
                rate_window=60,
                max_concurrency=5,
                http_read_timeout=300.0,
                http_write_timeout=10.0,
                http_connect_timeout=60.0,
                proxy="",
            ),
            rate_limiter=passthrough_rate_limiter(),
            account_id="test_account_id",
        )
        posted = _fake_upstream(monkeypatch, provider._client)
        try:
            frames = [
                frame
                async for frame in provider.stream_response(
                    _request(), request_id="req-parallel", reasoning=REASONING
                )
            ]
        finally:
            await provider.cleanup()
        assert frames, "the fake upstream must have produced a stream"
        assert posted, "the provider posted nothing"
        assert posted[-1][CELL] is expected


@pytest.mark.asyncio
async def test_the_shared_transport_wire_carries_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the OpenCode Zen/Go Responses door actually posts."""

    for expected in (True, False):
        _set(expected, monkeypatch)
        provider = _opencode_provider()
        transport = provider._responses
        posted = _fake_upstream(monkeypatch, transport._client)
        shaped = provider.with_stand_ins(_request())
        body, headers = transport.build_body(
            shaped, reasoning=REASONING, max_output_tokens=512
        )
        frames = [
            frame
            async for frame in transport.stream(
                shaped,
                input_tokens=3,
                reasoning=REASONING,
                body=body,
                headers=headers,
                surface_label="responses",
            )
        ]
        await transport.aclose()
        assert frames, "the fake upstream must have produced a stream"
        assert posted, "the transport posted nothing"
        assert posted[-1][CELL] is expected
