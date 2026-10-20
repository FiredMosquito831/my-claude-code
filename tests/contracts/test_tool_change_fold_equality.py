"""Every request MCC accepted before the tool-change fold is unchanged by it.

RESUMED-SESSION-422 (spec ``INVESTIGATION-RESUMED-SESSION-422.md``, decisions
of 2026-10-20 16:03): accepting Claude Code 2.1.304's ``tool_addition`` /
``tool_removal`` blocks made two changes every request passes through -- the
content union of ``Message`` became tagged on ``type``, and both Anthropic
doors call the fold before routing. The user's standing rule is that a body
without the new blocks reaches every provider byte for byte as before.

``tool_change_fold_equality_baseline.json`` was written by THIS module run on
the code before the change (``fork/main`` = the commit named in the file),
with ``MCC_TOOL_FOLD_BASELINE_WRITE`` set. For each body of the corpus it holds
the sha256 and byte length of the parsed request (Messages and count_tokens),
the token estimate, and the body every converter sends upstream (or the
refusal it raises): native Anthropic, OpenCode's three doors (Chat with the
free-tier stand-ins, Responses, Messages), NVIDIA NIM and ChatGPT OAuth. The
fixed code must reproduce every byte, and the fold must hand each body back as
the very same object.

The corpus: the recorded Claude Code 2.1.258 request, the request shapes the
Zen free-tier goldens build, one request per recorded harness tool catalogue,
and invented bodies that carry every block type the schema accepted before,
text-only system messages, and the per-message fields Claude Code 2.1.304 adds
(``caller``, a system message's ``output_config``).

To refresh: copy this file and ``tests/providers/opencode_family_bodies.py``
to a base checkout and run it there with
``MCC_TOOL_FOLD_BASELINE_WRITE=<path to the baseline>``.
"""

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import my_claude_code.core.anthropic as anthropic_protocol
from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    TokenCountRequest,
    get_token_count,
)
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.nvidia_nim.request_options import build_nim_request_body
from tests.providers.opencode_family_bodies import (
    CLAUDE_FULL,
    FREE,
    OPENCODE_NATIVE,
    REASONING,
    canonical,
    opencode_bodies,
    tool_request,
)

BASELINE = Path(__file__).with_name("tool_change_fold_equality_baseline.json")
WRITE_BASELINE = os.environ.get("MCC_TOOL_FOLD_BASELINE_WRITE", "")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
#: Looked up rather than imported so this module also runs on the code before
#: the fold existed, which is where the baseline is written.
FOLD = getattr(anthropic_protocol, "fold_tool_changes", None)


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


def _every_block_body() -> dict[str, Any]:
    """One invented conversation holding every block the schema accepted."""
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
    return {
        "model": FREE,
        "max_tokens": 512,
        "stream": True,
        "system": [
            {"type": "text", "text": "invented identity"},
            {
                "type": "text",
                "text": "invented instructions",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "output_config": {"effort": "high"},
        "metadata": {"user_id": "invented-user"},
        "tool_choice": {"type": "auto"},
        "tools": [_tool("Bash"), _tool("Read")],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "invented question"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": png,
                        },
                    },
                    {
                        "type": "text",
                        "text": "invented follow-up",
                        "citations": [],
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
            {
                "role": "system",
                "output_config": {"effort": "low"},
                "content": [
                    {"type": "text", "text": "SessionStart: invented hook text"}
                ],
            },
            {"role": "system", "content": "invented plain system note"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "invented reasoning",
                        "signature": "invented-signature",
                    },
                    {"type": "redacted_thinking", "data": "invented-redacted"},
                    {"type": "text", "text": "invented plan"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Bash",
                        "input": {"command": "ls"},
                        "caller": {"type": "direct"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_02",
                        "name": "Read",
                        "input": {"command": "a.txt"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": "a.txt\nb.txt",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_02",
                        "is_error": True,
                        "content": [
                            {"type": "text", "text": "invented error"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": png,
                                },
                            },
                        ],
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "invented answer"}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": "invented document",
                        },
                    },
                    {"type": "text", "text": "invented last question"},
                ],
            },
        ],
    }


def _server_tool_body() -> dict[str, Any]:
    """The server-tool blocks: accepted by the schema, refused by some doors."""
    return {
        "model": FREE,
        "max_tokens": 256,
        "stream": True,
        "tools": [_tool("Bash")],
        "messages": [
            {"role": "user", "content": "invented search request"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_01",
                        "name": "web_search",
                        "input": {"query": "invented"},
                    },
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_01",
                        "content": [{"type": "web_search_result", "url": "x"}],
                    },
                    {
                        "type": "web_fetch_tool_result",
                        "tool_use_id": "srvtoolu_01",
                        "content": {"type": "web_fetch_result", "url": "x"},
                    },
                    {"type": "text", "text": "invented summary"},
                ],
            },
            {"role": "user", "content": "invented thanks"},
        ],
    }


def _resumed_without_tool_changes() -> dict[str, Any]:
    """A Claude Code 2.1.304-shaped resume, minus the tool-change blocks."""
    return {
        "model": FREE,
        "max_tokens": 32000,
        "stream": True,
        "system": [{"type": "text", "text": "invented identity"}],
        "tools": [_tool(name) for name in ("Agent", "Bash", "Read", "Edit")],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "invented"}]},
            {
                "role": "system",
                "content": [{"type": "text", "text": "SessionStart: invented"}],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_09",
                        "name": "Bash",
                        "input": {"command": "pwd"},
                        "caller": {"type": "direct"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_09", "content": "/"}
                ],
            },
            {
                "role": "system",
                "output_config": {"effort": "medium"},
                "content": [
                    {
                        "type": "text",
                        "text": "invented resume note",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": "invented next question"},
        ],
    }


def _catalogue_body(path: Path) -> dict[str, Any]:
    catalogue = json.loads(path.read_text(encoding="utf-8"))
    return {
        "model": FREE,
        "max_tokens": 64,
        "stream": True,
        "system": "invented system prompt",
        "tools": catalogue["tools"],
        "messages": [{"role": "user", "content": "invented question"}],
    }


def corpus() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every (case, body) the fix must leave exactly as it was."""
    recorded = json.loads(
        (FIXTURES / "anthropic_oauth" / "claude_code_session.json").read_text(
            encoding="utf-8"
        )
    )
    yield "claude_code_2_1_258_recorded", recorded["body"]
    zen_shapes = {
        "zen_claude_code_free": tool_request(
            FREE, CLAUDE_FULL, history=["Bash", "Read", CLAUDE_FULL[-1]]
        ),
        "zen_sub_request_free": tool_request(FREE, ["Bash"]),
        "zen_tool_less_free": tool_request(FREE, []),
        "zen_opencode_native": tool_request(FREE, OPENCODE_NATIVE, history=["bash"]),
    }
    for case, request in zen_shapes.items():
        yield case, request.model_dump(mode="json", exclude_none=True)
    for path in sorted((FIXTURES / "tool_catalogues").glob("*.json")):
        yield f"catalogue_{path.stem}", _catalogue_body(path)
    yield "every_block", _every_block_body()
    yield "server_tool_blocks", _server_tool_body()
    yield "resumed_without_tool_changes", _resumed_without_tool_changes()


def _outcome(build: Any) -> Any:
    """The body a converter builds, or the refusal it raises, as data."""
    try:
        return build()
    except Exception as exc:
        return {"refused": type(exc).__name__, "message": str(exc)}


def surfaces(body: dict[str, Any]) -> dict[str, str]:
    """Everything one body becomes, as the exact bytes compared."""
    request = MessagesRequest.model_validate(body)
    counted = TokenCountRequest.model_validate(body)
    out: dict[str, Any] = {
        "parsed": request.model_dump(mode="json"),
        "parsed_count_tokens": counted.model_dump(mode="json"),
        "tokens": get_token_count(request.messages, request.system, request.tools),
        "native_anthropic": _outcome(
            lambda: build_anthropic_messages_body(request, reasoning=REASONING)
        ),
        "nvidia_nim": _outcome(
            lambda: build_nim_request_body(
                request, NimSettings(), reasoning=REASONING, provider_id="nvidia_nim"
            )
        ),
        "chatgpt_oauth": _outcome(
            lambda: build_chatgpt_oauth_request_body(request, reasoning=REASONING)
        ),
    }
    opencode = _outcome(lambda: opencode_bodies("opencode", request))
    if isinstance(opencode, dict) and "refused" not in opencode:
        for door, built in opencode.items():
            out[f"opencode_{door}"] = _session_minted(built)
    else:
        out["opencode"] = opencode
    return {name: _digest(canonical(value)) for name, value in out.items()}


def _session_minted(body: Any) -> Any:
    """``body`` with OpenCode's per-session ``prompt_cache_key`` held still.

    Since 7.92.2 the Responses door's cache key mirrors the ``x-opencode-
    session`` id minted on the client's clock, so it differs per process by
    design (``test_opencode_free_tier_harness_families._session_minted``). Its
    shape is checked; every other byte is compared.
    """
    if not isinstance(body, dict) or "prompt_cache_key" not in body:
        return body
    minted = body["prompt_cache_key"]
    assert isinstance(minted, str)
    assert minted.startswith("ses_")
    assert len(minted) == len("ses_") + 26
    return {**body, "prompt_cache_key": "ses_<minted per session>"}


def _digest(text: str) -> str:
    """The exact bytes, pinned by digest and length (the bodies total 1.4 MB)."""
    raw = text.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()} bytes:{len(raw)}"


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
    cases = {case: surfaces(body) for case, body in corpus()}
    document = {
        "_what": "RESUMED-SESSION-422 equality baseline; see the test module.",
        "_written_on": _base_label(),
        "cases": cases,
    }
    Path(WRITE_BASELINE).write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def _baseline() -> dict[str, dict[str, str]]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
@pytest.mark.parametrize("case", [case for case, _body in corpus()])
def test_every_surface_is_byte_identical_to_the_code_before(case: str) -> None:
    body = dict(corpus())[case]
    assert surfaces(body) == _baseline()[case]


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
def test_the_corpus_is_the_one_the_baseline_was_written_from() -> None:
    assert [case for case, _body in corpus()] == list(_baseline())


@pytest.mark.skipif(bool(WRITE_BASELINE), reason="baseline is being written")
@pytest.mark.parametrize("case", [case for case, _body in corpus()])
def test_the_fold_hands_every_corpus_body_back_as_the_same_object(case: str) -> None:
    assert FOLD is not None
    body = dict(corpus())[case]
    request = MessagesRequest.model_validate(body)
    counted = TokenCountRequest.model_validate(body)
    assert FOLD(request).request is request
    assert FOLD(counted).request is counted


def test_no_corpus_body_carries_a_tool_change() -> None:
    # The corpus is "what was accepted before": a tool-change block in it
    # would make the identity above a claim about the wrong bodies.
    for case, body in corpus():
        for message in body["messages"]:
            content = message.get("content")
            if isinstance(content, list):
                types = {block.get("type") for block in content}
                assert not types & {"tool_addition", "tool_removal"}, case
