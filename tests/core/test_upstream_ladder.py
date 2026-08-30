"""The holder that records every upstream try behind one route attempt."""

import json

from my_claude_code.config.constants import (
    REQUEST_LOG_LADDER_BODY_MAX_CHARS_DEFAULT,
)
from my_claude_code.core.upstream_ladder import (
    DEFAULT_LADDER_BODY_MAX_CHARS,
    MAX_TRIES_PER_ATTEMPT,
    AttemptLadder,
    CredentialDecision,
    LadderTry,
    format_status_census,
    install_ladder_trace,
    ladder_payload,
    ladder_root_cause,
    record_credential_decision,
    record_limiter_wait,
    record_upstream_try,
    record_upstream_wait,
    redact_try_body,
)


def _ladder_of(tries, decisions=(), *, limiter_ms=0.0, dropped=0) -> AttemptLadder:
    return AttemptLadder(
        tries=list(tries),
        decisions=list(decisions),
        time_limiter_ms=limiter_ms,
        tries_dropped=dropped,
    )


def test_default_cap_matches_the_settings_constant() -> None:
    """``core`` may not import ``config``, so the two are pinned by a test."""
    assert DEFAULT_LADDER_BODY_MAX_CHARS == REQUEST_LOG_LADDER_BODY_MAX_CHARS_DEFAULT


def test_ladder_payload_counts_statuses_and_time() -> None:
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, status=429, upstream_ms=400.0, waited_ms=2700.0),
            LadderTry(key_index=0, status=429, upstream_ms=410.0, waited_ms=4000.0),
            LadderTry(key_index=1, status=502, upstream_ms=830.0),
        ],
        limiter_ms=1180.0,
    )

    payload = ladder_payload(ladder)

    assert payload["summary"]["tries"] == 3
    assert payload["summary"]["statuses_by_code"] == {"429": 2, "502": 1}
    assert payload["summary"]["keys"] == 2
    assert payload["summary"]["time_upstream_ms"] == 1640.0
    assert payload["summary"]["time_sleeping_ms"] == 6700.0
    assert payload["summary"]["time_limiter_ms"] == 1180.0
    assert payload["summary"]["tries_dropped"] == 0
    # A term that was never measured is absent, not zero.
    assert "waited_ms" not in payload["tries"][2]


def test_root_cause_names_keys_charged_and_uncharged() -> None:
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, status=429, waited_ms=3000.0),
            LadderTry(key_index=0, status=429, waited_ms=3000.0),
            LadderTry(key_index=1, status=502, waited_ms=2000.0),
            LadderTry(key_index=1, status=502, waited_ms=2000.0),
        ],
        [
            CredentialDecision(
                key_index=0, cls="rate_limit", benched_for_s=60.0, status=429
            ),
            CredentialDecision(
                key_index=1, cls=None, status=502, reason="502 is not credential-shaped"
            ),
        ],
    )

    line = ladder_root_cause(
        ladder_payload(ladder),
        attempt_error_kind="upstream",
        attempt_duration_ms=20_000,
    )

    assert "2 keys \N{MULTIPLICATION SIGN} 2 tries" in line
    assert "2\N{MULTIPLICATION SIGN}429, 2\N{MULTIPLICATION SIGN}502" in line
    assert "key 0 benched 60s on 429 (no Retry-After)" in line
    assert "key 1 not charged (502 is not credential-shaped)" in line


def test_root_cause_reports_a_published_retry_after_as_published() -> None:
    """The provider's own number, never the operator cooldown standing in."""
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, status=429, retry_after=12.0),
            LadderTry(key_index=0, status=429, retry_after=12.0),
        ],
        [
            CredentialDecision(
                key_index=0,
                cls="rate_limit",
                benched_for_s=12.0,
                status=429,
                retry_after=12.0,
            )
        ],
    )

    line = ladder_root_cause(ladder_payload(ladder))

    assert "key 0 benched 12s on 429 (Retry-After 12s)" in line


def test_root_cause_is_empty_for_a_single_try_attempt() -> None:
    """Nothing is hidden, so nothing extra is said."""
    ladder = _ladder_of([LadderTry(key_index=0, status=400)])

    assert ladder_root_cause(ladder_payload(ladder)) == ""


def test_root_cause_labels_a_timeout_spent_in_backoff() -> None:
    """The ``req_460a...`` shape: the model never saw an accepted request."""
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, status=429, waited_ms=48_000.0),
            LadderTry(key_index=0, status=429, waited_ms=48_000.0),
            LadderTry(key_index=0, status=502),
        ],
        limiter_ms=52_000.0,
    )

    line = ladder_root_cause(
        ladder_payload(ladder),
        attempt_error_kind="timeout",
        attempt_duration_ms=120_000,
    )

    assert "the model never received an accepted request" in line
    assert "deadline reached after 148s of backoff" in line
    assert "2\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502" in line


