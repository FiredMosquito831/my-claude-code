"""Every body the OpenAI-format converter built before 7.103.2 is unchanged.

SYSTEM-MESSAGE-NO-TEXT-400 (spec ``INVESTIGATION-SYSTEM-MESSAGE-NO-TEXT-400.md``,
decisions of 2026-10-21 13:21): the shared converter now leaves out an inline
system message with no content (``content: []``, Claude Code's per-turn effort
marker) instead of refusing the whole request. The user's standing rule is that
every body that converted before reaches every provider byte for byte as
before, and Anthropic-format routes do not change at all.

``empty_system_message_equality_baseline.json`` was written by THIS module run
on the code before the change (``fork/main`` = the commit named in the file),
with ``MCC_EMPTY_SYSTEM_BASELINE_WRITE`` set. For each case it holds, per
dialect, the digest of the body built or the refusal raised: the base Chat
builder, every declarative Chat profile, DeepSeek, Mistral, Command Code's
Chat side, NVIDIA NIM, Gemini, Google's OpenAI endpoint, OpenRouter,
Cloudflare, the Responses builder and ChatGPT OAuth (with the notes each
records), OpenCode's three doors, and native Anthropic.

What the fixed code must reproduce, per case kind:

* ``unchanged`` (the 7.102.2 fold corpus, and every system-message shape that
  carries text, even ``""`` or empty text blocks): every surface byte-identical;
* ``marker`` (a request carrying the empty message): every surface the base
  built is byte-identical, and every OpenAI-format surface the base refused now
  yields exactly what the base produced for the same request with the empty
  messages taken out (its ``__without_markers`` twin);
* ``nothing_left`` (only empty system messages): the same refusal, word for
  word, on every OpenAI-format surface.

The Anthropic-format surfaces (native, OpenCode's Messages door) never
refused and must be byte-identical in every case (decision Q3).

To refresh: copy this file, ``test_tool_change_fold_equality.py`` and
``tests/providers/opencode_family_bodies.py`` to a base checkout and run it
there with ``MCC_EMPTY_SYSTEM_BASELINE_WRITE=<path to the baseline>``.
"""

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest

import my_claude_code.core.anthropic as anthropic_protocol
from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic import MessagesRequest, build_base_request_body
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.cloudflare import CloudflareProvider
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.gemini import GeminiProvider
from my_claude_code.providers.google_openai import GoogleOpenAIProvider
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from my_claude_code.providers.open_router import OpenRouterProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_responses import build_responses_request_body
from tests.contracts.test_tool_change_fold_equality import (
    _session_minted,
)
from tests.contracts.test_tool_change_fold_equality import (
    corpus as fold_corpus,
)
from tests.providers.opencode_family_bodies import (
    FREE,
    REASONING,
    _messages_wire_body,
    canonical,
    opencode_provider,
)
from tests.providers.support import passthrough_rate_limiter

BASELINE = Path(__file__).with_name("empty_system_message_equality_baseline.json")
WRITE_BASELINE = os.environ.get("MCC_EMPTY_SYSTEM_BASELINE_WRITE", "")
#: Looked up rather than imported so this module also runs on the code before
#: the change, which is where the baseline is written.
FOLD = getattr(anthropic_protocol, "fold_tool_changes", None)
NOTE_KEY = getattr(anthropic_protocol, "EMPTY_SYSTEM_MESSAGES_DROPPED", None)
NOTE = getattr(anthropic_protocol, "empty_system_messages_note", None)

#: Surfaces that speak the Anthropic format: never refused, never changed.
ANTHROPIC_FORMAT = frozenset({"native_anthropic", "opencode_messages"})
TWIN = "__without_markers"

EFFORT = {"effort": "high"}
MARKER: dict[str, Any] = {"role": "system", "content": [], "output_config": EFFORT}
SUMMARY: dict[str, Any] = {"role": "user", "content": "invented compact summary"}
REPLY: dict[str, Any] = {
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
}
ASK: dict[str, Any] = {
    "role": "user",
    "content": [{"type": "text", "text": "invented next ask"}],
}


def _tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"the {name} tool",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }


def _body(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "model": FREE,
        "max_tokens": 512,
        "stream": True,
        "system": [{"type": "text", "text": "invented identity"}],
        "tools": [_tool("Bash"), _tool("Read")],
        "messages": messages,
        **extra,
    }


