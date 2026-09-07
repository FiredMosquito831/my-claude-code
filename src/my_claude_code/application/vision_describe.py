"""Ask a sighted model what a picture shows, so a blind one can answer about it.

``VISION_ADAPTER_MODE=route`` -- the default, and everything MCC did up to
6.50.1 -- hands the whole request to ``MODEL_VISION`` the moment it carries an
image the route's own model cannot read. That is the right answer when the
picture *is* the question, and the wrong one for the case most people actually
have: a fast, cheap, text-only model doing the coding and a screenshot arriving
mid-conversation. In route mode the vision model then has to answer a coding
question it has no context for.

``describe`` mode inverts it. Each image is sent to the vision chain on its
own, with one instruction: say what is there. Its answer replaces the image
block in the transcript, and the model the route actually picked answers the
question it was asked, with the screenshot rendered as words it can read.

Three properties are worth stating because they are what make this safe:

* **The describe call is an ordinary routed request.** It is built as a
  :class:`~my_claude_code.application.routing.RoutedMessagesPlan` over the
  vision chain and handed to the same
  :class:`~my_claude_code.application.execution.ProviderExecutor`, so it
  inherits the health registry, the pause button, credential rotation, the
  retry policy, the deadline and wire capture rather than reimplementing any of
  them. A vision model benched by failures is skipped here exactly as it is
  everywhere else.
* **A description is a property of the picture, not of the request.** It is
  cached on the ``image_blobs`` row under the content address MCC already
  computes for the thumbnail, so a screenshot re-sent on every turn of a
  conversation is described once, however many turns follow.
* **Nothing here may fail a request.** Every failure path returns "describe
  mode did not happen", and the caller then does what it would have done
  without this module at all: divert to the vision model, or send the
  placeholder. Losing an answer because a screenshot could not be described is
  worse than answering without the screenshot.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from my_claude_code.application.execution import ProviderExecutor, RouteAttemptRecord
from my_claude_code.application.routing import ModelRouter, ResolvedModel
from my_claude_code.core.anthropic import (
    MessagesRequest,
    aggregate_anthropic_sse_to_message,
    request_image_inputs,
)
from my_claude_code.core.anthropic.models import Message
from my_claude_code.core.anthropic.request_modalities import ImageInput
from my_claude_code.core.anthropic.tool_result_media import (
    DESCRIBE_FAILED_TEXT,
    MediaBlockContext,
    collect_tool_names,
    described_media_placeholder,
    described_top_level_placeholder,
    media_block_contexts,
    replace_request_media_with_text,
)
from my_claude_code.core.reported_cost import paused_reported_cost
from my_claude_code.core.request_images import capture_images
from my_claude_code.core.request_log import RequestLogStore, paused_recovery_trace
from my_claude_code.core.upstream_ladder import paused_ladder
from my_claude_code.core.wire_capture import paused_wire_trace

#: What the vision model is told to do. One system-less user turn: an
#: instruction to report rather than to interpret, because the reader is
#: another model that will act on it and an opinion dressed as an observation
#: is the failure mode that costs the most.
DESCRIBE_PROMPT = (
    "Describe this image for another AI model that cannot see it. Report what"
    " is actually there: text verbatim where it is legible, layout, UI state,"
    " colours, error messages, numbers. Do not interpret, advise, or speculate"
    " about intent. If it is a screenshot of code or a terminal, transcribe it."
)

#: Ceiling on one description. A description that runs longer than this is not
#: a description any more, and the whole point of describe mode is that the
#: text costs less than the picture did.
DESCRIBE_MAX_TOKENS = 1024

#: How many images of one request are described at the same time. Concurrency
#: is what keeps a five-screenshot turn from costing five round trips in
#: series; the bound is what keeps it from opening five upstream connections on
#: a provider that rate-limits by concurrency.
DESCRIBE_CONCURRENCY = 3

#: Attempt indexes for describe calls start here, so they cannot collide with
#: the parent chain's own attempts -- which are the primary key of the request
#: log's attempt rows. Sixteen slots per image is more rungs than any vision
#: chain has.
DESCRIBE_ATTEMPT_BASE = 1000
DESCRIBE_ATTEMPT_STRIDE = 16

#: Called with (attempt record, image sha, image index, usage) for every
#: upstream try a describe call made. The parent request owns the row: a
#: describe call is an extra hop on this request, not phantom traffic of its
#: own.
#:
#: ``usage`` is the describe reply's own ``{"input_tokens", "output_tokens"}``,
#: and it is why the observer is called after the stream finishes rather than
#: during it: the tokens are only known once the SSE aggregator has assembled
#: the message. Before 6.53.0 the aggregator returned them and this module read
#: only text out of it, so the describe hop's cost was discarded entirely --
#: not misfiled, discarded. ``None`` means not measured, which is what every
#: failed attempt reports and what a host that publishes no usage reports.
DescribeAttemptObserver = Callable[
    [RouteAttemptRecord, str, int, dict[str, Any] | None], None
]


@dataclass(frozen=True, slots=True)
class DescribeResult:
    """What describe mode did to one request.

    ``applied`` false means the caller must route exactly as it always has --
    the mode is ``route``, nothing was visual, the primary can see, no vision
    model is configured, or a describe call failed. There is no partial state:
    either every image in the request became text, or none did.
    """

    applied: bool
    described: int = 0
    cached: int = 0
    describing_model: str | None = None
    #: Set when describe mode was in play and could not finish. The caller
    #: falls back to route mode, and to the placeholder if route mode has
    #: nowhere to go either.
    failed: bool = False
    #: The vision refs the chain could not be reached on at all -- exhausted,
    #: paused, timed out. Route mode diverts to exactly these models, so a plan
    #: made entirely of them is a diversion into the same wall; the caller
    #: reads this to tell "the vision model answered unusably" (try route)
    #: from "the vision model could not be reached" (do not).
    unreachable_refs: frozenset[str] = frozenset()


NOT_APPLIED = DescribeResult(applied=False)


@dataclass(frozen=True, slots=True)
class _Target:
    """One image of the request, and everything needed to describe it."""

    index: int
    sha: str
    image: ImageInput
    context: MediaBlockContext
    source_bytes: int | None


class VisionDescribeAdapter:
    """Turns the images of one request into text, or reports that it could not."""

    def __init__(
        self,
        *,
        router: ModelRouter,
        executor: ProviderExecutor,
        store: RequestLogStore | None,
        concurrency: int = DESCRIBE_CONCURRENCY,
    ) -> None:
        self._router = router
        self._executor = executor
        self._store = store
        self._concurrency = max(1, concurrency)

    async def apply(
        self,
        request: MessagesRequest,
        *,
        request_id: str,
        harness: str | None = None,
        on_attempt: DescribeAttemptObserver | None = None,
    ) -> DescribeResult:
        """Replace this request's images with descriptions, in place.

        Mutates ``request.messages`` only on full success, so a caller that
        gets ``applied=False`` still holds the request exactly as the client
        sent it and can divert it whole.
        """
        chain = self._router.vision_describe_chain(request, harness=harness)
        if not chain:
            return NOT_APPLIED
        targets = self._targets(request)
        if targets is None:
            return NOT_APPLIED
        model_ref = chain[0].provider_model_ref

        cached = self._cached_descriptions(targets)
        pending = [target for target in targets if target.sha not in cached]
        fresh, unreachable = await self._describe_all(
            pending, chain, request_id=request_id, on_attempt=on_attempt
        )
        if fresh is None:
            return DescribeResult(
                applied=False, failed=True, unreachable_refs=unreachable
            )

        descriptions = {**cached, **fresh}
        texts = [
            _wrap(descriptions[target.sha][0], descriptions[target.sha][1], target)
            for target in targets
        ]
        replaced = replace_request_media_with_text(
            request.messages,
            texts,
            tool_names=collect_tool_names(request.messages),
        )
        if replaced != len(targets):
            # The two walks disagreed about how many blocks there are, which
            # can only mean one of them was changed without the other. Leaving
            # the request half-substituted would send the model a transcript
            # nobody wrote, so nothing is kept.
            logger.warning(
                "VISION DESCRIBE: replaced {} of {} image block(s); falling back"
                " to route mode",
                replaced,
                len(targets),
            )
            return DescribeResult(applied=False, failed=True)
        logger.info(
            "VISION DESCRIBE: {} image(s) described by '{}' ({} from cache)",
            len(targets),
            model_ref,
            len(cached),
        )
        return DescribeResult(
            applied=True,
            described=len(fresh),
            cached=len(cached),
            describing_model=model_ref,
        )

    # ------------------------------------------------------------ internals ---

    def _targets(self, request: MessagesRequest) -> list[_Target] | None:
        """Pair each visual block with its content address, or refuse the lot.

        ``None`` means describe mode does not apply to this request at all: a
        document is pixels a vision model cannot be handed as an image block,
        and an image the client sent by URL has no bytes here to hash or to
        forward. Both are rare and both have a correct answer already -- route
        mode -- so the honest move is to decline rather than to describe some
        of the request and divert the rest.
        """
        images = request_image_inputs(request)
        if not images:
            return None
        contexts = media_block_contexts(
            request.messages, tool_names=collect_tool_names(request.messages)
        )
        if len(contexts) != len(images):
            return None
        captured = capture_images(images, max_pixels=0, store_pixels=False)
        targets: list[_Target] = []
        for index, (image, context, capture) in enumerate(
            zip(images, contexts, captured, strict=True)
        ):
            if context.kind != "image" or not image.data:
                return None
            targets.append(
                _Target(
                    index=index,
                    sha=capture.sha256,
                    image=image,
                    context=context,
                    source_bytes=capture.source_bytes,
                )
            )
        return targets

    def _cached_descriptions(
        self, targets: list[_Target]
    ) -> dict[str, tuple[str, str]]:
        if self._store is None:
            return {}
        found = self._store.image_descriptions([target.sha for target in targets])
        return {
            sha: (description, described_by or "a vision model")
            for sha, (description, described_by) in found.items()
        }

    async def _describe_all(
        self,
        targets: list[_Target],
        chain: tuple[ResolvedModel, ...],
        *,
        request_id: str,
        on_attempt: DescribeAttemptObserver | None,
    ) -> tuple[dict[str, tuple[str, str]] | None, frozenset[str]]:
        """Describe every uncached image, or report that one could not be.

        The second half of the answer is which vision refs turned out to be
        unreachable, which is what tells the caller whether route mode is a
        fallback or a second walk into the same wall.
        """
        if not targets:
            return {}, frozenset()
        semaphore = asyncio.Semaphore(self._concurrency)
        unreachable: set[str] = set()

        async def one(target: _Target) -> tuple[str, str] | None:
            async with semaphore:
                return await self._describe_one(
                    target,
                    chain,
                    request_id=request_id,
                    on_attempt=on_attempt,
                    unreachable=unreachable,
                )

        results = await asyncio.gather(
            *(one(target) for target in targets), return_exceptions=True
        )
        described: dict[str, tuple[str, str]] = {}
        for target, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException) or result is None:
                if isinstance(result, asyncio.CancelledError):
                    # The client went away. Propagating keeps the request's own
                    # cancellation semantics intact rather than turning a
                    # disconnect into a fallback nobody is waiting for.
                    raise result
                return None, frozenset(unreachable)
            described[target.sha] = result
        return described, frozenset(unreachable)

    async def _describe_one(
        self,
        target: _Target,
        chain: tuple[ResolvedModel, ...],
        *,
        request_id: str,
        on_attempt: DescribeAttemptObserver | None,
        unreachable: set[str],
    ) -> tuple[str, str] | None:
        describe_request = _describe_request(target)
        plan = self._router.vision_describe_plan(describe_request, chain)
        model_ref = plan.primary.resolved.provider_model_ref
        # Buffered rather than reported as they happen: an attempt's own token
        # usage is not known until the stream it opened has been aggregated,
        # and the row is written once. ``flush`` is called on every exit path,
        # including the failing ones, so a describe call that died still leaves
        # the same trail it did before -- with NULL tokens, which is the honest
        # answer for an attempt that never got a usage block.
        pending: list[RouteAttemptRecord] = []
        observer = None
        if on_attempt is not None:

            def observer(record: RouteAttemptRecord) -> None:
                pending.append(record)

        def flush(usage: dict[str, Any] | None) -> None:
            if on_attempt is None:
                return
            for position, record in enumerate(pending):
                # Only the last rung can be the one that answered, so it is the
                # only one the usage can belong to. Attributing it to an
                # earlier, failed attempt would invent a measurement.
                last = position == len(pending) - 1
                on_attempt(record, target.sha, target.index, usage if last else None)
            pending.clear()

        try:
            # The parent's per-attempt collectors are keyed by attempt index
            # within one executor run, and this is a second run inside the
            # same request. Left recording, a describe call would file its
            # body, its upstream tries and its retries under the client's own
            # attempt 0 -- which is how the first live proof of this feature
            # showed the vision model's API key on the primary's row.
            with (
                paused_wire_trace(),
                paused_ladder(),
                paused_recovery_trace(),
                paused_reported_cost(),
            ):
                stream = self._executor.stream(
                    plan,
                    wire_api="messages",
                    raw_log_label="DESCRIBE_PAYLOAD",
                    raw_log_payload=describe_request.model_dump(),
                    request_id=request_id,
                    on_attempt_result=observer,
                )
                body, error = await aggregate_anthropic_sse_to_message(stream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Nothing on the chain answered. Route mode diverts to these same
            # models, so the caller has to know that before it tries.
            unreachable.update(plan.model_refs())
            logger.warning(
                "VISION DESCRIBE: '{}' could not describe an image: {}",
                model_ref,
                exc,
            )
            flush(None)
            return None
        usage = body.get("usage") if isinstance(body, dict) else None
        flush(usage if isinstance(usage, dict) else None)
        if error is not None:
            unreachable.update(plan.model_refs())
            logger.warning(
                "VISION DESCRIBE: '{}' returned an error instead of a description: {}",
                model_ref,
                error,
            )
            return None
        description = _text_of(body).strip()
        if not description:
            logger.warning(
                "VISION DESCRIBE: '{}' returned no text for an image", model_ref
            )
            return None
        # The winning model, not the head of the chain: a fallback that
        # answered is the model that actually described the picture, and that
        # is what the cache and the request detail must say.
        answered = _answering_model(plan, body) or model_ref
        if self._store is not None:
            self._store.store_image_description(
                sha=target.sha,
                kind=target.context.kind,
                media_type=target.image.media_type,
                source_bytes=target.source_bytes,
                description=description,
                described_by=answered,
            )
        return description, answered


def describe_attempt_index(image_index: int, attempt: int) -> int:
    """Where one describe attempt's row sits, clear of the parent's own rows."""
    return DESCRIBE_ATTEMPT_BASE + image_index * DESCRIBE_ATTEMPT_STRIDE + attempt


def _wrap(description: str, model_ref: str, target: _Target) -> str:
    if target.context.nested:
        return described_media_placeholder(
            description, model_ref=model_ref, tool_name=target.context.tool_name
        )
    return described_top_level_placeholder(description, model_ref=model_ref)


def describe_failure_texts(count: int) -> list[str]:
    """The sentence each image gets when nothing could be done for it.

    Used only on the last rung of the ladder: describe mode failed *and* route
    mode had nowhere to divert to. It says what is missing and why, which is
    the whole reason not to fail the request instead.
    """
    return [DESCRIBE_FAILED_TEXT] * count


def _describe_request(target: _Target) -> MessagesRequest:
    """One image, one instruction, no history, no tools."""
    content: list[Any] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": target.image.media_type or "image/png",
                "data": target.image.data,
            },
        },
        {"type": "text", "text": _prompt_for(target)},
    ]
    return MessagesRequest(
        model="describe",
        max_tokens=DESCRIBE_MAX_TOKENS,
        messages=[Message(role="user", content=content)],
        # Never streamed: the caller wants one string, and a non-streaming plan
        # is also the one the executor may still fall back from after an
        # attempt has begun producing output, because nobody is watching it
        # arrive.
        stream=False,
    )


def _prompt_for(target: _Target) -> str:
    if target.context.tool_name:
        return (
            f"{DESCRIBE_PROMPT} This image was returned by the"
            f" {target.context.tool_name!r} tool."
        )
    return DESCRIBE_PROMPT


def _text_of(body: dict[str, Any]) -> str:
    blocks = body.get("content")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _answering_model(plan: Any, body: dict[str, Any]) -> str | None:
    """Which model on the describe chain actually produced this message.

    The aggregated body carries the provider model name the attempt sent, so
    the chain entry whose model matches it is the one that answered. A miss
    falls back to the head of the chain, which is what a single-model chain
    always is anyway.
    """
    answered = body.get("model")
    if not isinstance(answered, str) or not answered:
        return None
    for attempt in plan.attempts:
        if attempt.resolved.provider_model == answered:
            return str(attempt.resolved.provider_model_ref)
    return None


__all__ = [
    "DESCRIBE_CONCURRENCY",
    "DESCRIBE_MAX_TOKENS",
    "DESCRIBE_PROMPT",
    "DescribeAttemptObserver",
    "DescribeResult",
    "VisionDescribeAdapter",
    "describe_attempt_index",
    "describe_failure_texts",
]
