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
from dataclasses import dataclass, replace
from typing import Any, Literal

from loguru import logger

from my_claude_code.api.request_pricing import rate_cards
from my_claude_code.application.cost import MODE_AUTO, TokenUsage, resolve_cost
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.application.routing import (
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.application.vision_describe import describe_attempt_index
from my_claude_code.config.model_refs import format_model_ref_list
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    ImageInput,
    MessagesRequest,
    get_token_count,
    request_image_inputs,
)
from my_claude_code.core.anthropic.image_tokens import ImageTokenFamily, image_tokens
from my_claude_code.core.async_iterators import try_close_async_iterator
from my_claude_code.core.client_fingerprint import (
    harness_from_headers,
    install_fingerprint,
)
from my_claude_code.core.credential_attribution import install_attribution
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import failure_kind_name, find_execution_failure
from my_claude_code.core.image_geometry import image_dimensions
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
    combine_reasoning_adaptations,
)
from my_claude_code.core.reported_cost import install_reported_cost
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
from my_claude_code.core.waiting_clock import install_waiting_clock
from my_claude_code.core.wire_capture import (
    DEFAULT_WIRE_BODY_MAX_CHARS,
    WireRequest,
    install_wire_trace,
)


def _usage_int(usage: Mapping[str, Any] | None, key: str) -> int | None:
    """Read one token counter out of a reply's usage block.

    ``None`` for anything that is not a number, which is the same rule the
    request row's own counters follow: a host that reports no usage leaves NULL
    rather than a zero nobody measured.
    """
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _is_describe(attempt: RouteAttempt) -> bool:
    """Return whether this attempt row is a vision-adapter describe hop."""
    params = attempt.params
    return isinstance(params, dict) and params.get("kind") == "describe"