def _tool_run(tool_id: str = "toolu_1") -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "a.py"}
            ],
        },
    ]


def _tool_change(kind: str, names: list[str]) -> list[dict[str, Any]]:
    return [{"type": kind, "tool": {"name": name}} for name in names]


def _claude_code_2_1_306_shape() -> dict[str, Any]:
    """The recorded layout of the failing requests (spec §1.3), invented text."""
    reminders = [
        {"role": "system", "content": [{"type": "text", "text": f"reminder {i}"}]}
        for i in range(3)
    ]
    return _body(
        [
            {"role": "user", "content": "invented compact summary " * 40},
            MARKER,
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"invented part {i}"} for i in range(7)
                ],
            },
            {
                "role": "system",
                "output_config": EFFORT,
                "content": [
                    {"type": "text", "text": "invented deferred tools note"},
                    *_tool_change("tool_addition", ["Agent", "Edit", "Glob"]),
                ],
            },
            *reminders,
            {
                "role": "system",
                "output_config": EFFORT,
                "content": [
                    {"type": "text", "text": "invented removal note"},
                    *_tool_change("tool_removal", ["Agent", "Edit", "Glob"]),
                ],
            },
        ]
    )


def _system(content: Any, **extra: Any) -> dict[str, Any]:
    return {"role": "system", "content": content, **extra}


def probe_cases() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(case, kind, body) for every shape the spec probed (§1.6) and more."""
    image = {
        "type": "image",
        "source": {"type": "url", "url": "https://example.invalid/a.png"},
    }
    markers = {
        "marker_after_summary": _body([SUMMARY, MARKER, REPLY, ASK]),
        "marker_without_output_config": _body([SUMMARY, _system([]), REPLY, ASK]),
        "marker_trailing": _body([SUMMARY, REPLY, ASK, MARKER]),
        "marker_after_tool_result_run": _body([SUMMARY, *_tool_run(), MARKER, ASK]),
        "marker_twice": _body([SUMMARY, MARKER, REPLY, ASK, MARKER]),
        "marker_with_top_level_string_system": _body(
            [SUMMARY, MARKER, REPLY, ASK], system="invented string identity"
        ),
        "marker_before_an_image_system_block": _body(
            [SUMMARY, MARKER, _system([image]), ASK]
        ),
        "claude_code_2_1_306_shape": _claude_code_2_1_306_shape(),
    }
    for case, body in markers.items():
        yield case, "marker", body
        yield f"{case}{TWIN}", "unchanged", _without_markers(body)
    unchanged = {
        "empty_text_block": _body(
            [SUMMARY, _system([{"type": "text", "text": ""}]), REPLY, ASK]
        ),
        "two_empty_text_blocks": _body(
            [
                SUMMARY,
                _system([{"type": "text", "text": ""}, {"type": "text", "text": ""}]),
                REPLY,
                ASK,
            ]
        ),
        "only_tool_changes": _body(
            [
                SUMMARY,
                _system(_tool_change("tool_addition", ["Agent"])),
                REPLY,
                ASK,
            ]
        ),
        "text_tool_change_output_config": _body(
            [
                SUMMARY,
                _system(
                    [
                        {"type": "text", "text": "invented note"},
                        *_tool_change("tool_removal", ["Read"]),
                    ],
                    output_config=EFFORT,
                ),
                REPLY,
                ASK,
            ]
        ),
        "empty_text_cache_control_output_config": _body(
            [
                SUMMARY,
                _system(
                    [
                        {
                            "type": "text",
                            "text": "",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    output_config=EFFORT,
                ),
                REPLY,
                ASK,
            ]
        ),
        "string_clear_at": _body(
            [
                SUMMARY,
                _system("invented reminder", clear_at="next_user_message"),
                REPLY,
                ASK,
            ]
        ),
        "empty_string_output_config": _body(
            [SUMMARY, _system("", output_config=EFFORT), REPLY, ASK]
        ),
        "string_output_config": _body(
            [SUMMARY, _system("invented reminder", output_config=EFFORT), REPLY, ASK]
        ),
        "image_system_block": _body([SUMMARY, _system([image]), ASK]),
        "empty_transcript": _body([]),
        "empty_user_turn_only": _body([{"role": "user", "content": []}]),
    }
    for case, body in unchanged.items():
        yield case, "unchanged", body
    nothing_left = {
        "only_one_marker": _body([MARKER]),
        "only_two_markers": _body([MARKER, MARKER]),
        "only_a_bare_marker": _body([_system([])]),
        "only_a_marker_and_an_empty_user_turn": _body(
            [MARKER, {"role": "user", "content": []}]
        ),
        "only_a_marker_no_top_level_system": {
            "model": FREE,
            "max_tokens": 64,
            "messages": [MARKER],
        },
    }
    for case, body in nothing_left.items():
        yield case, "nothing_left", body


def _is_marker(message: dict[str, Any]) -> bool:
    return message.get("role") == "system" and message.get("content") == []


def _without_markers(body: dict[str, Any]) -> dict[str, Any]:
    return {
        **body,
        "messages": [m for m in body["messages"] if not _is_marker(m)],
    }


def corpus() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Every (case, kind, body): the 7.102.2 corpus, then the probe shapes."""
    for case, body in fold_corpus():
        yield f"fold_{case}", "unchanged", body
    yield from probe_cases()


