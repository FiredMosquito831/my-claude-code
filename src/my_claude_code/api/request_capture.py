"""Per-request analytics capture at the handler/stream layer.

One :class:`RequestCapture` per request accumulates routing metadata, output
text, usage and timing from the Anthropic SSE stream, then enqueues exactly
one :class:`RequestRecord` into the request log store when the request
terminates (success, error, or client cancellation).
"""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.application.routing import (
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.model_refs import format_model_ref_list
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    ImageInput,
    MessagesRequest,
    request_image_inputs,
)
from my_claude_code.core.async_iterators import try_close_async_iterator
from my_claude_code.core.credential_attribution import install_attribution
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import failure_kind_name, find_execution_failure
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
    combine_reasoning_adaptations,
)
from my_claude_code.core.request_headers import capture_headers
from my_claude_code.core.request_images import capture_images
from my_claude_code.core.request_log import (
    MAX_TEXT_CHARS,
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    install_recovery_trace,
    store_from_settings,
)
from my_claude_code.core.upstream_ladder import (
    DEFAULT_LADDER_BODY_MAX_CHARS,
    install_ladder_trace,
    ladder_payload,
    ladder_root_cause,
)
from my_claude_code.core.wire_capture import (
    DEFAULT_WIRE_BODY_MAX_CHARS,
    WireRequest,
    install_wire_trace,
)

WireProtocol = Literal["anthropic", "openai_responses"]