def _estimated_image_tokens(image: ImageInput, family: str) -> int:
    """What the estimator expects this one picture to cost on this host.

    The same arithmetic ``core.anthropic.tokens`` applies inside the request
    total, run again over just the pictures so the two numbers can be compared:
    ``est_image_tokens`` is the share of ``est_tokens_in`` that is images, and
    the Models page's billed-vs-estimated readout is only meaningful because
    they are computed from the same formula on the same dimensions.
    """
    if not image.data:
        return 85
    size = image_dimensions(image.data)
    if size is None:
        return max(85, len(image.data) // 3000)
    return image_tokens(size[0], size[1], family)


#: The inbound wire protocol a logged request arrived on. Stored verbatim in
#: the request log's ``protocol`` column, shown in the request detail pane, and
#: exported as-is, so a value added here becomes a user-visible vocabulary word.
WireProtocol = Literal["anthropic", "openai_responses", "openai_chat", "gemini"]


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
        harness: str | None = None,
        request: MessagesRequest | None = None,
        cost_enabled: bool = True,
        cost_mode: str = MODE_AUTO,
        cost_litellm_enabled: bool = False,
    ) -> None:
        self._store = store
        self._capture_bodies = capture_bodies
        self._cost_enabled = cost_enabled
        self._cost_mode = cost_mode
        self._cost_litellm_enabled = cost_litellm_enabled
        # Held undecoded until the request is over. Thumbnailing is real CPU
        # work and there is no reason for it to sit between the client and its
        # first token, so it happens at finalize time instead.
        self._images = images
        self._capture_images_pixels = capture_images_pixels
        # Position in the image walk -> the size that image actually left at,
        # for the ones the outbound downscaler shrank. Per attempt, and the
        # last attempt wins, exactly as ``image_delivery`` does: a chain that
        # falls back from one host to another may resize to two different
        # sizes, and the row must describe the one that answered.
        self._image_sent_sizes: dict[int, tuple[int, int]] = {}
        self._image_bytes: tuple[int, int] | None = None
        # How the answering host bills a picture, and the request the estimate
        # is computed against. Held so the estimate can be produced at finalize
        # -- after the client has its tokens -- rather than on the hot path.
        self._image_token_family: str | None = None
        self._request = request
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
        # Whether the upstream stream reached its own terminal event. It is
        # what separates "the reader stopped reading after the answer was
        # complete" from "the reader gave up mid-answer", and only the second
        # is a cancellation. See ``_observe``.
        self._saw_terminal_event = False
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
        # A host's own answer about what this request cost arrives the same
        # way, from the one statement in the OpenAI-shaped stream runner that
        # sees the final usage block. Anthropic SSE has no field for a cost,
        # so it cannot travel with the stream, and putting a proprietary
        # number in front of the client to move it two layers would be worse
        # than a collector.
        self._reported_cost = (
            install_reported_cost() if self.enabled and cost_enabled else None
        )
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
        # Unconditionally, unlike every trace above it: what a first-token
        # deadline measures must not depend on whether the request log is on.
        # Providers credit the seconds they spend asleep here, and the
        # executor's chunk wait re-arms for exactly those seconds.
        install_waiting_clock()
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
            harness=harness,
        )

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def record_describe_attempt(
        self,
        attempt: RouteAttemptRecord,
        image_sha: str,
        image_index: int,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Store one upstream try a describe call made, against this request.

        A describe call is an extra hop on the request that carried the
        picture, not traffic of its own: it has no request id, no client and no
        row in ``requests``. Recording it here is what makes the dashboard draw
        it as what it is, and what lets an operator add up what describe mode
        actually cost -- the per-image breakdown, which is the thing worth
        having, rather than one summed number on the request row.

        The attempt index is offset clear of the parent chain's own indexes
        because (request id, attempt) is the primary key of the attempt table.
        No wire body is attached: the wire trace is keyed by attempt index
        within one executor run, and the parent's own run overwrites those
        slots afterwards, so a body claimed here would be the wrong body.

        ``usage`` is the describe reply's own token counts. It goes on the
        attempt row and, summed, onto the request row's ``adapter_tokens_*``;
        it is never added into ``tokens_in``, which measures the model that
        answered the client and has measured only that since the log existed.
        ``None`` is not measured -- a failed attempt, or a host that reports no
        usage -- and stays NULL rather than becoming a confident zero.
        """
        if not self.enabled:
            return
        self._attempts.append(
            RouteAttempt(
                tokens_in=_usage_int(usage, "input_tokens"),
                tokens_out=_usage_int(usage, "output_tokens"),
                attempt=describe_attempt_index(image_index, attempt.attempt),
                provider=attempt.provider_id or None,
                model_ref=attempt.model_ref or None,
                outcome=RouteAttemptOutcome(attempt.outcome),
                error_kind=attempt.error_kind,
                error_message=attempt.error_message,
                duration_ms=attempt.duration_ms,
                params={
                    "kind": "describe",
                    "image_sha": image_sha,
                    "image_index": image_index,
                    "cached": False,
                },
            )
        )

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
        shape = (
            None if self._wire is None else self._wire.responses.get(attempt.attempt)
        )
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
                params=self._attempt_params(
                    attempt.attempt,
                    wire,
                    ladder,
                    attempt.bench,
                    attempt.truncated,
                    attempt.continuation,
                    shape,
                ),
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
        bench: dict[str, Any] | None = None,
        truncated: dict[str, Any] | None = None,
        continuation: dict[str, Any] | None = None,
        shape: dict[str, Any] | None = None,
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
        # Same reason again: the registry's account of a bench is a small
        # record about one attempt, so it nests rather than flattening five
        # more keys into the counters.
        if bench:
            params["bench"] = bench
        # And again: what the reader was left with when a committed stream was
        # ended early. It belongs on the attempt rather than the request,
        # because the request row's status is about what the client received --
        # a valid message -- and this is about which model failed to finish it.
        if truncated:
            params["truncated_after_commit"] = truncated
        # Which model this attempt inherited a half-written answer from, and
        # whether what it wrote was usable. The request row names the model
        # that *finished*, which is what "which model answered this" means to
        # every existing consumer; that another model started it is recoverable
        # only from here, so it is recorded here rather than as a column
        # nothing else would read.
        if continuation:
            params["continuation"] = continuation
        # The reply's shape, opposite the body that asked for it. Nested for
        # the same reason as ``wire``: it is a small structured record about
        # one attempt, and the flat counters above must keep their shape.
        # Absent means not measured, which is not the same as "nothing came
        # back" -- an attempt that was skipped has no reply to describe.
        if shape:
            params["response_shape"] = shape
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
        if plan.tier_route is not None:
            # In ``params`` rather than a new column. ``requested_model`` already
            # says ``mcc/best`` and ``resolved_model`` already says what answered;
            # what neither can say is whether *this agent's* override fired or
            # whether it quietly followed the global chain -- and an override
            # naming the same ref as the global chain is indistinguishable from
            # no override at all. Three keys, only ever written for a request
            # that named a tier, so no existing row and no export changes shape.
            params = dict(self._record.params or {})
            params["tier"] = plan.tier_route.tier.value
            params["tier_source"] = plan.tier_route.source
            params["tier_harness"] = plan.tier_route.harness
            self._record.params = params

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
        # Per attempt, because the decision is per attempt: a chain that falls
        # back from a blind model to a sighted one delivers the same picture
        # two different ways, and the row must name the one that answered.
        self._record.image_delivery = str(routed.image_delivery)
        # Same rule, same reason: the downscale is decided per attempt on the
        # per-attempt copy, so what is recorded is what the winning attempt did.
        self._image_token_family = routed.image_token_family
        self._image_sent_sizes = {
            resize.index: (resize.after_width, resize.after_height)
            for resize in routed.image_resizes
        }
        self._image_bytes = (
            (
                sum(resize.before_bytes for resize in routed.image_resizes),
                sum(resize.after_bytes for resize in routed.image_resizes),
            )
            if routed.image_resizes
            else None
        )
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
            # The consumer stopped reading. Whether that is a *cancellation*
            # depends on whether the answer had already finished: both OpenAI
            # adapters return as soon as they translate the terminal event,
            # which closes this generator while it is suspended on its last
            # ``yield``. Recording that as "cancelled" made every successful
            # ``/v1/chat/completions`` and ``/v1/responses`` request read as
            # abandoned on the Requests page, while the identical request to
            # ``/v1/messages`` -- whose reader drains the stream -- read as a
            # success. The terminal event is the fact that tells them apart.
            status = self._status_after_consumer_stopped()
            await self._finalize_off_loop(status)
            await try_close_async_iterator(body)
            raise
        except asyncio.CancelledError:
            status = self._status_after_consumer_stopped()
            # Deliberately the synchronous form. This task is already being
            # cancelled, so a fresh await here is not reliably resumed, and
            # losing the row is worse than holding the loop for a request
            # nobody is waiting on any more.
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
                await self._finalize_off_loop(status)

    def _status_after_consumer_stopped(
        self,
    ) -> Literal["success", "error", "cancelled"]:
        """Return the status for a stream whose reader stopped before it did.

        A stream that already emitted its terminal event produced a complete
        answer, and the row should say so. Anything else really was abandoned
        part-way, which is the case the "cancelled" status exists to record.
        """

        if self._error is not None:
            return "error"
        return "success" if self._saw_terminal_event else "cancelled"

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
        elif event_type == "message_stop":
            self._saw_terminal_event = True
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

    def _apply_adapter_tokens(self, record: RequestRecord) -> None:
        """Roll this request's describe attempts up onto the request row.

        Summed here rather than derived by the reader for the reason the column
        exists at all: analytics scans ``requests`` and must not have to walk
        every attempt's JSON to answer "what did the adapter cost". NULL rather
        than 0 when nothing measured anything, because "no describe call ran"
        and "a describe call ran and reported nothing" are different facts and
        a zero would erase the distinction.
        """
        measured_in = [
            attempt.tokens_in
            for attempt in self._attempts
            if _is_describe(attempt) and attempt.tokens_in is not None
        ]
        measured_out = [
            attempt.tokens_out
            for attempt in self._attempts
            if _is_describe(attempt) and attempt.tokens_out is not None
        ]
        record.adapter_tokens_in = sum(measured_in) if measured_in else None
        record.adapter_tokens_out = sum(measured_out) if measured_out else None

    def _apply_estimate(self, record: RequestRecord) -> None:
        """Store what the estimator expected this request to cost.

        Computed only for a request that carried a picture, and only at
        finalize -- after the client has every token it is going to get. The
        estimate is a full tiktoken pass over the prompt, which is real CPU
        work, and the question it exists to answer is about images: a text-only
        request has nothing to audit here and pays nothing for the privilege.
        NULL therefore means "not measured", which on a text request is exactly
        true.
        """
        request = self._request
        if request is None or not self._images:
            return
        family = self._image_token_family or ImageTokenFamily.UNKNOWN.value
        try:
            record.est_tokens_in = get_token_count(
                request.messages,
                request.system,
                request.tools,
                image_token_family=family,
            )
            # Walked again here rather than reusing the images captured at the
            # start of the request, because the two can legitimately differ:
            # in describe mode the pictures have become sentences by now, and
            # the honest answer is that they cost zero image tokens and their
            # words are already inside ``est_tokens_in``. Counting the client's
            # original pictures instead would make the image share of the
            # estimate describe a request that was never sent.
            record.est_image_tokens = sum(
                _estimated_image_tokens(image, family)
                for image in request_image_inputs(request)
            )
        except Exception as exc:
            # An estimate is an estimate. A request that has already succeeded
            # must never be reported as failed because arithmetic about it did.
            logger.debug("Request estimate skipped: {}", exc)

    def _apply_cost(self, record: RequestRecord) -> None:
        """Price this request, and each describe hop, once -- at the commit.

        Once, and stored: a price that changes next month must not silently
        rewrite last month's bill, so nothing recomputes this at read time.

        The parent row and a describe attempt are priced separately because
        they are different calls: a different model, usually a different
        provider, always a different key. Summing them into one figure would
        make the answering model look more expensive than it was and would
        leave no way to see what describe mode actually cost.

        Never fatal. A request that has already been answered must not be
        recorded as failed because arithmetic about it was -- the same rule
        ``_apply_estimate`` follows, for the same reason.
        """
        if not self._cost_enabled:
            return
        try:
            reported = self._reported_cost
            record.cost_usd, record.cost_source = self._price(
                record.provider,
                record.resolved_model,
                TokenUsage(
                    tokens_in=record.tokens_in,
                    tokens_out=record.tokens_out,
                    cache_read_tokens=record.cache_read_tokens,
                    cache_write_tokens=record.cache_write_tokens,
                    reasoning_tokens=(
                        None if reported is None else reported.reasoning_tokens
                    ),
                ),
                reported_usd=None if reported is None else reported.total_usd,
            )
            self._price_attempts()
        except Exception as exc:
            logger.debug("Request cost skipped: {}", exc)

    def _price(
        self,
        provider: str | None,
        model: str | None,
        usage: TokenUsage,
        *,
        reported_usd: float | None,
    ) -> tuple[float | None, str | None]:
        """Walk the ladder for one (provider, model, usage) and return its answer."""
        result = resolve_cost(
            reported_usd=reported_usd,
            usage=usage,
            cards=rate_cards(
                provider, model, litellm_enabled=self._cost_litellm_enabled
            ),
            mode=self._cost_mode,
        )
        return result.cost_usd, result.cost_source

    def _price_attempts(self) -> None:
        """Price every attempt that reported usage of its own.

        Only a describe hop does today: the request row's counters come from
        the client-facing stream, and an ordinary attempt that repeated them
        here would double every total that ever joined the two tables.
        """
        for index, attempt in enumerate(self._attempts):
            if attempt.tokens_in is None and attempt.tokens_out is None:
                continue
            cost, source = self._price(
                attempt.provider,
                attempt.model_ref,
                TokenUsage(tokens_in=attempt.tokens_in, tokens_out=attempt.tokens_out),
                # A describe hop runs with the parent's collector paused, so no
                # host-reported figure can reach it. It prices from a table, on
                # its own row, and says so.
                reported_usd=None,
            )
            if cost is None:
                continue
            self._attempts[index] = replace(attempt, cost_usd=cost, cost_source=source)

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
        """Complete the record and hand it to the store, on this thread.

        The synchronous form, kept for the short-circuit paths that never had
        a stream: an error raised before the wrapper took over, and a
        non-streamed answer built from one complete message. The streaming
        path uses :meth:`_finalize_off_loop` instead.
        """
        record = self._begin_finalize(status)
        if record is None:
            return
        self._compute_finalize_fields(record)
        self._commit_finalize(record)

    async def _finalize_off_loop(
        self, status: Literal["success", "error", "cancelled"]
    ) -> None:
        """Complete the record with the arithmetic moved to a worker thread.

        Everything between the client's last token and the store's queue is
        real CPU: a PIL decode and thumbnail per picture, a full tiktoken pass
        over the prompt, and the pricing ladder -- which may itself build a
        models.dev index. None of it can change what this request answered,
        because the answer has already been streamed. All of it used to run on
        the event loop, so one request's bookkeeping delayed every other
        request in flight: the heartbeat caught gaps of 371 ms and 1,108 ms
        inside this method alone. Off the loop, the numbers land in the log a
        few milliseconds later and nothing else waits for them.

        Awaited inside the request's own task on purpose, so the graceful
        shutdown budget still bounds it -- a fire-and-forget thread would
        outlive the loop it was started from.
        """
        record = self._begin_finalize(status)
        if record is None:
            return
        await asyncio.to_thread(self._compute_finalize_fields, record)
        self._commit_finalize(record)

    def _begin_finalize(
        self, status: Literal["success", "error", "cancelled"]
    ) -> RequestRecord | None:
        """Fill in what is already known; None when there is nothing to do."""
        if self._finalized or self._store is None:
            self._finalized = True
            return None
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
        if self._image_bytes is not None:
            record.image_bytes_in, record.image_bytes_out = self._image_bytes
        self._apply_adapter_tokens(record)
        return record

    def _compute_finalize_fields(self, record: RequestRecord) -> None:
        """The expensive half: thumbnails, the token estimate, the price.

        Kept together in one method because it is one hop off the loop, and
        because all three share the rule that has always governed them -- an
        answered request is never reported as failed because arithmetic about
        it was. ``capture_images`` is the only one that did not already say so
        in code, and it is guarded here for the same reason.
        """
        if self._images:
            try:
                record.images = capture_images(
                    self._images,
                    max_pixels=self._capture_images_pixels,
                    store_pixels=self._capture_images_pixels > 0,
                    sent_sizes=self._image_sent_sizes,
                )
            except Exception as exc:
                logger.debug("Request image capture skipped: {}", exc)
        self._apply_estimate(record)
        self._apply_cost(record)

    def _commit_finalize(self, record: RequestRecord) -> None:
        """Attach what only the loop knows, and hand the row to the store."""
        store = self._store
        if store is None:
            return
        record.key_index = self._credential.index
        record.key_label = self._credential.label
        record.attempts = tuple(self._attempts)
        store.enqueue(record)


def build_capture(
    settings: Settings,
    request: MessagesRequest,
    *,
    request_id: str,
    endpoint: str,
    protocol: WireProtocol,
    headers: Mapping[str, str] | None = None,
    stream: bool | None = None,
) -> RequestCapture:
    """Create the capture for one request; inert when logging is disabled.

    ``stream`` overrides what the internal request says. The Anthropic request
    this capture describes is always streaming -- MCC's pipeline has no other
    mode -- so a surface that also serves a complete JSON body has to say which
    of the two its client actually asked for, or every row would read "stream".
    """
    store = store_from_settings(settings)
    # Every inbound surface funnels its raw headers through here, so this is
    # the one place that can tell a provider what the client said about
    # itself. Installed unconditionally, before the request log's own switch:
    # a provider that has to mirror the client's user-agent upstream must not
    # start lying the moment request logging is turned off.
    install_fingerprint(headers)
    return RequestCapture(
        store,
        request_id=request_id,
        endpoint=endpoint,
        protocol=protocol,
        stream=bool(request.stream) if stream is None else stream,
        requested_model=request.model,
        input_text=extract_input_text(request),
        params=extract_request_params(request),
        capture_bodies=bool(getattr(settings, "request_log_capture_bodies", True)),
        images=request_image_inputs(request),
        request=request,
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
        # Attributed at write time, from the same classifier the historical
        # backfill uses. ``harness_from_headers`` answers ``unknown`` rather
        # than nothing when it recognises nothing, so ``.harness`` is always a
        # string and a row written from here is never NULL -- which is what
        # lets NULL keep meaning "predates the column" for the backfill.
        harness=harness_from_headers(headers).harness,
        cost_enabled=bool(getattr(settings, "cost_estimation_enabled", True)),
        cost_mode=str(getattr(settings, "cost_estimation_mode", MODE_AUTO)),
        cost_litellm_enabled=bool(
            getattr(settings, "cost_source_litellm_enabled", False)
        ),
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