def _config(base_url: str = "https://chat.invalid/v1") -> ProviderConfig:
    return ProviderConfig(api_key="sk-test", base_url=base_url)


@cache
def _chat_builders() -> dict[str, Callable[[MessagesRequest], Any]]:
    """Every Chat Completions builder, each as its own provider calls it.

    Built once per process: a provider holds an HTTP client, and the builders
    are pure functions of the request.
    """
    limiter = passthrough_rate_limiter()
    builders: dict[str, Callable[[MessagesRequest], Any]] = {
        "chat_base": build_base_request_body,
    }
    for provider_id in sorted(OPENAI_CHAT_PROFILES):
        provider = create_openai_chat_provider(provider_id, _config(), limiter)
        builders[f"chat_{provider_id}"] = lambda request, provider=provider: (
            provider._build_request_body(request, reasoning=REASONING)
        )
    named = {
        "deepseek": DeepSeekProvider(_config(), rate_limiter=limiter),
        "mistral": MistralProvider(_config(), rate_limiter=limiter),
        "gemini": GeminiProvider(_config(), rate_limiter=limiter),
        "open_router": OpenRouterProvider(_config(), rate_limiter=limiter),
        "cloudflare": CloudflareProvider(
            _config(), account_id="acct", rate_limiter=limiter
        ),
        "google_openai": GoogleOpenAIProvider(
            _config(),
            profile=GENERIC_OPENAI_PROFILE,
            rate_limiter=limiter,
            provider_id="google_openai",
        ),
        "commandcode_chat": CommandCodeProvider(
            _config(), rate_limiter=limiter
        )._openai,
    }
    for name, provider in named.items():
        builders[name] = lambda request, provider=provider: (
            provider._build_request_body(request, reasoning=REASONING)
        )
    builders["nvidia_nim"] = lambda request: build_nim_request_body(
        request, NimSettings(), reasoning=REASONING, provider_id="nvidia_nim"
    )
    return builders


def _outcome(build: Callable[[], Any]) -> str:
    """The digest of what one builder produced, or the refusal it raised."""
    try:
        built = build()
    except Exception as exc:
        return f"refused {type(exc).__name__}: {exc}"
    raw = canonical(built).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()} bytes:{len(raw)}"


def _notes_without_ours(notes: dict[str, str]) -> dict[str, str]:
    """What the base recorded: every note but the one this change adds."""
    return {key: value for key, value in notes.items() if key != NOTE_KEY}


def _responses_with_notes(
    build: Callable[[dict[str, str]], Any],
) -> tuple[str, str]:
    notes: dict[str, str] = {}
    body = _outcome(lambda: build(notes))
    if body.startswith("refused"):
        return body, body
    return body, _outcome(lambda: _notes_without_ours(notes))


