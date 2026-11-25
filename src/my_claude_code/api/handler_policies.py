"""Shared non-frozen request policies across MCC inbound API handlers.

Extracted so ``MessagesHandler``, ``ChatCompletionsHandler``, ``ResponsesHandler``,
and future doors (``GeminiHandler``) share identical tool-change folding,
``VisionDescribeAdapter`` orchestration + sidecar envelope invalidation,
safe ``RequestLogStore`` resolution, and safety-classifier no-thinking policy.
"""

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from loguru import logger

from my_claude_code.api.detection import is_safety_classifier_request
from my_claude_code.api.request_tool_changes import fold_request_tool_changes
from my_claude_code.application.routing import (
    ModelRouter,
    RouteDiversion,
    RoutedMessagesPlan,
)
from my_claude_code.application.vision_describe import (
    DescribeResult,
    describe_failure_texts,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.anthropic.request_modalities import request_image_inputs
from my_claude_code.core.anthropic.tool_result_media import (
    MediaDelivery,
    collect_tool_names,
    replace_request_media_with_text,
)
from my_claude_code.core.reasoning import ReasoningControl, ReasoningPolicy
from my_claude_code.core.request_log import RequestLogStore, store_from_settings
from my_claude_code.core.trace import trace_event

ENVELOPE_MEDIA_AND_MESSAGE_KEYS: tuple[str, ...] = (
    "input",
    "messages",
    "image_details",
)
ENVELOPE_TOOL_AND_MESSAGE_KEYS: tuple[str, ...] = (
    "tools",
    "input",
    "messages",
)


def try_store_from_settings(settings: Settings) -> RequestLogStore | None:
    """Resolve the shared ``RequestLogStore``, returning ``None`` if unregistered.

    Unit tests that construct handlers directly without calling
    ``set_request_log_path()`` hit ``RuntimeError`` inside ``store_from_settings``
    when ``request_log_enabled`` defaults to ``True``. Returning ``None`` keeps
    such tests isolated without disabling logging in real server processes.
    """
    try:
        return store_from_settings(settings)
    except RuntimeError:
        return None


def invalidate_envelope_keys(
    request: MessagesRequest, keys: Iterable[str]
) -> MessagesRequest:
    """Drop stale keys from sidecar envelopes if present."""
    key_set = frozenset(keys)
    updates: dict[str, Any] = {}
    if request.responses_envelope is not None and any(
        key in request.responses_envelope for key in key_set
    ):
        filtered_resp = {
            key: value
            for key, value in request.responses_envelope.items()
            if key not in key_set
        }
        updates["responses_envelope"] = filtered_resp or None
    if request.chat_envelope is not None and any(
        key in request.chat_envelope for key in key_set
    ):
        filtered_chat = {
            key: value
            for key, value in request.chat_envelope.items()
            if key not in key_set
        }
        updates["chat_envelope"] = filtered_chat or None
    if (
        request.gemini_envelope is not None
        and "messages" in key_set
        and "thought_parts" in request.gemini_envelope
    ):
        filtered_gemini = {
            key: value
            for key, value in request.gemini_envelope.items()
            if key != "thought_parts"
        }
        updates["gemini_envelope"] = filtered_gemini or None
    if not updates:
        return request
    return request.model_copy(update=updates)


def fold_request_tool_changes_with_envelope_sync(
    request: MessagesRequest, *, request_id: str
) -> MessagesRequest:
    """Fold mid-conversation tool changes and strip stale envelope keys if mutated."""
    folded = fold_request_tool_changes(request, request_id=request_id)
    if folded is request:
        return request
    return invalidate_envelope_keys(folded, ENVELOPE_TOOL_AND_MESSAGE_KEYS)


def with_media_delivery(
    plan: RoutedMessagesPlan, delivery: MediaDelivery
) -> RoutedMessagesPlan:
    """Stamp ``image_delivery`` onto every attempt in ``plan``."""
    return replace(
        plan,
        attempts=tuple(
            replace(attempt, image_delivery=delivery) for attempt in plan.attempts
        ),
    )


def route_is_a_dead_end(plan: RoutedMessagesPlan, described: DescribeResult) -> bool:
    """Whether diverting this request would walk into the same dead models."""
    if plan.diversion is RouteDiversion.VISION_UNAVAILABLE:
        return True
    if not described.unreachable_refs:
        return False
    return all(ref in described.unreachable_refs for ref in plan.model_refs())


def apply_describe_outcome(
    plan: RoutedMessagesPlan,
    request_data: MessagesRequest,
    described: DescribeResult,
    *,
    model_router: ModelRouter,
    harness: str | None,
) -> RoutedMessagesPlan:
    """Apply the result of ``VisionDescribeAdapter.apply`` onto the routed plan.

    When vision description fails on a dead-end route, replaces images with
    placeholder texts, invalidates stale media/message keys inside any sidecar
    envelopes, and re-resolves the plan so no blind fallback model receives a
    raw image payload.
    """
    if described.applied:
        return replace(
            with_media_delivery(plan, MediaDelivery.DESCRIBED),
            diversion=RouteDiversion.VISION_DESCRIBED,
        )
    if not described.failed:
        return plan
    if not route_is_a_dead_end(plan, described):
        logger.info("VISION DESCRIBE: falling back to route mode for this request")
        return plan
    images = request_image_inputs(request_data)
    replace_request_media_with_text(
        request_data.messages,
        describe_failure_texts(len(images)),
        tool_names=collect_tool_names(request_data.messages),
    )
    stripped_request = invalidate_envelope_keys(
        request_data, ENVELOPE_MEDIA_AND_MESSAGE_KEYS
    )
    logger.warning(
        "VISION DESCRIBE: no description and no vision route; sending {}"
        " placeholder(s) instead",
        len(images),
    )
    return replace(
        with_media_delivery(
            model_router.resolve_messages_plan(stripped_request, harness=harness),
            MediaDelivery.STRIP,
        ),
        diversion=RouteDiversion.VISION_UNAVAILABLE,
    )


def apply_message_routing_policies(
    plan: RoutedMessagesPlan,
    *,
    trace_fn: Callable[..., Any] | None = None,
) -> RoutedMessagesPlan:
    """Apply request-shaped policies (safety classifier no-thinking) to all attempts."""
    if not is_safety_classifier_request(plan.primary.request):
        return plan
    changed = any(
        attempt.reasoning.control is not ReasoningControl.OFF
        for attempt in plan.attempts
    )
    emit_trace = trace_fn or trace_event
    emit_trace(
        stage="routing",
        event="my_claude_code.api.optimization.safety_classifier_no_thinking",
        source="api",
        model=plan.primary.request.model,
        changed=changed,
    )
    if not changed:
        return plan
    return replace(
        plan,
        attempts=tuple(
            replace(attempt, reasoning=ReasoningPolicy.off())
            for attempt in plan.attempts
        ),
    )
