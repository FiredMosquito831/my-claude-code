"""Optimization handlers for fast-path API responses.

Each handler returns a :class:`LocalOptimization` if the request matches and
the optimization is enabled, otherwise None. A match means the proxy answers
the request itself and no provider is contacted at all, so every match is
recorded against the rule that produced it -- a rule nobody can count is a
rule nobody can evaluate.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from my_claude_code.application.execution import TokenCounter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    MessagesResponse,
    Usage,
    count_text_tokens,
)

from .detection import (
    is_model_routing_probe_request,
    is_suggestion_mode_request,
    is_title_generation_request,
)


@dataclass(frozen=True, slots=True)
class LocalOptimization:
    """A request answered inside the proxy instead of by a model.

    ``tokens_saved`` is the request's own input token count -- the tokens that
    would have gone upstream had the rule not matched. It is a measurement of
    this request, not an estimate of a bill: what the provider would have
    charged for the reply is unknowable and deliberately not guessed at.
    """

    rule: str
    response: MessagesResponse
    tokens_saved: int


@dataclass(frozen=True, slots=True)
class OptimizationRuleSpec:
    """What a rule is, in the terms a reader of the dashboard needs.

    ``answer`` is the literal string the proxy replies with, and the handler
    below sends this exact attribute rather than a second copy of the text.
    A page that showed one string while the wire carried another would be
    worse than a page that showed nothing.
    """

    rule: str
    label: str
    description: str
    answer: str
    env_key: str
    settings_attr: str


TITLE_GENERATION_SKIP = OptimizationRuleSpec(
    rule="title_generation_skip",
    label="Title generation skip",
    description="Claude Code asking a model to name your session.",
    answer="Conversation",
    env_key="ENABLE_TITLE_GENERATION_SKIP",
    settings_attr="enable_title_generation_skip",
)

SUGGESTION_MODE_SKIP = OptimizationRuleSpec(
    rule="suggestion_mode_skip",
    label="Suggestion mode skip",
    description="The suggested next message Claude Code offers you.",
    answer="",
    env_key="ENABLE_SUGGESTION_MODE_SKIP",
    settings_attr="enable_suggestion_mode_skip",
)

PROBE_AUTO_RESPONSE = OptimizationRuleSpec(
    rule="probe_auto_response",
    label="Model routing probe",
    description=(
        "A client harness's startup reachability check asking the endpoint to "
        "say OK. Answered locally instead of spending an upstream call; the "
        "reply echoes the routed model so a substitution is still detected."
    ),
    answer="OK",
    env_key="ENABLE_PROBE_AUTO_RESPONSE",
    settings_attr="enable_probe_auto_response",
)

OPTIMIZATION_RULE_SPECS: tuple[OptimizationRuleSpec, ...] = (
    TITLE_GENERATION_SKIP,
    SUGGESTION_MODE_SKIP,
    PROBE_AUTO_RESPONSE,
)


def _answer(
    request_data: MessagesRequest,
    text: str,
    *,
    rule: str,
    token_counter: TokenCounter,
) -> LocalOptimization:
    """Build the local reply, with usage counted rather than invented.

    The counts this reports used to be hardcoded (``input_tokens=100``,
    ``output_tokens=5``) regardless of the request. They reach the client and
    feed its own accounting, so they are measured now: cl100k over the real
    request costs 0.5 ms at 1.5 KB and 7 ms at the median title prompt,
    against the multi-second upstream round trip the match avoids.
    """
    input_tokens = token_counter(
        request_data.messages, request_data.system, request_data.tools
    )
    output_tokens = count_text_tokens(text)
    logger.info("Optimization: {} answered locally", rule)
    return LocalOptimization(
        rule=rule,
        response=MessagesResponse(
            id=f"msg_{uuid.uuid4()}",
            model=request_data.model,
            content=[{"type": "text", "text": text}],
            stop_reason="end_turn",
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        ),
        tokens_saved=input_tokens,
    )


def try_title_skip(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Skip title generation requests."""
    if not settings.enable_title_generation_skip:
        return None
    if not is_title_generation_request(request_data):
        return None

    return _answer(
        request_data,
        TITLE_GENERATION_SKIP.answer,
        rule=TITLE_GENERATION_SKIP.rule,
        token_counter=token_counter,
    )


def try_suggestion_skip(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Skip suggestion mode requests."""
    if not settings.enable_suggestion_mode_skip:
        return None
    if not is_suggestion_mode_request(request_data):
        return None

    return _answer(
        request_data,
        SUGGESTION_MODE_SKIP.answer,
        rule=SUGGESTION_MODE_SKIP.rule,
        token_counter=token_counter,
    )


def try_probe_auto_response(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Answer a client's model-routing probe locally.

    The reply carries the RESOLVED model id -- routing has already run by the
    time this intercept fires -- so the probe still detects a routing
    substitution truthfully, which is the check's real purpose. What it no
    longer proves is upstream liveness; the run's first real request proves
    that anyway, and fails visibly where a synthetic OK would not.
    """
    if not settings.enable_probe_auto_response:
        return None
    if not is_model_routing_probe_request(request_data):
        return None

    return _answer(
        request_data,
        PROBE_AUTO_RESPONSE.answer,
        rule=PROBE_AUTO_RESPONSE.rule,
        token_counter=token_counter,
    )


OptimizationHandler = Callable[
    [MessagesRequest, Settings, TokenCounter], LocalOptimization | None
]

# Cheapest/most common optimizations first for faster short-circuit.
OPTIMIZATION_HANDLERS: list[OptimizationHandler] = [
    try_title_skip,
    try_suggestion_skip,
    try_probe_auto_response,
]

# Every rule name this module can record, so a consumer can enumerate them
# without importing the handlers or scraping strings out of the log. Derived
# from the specs rather than typed twice: a rule the registry does not describe
# is a rule the dashboard cannot report on, and the two lists drifting apart is
# exactly the failure that would hide it.
OPTIMIZATION_RULES: tuple[str, ...] = tuple(
    spec.rule for spec in OPTIMIZATION_RULE_SPECS
)


def try_optimizations(
    request_data: MessagesRequest, settings: Settings, token_counter: TokenCounter
) -> LocalOptimization | None:
    """Run optimization handlers in order. Returns first match or None."""
    for handler in OPTIMIZATION_HANDLERS:
        result = handler(request_data, settings, token_counter)
        if result is not None:
            return result
    return None