class RequestCapture:
    """Accumulate one request's analytics and emit exactly one log record."""

    def __init__(
        self,
        store: RequestLogStore | None,
        *,
        request_id: str,
        endpoint: str,
        protocol: WireProtocol,
        stream: bool,
        requested_model: str | None,
        input_text: str | None,
        params: dict[str, Any] | None,
        capture_bodies: bool = True,
        images: tuple[ImageInput, ...] = (),
        capture_images_pixels: int = 0,
        wire_body_max_chars: int = DEFAULT_WIRE_BODY_MAX_CHARS,
        ladder_body_max_chars: int = DEFAULT_LADDER_BODY_MAX_CHARS,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._capture_bodies = capture_bodies
        # Held undecoded until the request is over. Thumbnailing is real CPU
        # work and there is no reason for it to sit between the client and its
        # first token, so it happens at finalize time instead.
        self._images = images
        self._capture_images_pixels = capture_images_pixels
        # Filled once, at the end of the chain, not per attempt: an attempt
        # verdict is never worth a database round trip while a client is
        # still waiting for tokens.
        self._attempts: list[RouteAttempt] = []
        # The client's own ``max_tokens`` for any attempt whose allowance was
        # raised because it was going to think, keyed by attempt index. Kept
        # beside the attempts rather than on the record: it is a per-attempt
        # fact, and a fallback on a smaller model may not have been widened at
        # all. Absent means "the client's ask is what left", the common case.
        self._output_widened_from: dict[int, int] = {}
        self._start = time.perf_counter()
        self._ttft_ms: float | None = None
        self._output_parts: list[str] = []
        self._output_chars = 0
        self._stored_chars = 0
        self._thinking_parts: list[str] = []
        self._thinking_chars = 0
        self._stored_thinking_chars = 0
        # Streamed tool calls arrive as a ``content_block_start`` naming the
        # tool followed by ``input_json_delta`` fragments, both keyed by block
        # index, so the partial arguments are accumulated per index.
        self._tool_blocks: dict[int, dict[str, Any]] = {}
        self._tokens_in: int | None = None
        self._cache_read_tokens: int | None = None
        self._cache_write_tokens: int | None = None
        self._tokens_out: int | None = None
        self._primary_model_ref: str | None = None
        self._error: tuple[str | None, str | None] | None = None
        self._finalized = False
        # The rotating provider writes the credential it picks into this slot
        # from deep in the call stack; it is read back at finalize time.
        self._credential = install_attribution()
        # Stream-recovery counters arrive the same way: a provider's runner
        # increments this collector from inside its holdback and retry
        # machinery, however many context copies the streaming response runs
        # through. Only a logged request installs one, so providers exercised
        # directly stay unrecorded.
        self._recovery = install_recovery_trace() if self.enabled else None
        # The outbound body arrives the same way, from the one statement in
        # each provider that hands a body to its SDK. Reading ``max_tokens``
        # and the tool count off the *inbound* request here -- which is what
        # ``params`` below still does, deliberately, as the client's ask --
        # reported the client's numbers as if they were the wire's.
        self._wire = install_wire_trace(wire_body_max_chars) if self.enabled else None
        # Every upstream try behind each attempt arrives the same way, from the
        # one retry frame every provider commits through. Without it an attempt
        # row carried one status however many the provider had actually seen.
        self._ladder = (
            install_ladder_trace(ladder_body_max_chars) if self.enabled else None
        )
        # Routing's own verdict, kept so a provider-level adaptation recorded
        # after the request left can be merged with it at commit time rather
        # than overwriting it.
        self._reasoning_adaptation: ReasoningAdaptation | None = None
        input_chars = len(input_text) if input_text else None
        self._record = RequestRecord(
            id=request_id,
            endpoint=endpoint,
            protocol=protocol,
            stream=stream,
            requested_model=requested_model,
            input_text=input_text if capture_bodies else None,
            input_sha256=(
                None if input_text is None or capture_bodies else _sha256(input_text)
            ),
            input_chars=input_chars,
            params=params,
            headers=headers,
        )

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def record_attempt_result(self, attempt: RouteAttemptRecord) -> None:
        """Store one model's verdict for the request log.

        The chain's own account of itself: which models it tried, which it
        benched, which it never reached, and why. The request row can only name
        the model that answered, so without this a fallback that rescued a
        request left no trace of what it rescued it from.
        """
        if not self.enabled:
            return
        wire = None if self._wire is None else self._wire.requests.get(attempt.attempt)
        ladder = self._ladder_payload(attempt)
        key_index, key_label = self._attempt_credential(ladder)
        self._attempts.append(
            RouteAttempt(
                attempt=attempt.attempt,
                provider=attempt.provider_id or None,
                model_ref=attempt.model_ref or None,
                outcome=RouteAttemptOutcome(attempt.outcome),
                error_kind=attempt.error_kind,
                error_message=attempt.error_message,
                duration_ms=attempt.duration_ms,
                params=self._attempt_params(attempt.attempt, wire, ladder),
                wire_body=None if wire is None else wire.body_json,
                reasoning_emitted=None if wire is None else wire.reasoning_emitted,
                key_index=key_index,
                key_label=key_label,
                ladder_tries=None if ladder is None else len(ladder["tries"]),
            )
        )

    def _ladder_payload(self, attempt: RouteAttemptRecord) -> dict[str, Any] | None:
        """Render this attempt's upstream ladder, root-cause line included.

        The sentence is stored rather than recomputed in the dashboard, so the
        modal, all four exports and a test all read the same string.
        """
        if self._ladder is None:
            return None
        ladder = self._ladder.ladders.get(attempt.attempt)
        if ladder is None or not ladder.tries:
            return None
        payload = ladder_payload(ladder)
        payload["root_cause"] = ladder_root_cause(
            payload,
            attempt_error_kind=attempt.error_kind,
            attempt_duration_ms=attempt.duration_ms,
        )
        return payload

    def _attempt_credential(
        self, ladder: dict[str, Any] | None
    ) -> tuple[int | None, str | None]:
        """The credential *this* attempt used, not the chain's last one.

        The observer that writes these rows fires once, at the end of the
        chain, for every attempt in one loop -- so reading the shared
        attribution slot here stamped the last key of the whole request onto
        every row, including skipped attempts and attempts against a different
        provider's pool entirely. The ladder knows which key each try actually
        held; the slot is the fallback for an attempt that recorded no try.
        """
        if ladder is not None:
            for row in reversed(ladder["tries"]):
                if row.get("source") != "upstream":
                    continue
                if "key_index" in row:
                    return row["key_index"], row.get("key_label")
        return self._credential.index, self._credential.label

    def _attempt_params(
        self,
        attempt_index: int,
        wire: WireRequest | None,
        ladder: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Merge what the provider survived with what it actually sent.

        Both are facts about one attempt, and ``params`` is the column that
        already models them. The wire summary is nested under ``wire`` so the
        flat recovery counters keep their existing shape.
        """
        params = self._recovery_events_for(attempt_index) or {}
        if wire is not None and wire.params:
            params["wire"] = wire.params
        # "From" for the "to" that ``params.wire.max_tokens`` already carries.
        # Flat, beside the recovery counters, because it is a fact about the
        # decision rather than about the body that left.
        widened = self._output_widened_from.get(attempt_index)
        if widened is not None:
            params["output_widened_from"] = widened
        # Nested beside ``wire`` for the same reason: it is a list of facts of
        # variable shape about one attempt, and the flat counters above must
        # keep theirs.
        if ladder is not None:
            params["ladder"] = ladder
        return params or None

    def _recovery_events_for(self, attempt_index: int) -> dict[str, Any] | None:
        """Snapshot the recovery counters recorded while this attempt ran."""
        if self._recovery is None:
            return None
        events = self._recovery.events.get(attempt_index)
        return dict(events) if events else None

    def set_plan(self, plan: RoutedMessagesPlan) -> None:
        """Record the whole routing decision before any attempt is made.

        The chain is stored even when the primary answers: "a chain existed and
        was not needed" and "there was no chain" are different facts, and only
        the first one tells you your fallbacks are configured. The diversion
        pair is the only trace that the vision adapter did anything -- without
        it a diverted request is indistinguishable from a route that points at
        the adapter model directly.
        """
        if not self.enabled:
            return
        self._record.route_chain = format_model_ref_list(plan.model_refs())
        self._record.route_diverted_from = plan.diverted_from
        self._record.route_diversion = (
            plan.diversion.value if plan.diversion is not None else None
        )

    def set_routing(self, routed: RoutedMessagesRequest, attempt: int = 0) -> None:
        """Attach provider/model/reasoning metadata for the attempt in flight.

        Called again for each fallback, so the row always names the model that
        actually answered. The first call also remembers the route's own model,
        which is the only way to tell afterwards what it fell back *from*.
        """
        if not self.enabled:
            return
        # Attempt boundary for recovery attribution: everything a provider
        # records from here until the next boundary belongs to this chain
        # index -- including every counter on a single-model route.
        if self._recovery is not None:
            self._recovery.current_attempt = attempt
        if self._wire is not None:
            self._wire.current_attempt = attempt
        if self._ladder is not None:
            self._ladder.current_attempt = attempt
        if attempt == 0:
            self._primary_model_ref = routed.resolved.provider_model_ref
        self._record.route_attempt = attempt
        self._record.route_primary_model = (
            self._primary_model_ref if attempt > 0 else None
        )
        self._record.provider = routed.resolved.provider_id
        self._record.resolved_model = routed.resolved.provider_model
        # Recorded per attempt, and only when the reasoning widening actually
        # raised the number that will be sent. ``None`` is not stored: absence
        # is the finding, exactly as it is for every other wire knob.
        if routed.output_widened_from is not None:
            self._output_widened_from[attempt] = routed.output_widened_from
        # ``reasoning`` is the applied policy (post per-model gating);
        # ``requested_reasoning`` is what was asked for before it. They are
        # equal on an ungated request and differ exactly when the model's
        # capability changed what we sent.
        self._record.reasoning = _describe_reasoning(routed.reasoning)
        self._record.requested_reasoning = _describe_reasoning(
            routed.requested_reasoning,
            client_thinking_type=_client_thinking_type(routed.request),
        )
        # Why the applied policy differs from what was asked for: the warning
        # gating would otherwise emit only to the server log, now surfaced in
        # the request log and admin UI. NULL whenever gating changed nothing.
        self._reasoning_adaptation = routed.reasoning_adaptation
        self._record.reasoning_adaptation = routed.reasoning_adaptation.message
        # The message is prose and PR-owned; the kind is the programmatic
        # signal the wire pane styles on, so a reworded warning can never
        # move a badge. UNCHANGED is stored as NULL: nothing happened.
        kind = routed.reasoning_adaptation.kind
        self._record.reasoning_adaptation_kind = (
            None if kind is ReasoningAdaptationKind.UNCHANGED else str(kind)
        )

    def set_optimization(self, rule: str, tokens_saved: int) -> None:
        """Record that a local rule answered this request, and drop the route.

        ``set_routing`` has already run by the time an intercept fires, so the
        row names the provider the request *would* have gone to. Leaving it
        there is an active lie: 3,246 rows in a production log were attributed
        to providers that never received them, dragging every per-provider
        average with them. The model the route resolved to is still recorded on
        ``requested_model`` and ``route_chain``, so what would have happened
        stays answerable -- only the claim that it *did* happen is removed.

        ``tokens_in``/``tokens_out`` stay NULL rather than 0: NULL is silence,
        and no provider spoke. What was avoided lives in its own column.
        """
        if not self.enabled:
            return
        self._record.optimization = rule
        self._record.optimization_tokens_saved = tokens_saved
        self._record.provider = None
        self._record.resolved_model = None

    def finish_error(self, exc: BaseException) -> None:
        """Finalize for an error raised before the stream wrapper takes over."""
        failure = find_execution_failure(exc)
        message = (
            failure.message if failure is not None else safe_exception_message(exc)
        )
        self._error = (failure_kind_name(exc), message)
        self._finalize("error")

    def finish_success(self, output_text: str | None = None) -> None:
        """Finalize a non-streamed (short-circuited) successful response."""
        if output_text:
            self._append_output(output_text)
        self._finalize("success")

    def finish_success_from_message(self, message: Any) -> None:
        """Finalize from a complete message, keeping its blocks apart."""
        turn = extract_turn_from_message(message)
        if turn.text:
            self._append_output(turn.text)
        if turn.thinking:
            self._append_thinking(turn.thinking)
        for index, call in enumerate(turn.tool_calls):
            name = call.get("name")
            self._tool_blocks[index] = {
                "name": name if isinstance(name, str) else "(unnamed tool)",
                "parts": [json.dumps(call.get("input") or {})],
            }
        self._finalize("success")

    def wrap(self, body: AsyncIterator[str]) -> AsyncIterator[str]:
        """Wrap the Anthropic SSE stream, observing every chunk pass through."""
        if not self.enabled:
            return body
        return self._observe(body)

    async def _observe(self, body: AsyncIterator[str]) -> AsyncIterator[str]:
        buffer = ""
        status: Literal["success", "error", "cancelled"] = "success"
        saw_chunk = False
        try:
            async for chunk in body:
                if self._ttft_ms is None:
                    self._ttft_ms = (time.perf_counter() - self._start) * 1000
                saw_chunk = True
                buffer = self._consume_buffer(buffer + chunk)
                yield chunk
            if self._error is not None:
                status = "error"
            elif not saw_chunk:
                self._error = ("empty_stream", "Stream ended before any content.")
                status = "error"
        except GeneratorExit:
            status = "cancelled"
            self._finalize(status)
            await try_close_async_iterator(body)
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            self._finalize(status)
            raise
        except BaseException as exc:
            failure = find_execution_failure(exc)
            self._error = (
                failure_kind_name(exc),
                failure.message if failure is not None else safe_exception_message(exc),
            )
            status = "error"
            raise
        finally:
            if status != "cancelled":
                self._finalize(status)

    def _consume_buffer(self, buffer: str) -> str:
        """Parse complete SSE frames from the buffer; return the remainder."""
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            self._parse_frame(frame)
        return buffer

    def _parse_frame(self, frame: str) -> None:
        data_lines: list[str] = [
            line[len("data:") :].strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            return
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        if event_type == "message_start":
            message = payload.get("message")
            if isinstance(message, dict):
                usage = message.get("usage")
                if isinstance(usage, dict):
                    self._tokens_in = _int_or_none(usage.get("input_tokens"))
                    self._read_cache_usage(usage)
        elif event_type == "content_block_start":
            self._start_content_block(payload)
        elif event_type == "content_block_delta":
            self._consume_content_delta(payload)
        elif event_type == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                output_tokens = _int_or_none(usage.get("output_tokens"))
                if output_tokens is not None:
                    self._tokens_out = output_tokens
                # message_start carries our own pre-flight estimate, because
                # the upstream has not reported anything yet. The real count
                # arrives here, and it is the one worth keeping -- storing the
                # estimate alongside a provider-reported cache figure produced
                # rows where the cached tokens exceeded the whole input.
                input_tokens = _int_or_none(usage.get("input_tokens"))
                if input_tokens is not None:
                    self._tokens_in = input_tokens
                # Anthropic-native upstreams report cache counters up front on
                # message_start, but everything translated from an OpenAI-shaped
                # provider only learns them from the final usage chunk, so they
                # arrive here. Reading both is what makes the figure appear for
                # OpenRouter, DeepSeek and the rest.
                self._read_cache_usage(usage)
        elif event_type == "error":
            error = payload.get("error")
            if isinstance(error, dict):
                kind = error.get("type")
                message = error.get("message")
                self._error = (
                    kind if isinstance(kind, str) else "api_error",
                    message if isinstance(message, str) else "Stream error.",
                )

    def _start_content_block(self, payload: dict[str, Any]) -> None:
        """Note a tool_use block so its streamed arguments can be attributed."""
        block = payload.get("content_block")
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return
        index = _int_or_none(payload.get("index"))
        if index is None:
            return
        name = block.get("name")
        self._tool_blocks[index] = {
            "name": name if isinstance(name, str) else "(unnamed tool)",
            "parts": [],
        }

    def _consume_content_delta(self, payload: dict[str, Any]) -> None:
        """Route a block delta to prose, reasoning, or tool arguments."""
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text")
            if isinstance(text, str):
                self._append_output(text)
        elif delta_type == "thinking_delta":
            thinking = delta.get("thinking")
            if isinstance(thinking, str):
                self._append_thinking(thinking)
        elif delta_type == "input_json_delta":
            index = _int_or_none(payload.get("index"))
            block = self._tool_blocks.get(index) if index is not None else None
            partial = delta.get("partial_json")
            if block is not None and isinstance(partial, str):
                block["parts"].append(partial)

    def _read_cache_usage(self, usage: dict[str, object]) -> None:
        """Record cache counters from whichever usage payload carries them."""

        cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
        if cache_read is not None:
            self._cache_read_tokens = cache_read
        cache_write = _int_or_none(usage.get("cache_creation_input_tokens"))
        if cache_write is not None:
            self._cache_write_tokens = cache_write

    def _append_output(self, text: str) -> None:
        self._output_chars += len(text)
        remaining = MAX_TEXT_CHARS - self._stored_chars
        if remaining > 0:
            self._output_parts.append(text[:remaining])
            self._stored_chars += min(remaining, len(text))

    def _append_thinking(self, text: str) -> None:
        self._thinking_chars += len(text)
        remaining = MAX_TEXT_CHARS - self._stored_thinking_chars
        if remaining > 0:
            self._thinking_parts.append(text[:remaining])
            self._stored_thinking_chars += min(remaining, len(text))

    def _collected_tool_calls(self) -> list[dict[str, Any]]:
        """Return the streamed tool calls in block order, arguments parsed."""
        calls: list[dict[str, Any]] = []
        for index in sorted(self._tool_blocks):
            block = self._tool_blocks[index]
            raw = "".join(block["parts"])
            call: dict[str, Any] = {"name": block["name"]}
            # A cancelled or truncated stream leaves the argument JSON
            # incomplete; keep the fragment rather than dropping the call.
            try:
                call["input"] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                call["input_partial"] = raw[:MAX_TEXT_CHARS]
            calls.append(call)
        return calls

    def _merge_provider_reasoning_adaptations(self) -> None:
        """Fold a create-level reasoning strip into the row's single verdict.

        Routing decides before the request leaves and writes its verdict in
        :meth:`set_routing`; a create-level retry decides after the host has
        already refused it. The row has one verdict, so the two are combined
        under the more severe kind, with both messages kept in the order they
        happened. ``UNCHANGED`` stays NULL, exactly as ``set_routing`` stores
        it.
        """
        if self._wire is None or not self._wire.reasoning_adaptations:
            return
        routed = self._reasoning_adaptation
        parts = [] if routed is None else [routed]
        parts.extend(self._wire.reasoning_adaptations)
        combined = combine_reasoning_adaptations(*parts)
        self._record.reasoning_adaptation = combined.message
        self._record.reasoning_adaptation_kind = (
            None
            if combined.kind is ReasoningAdaptationKind.UNCHANGED
            else str(combined.kind)
        )

    def _finalize(self, status: Literal["success", "error", "cancelled"]) -> None:
        if self._finalized or self._store is None:
            self._finalized = True
            return
        self._finalized = True
        record = self._record
        self._merge_provider_reasoning_adaptations()
        record.status = status
        record.duration_ms = (time.perf_counter() - self._start) * 1000
        record.ttft_ms = self._ttft_ms
        record.tokens_in = self._tokens_in
        record.tokens_out = self._tokens_out
        record.cache_read_tokens = self._cache_read_tokens
        record.cache_write_tokens = self._cache_write_tokens
        output_text = "".join(self._output_parts)
        record.output_chars = self._output_chars
        if self._capture_bodies:
            record.output_text = output_text or None
        elif output_text:
            record.output_sha256 = _sha256(output_text)
        tool_calls = self._collected_tool_calls()
        record.tool_call_count = len(tool_calls) or None
        # 0 is a measurement ("this stream returned no reasoning"), NULL is
        # the absence of one ("nobody was counting"). Folding them together
        # with ``or None`` made a silent thinking model indistinguishable from
        # an unmeasured row, which is precisely the question
        # ``reasoning_by_model`` exists to answer. Rows written before 6.8.0
        # keep their NULL and keep counting as unmeasured; a backfill would
        # invent measurements.
        record.thinking_chars = self._thinking_chars
        # Reasoning text and tool arguments are request bodies, so they follow
        # the same capture switch; the counts above stay either way.
        if self._capture_bodies:
            record.thinking_text = "".join(self._thinking_parts) or None
            record.tool_calls = tool_calls or None
        if self._error is not None:
            record.error_kind, record.error_message = self._error
        record.input_image_count = len(self._images) or None
        if self._images:
            record.images = capture_images(
                self._images,
                max_pixels=self._capture_images_pixels,
                store_pixels=self._capture_images_pixels > 0,
            )
        record.key_index = self._credential.index
        record.key_label = self._credential.label
        record.attempts = tuple(self._attempts)
        self._store.enqueue(record)


def build_capture(
    settings: Settings,
    request: MessagesRequest,
    *,
    request_id: str,
    endpoint: str,
    protocol: WireProtocol,
    headers: Mapping[str, str] | None = None,
) -> RequestCapture:
    """Create the capture for one request; inert when logging is disabled."""
    store = store_from_settings(settings)
    return RequestCapture(
        store,
        request_id=request_id,
        endpoint=endpoint,
        protocol=protocol,
        stream=bool(request.stream),
        requested_model=request.model,
        input_text=extract_input_text(request),
        params=extract_request_params(request),
        capture_bodies=bool(getattr(settings, "request_log_capture_bodies", True)),
        images=request_image_inputs(request),
        capture_images_pixels=_image_pixels(settings),
        wire_body_max_chars=int(
            getattr(
                settings,
                "request_log_wire_body_max_chars",
                DEFAULT_WIRE_BODY_MAX_CHARS,
            )
        ),
        ladder_body_max_chars=int(
            getattr(
                settings,
                "request_log_ladder_body_max_chars",
                DEFAULT_LADDER_BODY_MAX_CHARS,
            )
        ),
        headers=capture_headers(headers),
    )


def _image_pixels(settings: Settings) -> int:
    """Return the thumbnail edge to store, or 0 to record images without pixels."""
    if not getattr(settings, "request_log_capture_images", True):
        return 0
    return int(getattr(settings, "request_log_image_max_pixels", 0) or 0)


def extract_input_text(request: MessagesRequest) -> str | None:
    """Concatenate system and message text for the request log."""
    parts: list[str] = []
    system = request.system
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    for message in request.messages:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
    joined = "\n".join(part for part in parts if part)
    return joined or None


def extract_request_params(request: MessagesRequest) -> dict[str, Any]:
    """Snapshot non-credential request parameters for the request log."""
    params: dict[str, Any] = {
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "top_k": request.top_k,
        "stop_sequences": request.stop_sequences,
        "tools_count": len(request.tools) if request.tools else 0,
        "tool_choice": request.tool_choice,
        "thinking": (
            request.thinking.model_dump(mode="json", exclude_none=True)
            if request.thinking is not None
            else None
        ),
    }
    return {key: value for key, value in params.items() if value is not None}


def _describe_reasoning(
    policy: ReasoningPolicy, *, client_thinking_type: str | None = None
) -> str | None:
    parts = [f"control={policy.control.value}"]
    # A client asking for Anthropic's adaptive thinking resolves to control=on,
    # because "adaptive" is not representable on providers without an adaptive
    # channel and they must keep receiving a thinking request. That makes the
    # control alone unable to tell "the client asked for adaptive" from "the
    # client asked for enabled", so the client's own wording is recorded beside
    # it. This is a recording-only note: the resolved policy, and therefore
    # every outgoing request, is untouched by it.
    if client_thinking_type == "adaptive":
        parts.append("client=adaptive")
    if policy.effort is not None:
        parts.append(f"effort={policy.effort.value}")
    if policy.budget_tokens is not None:
        parts.append(f"budget={policy.budget_tokens}")
    return ",".join(parts)


def _client_thinking_type(request: MessagesRequest) -> str | None:
    """Return the ``thinking.type`` the client itself sent, if any."""

    thinking = request.thinking
    if thinking is None or not isinstance(thinking.type, str):
        return None
    return thinking.type.strip().lower() or None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclass(frozen=True, slots=True)
class MessageTurn:
    """The three kinds of block a complete assistant message can carry."""

    text: str | None
    thinking: str | None
    tool_calls: list[dict[str, Any]]


def extract_turn_from_message(message: Any) -> MessageTurn:
    """Split a complete Anthropic message into prose, reasoning and tool calls."""
    blocks = _message_content_blocks(message)
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif block_type == "thinking" and isinstance(block.get("thinking"), str):
            thinking_parts.append(block["thinking"])
        elif block_type == "tool_use":
            name = block.get("name")
            tool_calls.append(
                {
                    "name": name if isinstance(name, str) else "(unnamed tool)",
                    "input": block.get("input") or {},
                }
            )
    return MessageTurn(
        text="\n".join(text_parts) or None,
        thinking="\n".join(thinking_parts) or None,
        tool_calls=tool_calls,
    )


def _message_content_blocks(message: Any) -> list[dict[str, Any]]:
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        message = model_dump(mode="json")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


__all__ = [
    "MessageTurn",
    "RequestCapture",
    "build_capture",
    "extract_input_text",
    "extract_request_params",
    "extract_turn_from_message",
]
