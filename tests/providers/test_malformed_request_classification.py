"""A 400 ends the route only when the provider says the request is malformed.

``FailureKind.INVALID_REQUEST`` was the catch-all for every 400 that was not
quota and not a context overflow, and it is in ``FALLBACK_SKIP_KINDS`` by
default -- so any 400 ended the whole route. Measured against the live request
log (274,375 requests, 2026-08-01 -> 2026-09-05), not one of the distinct 400
wordings any configured provider actually sent was a body fault that would fail
identically on every model. They were a host's own required field, a sampling
value one model pins, a per-dialect length cap, a model that does not exist on
that endpoint.

Since 6.46.0 the split is made on the provider's own words: ``malformed`` and
``request``, both present, is ``INVALID_REQUEST``; everything else is
``MODEL_REJECTED``, which is deliberately absent from the default skip list.
Every body below except the two synthetic ones is copied from that log.
"""

from collections.abc import Mapping

import httpx
import openai
import pytest

from my_claude_code.core.anthropic.upstream_errors import anthropic_stream_failure
from my_claude_code.core.failures import FailureKind
from my_claude_code.providers.failure_policy import classify_provider_failure


def _sdk_400(body: Mapping[str, object]) -> openai.BadRequestError:
    """The OpenAI-dialect carrier: a parsed ``body`` on the exception."""
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": body})
    return openai.BadRequestError(
        str(body.get("message", "")), response=response, body={"error": body}
    )


def _raw_400(body: Mapping[str, object]) -> httpx.HTTPStatusError:
    """The Anthropic/Responses carrier: raw ``httpx``, words in the response."""
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(400, request=request, json={"error": body})
    return httpx.HTTPStatusError(
        str(body.get("message", "")), request=request, response=response
    )


def _classify(exc: Exception) -> FailureKind:
    return classify_provider_failure(
        exc,
        provider_name="under_test",
        read_timeout_s=None,
        request_id=None,
        mark_rate_limited=lambda _seconds: None,
        cooldown_seconds=0.0,
    ).kind


def _both_carriers(body: Mapping[str, object]) -> tuple[FailureKind, FailureKind]:
    """Both error carriers, because a fix applied to one is applied to neither.

    The OpenAI SDK path and the raw ``httpx`` path are two separate branches of
    ``_classify_provider_failure``; the Anthropic-protocol providers only ever
    reach the second.
    """
    return _classify(_sdk_400(body)), _classify(_raw_400(body))


# ------------------------------------------------ the narrow kind, kept narrow


def test_a_body_the_host_calls_malformed_still_ends_the_route() -> None:
    """T1 -- the words the rule names, in prose."""
    assert _both_carriers(
        {"message": "The request body is malformed.", "type": "invalid_request_error"}
    ) == (FailureKind.INVALID_REQUEST, FailureKind.INVALID_REQUEST)


def test_a_machine_code_spelling_counts_as_the_same_two_words() -> None:
    """T2 -- ``_`` is a word character, so the separator is normalised first.

    Without that normalisation ``\\brequest\\b`` never matches inside
    ``malformed_request`` and a host that answers in codes rather than prose
    would fall through to ``MODEL_REJECTED``.
    """
    assert _both_carriers({"code": "malformed_request", "message": "bad body"}) == (
        FailureKind.INVALID_REQUEST,
        FailureKind.INVALID_REQUEST,
    )


# ----------------------------- every real wording in the log, one test each --
#
# These seven are the regression suite. A src-only revert must redden all of
# them, which is why each carries the provider's exact bytes rather than a
# paraphrase.


def test_nous_portal_missing_tags_is_the_hosts_own_required_field() -> None:
    """T3 -- 153 rows. Nous Portal requires a field no other host asks for."""
    assert _both_carriers(
        {
            "message": (
                "Check the model name and other parameters. "
                "Additional info: missing tags"
            ),
            "type": "invalid_request_error",
        }
    ) == (FailureKind.MODEL_REJECTED, FailureKind.MODEL_REJECTED)


def test_nvidia_nim_pins_top_p_for_one_model_only() -> None:
    """T4 -- 42 rows. A per-model sampling pin, not a fault in the body."""
    assert _both_carriers(
        {
            "message": (
                "Validation: `top_p` is immutable for this model and must be 0.95"
            ),
            "type": "invalid_request_error",
        }
    ) == (FailureKind.MODEL_REJECTED, FailureKind.MODEL_REJECTED)


def test_chatgpt_oauth_caps_a_tool_name_at_128() -> None:
    """T5 -- 42 rows. A per-dialect cap; the Anthropic wire has no such limit."""
    assert _both_carriers(
        {
            "message": (
                "Invalid 'input[62].name': string too long. Expected maximum "
                "length 128, but got a string of length 214 instead."
            ),
            "type": "invalid_request_error",
            "param": "input[62].name",
            "code": "string_above_max_length",
        }
    ) == (FailureKind.MODEL_REJECTED, FailureKind.MODEL_REJECTED)