def surfaces(body: dict[str, Any]) -> dict[str, str]:
    """Everything one body becomes on every OpenAI- and Anthropic-format door."""
    request = MessagesRequest.model_validate(body)
    if FOLD is not None:
        # What both Anthropic doors do before routing (7.102.2).
        request = FOLD(request).request
    out: dict[str, str] = {}
    for name, build in _chat_builders().items():
        out[name] = _outcome(lambda build=build: build(request))
    out["responses"], out["responses_wire_notes"] = _responses_with_notes(
        lambda notes: build_responses_request_body(
            request, reasoning=REASONING, wire_notes=notes
        )
    )
    out["chatgpt_oauth"], out["chatgpt_oauth_wire_notes"] = _responses_with_notes(
        lambda notes: build_chatgpt_oauth_request_body(
            request, reasoning=REASONING, wire_notes=notes
        )
    )
    out["native_anthropic"] = _outcome(
        lambda: build_anthropic_messages_body(request, reasoning=REASONING)
    )
    opencode = opencode_provider("opencode")
    door_request = opencode.with_stand_ins(request)
    out["opencode_chat"] = _outcome(
        lambda: opencode._build_request_body(door_request, reasoning=REASONING)
    )
    out["opencode_responses"] = _outcome(
        lambda: _session_minted(
            opencode._responses.build_body(
                door_request, reasoning=REASONING, max_output_tokens=64
            )[0]
        )
    )
    out["opencode_messages"] = _outcome(
        lambda: asyncio.run(_messages_wire_body(opencode, door_request))
    )
    return out


def _base_label() -> str:
    head = subprocess.run(
        ["git", "log", "-1", "--format=%h %s"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    return head.stdout.strip()


@pytest.mark.skipif(not WRITE_BASELINE, reason="writes the baseline on a base checkout")
def test_write_baseline() -> None:
    cases = {case: surfaces(body) for case, _kind, body in corpus()}
    document = {
        "_what": "SYSTEM-MESSAGE-NO-TEXT-400 equality baseline; see the test module.",
        "_written_on": _base_label(),
        "cases": cases,
    }
    Path(WRITE_BASELINE).write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def _baseline() -> dict[str, dict[str, str]]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]


CASES = [(case, kind) for case, kind, _body in corpus()]


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
def test_the_corpus_is_the_one_the_baseline_was_written_from() -> None:
    assert [case for case, _kind in CASES] == list(_baseline())


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
@pytest.mark.parametrize(("case", "kind"), CASES)
def test_every_body_that_converted_before_is_byte_identical(
    case: str, kind: str
) -> None:
    body = {name: raw for name, _kind, raw in corpus()}[case]
    base = _baseline()
    before = base[case]
    now = surfaces(body)
    assert list(now) == list(before)
    for surface, was in before.items():
        if surface in ANTHROPIC_FORMAT or kind != "marker":
            # Anthropic-format doors never change (Q3); every other case is
            # either unchanged or still refused with the very same words (Q2).
            assert now[surface] == was, (case, surface)
            continue
        if not was.startswith("refused"):
            assert now[surface] == was, (case, surface)
            continue
        # Refused before only because of the empty message: now exactly what
        # the same request without it produced before.
        assert now[surface] == base[f"{case}{TWIN}"][surface], (case, surface)


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
def test_the_change_is_exercised_and_the_refusal_kept() -> None:
    """Guards against a corpus that would let a no-op pass."""
    base = _baseline()
    kinds = dict(CASES)
    refused_before = {
        case
        for case, kind in kinds.items()
        if kind == "marker" and base[case]["chat_base"].startswith("refused")
    }
    assert refused_before == {case for case, kind in kinds.items() if kind == "marker"}
    for case, kind in kinds.items():
        if kind == "nothing_left":
            assert base[case]["chat_base"] == (
                "refused OpenAIConversionError: OpenAI chat conversion requires an "
                "inline Anthropic system message to contain text."
            ), case
    assert not any(
        _is_marker(message)
        for case, kind, body in corpus()
        if kind == "unchanged"
        for message in body["messages"]
    )


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
@pytest.mark.parametrize(("case", "kind"), CASES)
def test_the_note_is_recorded_only_where_something_was_left_out(
    case: str, kind: str
) -> None:
    assert NOTE is not None
    assert NOTE_KEY is not None
    body = {name: raw for name, _kind, raw in corpus()}[case]
    request = MessagesRequest.model_validate(body)
    if FOLD is not None:
        request = FOLD(request).request
    note = NOTE(request.messages)
    notes: dict[str, str] = {}
    try:
        build_responses_request_body(request, reasoning=REASONING, wire_notes=notes)
    except Exception:
        # Only where the base refused too (for a marker case: refused the
        # request without the marker), so a refusal is never new.
        base = _baseline()
        reference = base[f"{case}{TWIN}"] if kind == "marker" else base[case]
        assert reference["responses"].startswith("refused"), case
        return
    if kind == "marker":
        assert note
        assert notes[NOTE_KEY] == note[NOTE_KEY]
    else:
        assert note == {}
        assert NOTE_KEY not in notes
