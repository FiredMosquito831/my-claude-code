"""OpenCode's free tier reads the tool catalogue, so MCC sends OpenCode's.

The defect: since some moment in the twelve silent hours before 2026-09-18
00:52 UTC, the OpenCode Zen free tier answers HTTP 403 ``FreeTierError`` to
every request whose tool names are not its own client's. MCC's identity
headers never changed -- byte-identical 6.74.0 -> 7.26.1 -- and no MCC release
falls on the boundary. The gate moved server-side and into the *body*.

Measured 2026-09-18, MCC's headers untouched in every row
(``specs/INVESTIGATION-ZEN-403-FREETIER.md``):

    E   tools bash, edit, glob, grep, read      200, SSE stream
    F   tools Bash, Edit, Glob, Grep, Read      403 FreeTierError
    A   no tools at all                         403 FreeTierError
    H   both casings in one catalogue           500 server_error, twice

What these tests hold:

* the scope is **data** -- a free tag, an operator's roster, a published price
  of zero -- and a paid model on the same host, ``chatgpt_oauth`` and an
  ordinary Chat Completions provider all send byte-identical bodies;
* the five names OpenCode has round-trip exactly, every other name goes
  through the 7.18.1 alias codec, and a name that would collide
  case-insensitively with a catalogue spelling is aliased away (probe H);
* the answer decodes back on all three surfaces before the client sees it;
* ``OPENCODE_CLIENT_IDENTITY=mcc`` turns the whole impersonation off;
* a ``FreeTierError`` is its own failure kind, benches nothing, and does not
  stop the chain;
* nothing is ever appended, and a tool-less request is left tool-less --
  which is what the real client sends on its own sub-requests.
"""

import json
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.application.route_health import failure_counts_toward_bench
from my_claude_code.config.settings import (
    Settings,
    configured_opencode_free_tier_models,
    get_settings,
)
from my_claude_code.core.anthropic.errors import anthropic_error_type_for_failure
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import (
    OpenAIToolNameCodec,
    decode_anthropic_sse_event,
    encode_anthropic_body_tool_names,
)
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.gemini_api.errors import gemini_status_for_failure
from my_claude_code.core.openai_common.errors import openai_error_type_for_failure
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.credential_rotation import (
    credential_failure_class,
    error_justifies_rotation,
)
from my_claude_code.providers.failure_policy import (
    classify_provider_failure,
    is_free_tier_error,
)
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.opencode_catalogue import (
    OPENCODE_FREE_TIER_CATALOGUE,
    OPENCODE_TOOL_CATALOGUE,
    FreeTierToolCatalogue,
    carries_free_tag,
)
from my_claude_code.providers.openai_chat.opencode_identity import (
    USER_AGENT_HEADER,
    opencode_constant_headers,
    opencode_user_agent,
)
from my_claude_code.providers.openai_chat.tool_calls import OpenAIToolCallAssembler
from my_claude_code.providers.openai_responses import ResponsesStreamConverter
from tests.providers.support import passthrough_rate_limiter

REASONING = ReasoningPolicy.on()
FREE = "muse-spark-1.3-contributor-free"
PAID = "claude-sonnet-4-5"
LONG = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_console_messages"

#: What OpenCode's free tier answers, verbatim, 20 times in the request log.
FREE_TIER_BODY = {
    "error": {
        "type": "FreeTierError",
        "message": "OpenCode's free tier can only be used from within OpenCode",
    }
}


def _request(model: str, *, tools: list[str] | None = None) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "list the files"}],
    }
    if tools is not None:
        payload["tools"] = [
            {
                "name": name,
                "description": f"the {name} tool",
                "input_schema": {"type": "object"},
            }
            for name in tools
        ]
    return MessagesRequest.model_validate(payload)


def _provider(provider_id: str = "opencode") -> Any:
    return create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )


def _wire(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _responses_body(provider: Any, request: MessagesRequest) -> dict[str, Any]:
    body, _headers = provider._responses.build_body(
        request, reasoning=REASONING, max_output_tokens=64
    )
    return body


def _claude_code_tools() -> list[str]:
    """The five OpenCode has, two it does not, and one 68-character MCP name."""
    return ["Bash", "Read", "Edit", "Glob", "Grep", "Write", "WebFetch", LONG]


def _classify(error: Exception):
    return classify_provider_failure(
        error,
        provider_name="OPENCODE",
        read_timeout_s=None,
        request_id=None,
        mark_rate_limited=lambda _seconds: None,
    )


def _status_error(status: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://opencode.ai/zen/v1/responses")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# -- the declaration and its scope --------------------------------------------


def test_only_the_two_opencode_profiles_declare_a_catalogue() -> None:
    declared = sorted(
        name
        for name, profile in OPENAI_CHAT_PROFILES.items()
        if profile.free_tier_tool_catalogue is not None
    )
    assert declared == ["opencode", "opencode_go"]


def test_both_opencode_profiles_share_one_declaration() -> None:
    """Zen and Go cannot drift apart, for the reason the identity cannot."""
    assert (
        OPENAI_CHAT_PROFILES["opencode"].free_tier_tool_catalogue
        is OPENAI_CHAT_PROFILES["opencode_go"].free_tier_tool_catalogue
    )


def test_the_catalogue_is_the_spellings_the_shipped_client_sends() -> None:
    """Cited from ``opencode-ai@1.18.31``'s own bundle, not from a blog post."""
    assert dict(OPENCODE_TOOL_CATALOGUE) == {
        "Bash": "bash",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Read": "read",
    }


@pytest.mark.parametrize(
    ("model", "tagged"),
    [
        ("muse-spark-1.3-contributor-free", True),
        ("opencode/deepseek-v4-flash-free", True),
        ("some-model:free", True),
        ("some-model-free-preview", True),
        # The tag has to be a tag, not the first syllable of a longer word.
        ("glm-5.3-freeform", False),
        ("big-pickle", False),
        ("claude-sonnet-4-5", False),
    ],
)
def test_the_free_tag_is_read_as_a_tag(model: str, tagged: bool) -> None:
    assert carries_free_tag(model) is tagged


def test_an_untagged_free_model_comes_from_the_operator_setting() -> None:
    """``big-pickle`` is free and its name does not say so."""
    assert OPENCODE_FREE_TIER_CATALOGUE.applies_to("big-pickle") is True
    assert OPENCODE_FREE_TIER_CATALOGUE.applies_to("claude-sonnet-4-5") is False


def test_a_model_priced_at_zero_on_this_host_is_in_scope() -> None:
    """The third door, for a free model nobody tagged and nobody listed."""
    assert (
        OPENCODE_FREE_TIER_CATALOGUE.applies_to("mystery-model", zero_cost=True) is True
    )
    assert (
        OPENCODE_FREE_TIER_CATALOGUE.applies_to("mystery-model", zero_cost=False)
        is False
    )


def test_the_setting_is_read_per_request_not_captured_at_import() -> None:
    """A dashboard save has to take effect without a release.

    Asserted on the mechanism rather than through the environment: the roster
    is held as a callable precisely so that a value captured when the profile
    table was built could not outlive a save, and calling it twice across a
    change is what says so.
    """

    roster: list[str] = []
    catalogue = FreeTierToolCatalogue(extra_models=lambda: tuple(roster))
    assert catalogue.applies_to("mystery-model") is False
    roster.append("mystery-model")
    assert catalogue.applies_to("mystery-model") is True
    # And the shipped instance reads the setting through that same door.
    assert (
        OPENCODE_FREE_TIER_CATALOGUE.extra_models
        is configured_opencode_free_tier_models
    )


def test_the_setting_is_normalised_once_when_it_is_stored() -> None:
    settings = Settings.model_validate(
        {"OPENCODE_FREE_TIER_MODELS": " Big-Pickle , ,big-pickle, X "}
    )
    assert settings.opencode_free_tier_models == "big-pickle,x"


# -- outbound: the five are renamed, everything else is aliased ----------------


def test_a_free_model_sends_opencodes_own_spellings() -> None:
    body = _responses_body(_provider(), _request(FREE, tools=_claude_code_tools()))
    names = [tool["name"] for tool in body["tools"]]

    assert names[:5] == ["bash", "read", "edit", "glob", "grep"]
    # Untouched: OpenCode has no counterpart and the name is already legal.
    assert "Write" in names
    assert "WebFetch" in names
    # Aliased by the 7.18.1 codec, because 68 characters is past the ceiling.
    assert LONG not in names
    assert all(len(name) <= 64 for name in names)


def test_nothing_is_ever_appended() -> None:
    """Probe H: decoys collide case-insensitively and the model answers 500."""
    tools = _claude_code_tools()
    body = _responses_body(_provider(), _request(FREE, tools=tools))
    assert len(body["tools"]) == len(tools)


def test_no_two_wire_names_collide_case_insensitively() -> None:
    """The rule probe H's 500 wrote down, asserted on the body that goes out."""
    tools = [*_claude_code_tools(), "bash", "READ"]
    body = _responses_body(_provider(), _request(FREE, tools=tools))
    folded = [tool["name"].casefold() for tool in body["tools"]]
    assert len(folded) == len(set(folded))


def test_a_client_tool_that_shadows_a_catalogue_name_is_aliased_away() -> None:
    """``Bash`` owns ``bash``; a literal ``bash`` may not also have it.

    The rule is a pure function of the one name, so it holds whether or not
    ``Bash`` is in the same request -- which is what keeps the alias stable
    across a turn that adds a tool.
    """
    codec = OpenAIToolNameCodec.from_names(["bash"], catalogue=OPENCODE_TOOL_CATALOGUE)
    with_both = OpenAIToolNameCodec.from_names(
        ["Bash", "bash"], catalogue=OPENCODE_TOOL_CATALOGUE
    )
    assert codec.encode("bash") != "bash"
    assert codec.encode("bash") == with_both.encode("bash")
    assert with_both.encode("Bash") == "bash"


def test_every_name_round_trips_exactly() -> None:
    names = [*_claude_code_tools(), "bash", "READ"]
    codec = OpenAIToolNameCodec.from_names(names, catalogue=OPENCODE_TOOL_CATALOGUE)
    assert [codec.decode(codec.encode(name)) for name in names] == names


def test_an_alias_is_the_same_with_and_without_the_catalogue() -> None:
    """The prompt-cache prefix does not move because a catalogue arrived."""
    plain = OpenAIToolNameCodec.from_names(_claude_code_tools())
    catalogued = OpenAIToolNameCodec.from_names(
        _claude_code_tools(), catalogue=OPENCODE_TOOL_CATALOGUE
    )
    assert plain.encode(LONG) == catalogued.encode(LONG)


def test_a_tool_added_mid_session_moves_no_existing_name() -> None:
    first = OpenAIToolNameCodec.from_names(
        ["Bash", LONG], catalogue=OPENCODE_TOOL_CATALOGUE
    )
    later = OpenAIToolNameCodec.from_names(
        ["Bash", LONG, "Write", "Glob"], catalogue=OPENCODE_TOOL_CATALOGUE
    )
    assert first.encode("Bash") == later.encode("Bash") == "bash"
    assert first.encode(LONG) == later.encode(LONG)


def test_a_sub_request_shaped_turn_is_translated_too() -> None:
    """Title, summary and compaction turns are what 403s upstream."""
    request = MessagesRequest.model_validate(
        {
            "model": FREE,
            "max_tokens": 16,
            "system": "You are a title generator. You output ONLY a thread title.",
            "messages": [{"role": "user", "content": "summarise"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "run",
                    "input_schema": {"type": "object"},
                }
            ],
        }
    )
    body = _responses_body(_provider(), request)
    assert [tool["name"] for tool in body["tools"]] == ["bash"]


def test_a_tool_less_request_stays_tool_less() -> None:
    """MCC does not invent a catalogue the real client would not have sent.

    Captured 2026-09-19: the genuine ``opencode`` CLI's own title sub-request
    carries no ``tools`` key, no ``tool_choice`` and no ``prompt_cache_key``.
    Its free tier refuses that request too (anomalyco/opencode#49592, #49723).
    Adding a catalogue MCC cannot honour, to a request the client it names
    would not have added one to, would be the larger lie.
    """
    body = _responses_body(_provider(), _request(FREE))
    assert not body.get("tools")


# -- scope: everything outside it is byte-identical ----------------------------


def test_a_paid_model_on_the_same_host_is_byte_identical() -> None:
    provider = _provider()
    tools = _claude_code_tools()
    body = _responses_body(provider, _request(PAID, tools=tools))
    names = [tool["name"] for tool in body["tools"]]

    assert names[:7] == ["Bash", "Read", "Edit", "Glob", "Grep", "Write", "WebFetch"]
    # The 7.18.1 alias still applies: that is the host's ceiling, not the tier.
    assert LONG not in names
    assert provider.tool_catalogue_for(PAID) == {}


def test_the_opt_out_turns_the_whole_impersonation_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_CLIENT_IDENTITY", "mcc")
    get_settings.cache_clear()
    try:
        provider = _provider()
        assert provider.tool_catalogue_for(FREE) == {}
        body = _responses_body(provider, _request(FREE, tools=_claude_code_tools()))
        assert [tool["name"] for tool in body["tools"]][:5] == [
            "Bash",
            "Read",
            "Edit",
            "Glob",
            "Grep",
        ]
        assert opencode_constant_headers()[USER_AGENT_HEADER].startswith(
            "my-claude-code/"
        )
    finally:
        get_settings.cache_clear()


def test_a_chat_completions_provider_is_untouched() -> None:
    provider = create_openai_chat_provider(
        "groq",
        ProviderConfig(api_key="sk-test", base_url="https://api.groq.com/openai/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["groq"],
    )
    body = provider._build_request_body(
        _request("llama-4-70b", tools=_claude_code_tools()), reasoning=REASONING
    )
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names[:5] == ["Bash", "Read", "Edit", "Glob", "Grep"]
    assert provider.tool_catalogue_for("llama-4-70b") == {}
    assert provider.tool_name_codec(_request("llama-4-70b")) is None


def test_chatgpt_oauth_declares_no_catalogue() -> None:
    from my_claude_code.providers.chatgpt_oauth.conversion import (
        build_chatgpt_oauth_request_body,
    )

    request = _request("gpt-5", tools=_claude_code_tools())
    body = build_chatgpt_oauth_request_body(request, reasoning=REASONING)
    assert [tool["name"] for tool in body["tools"]][:5] == [
        "Bash",
        "Read",
        "Edit",
        "Glob",
        "Grep",
    ]
    assert LONG in _wire(body)


# -- inbound: the answer comes back under the client's own names ---------------


def test_the_responses_stream_decodes_the_call_back() -> None:
    codec = OpenAIToolNameCodec.from_names(
        _claude_code_tools(), catalogue=OPENCODE_TOOL_CATALOGUE
    )
    ledger = AnthropicStreamLedger("msg_1", "m")
    converter = ResponsesStreamConverter(ledger, tool_names=codec)
    events = "".join(
        converter.feed(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "call_1", "name": "bash"},
            }
        )
    )
    assert '"name":"Bash"' in events.replace(" ", "")


def test_the_chat_stream_decodes_the_call_back() -> None:
    codec = OpenAIToolNameCodec.from_names(
        _claude_code_tools(), catalogue=OPENCODE_TOOL_CATALOGUE
    )
    ledger = AnthropicStreamLedger("msg_1", "m")
    assembler = OpenAIToolCallAssembler(tool_names=codec)
    events = "".join(
        assembler.process_tool_call(
            {
                "index": 0,
                "id": "call_1",
                "function": {"name": "grep", "arguments": ""},
            },
            ledger,
        )
    )
    assert '"name":"Grep"' in events.replace(" ", "")


def test_the_chat_stream_without_a_codec_is_untouched() -> None:
    ledger = AnthropicStreamLedger("msg_1", "m")
    assembler = OpenAIToolCallAssembler()
    events = "".join(
        assembler.process_tool_call(
            {"index": 0, "id": "call_1", "function": {"name": "grep", "arguments": ""}},
            ledger,
        )
    )
    assert '"name":"grep"' in events.replace(" ", "")


def test_the_messages_surface_encodes_the_body_and_decodes_the_stream() -> None:
    codec = OpenAIToolNameCodec.from_names(
        _claude_code_tools(), catalogue=OPENCODE_TOOL_CATALOGUE
    )
    body = {
        "model": FREE,
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": "Read"},
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Glob"}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1"}],
            },
        ],
    }
    encoded = encode_anthropic_body_tool_names(body, codec)

    assert encoded["tools"][0]["name"] == "bash"
    assert encoded["tool_choice"]["name"] == "read"
    assert encoded["messages"][0]["content"][0]["name"] == "glob"
    # A tool_result references a call by id, never by name.
    assert encoded["messages"][1] == body["messages"][1]
    # The caller's body is recorded and may be retried: never mutated.
    assert body["tools"][0]["name"] == "Bash"

    frame = (
        "event: content_block_start\n"
        + "data: "
        + json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t9", "name": "edit"},
            }
        )
        + "\n\n"
    )
    assert '"name": "Edit"' in decode_anthropic_sse_event(frame, codec)


def test_a_messages_frame_without_a_tool_name_is_returned_by_identity() -> None:
    codec = OpenAIToolNameCodec.from_names(
        _claude_code_tools(), catalogue=OPENCODE_TOOL_CATALOGUE
    )
    frame = 'event: ping\ndata: {"type":"ping"}\n\n'
    assert decode_anthropic_sse_event(frame, codec) is frame


# -- the user-agent ------------------------------------------------------------


def test_the_user_agent_has_the_three_segments_the_real_client_sends() -> None:
    segments = opencode_constant_headers()[USER_AGENT_HEADER].split(" ")
    assert len(segments) == 3
    assert segments[0].startswith("opencode/")
    assert segments[1] == "ai-sdk/provider-utils/4.0.40"
    assert segments[2] == "runtime/bun/1.3.14"


def test_each_appended_segment_is_pinnable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_CLIENT_AI_SDK_VERSION", "9.9.9")
    monkeypatch.setenv("OPENCODE_CLIENT_RUNTIME", "node/24.0.0")
    get_settings.cache_clear()
    try:
        assert opencode_user_agent("1.2.3") == (
            "opencode/1.2.3 ai-sdk/provider-utils/9.9.9 runtime/node/24.0.0"
        )
    finally:
        get_settings.cache_clear()


