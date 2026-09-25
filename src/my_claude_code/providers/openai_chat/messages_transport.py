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

Since 7.33.0 it also carries the **recovery ladder** 7.23.0 gave the Responses
door, in the two rungs a Messages-dialect host can state in a 400: a tool-name
ceiling (Anthropic's own limit is 64, and Claude Code's MCP tool names run to
76) and a ``tool_choice`` the host takes only as ``auto``. One rewrite per
rung, the host's own words as the only trigger, and anything unrecognised
raised exactly as it was. The two facts it learns are written under the kinds
7.23.0 created, at the scopes those were measured at, because they are
statements about the **host's validator** rather than about one of its doors --
so the Models page's "Learned" column and its Forget control already render
them.

**The native providers are not on this path.** ``anthropic`` and
``anthropic_oauth`` reach ``AnthropicMessagesProvider`` directly, and its own
ladder -- the output cap and the reasoning strip, wired in
``anthropic_messages/provider.py`` -- is the only one they have. This class is
constructed by exactly one caller, ``OpenAIChatProvider``, for a gateway that
declares the Messages surface, which is what keeps one ladder per request.

Since 7.33.0 the gateway in front of it need not be OpenCode's: a custom
provider whose entry declares ``messages`` is routed here with its own base
URL, its own key pool and **no** identity headers, because a declared identity
is a property of a profile and the generic one declares none.

**No live model exists to test this against.** Of the seven free models Zen
listed on 2026-09-11, none is on the Messages surface, and the one that was
(``minimax-m3-free``) has been withdrawn: the capture's own answer is
HTTP 401 ``Model minimax-m3-free is not supported``. This module is therefore
proven against that recorded capture and a fake upstream, and that is stated
rather than dressed up. What it changes today is that such a model is
*reachable* the moment one returns, instead of being listed as unservable.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import (
    EMPTY_TOOL_CATALOGUE,
    OPENAI_TOOL_NAME_MAX_LENGTH,
    OpenAIToolNameCodec,
)
from my_claude_code.core.client_fingerprint import current_fingerprint
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.upstream_ladder import note_recovery_rung
from my_claude_code.providers.anthropic_messages import (
    ANTHROPIC_API_VERSION,
    AnthropicMessagesProvider,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    RecoveryMemory,
    complaint_evidence_snippet,
    is_bad_request,
    is_tool_choice_auto_only,
    rejected_tool_name_max_length,
    upstream_complaint,
)

from .client_identity import ClientIdentity, identity_headers_for_body

#: The two rungs this surface adds, named once so the ladder row, the log line
#: and the learning all agree on the word the operator reads. Spelled for the
#: door they fire on: a Messages ``tools`` catalogue and a Messages
#: ``tool_choice`` are not the Responses ones, and an operator reading a ladder
#: row is entitled to know which endpoint refused.
_RUNG_TOOL_NAME_LENGTH = "messages_tool_name_length"
_RUNG_TOOL_CHOICE = "messages_tool_choice"


@dataclass(frozen=True, slots=True)
class _MessagesLearning:
    """One fired rung: what to send now, and what to remember if it works."""

    kind: str
    request: MessagesRequest
    tool_name_max_length: int | None
    value: Any
    evidence: str
    log_line: str


def _forces_a_tool(tool_choice: Mapping[str, Any] | None) -> bool:
    """Whether a client's ``tool_choice`` asks for anything but ``auto``.

    Read off the Anthropic request, which is this surface's *own* protocol:
    ``{"type": "auto"}`` and omission are the same instruction, while ``any``,
    ``tool`` and ``none`` are the three a strict host may refuse.
    """

    if not tool_choice:
        return False
    return tool_choice.get("type") not in {None, "auto"}


def _stated_rejection(error: Exception) -> Exception:
    """The host's own 400 behind whatever the provider raised.

    ``AnthropicMessagesProvider`` classifies an upstream failure and raises the
    canonical :class:`ExecutionFailure` ``from`` the transport error, so the
    words the host actually wrote are one ``__cause__`` away. Read through it
    rather than around it: the matchers in ``providers/recovery`` are written
    against the carrier the protocol raised, and re-deriving them from a
    classified failure would be a second, weaker copy of them.

    The cause is preferred rather than merely accepted, and that is the whole
    point of the function. A classified failure *does* answer ``is_bad_request``
    and its text *does* contain the host's words -- but JSON-escaped, inside the
    "Upstream error:" block it prints, so ``only `\\"auto\\"` is supported``
    carries two backslashes the matcher's character class does not admit and
    the rung silently never fires. The carrier has the body parsed.
    """

    cause = error.__cause__
    if isinstance(cause, Exception) and is_bad_request(cause):
        return cause
    return error