def test_uneven_per_key_try_counts_read_as_a_total() -> None:
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, status=429),
            LadderTry(key_index=0, status=429),
            LadderTry(key_index=1, status=502),
        ]
    )

    assert ladder_root_cause(ladder_payload(ladder)).startswith("3 tries across 2 keys")


def test_a_transport_failure_is_censused_by_its_exception_name() -> None:
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, kind="ReadTimeout", error_kind="timeout"),
            LadderTry(key_index=0, kind="ReadTimeout", error_kind="timeout"),
        ]
    )

    payload = ladder_payload(ladder)

    assert payload["summary"]["statuses_by_code"] == {"ReadTimeout": 2}
    assert "2\N{MULTIPLICATION SIGN}ReadTimeout" in ladder_root_cause(payload)


def test_try_body_is_redacted_by_key_name_and_by_value_shape() -> None:
    body = {
        "api_key": "nvapi-plaintext",
        "detail": "rejected token sk-abcdef1234567890",
    }

    text, truncated = redact_try_body(body, 800)

    assert text is not None
    assert truncated is False
    assert "nvapi-plaintext" not in text
    assert "sk-abcdef1234567890" not in text
    assert json.loads(text)["detail"] != body["detail"]


def test_try_body_is_capped_and_marked_truncated() -> None:
    text, truncated = redact_try_body("x" * 5_000, 800)

    assert text is not None
    assert truncated is True
    assert len(text) == 801
    assert text.endswith("…")


def test_ladder_drops_tries_past_the_cap_and_says_how_many() -> None:
    trace = install_ladder_trace()
    for _ in range(MAX_TRIES_PER_ATTEMPT + 7):
        record_upstream_try(key_index=0, status=429)

    payload = ladder_payload(trace.slot())

    assert payload["summary"]["tries"] == MAX_TRIES_PER_ATTEMPT
    assert payload["summary"]["tries_dropped"] == 7


def test_holder_is_a_no_op_outside_a_tracked_request() -> None:
    """Providers exercised directly need no special handling."""
    record_upstream_try(key_index=0, status=429)
    record_upstream_wait(1.0)
    record_limiter_wait(1.0)
    record_credential_decision(key_index=0, cls="rate_limit")


def test_a_sleep_back_fills_the_try_it_followed() -> None:
    trace = install_ladder_trace()
    record_upstream_try(key_index=0, status=429)
    record_upstream_wait(2.7)
    record_upstream_try(key_index=0, status=429)

    payload = ladder_payload(trace.slot())

    assert payload["summary"]["tries"] == 2
    assert payload["tries"][0]["waited_ms"] == 2700.0


def test_a_limiter_wait_keeps_its_own_row_and_its_own_total() -> None:
    trace = install_ladder_trace()
    record_upstream_try(key_index=0, status=429)
    record_upstream_wait(2.0)
    record_limiter_wait(51.9)

    payload = ladder_payload(trace.slot())

    assert payload["summary"]["time_sleeping_ms"] == 2000.0
    assert payload["summary"]["time_limiter_ms"] == 51_900.0
    assert payload["tries"][-1]["source"] == "limiter_wait"


def test_each_chain_index_gets_its_own_ladder() -> None:
    trace = install_ladder_trace()
    record_upstream_try(key_index=0, status=429)
    trace.current_attempt = 1
    record_upstream_try(key_index=3, status=502)

    assert len(trace.ladders[0].tries) == 1
    assert len(trace.ladders[1].tries) == 1
    assert trace.ladders[1].tries[0].key_index == 3


def test_decisions_borrow_the_key_label_the_tries_recorded() -> None:
    ladder = _ladder_of(
        [
            LadderTry(key_index=0, key_label="user...ubCk", status=429),
            LadderTry(key_index=0, key_label="user...ubCk", status=429),
        ],
        [CredentialDecision(key_index=0, cls="rate_limit", benched_for_s=60.0)],
    )

    assert ladder_payload(ladder)["credentials"][0]["key_label"] == "user...ubCk"


def test_export_census_renders_code_first() -> None:
    assert (
        format_status_census({"429": 12, "502": 3})
        == "429\N{MULTIPLICATION SIGN}12, 502\N{MULTIPLICATION SIGN}3"
    )
