"""Google AI Studio Gemini provider (OpenAI-compatible chat completions)."""

import asyncio
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import (
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.config.media_surfaces import GEMINI_API_KEY_HEADER
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import connection_id
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.google_openai.signature_cache import (
    GOOGLE_THOUGHT_SIGNATURES,
)
from my_claude_code.providers.model_listing import (
    declared_from_row,
    published_row_text,
    record_with_declared,
)
from my_claude_code.providers.openai_chat import (
    NamedEffortReasoning,
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
    build_openai_chat_request_body,
    model_accepts_pdf_for_provider,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .quirks import apply_gemini_request_quirks

_MAX_TOOL_CALL_EXTRA_CONTENT_CACHE = 4096

_REQUEST_POLICY = OpenAIChatRequestPolicy(
    provider_name="GEMINI",
    reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
)
_PROFILE = OpenAIChatProfile(
    _REQUEST_POLICY,
    NamedEffortReasoning(
        (
            (ReasoningEffort.MINIMAL, "minimal"),
            (ReasoningEffort.LOW, "low"),
            (ReasoningEffort.MEDIUM, "medium"),
            (ReasoningEffort.HIGH, "high"),
            (ReasoningEffort.XHIGH, "high"),
            (ReasoningEffort.MAX, "high"),
        ),
        disabled_value="none",
    ),
)


class GeminiProvider(OpenAIChatProvider):
    """Gemini API using ``https://generativelanguage.googleapis.com/v1beta/openai/``."""

    def __init__(self, config: ProviderConfig, *, rate_limiter: ProviderRateLimiter):
        super().__init__(
            config,
            profile=_PROFILE,
            rate_limiter=rate_limiter,
            provider_id="gemini",
        )
        # The signatures this credential captured, by tool-call id. Restored
        # from the durable store on construction (8.19.0), so a tool loop that
        # spans a restart still replays them instead of falling back to the
        # skip-validator sentinel.
        self._tool_call_extra_content_by_id: dict[str, dict[str, Any]] = (
            GOOGLE_THOUGHT_SIGNATURES.rows_for(self._provider_id, self._credential_id)
        )

    @property
    def _credential_id(self) -> str:
        """Which credential this instance holds -- hashed, never the secret.

        ``connection_id`` of the credential's fingerprint, the rule
        ``core/openai_responses/carrier.py`` states for the reasoning carrier:
        hash the identity, never the token. The access token of a Google
        account refreshes; hashing it would refuse every signature captured
        before a refresh, silently, every hour. The fingerprint is the same
        value the request log and the exit memory key on, so a signature and
        the request that used it name the credential the same way.

        ``""`` when no credential is configured, which is a stable constant and
        keeps a keyless instance's cache separate from every keyed one.
        """

        key = self._config.api_key
        return connection_id(credential_fingerprint(key) if key else "")

    def _record_tool_call_extra_content(
        self, tool_call_id: str, extra_content: dict[str, Any]
    ) -> None:
        if (
            tool_call_id not in self._tool_call_extra_content_by_id
            and len(self._tool_call_extra_content_by_id)
            >= _MAX_TOOL_CALL_EXTRA_CONTENT_CACHE
        ):
            self._tool_call_extra_content_by_id.pop(
                next(iter(self._tool_call_extra_content_by_id))
            )
        self._tool_call_extra_content_by_id[tool_call_id] = deepcopy(extra_content)
        # The durable half. It evicts on its own ceiling and hands back the row
        # it dropped, so the two maps cannot disagree about what is cached.
        evicted = GOOGLE_THOUGHT_SIGNATURES.record(
            self._provider_id,
            self._credential_id,
            tool_call_id,
            extra_content,
        )
        if evicted is not None and evicted[0:2] == (
            self._provider_id,
            self._credential_id,
        ):
            self._tool_call_extra_content_by_id.pop(evicted[2], None)

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """Return primary OpenAI-compat models enriched with native ``/v1beta/models``."""
        primary_infos = await super().list_model_infos()
        native_payload = await self._fetch_native_models_payload()
        if not isinstance(native_payload, Mapping):
            return primary_infos
        native_models = native_payload.get("models")
        if not isinstance(native_models, list) or not native_models:
            return primary_infos
        return _merge_gemini_native_models(primary_infos, native_models)

    async def _fetch_native_models_payload(self) -> Any:
        """Fetch native Gemini ``/v1beta/models`` metadata; return ``None`` on error."""
        http_client = getattr(self._client, "_client", None)
        if not isinstance(http_client, httpx.AsyncClient):
            return None
        native_url = _gemini_native_models_url(self._base_url)
        headers: dict[str, str] = {}
        api_key = (
            await self._api_key_provider()
            if self._api_key_provider is not None
            else self._api_key
        )
        if api_key:
            headers[GEMINI_API_KEY_HEADER] = api_key
        try:
            response = await http_client.get(
                native_url,
                headers=headers,
                params={"pageSize": "100"},
            )
            response.raise_for_status()
            return response.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "Gemini native model-list enrichment skipped: {}",
                type(exc).__name__,
            )
            return None

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
        wire_notes: dict[str, Any] | None = None,
    ) -> dict:
        return build_openai_chat_request_body(
            request,
            reasoning=reasoning,
            policy=self.effective_request_policy_for(request.model, _REQUEST_POLICY),
            postprocessors=(
                lambda body, request_data, policy: apply_gemini_request_quirks(
                    body,
                    request_data,
                    policy,
                    tool_call_extra_content_by_id=self._tool_call_extra_content_by_id,
                    wire_notes=wire_notes,
                ),
                _PROFILE.apply_reasoning,
            ),
            provider_id=self._provider_id,
            accepts_documents=model_accepts_pdf_for_provider(
                self._provider_id, request.model
            ),
            suppress_structured_output=self._recovery_memory.structured_output_refused(
                request.model
            ),
        )