def test_a_blanked_pin_falls_back_rather_than_leaving_a_hole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_CLIENT_AI_SDK_VERSION", "   ")
    get_settings.cache_clear()
    try:
        assert "ai-sdk/provider-utils/4.0.40" in opencode_user_agent("1.2.3")
    finally:
        get_settings.cache_clear()


# -- the failure kind ----------------------------------------------------------


def test_the_vendors_own_words_are_read_as_a_tier_refusal() -> None:
    assert is_free_tier_error(_status_error(403, FREE_TIER_BODY)) is True


def test_an_ordinary_403_is_still_an_authentication_failure() -> None:
    failure = _classify(_status_error(403, {"error": {"message": "invalid api key"}}))
    assert failure.kind is FailureKind.AUTHENTICATION


def test_a_free_tier_refusal_gets_its_own_kind_and_its_own_sentence() -> None:
    failure = _classify(_status_error(403, FREE_TIER_BODY))
    assert failure.kind is FailureKind.FREE_TIER
    assert "Check API key" not in failure.message
    assert "free tier" in failure.message


def test_the_sdks_own_permission_error_is_classified_the_same_way() -> None:
    request = httpx.Request("POST", "https://opencode.ai/zen/v1/responses")
    response = httpx.Response(403, json=FREE_TIER_BODY, request=request)
    error = openai.PermissionDeniedError(
        "forbidden", response=response, body=FREE_TIER_BODY
    )
    assert _classify(error).kind is FailureKind.FREE_TIER