def test_commandcode_does_not_serve_that_model_on_that_endpoint() -> None:
    """T6 -- 36 rows. The clearest case of all: the *next* model is the answer."""
    assert _both_carriers(
        {
            "message": 'Model "stealth/ox-alpha" is not supported on this endpoint.',
            "type": "invalid_request_error",
        }
    ) == (FailureKind.MODEL_REJECTED, FailureKind.MODEL_REJECTED)


def test_commandcode_caps_a_tool_name_at_64() -> None:
    """T7 -- 4 rows. 64 here, 128 on ChatGPT, unbounded on the Anthropic wire."""
    assert _both_carriers(
        {
            "message": "`name` must be at most 64 characters, got 68",
            "type": "invalid_request_error",
        }
    ) == (FailureKind.MODEL_REJECTED, FailureKind.MODEL_REJECTED)


def test_opencode_wants_the_reasoning_content_echoed_back() -> None:
    """T8 -- 1 row. A host protocol quirk, and only that host's."""
    assert _both_carriers(
        {
            "message": (
                "The reasoning_content in the thinking mode must be passed "
                "back to the API."
            ),
            "type": "invalid_request_error",
        }
    ) == (FailureKind.MODEL_REJECTED, FailureKind.MODEL_REJECTED)


def test_a_bare_validation_complaint_is_the_models_and_not_the_bodys() -> None:
    """T9 -- the generic shape, standing in for every wording not yet seen.

    The default for an unexplained 400 is now "try the next model", which is
    the whole behaviour change. Pinned so nobody restores the old catch-all by
    widening the malformed test instead of narrowing it.
    """
    assert _both_carriers({"message": "messages: field required"}) == (
        FailureKind.MODEL_REJECTED,
        FailureKind.MODEL_REJECTED,
    )


# ------------------------------------------------ the reason the reader matters


def test_a_prompt_that_talks_about_malformed_requests_is_not_one() -> None:
    """T10 -- the echo case, and the reason ``upstream_complaint`` is used.

    A pydantic-style validation error echoes the whole submitted request back
    under ``input``. ``transient_error_text`` concatenates the response text and
    would read that echo as the host's own words, so a user asking about
    malformed requests would end their own route on a 400 that had nothing to
    do with them. This test is what fails if anyone swaps the reader.
    """
    body = {
        "message": "`name` must be at most 64 characters, got 68",
        "type": "invalid_request_error",
        "input": [
            {
                "role": "user",
                "content": (
                    "why do I keep getting a malformed request error from this "
                    "API when the request looks fine?"
                ),
            }
        ],
    }

    assert _both_carriers(body) == (
        FailureKind.MODEL_REJECTED,
        FailureKind.MODEL_REJECTED,
    )


# --------------------------------------------- the branches above it, in order


@pytest.mark.parametrize(
    ("vendor", "message"),
    (
        (
            "nvidia_nim",
            "This model's maximum context length is 262144 tokens. However, "
            "you requested 262294 tokens for this request.",
        ),
        (
            "open_router",
            "This endpoint's maximum context length is 256000 tokens. However, "
            "you requested about 256487 tokens in the request.",
        ),
    ),
)
def test_a_context_overflow_is_still_context_length(vendor: str, message: str) -> None:
    """T11 -- and both wordings contain the word "request". Branch order matters."""
    assert _both_carriers({"message": message, "type": "invalid_request_error"}) == (
        FailureKind.CONTEXT_LENGTH,
        FailureKind.CONTEXT_LENGTH,
    ), vendor


def test_an_empty_wallet_is_still_quota() -> None:
    """T12 -- ``is_quota_error`` runs before every status branch. Prove it still does.

    The exact body of req_df6a8ed49c6a46bb81765ba8039b8703 (2026-09-02 11:52Z),
    which is why ``QUOTA`` exists at all.
    """
    assert _both_carriers(
        {
            "message": (
                "You have insufficient credits to make this request. Please "
                "purchase more credits to continue using the service."
            ),
            "type": "invalid_request_error",
            "code": "BAD_REQUEST",
        }
    ) == (FailureKind.QUOTA, FailureKind.QUOTA)


# ------------------------------------- the third producer: the in-stream frame


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        (
            'Model "stealth/ox-alpha" is not supported on this endpoint.',
            FailureKind.MODEL_REJECTED,
        ),
        ("The request body is malformed.", FailureKind.INVALID_REQUEST),
        ("malformed_request", FailureKind.INVALID_REQUEST),
    ),
)
def test_an_in_stream_anthropic_rejection_splits_the_same_way(
    message: str, expected: FailureKind
) -> None:
    """T13 -- the Anthropic dialect has one 400 type for every rejection.

    This is the path Command Code's Anthropic half and the two first-party
    Anthropic providers take, so leaving it on the old catch-all would have
    shipped the fix half-applied.
    """
    failure = anthropic_stream_failure(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
    )

    assert failure.kind is expected
    assert failure.status_code == 400
    # ``retryable`` means "safe to retry the same credential", and on neither
    # kind is it: the same model rejects the same body again.
    assert failure.retryable is False
