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
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import EMPTY_TOOL_CATALOGUE
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.client_fingerprint import current_fingerprint
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.upstream_ladder import note_recovery_rung, note_response_head
from my_claude_code.core.wire_capture import (
    record_response_shape,
    record_wire_request,
    start_response_shape,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import error_response_headers, read_error_body
from my_claude_code.providers.openai_responses import (
    RESPONSES_TOOL_SCHEMA_DIALECT,
    ResponsesStreamConverter,
    ToolSchemaDialect,
    alias_responses_body_tool_names,
    build_responses_request_body,
    iter_responses_sse_events,
    note_responses_event_shape,
    responses_tool_name_codec,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    RUNG_TOOL_SCHEMA,
    RecoveryMemory,
    SchemaKeywordRefusal,
    ToolSchemaRecovery,
    apply_learned_tool_schema_refusals,
    clone_body_without_tool_choice,
    complaint_evidence_snippet,
    is_tool_choice_auto_only,
    merge_tool_schema_markers,
    refusal_from_detail,
    rejected_tool_name_max_length,
    tool_schema_recovery,
    upstream_complaint,
)
from my_claude_code.providers.socks_deadline import bound_socks_handshake

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

#: The three rungs this surface adds, named once so the ladder row, the log
#: line and the learning all agree on the word the operator reads.
_RUNG_TOOL_NAME_LENGTH = "responses_tool_name_length"
_RUNG_TOOL_CHOICE = "responses_tool_choice"
#: Shared with ``chatgpt_oauth``, which registers the same recovery on its own
#: ladder: one word for one event, whichever of the two senders paid for it.
_RUNG_TOOL_SCHEMA = RUNG_TOOL_SCHEMA


@dataclass(frozen=True, slots=True)
class _ResponsesLearning:
    """One fired rung: what to send now, and what to remember if it works."""

    kind: str
    body: dict[str, Any]
    value: Any
    evidence: str
    log_line: str


def _forces_a_tool(tool_choice: Any) -> bool:
    """Whether a client's ``tool_choice`` asks for anything but ``auto``.

    Read off the *Anthropic* request rather than the built body, because that
    is what decides whether a body is built with the field at all. ``None`` and
    an explicit ``auto`` are the same instruction as omission.
    """

    if tool_choice is None:
        return False
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") not in {None, "auto"}
    return tool_choice != "auto"


def _next_responses_recovery(
    request: MessagesRequest,
    error: Exception,
    body: Mapping[str, Any],
    used: set[str],
    tool_catalogue: Mapping[str, str] = EMPTY_TOOL_CATALOGUE,
) -> _ResponsesLearning | None:
    """The next rewrite this refusal calls for, or ``None`` to raise it.

    A free function because it holds no state: the rungs read the host's own
    words and the body in hand, and ``used`` -- owned by the caller, one set
    per request -- is what makes each rung fire at most once. Concurrent
    requests through one transport therefore never share a rung budget.
    """

    if _RUNG_TOOL_SCHEMA not in used:
        # First, and not merely by convention. This is the one refusal of the
        # three that carries a machine-readable verdict (``code:
        # invalid_json_schema``), and it is the one that can be *mistaken* for
        # another: a schema path ending ``$.properties.name.maxLength`` names
        # the ``name`` parameter and talks about length, which is exactly what
        # ``rejected_tool_name_max_length`` reads -- and that matcher falls
        # back to 64 when it finds no number, so it would answer a schema
        # refusal by aliasing every tool name and fixing nothing. Neither of
        # the other two matchers can produce this code, so the order is total.
        schema = tool_schema_recovery(error, body)
        if schema is not None:
            used.add(_RUNG_TOOL_SCHEMA)
            return _ResponsesLearning(
                kind=_RUNG_TOOL_SCHEMA,
                body=schema.body,
                value=schema,
                evidence=schema.evidence,
                log_line=schema.log_line,
            )
    if _RUNG_TOOL_NAME_LENGTH not in used:
        stated = rejected_tool_name_max_length(error)
        if stated is not None:
            codec = responses_tool_name_codec(request, stated, tool_catalogue)
            retry = (
                alias_responses_body_tool_names(dict(body), codec)
                if codec is not None
                else None
            )
            if retry is not None:
                used.add(_RUNG_TOOL_NAME_LENGTH)
                return _ResponsesLearning(
                    kind=_RUNG_TOOL_NAME_LENGTH,
                    body=retry,
                    value=stated,
                    evidence=complaint_evidence_snippet(upstream_complaint(error)),
                    log_line=(
                        f"host states tool names must be at most {stated} characters"
                    ),
                )
    if _RUNG_TOOL_CHOICE not in used and is_tool_choice_auto_only(error):
        retry = clone_body_without_tool_choice(dict(body))
        if retry is not None:
            used.add(_RUNG_TOOL_CHOICE)
            return _ResponsesLearning(
                kind=_RUNG_TOOL_CHOICE,
                body=retry,
                value=True,
                evidence=complaint_evidence_snippet(upstream_complaint(error)),
                log_line="host states only tool_choice=auto is supported",
            )
    return None


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
        tool_name_max_length: int | None = None,
        tool_catalogue_for: Callable[[str], Mapping[str, str]] | None = None,
        memory: RecoveryMemory | None = None,
        tool_schema_dialect: ToolSchemaDialect = RESPONSES_TOOL_SCHEMA_DIALECT,
    ) -> None:
        self._config = config
        self._declared_tool_name_max_length = tool_name_max_length
        # What this host's validator refuses in a tool schema. The Responses
        # default unless the profile declared another vocabulary -- never
        # absent, because the seam it feeds is not optional: a Responses host
        # that could be built without it is how the lookaround 400 comes back.
        self._tool_schema_dialect = tool_schema_dialect
        # Which tool spellings this host wants for one model, or ``None`` for
        # a host with no catalogue of its own -- which is every host but
        # OpenCode's free tier, and is why a transport built without it sends
        # exactly the bytes it sent before 7.28.0. A callable rather than a
        # mapping because the answer is per *model*: the profile that declares
        # it fronts paid models too, and those are entitled to their own tool
        # names.
        self._tool_catalogue_for = tool_catalogue_for
        # What this host has taught MCC about its own Responses validator.
        # A bare transport (a unit test, an embedded use) gets an unpersisted
        # memory and behaves exactly as one built before 7.23.0 did: nothing
        # has been learned, so nothing is applied.
        self._memory = memory if memory is not None else RecoveryMemory()
        self._rate_limiter = rate_limiter
        self._base_url = base_url.rstrip("/")
        self._provider_name = provider_name
        self._identity = identity
        self._api_key = api_key
        self._api_key_provider = api_key_provider
        self._client = bound_socks_handshake(
            httpx.AsyncClient(
                proxy=config.proxy or None,
                timeout=httpx.Timeout(
                    config.http_read_timeout,
                    connect=config.http_connect_timeout,
                    read=config.http_read_timeout,
                    write=config.http_write_timeout,
                ),
            )
        )

    @property
    def tool_name_max_length(self) -> int | None:
        """The longest tool name this host accepts, or ``None`` for no limit.

        **The one resolver.** Declared first -- the profile's
        ``responses_tool_name_max_length``, which ``opencode`` and
        ``opencode_go`` set to 64 -- and otherwise whatever this host *stated*
        in a rejection and MCC wrote down. Never inferred from a model id or a
        provider name.

        It decides whether the body carries aliases and, with the same value,
        whether the stream decodes them. The two must agree or the model's
        call comes back under a name the client never sent, which is why both
        read this one property and why a learned limit is indistinguishable
        from a declared one everywhere below it.
        """

        if self._declared_tool_name_max_length is not None:
            return self._declared_tool_name_max_length
        return self._memory.responses_tool_name_max_length

    def tool_catalogue(self, request: MessagesRequest) -> Mapping[str, str]:
        """This host's own tool spellings for one request's model.

        Resolved in one place for the same reason
        :attr:`tool_name_max_length` is: the body encoder and the stream
        decoder must be handed the same answer or the model's call comes back
        under a name the client never sent.
        """

        if self._tool_catalogue_for is None:
            return EMPTY_TOOL_CATALOGUE
        return self._tool_catalogue_for(request.model)

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
        wire_notes: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Build one Responses body and the headers that will carry it.

        Returned together because they are not independent: the cache key in
        the body is the session id in the headers, and computing them apart is
        how they would drift.

        ``wire_notes`` receives what this host's declared schema dialect took
        out of the tools; hand the same mapping to :meth:`stream` so it is
        recorded beside the body it describes.

        Both learned refusals are applied here rather than paid for again: a
        stated tool-name ceiling aliases from the first try, and a model proven
        to take only ``auto`` never has a forced ``tool_choice`` built into its
        body at all. A host that has refused nothing gets the body it has
        always got.
        """

        body = build_responses_request_body(
            request,
            reasoning=reasoning,
            store=False,
            stream=True,
            parallel_tool_calls=False,
            max_output_tokens=max_output_tokens,
            extra_body=extra_body,
            tool_name_max_length=self.tool_name_max_length,
            tool_catalogue=self.tool_catalogue(request),
            include_tool_choice=not self._tool_choice_refused(request),
            tool_schema_dialect=self._tool_schema_dialect,
            wire_notes=wire_notes,
        )
        headers = self._headers(body)
        cache_key = self._prompt_cache_key(headers)
        if cache_key:
            body["prompt_cache_key"] = cache_key
        return body, headers

    def _learned_schema_refusals(self) -> tuple[SchemaKeywordRefusal, ...]:
        """Every schema-keyword class this host has been proven to refuse.

        Read off the memory on every send rather than captured once, so a
        *Forget* on the Models page reaches the very next request -- the store
        rebuilds the memory in place, and a transport holding its own copy
        would keep sweeping a keyword nobody believes in any more.
        """

        return tuple(
            refusal
            for detail in self._memory.responses_tool_schema_details()
            if (refusal := refusal_from_detail(detail)) is not None
        )

    def _tool_choice_refused(self, request: MessagesRequest) -> bool:
        """Whether this model has been proven to accept only ``auto``.

        A client that forced a tool still gets a correct answer: the Responses
        default *is* ``auto``, so the model is free to call the tool and
        normally does. If it calls a different one or none at all, that is the
        model's behaviour rather than MCC's -- and the marker
        :meth:`_note_dropped_tool_choice` writes is what makes the difference
        visible in the request log instead of mysterious.
        """

        if not self._memory.responses_tool_choice_refused(request.model):
            return False
        return _forces_a_tool(request.tool_choice)

    def _note_dropped_tool_choice(self, model: str) -> dict[str, str]:
        """The wire marker for a ``tool_choice`` this host would have refused.

        Returned as a mapping to merge into ``record_wire_request`` rather
        than written here, so it lands in the same ``params.wire`` record as
        the body it describes and can never be recorded for a request that was
        not actually sent.
        """

        learned = self._memory.responses_tool_choice_learned_on(model)
        return {
            "tool_choice_dropped": (
                "this model accepts only auto"
                + (f" (learned {learned})" if learned else "")
            )
        }

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
            # Raw first, decoded second, and the status raised whatever the
            # decode did. See ``providers/http.read_error_body``: an edge that
            # labels a refusal ``gzip`` when it is not used to raise
            # ``DecodingError`` here, one frame before the status existed, and
            # 293 refusals by the host were filed as faults of the model.
            error = await read_error_body(response)
            await response.aclose()
            note_response_head(error.head)
            raise httpx.HTTPStatusError(
                f"{self._provider_name} Responses API error {response.status_code}",
                request=request,
                response=httpx.Response(
                    response.status_code,
                    headers=error_response_headers(response.headers),
                    content=error.content,
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

    async def _send_with_recovery(
        self,
        request: MessagesRequest,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        surface_label: str,
        request_id: str | None,
        wire_notes: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one body, answering the two refusals this surface can recover.

        The same contract every rung in ``providers/recovery/ladder.py`` has:
        one rewrite per rung per request, the host's own words as the only
        trigger, and a rejection nothing recognises raised exactly as it was.
        A host that keeps refusing after its own stated fix fails visibly on
        the second try -- ``used`` is never cleared, so there is no loop.

        Placement inside the transport rather than beside the Chat
        Completions ladder is not a second ladder: both rungs are statements
        about a *Responses body* -- a ``tools`` catalogue and a
        ``tool_choice`` in the Responses spelling -- and neither can be
        applied to a Chat Completions body at all. The refusal has also never
        been demonstrated on Chat Completions (the 1,040 logged 400s and the
        2026-09-17 probe are all ``/zen/v1/responses``), and the scope rule
        for this project is to fix the path where it is broken.

        Ordering, and why the two rungs are in this order: tool-name length is
        a number the host *stated*, exactly the evidence class the output-cap
        rung sits first for, while ``tool_choice`` is the more destructive
        rewrite -- it removes an instruction the client gave. Narrowest and
        most certain first is the ladder's own rule, and a 400 that names
        ``tool_choice`` can never be read as a name-length complaint anyway
        (:func:`rejected_tool_name_max_length` refuses it outright), so the
        order is total rather than merely conventional.
        """

        used: set[str] = set()
        # What the rungs that have fired will teach the memory, written only
        # once a send has actually been accepted. The rule the reasoning strip
        # states and this one keeps: a rewrite that did not fix anything is not
        # evidence about the host.
        pending: list[_ResponsesLearning] = []
        # What this host has already been proven to refuse, applied before the
        # first send rather than after it: the 400 is then paid once per
        # provider instead of once per request, and the catalogue is the same
        # on every turn and after every restart -- which is what keeps the
        # vendor's implicit tools prefix cached from request two onward.
        # Identity when there is nothing to apply, so a host that has never
        # refused a schema sends exactly the bytes it always did.
        swept, learned_marker = apply_learned_tool_schema_refusals(
            body, self._learned_schema_refusals()
        )
        # Declared first, learned second: the order the two sweeps ran in.
        base_marker = merge_tool_schema_markers(wire_notes or {}, learned_marker)
        schema_marker = base_marker
        current = dict(swept)
        while True:
            identity = self.identity_headers(current)
            marker = (
                self._note_dropped_tool_choice(request.model)
                if self._tool_choice_refused(request)
                else {}
            )
            # The commit boundary, the same one the Chat Completions path has:
            # the body is final once it is handed to the sender, and the
            # surface it was sent on is recorded beside it.
            record_wire_request(
                current,
                surface=surface_label,
                **(
                    {"client_identity": identity_wire_record(identity)}
                    if identity
                    else {}
                ),
                **marker,
                **schema_marker,
            )
            try:
                response = await self._rate_limiter.execute_with_retry(
                    self.send, body=current, headers=headers
                )
            except Exception as error:
                learning = _next_responses_recovery(
                    request, error, current, used, self.tool_catalogue(request)
                )
                if learning is None:
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
                        cooldown=self._config.rate_limit_cooldown(),
                        mark_rate_limited_enabled=(
                            not self._config.routes_around_model
                        ),
                    ) from error
                logger.warning(
                    "{}_RESPONSES: {} -- retrying once ({})",
                    self._provider_name,
                    learning.log_line,
                    learning.evidence,
                )
                current = learning.body
                if isinstance(learning.value, ToolSchemaRecovery):
                    # Carried onto the retry row, so the modal names what the
                    # second body lost rather than leaving the operator to
                    # infer it from a rung name.
                    schema_marker = merge_tool_schema_markers(
                        base_marker, learning.value.marker
                    )
                pending.append(learning)
                # Carried on the *retry* row, so the ladder in the modal reads
                # "400 ... / 200 (responses_tool_choice)" and the operator can
                # see which rewrite the second body is.
                note_recovery_rung(learning.kind)
                continue
            for learned in pending:
                self._remember(request, learned)
            return response

    def _remember(self, request: MessagesRequest, learning: _ResponsesLearning) -> None:
        """Write one rung's learning down, now that a send has proven it."""

        if isinstance(learning.value, ToolSchemaRecovery):
            recovery = learning.value
            self._memory.remember_responses_tool_schema_refusal(
                recovery.refusal.detail, evidence=learning.evidence
            )
            logger.warning(
                "{}_RESPONSES: this host refuses {} in tool schemas -- later "
                "requests are swept before the first send",
                self._provider_name,
                recovery.refusal.words,
            )
            return
        if learning.kind == _RUNG_TOOL_NAME_LENGTH:
            self._memory.learn_responses_tool_name_limit(
                int(learning.value), evidence=learning.evidence
            )
            logger.warning(
                "{}_RESPONSES: this host caps tool names at {} -- later "
                "requests alias from the first try",
                self._provider_name,
                learning.value,
            )
            return
        if self._memory.remember_responses_tool_choice_refusal(
            request.model, evidence=learning.evidence
        ):
            logger.warning(
                "{}_RESPONSES: {} accepts only tool_choice=auto -- later "
                "requests omit the field without paying the rejection",
                self._provider_name,
                request.model,
            )

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
        wire_notes: Mapping[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Run one Responses request and yield Anthropic SSE.

        The body and headers are built by the caller (:meth:`build_body`) so
        that the surface rung can hand the *same* request to a second endpoint
        after the first refused it, rather than rebuilding one and hoping the
        two match. ``wire_notes`` is what that build recorded about the body.
        """

        async def _run() -> AsyncIterator[str]:
            ledger = AnthropicStreamLedger(
                f"msg_{uuid.uuid4()}",
                request.model,
                input_tokens,
                log_raw_events=self._config.log_raw_sse_events,
            )
            async with self._rate_limiter.concurrency_slot():
                response = await self._send_with_recovery(
                    request,
                    body=body,
                    headers=headers,
                    surface_label=surface_label,
                    request_id=request_id,
                    wire_notes=wire_notes,
                )
                # Built from the ceiling the accepted body was aliased under,
                # not from the one the first try used: decoding has to undo
                # exactly the encoding that went out, and a learned limit is
                # resolved between those two moments.
                converter = ResponsesStreamConverter(
                    ledger,
                    log_raw_events=self._config.log_raw_sse_events,
                    output_reasoning=reasoning.output_enabled,
                    tool_names=responses_tool_name_codec(
                        request,
                        self.tool_name_max_length,
                        self.tool_catalogue(request),
                    ),
                )
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
