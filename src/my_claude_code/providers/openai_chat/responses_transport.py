"""The Responses surface, for a provider whose other surface is the OpenAI SDK.

The OpenAI SDK client this family is built on knows one endpoint --
``chat.completions.create`` -- and a profile's ``base_url`` is a prefix, not a
path, so there has never been a way to send this family's request anywhere
else. This module is the second door: a small raw-``httpx`` sender that posts
to ``{base_url}/responses`` and hands the SSE stream to
:mod:`my_claude_code.providers.openai_responses`, the protocol translation
``chatgpt_oauth`` has been using since 4.x.

Three things it must get exactly right, all of them measured against a capture
of the real ``opencode-ai@1.18.30`` CLI taken on 2026-09-11
(``tests/contracts/opencode_reference_responses_request.json``):

* **the same identity.** The five ``x-opencode-*``/``User-Agent`` headers ride
  on this request exactly as they ride on the Chat Completions one; the
  capture shows the CLI sending them on ``/responses`` too, and the free-tier
  gate that refuses a request without them sits in front of both endpoints.
* **the session id is also the cache key.** The CLI sets
  ``prompt_cache_key`` to the value of its session header, which is where that
  header earns its keep -- it is what makes turn two of a conversation hit the
  vendor's prompt cache. Read off the declared identity, never off a literal
  header name.
* **failures look like the SDK's.** An ``httpx.HTTPStatusError`` carrying the
  real response is what every matcher in ``providers/recovery`` already knows
  how to read, so classification, the recovery ladder and the surface rung all
  work unchanged.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.client_fingerprint import current_fingerprint
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.wire_capture import (
    record_response_shape,
    record_wire_request,
    start_response_shape,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.openai_responses import (
    ResponsesStreamConverter,
    build_responses_request_body,
    iter_responses_sse_events,
    note_responses_event_shape,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .client_identity import ClientIdentity, identity_headers_for_body
from .opencode_identity import identity_wire_record

#: What a surface probe asks for. Small enough that a host which answers it has
#: cost the user a rounding error, and large enough that a model with a
#: minimum-output rule still accepts it.
PROBE_MAX_OUTPUT_TOKENS = 16

#: The probe's prompt. One word, no system prompt, no tools: the question is
#: "does this endpoint serve this model at all", and anything more would be
#: asking a second question at the same time.
PROBE_PROMPT = "hi"


class ResponsesTransport:
    """One provider's client for the Responses surface.

    Built lazily and only by a profile that declares the surface, so a provider
    that will never use it never constructs an HTTP client for it.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        base_url: str,
        provider_name: str,
        identity: ClientIdentity | None,
        api_key: str | None,
        rate_limiter: ProviderRateLimiter,
        api_key_provider: Any | None = None,
    ) -> None:
        self._config = config
        self._rate_limiter = rate_limiter
        self._base_url = base_url.rstrip("/")
        self._provider_name = provider_name
        self._identity = identity
        self._api_key = api_key
        self._api_key_provider = api_key_provider
        self._client = httpx.AsyncClient(
            proxy=config.proxy or None,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    @property
    def url(self) -> str:
        """The one endpoint this transport posts to."""

        return f"{self._base_url}/responses"

    async def aclose(self) -> None:
        """Release the HTTP client."""

        await self._client.aclose()

    async def _credential(self) -> str:
        """The bearer this request carries, honouring a rotating provider."""

        if self._api_key_provider is not None:
            return await self._api_key_provider()
        return self._api_key or ""

    def _headers(self, body: Mapping[str, Any]) -> dict[str, str]:
        """Authorization, content type, and the declared identity -- in order."""

        headers = {
            "Content-Type": "application/json",
        }
        headers.update(self.identity_headers(body))
        return headers

    def identity_headers(self, body: Mapping[str, Any]) -> dict[str, str]:
        """The complete declared identity for one outbound Responses body.

        The constant half and the per-request half, in the order the identified
        client emits them. ``input`` stands in for ``messages`` so the same
        conversation-key derivation serves both surfaces; a conversation the
        inbound client named already supplies the key and never reaches it.
        """

        if self._identity is None:
            return {}
        constant = self._identity.default_headers()
        varying = identity_headers_for_body(
            self._identity,
            {"messages": body.get("input")},
            current_fingerprint().session_id,
        )
        merged = {**constant, **varying}
        return {name: merged[name] for name in self._identity.order if name in merged}

    def _prompt_cache_key(self, headers: Mapping[str, str]) -> str | None:
        """The session id, read off the declared identity rather than a literal."""

        if self._identity is None or self._identity.session_header is None:
            return None
        return headers.get(self._identity.session_header)

    def build_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy,
        max_output_tokens: int | None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build one Responses body and the headers that will carry it.

        Returned together because they are not independent: the cache key in
        the body is the session id in the headers, and computing them apart is
        how they would drift.
        """

        body = build_responses_request_body(
            request,
            reasoning=reasoning,
            store=False,
            stream=True,
            parallel_tool_calls=False,
            max_output_tokens=max_output_tokens,
            extra_body=extra_body,
        )
        headers = self._headers(body)
        cache_key = self._prompt_cache_key(headers)
        if cache_key:
            body["prompt_cache_key"] = cache_key
        return body, headers

    async def send(
        self, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> httpx.Response:
        """POST one body, raising the SDK-shaped error every matcher reads.

        A streaming response is returned with its body still open; the caller
        owns closing it. A refusal is read whole, closed here, and re-raised as
        an :class:`httpx.HTTPStatusError` carrying the real response -- which is
        what lets ``upstream_error_payload`` parse the host's own words rather
        than MCC's wording of them.
        """

        sent = dict(headers)
        sent["Authorization"] = f"Bearer {await self._credential()}"
        streaming = bool(body.get("stream"))
        request = self._client.build_request(
            "POST", self.url, headers=sent, json=dict(body)
        )
        response = await self._client.send(request, stream=streaming)
        if response.status_code >= 400:
            error_body = await response.aread()
            await response.aclose()
            raise httpx.HTTPStatusError(
                f"{self._provider_name} Responses API error {response.status_code}",
                request=request,
                response=httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=error_body,
                    request=request,
                ),
            )
        if not streaming:
            await response.aread()
        return response

    async def probe(self, model_id: str) -> None:
        """Ask this endpoint whether it serves one model, as cheaply as possible.

        Returns on success and raises whatever the host answered otherwise, so
        the caller can tell "the other surface works" from "both are broken"
        without this function having an opinion about either.
        """

        body: dict[str, Any] = {
            "model": model_id,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": PROBE_PROMPT}],
                }
            ],
            "store": False,
            "stream": False,
            "max_output_tokens": PROBE_MAX_OUTPUT_TOKENS,
        }
        response = await self.send(body, self._headers(body))
        await response.aclose()

    def stream(
        self,
        request: MessagesRequest,
        *,
        input_tokens: int,
        reasoning: ReasoningPolicy,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        surface_label: str,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Run one Responses request and yield Anthropic SSE.

        The body and headers are built by the caller (:meth:`build_body`) so
        that the surface rung can hand the *same* request to a second endpoint
        after the first refused it, rather than rebuilding one and hoping the
        two match.
        """

        async def _run() -> AsyncIterator[str]:
            ledger = AnthropicStreamLedger(
                f"msg_{uuid.uuid4()}",
                request.model,
                input_tokens,
                log_raw_events=self._config.log_raw_sse_events,
            )
            converter = ResponsesStreamConverter(
                ledger,
                log_raw_events=self._config.log_raw_sse_events,
                output_reasoning=reasoning.output_enabled,
            )
            identity = self.identity_headers(body)
            # The commit boundary, the same one the Chat Completions path has:
            # the body is final once it is handed to the sender, and the
            # surface it was sent on is recorded beside it.
            record_wire_request(
                body,
                surface=surface_label,
                **(
                    {"client_identity": identity_wire_record(identity)}
                    if identity
                    else {}
                ),
            )
            async with self._rate_limiter.concurrency_slot():
                try:
                    response = await self._rate_limiter.execute_with_retry(
                        self.send, body=body, headers=headers
                    )
                except Exception as error:
                    # Classified here rather than left raw so a Responses
                    # refusal reaches routing as the same ``ExecutionFailure``
                    # a Chat Completions refusal does -- the fallback chain,
                    # the bench and the request log all read that one type.
                    raise classify_provider_failure(
                        error,
                        provider_name=self._provider_name,
                        request_id=request_id,
                        read_timeout_s=self._config.http_read_timeout,
                        mark_rate_limited=self._rate_limiter.extend_reactive_block,
                        mark_rate_limited_enabled=(
                            not self._config.routes_around_model
                        ),
                    ) from error
                try:
                    yield ledger.message_start()
                    shape = start_response_shape()
                    async for event in iter_responses_sse_events(response.aiter_raw()):
                        note_responses_event_shape(shape, event)
                        for sse_event in converter.feed(event):
                            yield sse_event
                    for sse_event in converter.finish():
                        yield sse_event
                    record_response_shape(shape)
                finally:
                    await response.aclose()

        logger.debug(
            "{}_STREAM: {} on the Responses surface", self._provider_name, request.model
        )
        return _run()