def test_a_free_tier_refusal_benches_neither_the_key_nor_the_route() -> None:
    """The key is fine, so neither pool may charge it.

    The status on the failure is MCC's conclusion rather than the host's byte
    -- 403 is the number the credential pool reads as "this key was rejected"
    and charges a lockout tier for, and this key was not rejected. The client
    still sees 403: that comes from the wire *type*, which is
    ``permission_error`` on all three protocols.
    """

    failure = _classify(_status_error(403, FREE_TIER_BODY))
    assert failure_counts_toward_bench(FailureKind.FREE_TIER) is False
    assert credential_failure_class(failure) is None
    assert error_justifies_rotation(failure) is False


def test_the_client_still_sees_a_403_on_every_protocol() -> None:
    failure = _classify(_status_error(403, FREE_TIER_BODY))
    assert anthropic_error_type_for_failure(failure) == "permission_error"
    assert openai_error_type_for_failure(failure) == "permission_error"
    assert gemini_status_for_failure(failure) == "PERMISSION_DENIED"


def test_a_free_tier_refusal_does_not_stop_the_chain() -> None:
    """It is not in the shipped ``FALLBACK_SKIP_KINDS``, so the route goes on."""
    assert "free_tier" not in Settings().fallback_skip_kinds


def test_the_kind_is_never_a_default_proxy_trigger() -> None:
    """The same account is refused from every address."""
    from my_claude_code.config.proxy_chains import (
        DEFAULT_TRIGGER_KINDS,
        TRIGGER_KIND_ORDER,
    )

    assert "free_tier" in TRIGGER_KIND_ORDER
    assert "free_tier" not in DEFAULT_TRIGGER_KINDS


def test_the_refusal_is_labelled_on_the_proxying_page() -> None:
    from my_claude_code.api.admin_proxy_routes import UNLIKELY_REASON

    assert "free_tier" in UNLIKELY_REASON
    assert UNLIKELY_REASON["free_tier"]