def _next_messages_recovery(
    request: MessagesRequest,
    error: Exception,
    codec: OpenAIToolNameCodec | None,
    used: set[str],
    codec_for: Callable[[MessagesRequest, int | None], OpenAIToolNameCodec | None],
) -> _MessagesLearning | None:
    """The next rewrite this refusal calls for, or ``None`` to raise it.

    A free function because it holds no state: the rungs read the host's own
    words and the request in hand, and ``used`` -- owned by the caller, one set
    per request -- is what makes each rung fire at most once. Concurrent
    requests through one transport therefore never share a rung budget.

    Rung order is 7.23.0's, and for 7.23.0's reason: a tool-name ceiling is a
    number the host *stated*, while dropping ``tool_choice`` removes an
    instruction the client gave, and narrowest-and-most-certain goes first. The
    order is total rather than conventional --
    :func:`rejected_tool_name_max_length` refuses a complaint that names
    ``tool_choice`` outright.
    """

    if _RUNG_TOOL_NAME_LENGTH not in used:
        stated = rejected_tool_name_max_length(error)
        if stated is not None:
            retry = codec_for(request, stated)
            # A codec identical to the one that was just refused would send
            # the same bytes again, which is a retry that cannot fix anything
            # -- the same rule ``clone_body_without_tool_choice`` states by
            # returning ``None`` when it removed nothing.
            if retry is not None and retry.has_aliases and retry != codec:
                used.add(_RUNG_TOOL_NAME_LENGTH)
                return _MessagesLearning(
                    kind=_RUNG_TOOL_NAME_LENGTH,
                    request=request,
                    tool_name_max_length=stated,
                    value=stated,
                    evidence=complaint_evidence_snippet(upstream_complaint(error)),
                    log_line=(
                        f"host states tool names must be at most {stated} characters"
                    ),
                )
    if (
        _RUNG_TOOL_CHOICE not in used
        and is_tool_choice_auto_only(error)
        and _forces_a_tool(request.tool_choice)
    ):
        used.add(_RUNG_TOOL_CHOICE)
        return _MessagesLearning(
            kind=_RUNG_TOOL_CHOICE,
            request=request.model_copy(update={"tool_choice": None}),
            tool_name_max_length=None,
            value=True,
            evidence=complaint_evidence_snippet(upstream_complaint(error)),
            log_line="host states only tool_choice=auto is supported",
        )
    return None


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
        tool_catalogue_for_request: (
            Callable[[MessagesRequest], Mapping[str, str]] | None
        ) = None,
        memory: RecoveryMemory | None = None,
    ) -> None:
        self._identity = identity
        # What this host has taught MCC about its own request validator. The
        # same memory the other two doors write to, under the same fact kinds,
        # because the facts are statements about the *host*: a deployment that
        # caps tool names at 48 caps them at 48 whichever endpoint the request
        # reached. A bare transport (a unit test, an embedded use) gets an
        # unpersisted memory and behaves exactly as one built before 7.33.0.
        self._memory = memory if memory is not None else RecoveryMemory()
        # The same resolver the other two doors read, so a model that is
        # inside a host's free-tier scope is inside it whichever surface it
        # resolves to. ``None`` -- every profile that declares no catalogue --
        # sends this protocol exactly as it has always been sent.
        self._tool_catalogue_for_request = tool_catalogue_for_request
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

    @property
    def tool_name_max_length(self) -> int | None:
        """The longest tool name this host accepts, or ``None`` for no limit.

        Learned only. No profile declares a ceiling for this surface: the
        profile field is the *Responses* one by its own docstring, and the
        Messages dialect's own limit -- Anthropic documents 64, the same number
        -- is a property of the host in front of MCC rather than of the
        protocol, so it is read from what this deployment stated.
        """

        return self._memory.responses_tool_name_max_length

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

        What *is* here is the recovery ladder of 7.23.0, in the two rungs a
        Messages-dialect host can state in a 400. It sits around the whole
        attempt rather than inside
        :class:`~my_claude_code.providers.anthropic_messages.AnthropicMessagesProvider`
        for two reasons, and both matter:

        * **no double ladder.** That provider already owns the recovery the
          *protocol* calls for -- an output cap and a reasoning strip, wired at
          ``anthropic_messages/provider.py`` -- and the native ``anthropic``
          and ``anthropic_oauth`` providers reach it through the same code.
          Nothing here is visible to them: this class is constructed by one
          caller, ``OpenAIChatProvider``, for a gateway that declares the
          Messages surface.
        * **the codec has to move with the body.** A stated tool-name ceiling
          is answered by re-aliasing the catalogue, and the *decode* half of
          that aliasing runs over the SSE frames. Retrying at this level
          rebuilds both from one codec, so what goes out and what comes back
          can never be aliased under two different ceilings.
        """

        return self._stream_with_recovery(
            request,
            input_tokens=input_tokens,
            reasoning=reasoning,
            surface_label=surface_label,
            request_id=request_id,
        )

    async def _stream_with_recovery(
        self,
        request: MessagesRequest,
        *,
        input_tokens: int,
        reasoning: ReasoningPolicy,
        surface_label: str,
        request_id: str | None,
    ) -> AsyncIterator[str]:
        """Stream one request, answering the two refusals this door recovers.

        The same contract every rung in ``providers/recovery/ladder.py`` has:
        one rewrite per rung per request, the host's own words as the only
        trigger, and a rejection nothing recognises raised exactly as it was.
        ``used`` is never cleared, so a host that keeps refusing after its own
        stated fix fails visibly rather than looping.

        A refusal is only recoverable **before the first byte reaches the
        client**. Once an event has been yielded the client is committed and
        the error is re-raised, exactly as ``_stream_across_surfaces`` does one
        level up.
        """

        used: set[str] = set()
        current = self._without_refused_tool_choice(request)
        limit = self.tool_name_max_length
        pending: list[_MessagesLearning] = []
        while True:
            codec = self._codec_for(current, limit)
            committed = False
            try:
                logger.debug(
                    "{}_STREAM: {} on the Messages surface",
                    self._provider_name,
                    current.model,
                )
                stream = self._provider.stream_response(
                    current,
                    input_tokens,
                    request_id=request_id,
                    reasoning=reasoning,
                    wire_surface=surface_label,
                    tool_names=codec,
                )
                async for event in stream:
                    if not committed:
                        committed = True
                        for learned in pending:
                            self._remember(current, learned)
                    yield event
                return
            except Exception as error:
                if committed:
                    raise
                learning = _next_messages_recovery(
                    current, _stated_rejection(error), codec, used, self._codec_for
                )
                if learning is None:
                    raise
                logger.warning(
                    "{}_MESSAGES: {} -- retrying once ({})",
                    self._provider_name,
                    learning.log_line,
                    learning.evidence,
                )
                current = learning.request
                if learning.tool_name_max_length is not None:
                    limit = learning.tool_name_max_length
                pending.append(learning)
                # Carried on the *retry* row, so the ladder in the modal reads
                # "400 ... / 200 (messages_tool_choice)" and the operator can
                # see which rewrite the second body is.
                note_recovery_rung(learning.kind)

    def _remember(self, request: MessagesRequest, learning: _MessagesLearning) -> None:
        """Write one rung's learning down, now that a stream has proven it.

        Under the fact kinds 7.23.0 created, so the Models page's "Learned"
        column and its Forget control render these without knowing a third
        surface exists. Both are statements about the host's validator, and
        their scopes are the measured ones: the ceiling is host-wide, the
        ``tool_choice`` refusal is per model.
        """

        if learning.kind == _RUNG_TOOL_NAME_LENGTH:
            self._memory.learn_responses_tool_name_limit(
                int(learning.value), evidence=learning.evidence
            )
            logger.warning(
                "{}_MESSAGES: this host caps tool names at {} -- later "
                "requests alias from the first try",
                self._provider_name,
                learning.value,
            )
            return
        if self._memory.remember_responses_tool_choice_refusal(
            request.model, evidence=learning.evidence
        ):
            logger.warning(
                "{}_MESSAGES: {} accepts only tool_choice=auto -- later "
                "requests omit the field without paying the rejection",
                self._provider_name,
                request.model,
            )

    def _without_refused_tool_choice(self, request: MessagesRequest) -> MessagesRequest:
        """Drop a ``tool_choice`` this model has been proven to refuse.

        A client that forced a tool still gets a correct answer: the Messages
        default is ``auto``, so the model is free to call the tool and normally
        does. Dropping it here rather than in the body builder keeps one
        statement of the rule, and a model that has refused nothing is returned
        untouched -- the same object, so nothing about its request moves.
        """

        if not self._memory.responses_tool_choice_refused(request.model):
            return request
        if not _forces_a_tool(request.tool_choice):
            return request
        logger.debug(
            "{}_MESSAGES: omitting tool_choice for {} -- this host refused it on {}",
            self._provider_name,
            request.model,
            self._memory.responses_tool_choice_learned_on(request.model)
            or "an earlier request",
        )
        return request.model_copy(update={"tool_choice": None})

    def _codec_for(
        self, request: MessagesRequest, max_length: int | None
    ) -> OpenAIToolNameCodec | None:
        """Build this request's codec under one ceiling, or ``None`` for none.

        ``None`` when the host has neither a catalogue of its own nor a stated
        ceiling, which is every deployment that has refused nothing -- and is
        why a host MCC has learned nothing about sends exactly the bytes it
        sent before this ladder existed.
        """

        catalogue = (
            self._tool_catalogue_for_request(request)
            if self._tool_catalogue_for_request is not None
            else EMPTY_TOOL_CATALOGUE
        )
        if not catalogue and max_length is None:
            return None
        return OpenAIToolNameCodec.from_request(
            request,
            max_length=(
                OPENAI_TOOL_NAME_MAX_LENGTH if max_length is None else max_length
            ),
            catalogue=catalogue,
        )