def _gemini_native_models_url(base_url: str) -> str:
    """Derive the native ``.../models`` URL from Gemini's OpenAI-compat base URL."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/openai"):
        normalized = normalized.removesuffix("/openai")
    return f"{normalized}/models"


def _normalize_gemini_native_id(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    if not stripped:
        return None
    bare = stripped.removeprefix("models/")
    return bare or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _merge_native_published_row(
    existing_row: str | None, native_row: Mapping[str, Any]
) -> str | None:
    base: dict[str, Any] = {}
    if existing_row:
        try:
            loaded = json.loads(existing_row)
            if isinstance(loaded, dict):
                base = loaded
        except ValueError, TypeError:
            base = {}
    merged = dict(base)
    for key in (
        "name",
        "displayName",
        "description",
        "inputTokenLimit",
        "outputTokenLimit",
        "supportedGenerationMethods",
        "supported_generation_methods",
        "temperature",
        "topP",
        "topK",
        "maxTemperature",
    ):
        if key in native_row and (key not in merged or key.startswith("supported")):
            merged[key] = native_row[key]
    try:
        return json.dumps(
            merged, ensure_ascii=False, separators=(",", ":"), default=str
        )
    except Exception:
        return existing_row


def _merge_native_declared(
    existing: ProviderModelDeclaration | None,
    native_declared: ProviderModelDeclaration | None,
) -> ProviderModelDeclaration | None:
    if native_declared is None:
        return existing
    if existing is None:
        return native_declared
    endpoints = existing.endpoints
    if native_declared.endpoints:
        endpoints = tuple(
            sorted(set(existing.endpoints or ()) | set(native_declared.endpoints))
        )
    return ProviderModelDeclaration(
        modalities=existing.modalities
        if existing.modalities is not None
        else native_declared.modalities,
        model_type=existing.model_type
        if existing.model_type is not None
        else native_declared.model_type,
        endpoints=endpoints,
        context_length=existing.context_length
        if existing.context_length is not None
        else native_declared.context_length,
        max_output_tokens=existing.max_output_tokens
        if existing.max_output_tokens is not None
        else native_declared.max_output_tokens,
        input_price=existing.input_price
        if existing.input_price is not None
        else native_declared.input_price,
        output_price=existing.output_price
        if existing.output_price is not None
        else native_declared.output_price,
        reasoning=existing.reasoning
        if existing.reasoning is not None
        else native_declared.reasoning,
        tool_calls=existing.tool_calls
        if existing.tool_calls is not None
        else native_declared.tool_calls,
        vision=existing.vision
        if existing.vision is not None
        else native_declared.vision,
        published_at=existing.published_at
        if existing.published_at is not None
        else native_declared.published_at,
        retires_at=existing.retires_at
        if existing.retires_at is not None
        else native_declared.retires_at,
        knowledge_cutoff=existing.knowledge_cutoff
        if existing.knowledge_cutoff is not None
        else native_declared.knowledge_cutoff,
        description=existing.description
        if existing.description is not None
        else native_declared.description,
    )


def _merge_gemini_native_models(
    primary_infos: frozenset[ProviderModelInfo],
    native_models: list[Any],
) -> frozenset[ProviderModelInfo]:
    by_id: dict[str, ProviderModelInfo] = {
        info.model_id: info for info in primary_infos
    }
    for row in native_models:
        if not isinstance(row, Mapping):
            continue
        bare_id = _normalize_gemini_native_id(row.get("name"))
        if bare_id is None:
            continue
        native_declared = declared_from_row(row)
        input_limit = _positive_int(row.get("inputTokenLimit"))
        output_limit = _positive_int(row.get("outputTokenLimit"))

        matched_key = (
            bare_id
            if bare_id in by_id
            else (f"models/{bare_id}" if f"models/{bare_id}" in by_id else None)
        )
        if matched_key is not None:
            existing = by_id[matched_key]
            by_id[matched_key] = record_with_declared(
                replace(
                    existing,
                    context_length=(
                        existing.context_length
                        if existing.context_length is not None
                        else input_limit
                    ),
                    max_output_tokens=(
                        existing.max_output_tokens
                        if existing.max_output_tokens is not None
                        else output_limit
                    ),
                    declared=_merge_native_declared(existing.declared, native_declared),
                    published_row=_merge_native_published_row(
                        existing.published_row, row
                    ),
                )
            )
        else:
            by_id[bare_id] = record_with_declared(
                ProviderModelInfo(
                    model_id=bare_id,
                    context_length=input_limit,
                    max_output_tokens=output_limit,
                    declared=native_declared,
                    published_row=published_row_text(row),
                )
            )
    return frozenset(by_id.values())


__all__ = ["GeminiProvider"]
