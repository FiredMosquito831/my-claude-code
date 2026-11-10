"""Shared behaviour for gateways that speak OpenRouter's dialect.

Several aggregators expose OpenRouter's exact wire shape rather than a plain
OpenAI-compatible endpoint: ``/models`` entries carry ``supported_parameters``,
and reasoning is negotiated with a ``reasoning`` object and replayed through
``reasoning_details`` on assistant turns.

This module is a neutral owner for that dialect so no gateway imports another
gateway's package (see the architecture contract tests).
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.constants import ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.openai_responses import is_carrier
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.model_listing import (
    extract_openrouter_tool_model_ids,
    extract_openrouter_tool_model_infos,
)
from my_claude_code.providers.openai_chat import (
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
    ReasoningObject,
    validate_extra_body_does_not_override_canonical_fields,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter


def openrouter_gateway_profile(provider_name: str) -> OpenAIChatProfile:
    """Build the OpenRouter-dialect profile for one gateway."""
    return OpenAIChatProfile(
        OpenAIChatRequestPolicy(
            provider_name=provider_name,
            reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
            include_extra_body=True,
            extra_body_validator=validate_extra_body_does_not_override_canonical_fields,
            default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
            # This profile's extractor, and the declaration that makes its
            # request body carry a ``reasoning_details`` field at all: a policy
            # with no extractor attaches nothing, which is what keeps every
            # other Chat provider byte-identical. It is this file's own copy
            # rather than the ``openai_chat`` one on purpose -- the two files
            # keep two copies (Q6: both, or neither), so a fix to one cannot
            # pass unnoticed.
            reasoning_details_source=assistant_reasoning_details,
        ),
        ReasoningObject(tuple((effort, effort.value) for effort in ReasoningEffort)),
    )


class OpenRouterGatewayProvider(OpenAIChatProvider):
    """Provider for a gateway exposing OpenRouter's Chat Completions dialect."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        profile: OpenAIChatProfile,
        rate_limiter: ProviderRateLimiter,
        provider_id: str = "",
    ):
        super().__init__(
            config,
            profile=profile,
            rate_limiter=rate_limiter,
            provider_id=provider_id,
        )

    async def list_model_ids(self) -> frozenset[str]:
        """Only advertise gateway models that can run Claude Code tools."""
        payload = await self._client.models.list()
        return extract_openrouter_tool_model_ids(
            payload, provider_name=self._provider_name
        )

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """Advertise tool-capable gateway models with reasoning capability metadata."""
        payload = await self._client.models.list()
        return extract_openrouter_tool_model_infos(
            payload, provider_name=self._provider_name
        )

    def _handle_extra_reasoning(
        self, delta: Any, ledger: AnthropicStreamLedger, *, output_reasoning: bool
    ) -> Iterator[str]:
        """Map gateway reasoning details onto Anthropic thinking blocks."""
        if not output_reasoning:
            return iter(())
        return _iter_reasoning_detail_events(delta, ledger)


def assistant_reasoning_details(messages: Any) -> dict[int, list[dict[str, Any]]]:
    """Every assistant turn's replayed details, keyed by its source index.

    The byte-identical twin of ``providers/openai_chat/reasoning_details.py``'s
    function of the same name (Q6: both, or neither). The two files keep two
    copies on purpose -- a unification is its own concern (IV.18) -- and the
    contract test parametrises over both so a fix to one cannot pass.

    A turn that yields no detail is simply **absent**, which is what stops an
    empty turn shifting every later turn's details up by one.
    """

    if not _is_sequence(messages):
        return {}
    result: dict[int, list[dict[str, Any]]] = {}
    for index, message in enumerate(messages):
        if _field(message, "role") != "assistant":
            continue
        details = _redacted_reasoning_details(_field(message, "content"))
        if details:
            result[index] = details
    return result


def _redacted_reasoning_details(content: Any) -> list[dict[str, Any]]:
    if not _is_sequence(content):
        return []
    details: list[dict[str, Any]] = []
    for block in content:
        if _field(block, "type") != "redacted_thinking":
            continue
        data = _field(block, "data")
        if not isinstance(data, str) or not data:
            continue
        if is_carrier(data):
            # A carrier minted for another host must not reach a gateway
            # upstream. Before 8.16.1 this line was the leak: the block fell
            # through to the ``reasoning.encrypted`` branch below and the
            # envelope went out verbatim, because ``convert_messages`` had
            # already dropped it and this second pass never learned about it.
            # The prefix test cannot collide with the JSON branch: a carrier
            # starts ``mcc:``, a JSON container starts ``{`` or ``[``.
            #
            # This is the byte-identical twin of the copy in
            # ``providers/openai_chat/reasoning_details.py`` (Q6: both, or
            # neither). The contract test parametrises over both so a fix to
            # one cannot pass.
            continue
        parsed = _json_payload(data)
        if isinstance(parsed, list):
            details.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            details.append(parsed)
        else:
            details.append({"type": "reasoning.encrypted", "data": data})
    return details


def _iter_reasoning_detail_events(
    delta: Any, ledger: AnthropicStreamLedger
) -> Iterator[str]:
    details = _field(delta, "reasoning_details")
    if details is None:
        extra = _field(delta, "model_extra")
        if isinstance(extra, Mapping):
            details = extra.get("reasoning_details")
    if not _is_sequence(details):
        return

    native_reasoning = _field(delta, "reasoning_content")
    has_native_reasoning = isinstance(native_reasoning, str) and bool(native_reasoning)
    for detail in details:
        encrypted = _reasoning_detail_encrypted(detail)
        if encrypted:
            yield from ledger.close_content_blocks()
            index = ledger.blocks.allocate_index()
            yield ledger.content_block_start(index, "redacted_thinking", data=encrypted)
            yield ledger.content_block_stop(index)
            continue
        if has_native_reasoning:
            continue
        text = _reasoning_detail_text(detail)
        if not text:
            continue
        yield from ledger.ensure_thinking_block()
        yield ledger.emit_thinking_delta(text)


def _reasoning_detail_text(detail: Any) -> str | None:
    kind = str(_field(detail, "type") or "").lower()
    if "encrypted" in kind or "redacted" in kind:
        return None
    for key in ("text", "content", "reasoning"):
        value = _field(detail, key)
        if isinstance(value, str) and value:
            return value
    return None


def _reasoning_detail_encrypted(detail: Any) -> str | None:
    kind = str(_field(detail, "type") or "").lower()
    if "encrypted" not in kind and "redacted" not in kind and "summary" not in kind:
        return None
    if isinstance(detail, Mapping):
        return json.dumps(dict(detail), separators=(",", ":"))
    return None


def _json_payload(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )
