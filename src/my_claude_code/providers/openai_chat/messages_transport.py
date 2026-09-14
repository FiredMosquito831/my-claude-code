"""The Messages surface, for a provider whose other surfaces are OpenAI-shaped.

The third door. OpenCode Zen is a front door onto four APIs and its own
registry says which one each model is behind: a model whose ``provider.npm`` is
``@ai-sdk/anthropic`` is served on ``{base_url}/messages``, in Anthropic's
Messages protocol, which the OpenAI SDK client this family is built on cannot
speak at all. Since 6.74.0 such a model has resolved to
:data:`~my_claude_code.application.model_metadata.ResponseSurface.UNSERVABLE`
and been listed with that reason attached.

**Nothing new speaks the protocol.** ``providers/anthropic_messages/`` has
spoken it since 4.x -- for Anthropic's own API and for Command Code -- and this
module re-points it: same body builder, same SSE reader, same recovery ladder,
same learned-facts store, same output-cap and reasoning-rejection tables, keyed
on the same catalogue id as the gateway's other two surfaces. What this module
adds is the three things that are the *gateway's*, not the protocol's:

* **the same identity.** The five ``x-opencode-*``/``User-Agent`` headers ride
  on this request exactly as they ride on the Chat Completions and Responses
  ones. The recorded capture of ``POST /zen/v1/messages`` taken on 2026-09-11
  shows them, and the free-tier gate that reads them sits in front of all three
  endpoints. The identity is **unchanged** by this module -- decision Q6,
  2026-09-13, is that OpenCode identity is not touched.
* **the credential in both shapes.** That same capture carries
  ``Authorization: Bearer <key>`` *and* ``x-api-key: <key>`` *and*
  ``anthropic-version: 2023-06-01`` on one request. The gateway authenticates
  like itself and the protocol behind it authenticates like Anthropic, so both
  go out, in the captured order.
* **a rotating key.** This family resolves its bearer per request, because a
  shared credential may rotate between attempts; the auth strategy below is
  asked each time rather than captured at construction.

**No live model exists to test this against.** Of the seven free models Zen
listed on 2026-09-11, none is on the Messages surface, and the one that was
(``minimax-m3-free``) has been withdrawn: the capture's own answer is
HTTP 401 ``Model minimax-m3-free is not supported``. This module is therefore
proven against that recorded capture and a fake upstream, and that is stated
rather than dressed up. What it changes today is that such a model is
*reachable* the moment one returns, instead of being listed as unservable.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any

from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.client_fingerprint import current_fingerprint
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.anthropic_messages import (
    ANTHROPIC_API_VERSION,
    AnthropicMessagesProvider,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .client_identity import ClientIdentity, identity_headers_for_body


class GatewayMessagesAuth:
    """Both credential shapes one gateway's Messages door expects.

    The bearer the gateway itself checks and the ``x-api-key`` /
    ``anthropic-version`` pair the protocol behind it checks, resolved per
    request so a rotating credential is honoured. Written as an auth strategy
    rather than as ``extra_headers`` because that is what
    :mod:`my_claude_code.providers.anthropic_messages.auth` exists for: "a new
    upstream contributes headers rather than a second copy of the streaming
    loop".
    """

    __slots__ = ("_api_key", "_api_key_provider", "_version")

    def __init__(
        self,
        api_key: str | None,
        *,
        api_key_provider: Any | None = None,
        version: str = ANTHROPIC_API_VERSION,
    ) -> None:
        self._api_key = api_key or ""
        self._api_key_provider = api_key_provider
        self._version = version

    async def headers(self) -> dict[str, str]:
        credential = (
            await self._api_key_provider()
            if self._api_key_provider is not None
            else self._api_key
        )
        return {
            "Authorization": f"Bearer {credential}",
            "x-api-key": credential,
            "anthropic-version": self._version,
        }


class MessagesTransport:
    """One provider's client for the Messages surface.

    Built lazily and only by a profile that declares the surface, so a provider
    that will never use it never constructs an HTTP client for it.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        base_url: str,
        provider_name: str,
        provider_id: str,
        identity: ClientIdentity | None,
        api_key: str | None,
        rate_limiter: ProviderRateLimiter,
        api_key_provider: Any | None = None,
    ) -> None:
        self._identity = identity
        self._provider_name = provider_name
        self._base_url = base_url.rstrip("/")
        # ``replace`` rather than a fresh ``ProviderConfig``: every timeout,
        # retry budget and holdback the operator configured for this provider
        # applies to this door too, and listing the fields by hand is how one
        # of them silently stops applying the next time the dataclass grows.
        self._provider = AnthropicMessagesProvider(
            replace(config, api_key=api_key or "", base_url=self._base_url),
            provider_name=provider_name,
            rate_limiter=rate_limiter,
            auth=GatewayMessagesAuth(api_key, api_key_provider=api_key_provider),
            header_provider=self.identity_headers,
            # The same catalogue id the gateway's other surfaces use: what this
            # host said about a model does not stop being true because the
            # request reached it through a different door.
            provider_id=provider_id,
        )

    @property
    def url(self) -> str:
        """The one endpoint this transport posts to."""

        return f"{self._base_url}/messages"

    def identity_headers(self, body: Mapping[str, Any]) -> dict[str, str]:
        """The complete declared identity for one outbound Messages body.

        The constant half and the per-request half, in the order the identified
        client emits them -- the same derivation the other two surfaces use, so
        the three doors cannot drift into three identities.
        """

        if self._identity is None:
            return {}
        constant = self._identity.default_headers()
        varying = identity_headers_for_body(
            self._identity,
            {"messages": body.get("messages")},
            current_fingerprint().session_id,
        )
        merged = {**constant, **varying}
        return {name: merged[name] for name in self._identity.order if name in merged}

    async def aclose(self) -> None:
        """Release the HTTP client."""

        await self._provider.cleanup()

    async def probe(self, model_id: str) -> None:
        """Ask this endpoint whether it serves one model. Raises what it said."""

        await self._provider.probe(model_id)

    def stream(
        self,
        request: MessagesRequest,
        *,
        input_tokens: int,
        reasoning: ReasoningPolicy,
        surface_label: str = "",
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Run one Messages request and yield Anthropic SSE.

        No body is built here and no converter is installed: the client already
        spoke Anthropic Messages on the way in, and this surface speaks it on
        the way out, so the translation the other two surfaces need is simply
        absent. That is the whole reason re-pointing was an S and a third
        adapter would not have been.
        """

        logger.debug(
            "{}_STREAM: {} on the Messages surface",
            self._provider_name,
            request.model,
        )
        return self._provider.stream_response(
            request,
            input_tokens,
            request_id=request_id,
            reasoning=reasoning,
            wire_surface=surface_label,
        )
