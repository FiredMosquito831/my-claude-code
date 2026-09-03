"""Security tests for allow-listed inbound header capture.

The point of these tests is the leak assertion: they run credential-bearing
headers through the real capture path and assert on the ENTIRE serialised row,
not on the handful of fields anyone remembered to check.
"""

import json
from typing import cast

import pytest

from my_claude_code.api.request_capture import build_capture
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.request_headers import (
    ALLOWED_HEADERS,
    MAX_NAME_CHARS,
    MAX_TOTAL_CHARS,
    MAX_UNLISTED_NAMES,
    MAX_VALUE_CHARS,
    UNLISTED_NAMES_KEY,
    capture_headers,
)
from my_claude_code.core.request_log import RequestLogStore

FAKE_BEARER = "sk-ant-notarealkey-000111222333"

SECRET_HEADERS = {
    "authorization": f"Bearer {FAKE_BEARER}",
    "x-api-key": "notarealapikey-444555666",
    "cookie": "session=notarealcookievalue777",
    "proxy-authorization": "Basic bm90YXJlYWw6c2VjcmV0",
}

SECRET_VALUES = (
    *SECRET_HEADERS.values(),
    FAKE_BEARER,
    "notarealapikey-444555666",
    "notarealcookievalue777",
    "bm90YXJlYWw6c2VjcmV0",
)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=16,
        stream=False,
        messages=[Message(role="user", content="hi")],
    )


def _unlisted(captured: dict[str, str]) -> list[str]:
    raw = captured.get(UNLISTED_NAMES_KEY)
    return raw.split(",") if raw else []


def _serialised_row(row: object) -> str:
    return json.dumps(row, default=repr)


# --------------------------------------------------------------- allow-list


def test_allow_listed_headers_are_stored_with_values_and_lowercased_keys() -> None:
    captured = capture_headers(
        {
            "User-Agent": "claude-cli/2.0.0",
            "X-App": "cli",
            "Anthropic-Version": "2023-06-01",
            "Anthropic-Beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-MCC-Harness": "opencode",
            "X-MCC-Harness-Version": "1.2.3",
        }
    )
    assert captured == {
        "user-agent": "claude-cli/2.0.0",
        "x-app": "cli",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "accept": "application/json",
        "content-type": "application/json",
        "x-mcc-harness": "opencode",
        "x-mcc-harness-version": "1.2.3",
    }
    assert captured is not None
    assert UNLISTED_NAMES_KEY not in captured
    assert set(captured) == set(ALLOWED_HEADERS)


def test_the_harness_headers_are_allow_listed_by_name() -> None:
    """The launcher's own attribution claim has to survive to storage.

    Without both of these on the list the header path is indistinguishable
    from the user-agent path on a stored row, and the detail pane can no
    longer say which of the two produced the harness id.
    """
    assert "x-mcc-harness" in ALLOWED_HEADERS
    assert "x-mcc-harness-version" in ALLOWED_HEADERS


def test_a_harness_header_alone_is_captured_with_its_value() -> None:
    """A launcher that sends only its claim must still produce a stored row."""
    captured = capture_headers({"x-mcc-harness": "droid"})
    assert captured == {"x-mcc-harness": "droid"}
    assert captured is not None
    assert UNLISTED_NAMES_KEY not in captured


def test_unlisted_header_records_name_only_and_never_its_value() -> None:
    captured = capture_headers(
        {"user-agent": "claude-cli/2.0.0", "X-Weird-Client": "supersecretpayload"}
    )
    assert captured is not None
    assert "x-weird-client" not in captured
    assert _unlisted(captured) == ["x-weird-client"]
    assert "supersecretpayload" not in json.dumps(captured)


def test_unlisted_names_are_sorted_for_stable_output() -> None:
    captured = capture_headers(dict.fromkeys(["z-one", "a-one", "m-one"], "v"))
    assert captured is not None
    assert _unlisted(captured) == ["a-one", "m-one", "z-one"]


def test_unlisted_name_list_never_contains_a_value() -> None:
    captured = capture_headers({**SECRET_HEADERS, "user-agent": "claude-cli/2.0.0"})
    assert captured is not None
    names = captured[UNLISTED_NAMES_KEY]
    assert ":" not in names
    assert " " not in names
    for secret in SECRET_VALUES:
        assert secret not in names
    # The NAMES are allowed to appear -- that is the whole point of the field.
    assert set(_unlisted(captured)) == set(SECRET_HEADERS)


# ------------------------------------------------------------------- caps


def test_many_unlisted_headers_stay_bounded() -> None:
    captured = capture_headers({f"x-h{index:03d}": "v" for index in range(500)})
    assert captured is not None
    names = _unlisted(captured)
    assert len(names) == MAX_UNLISTED_NAMES + 1
    assert names[-1] == f"+{500 - MAX_UNLISTED_NAMES}-more"
    assert len(json.dumps(captured)) <= MAX_TOTAL_CHARS


def test_absurdly_long_allow_listed_value_is_truncated() -> None:
    captured = capture_headers({"user-agent": "A" * 1_000_000})
    assert captured is not None
    assert len(captured["user-agent"]) == MAX_VALUE_CHARS + 3
    assert len(json.dumps(captured)) <= MAX_TOTAL_CHARS


def test_absurdly_long_unlisted_name_is_truncated() -> None:
    captured = capture_headers({"x-" + ("n" * 5_000): "v"})
    assert captured is not None
    assert len(_unlisted(captured)[0]) <= MAX_NAME_CHARS


def test_hostile_client_cannot_bloat_the_row() -> None:
    long_name = "n" * 60
    hostile = {f"x-{long_name}-{index:04d}": "V" * 100_000 for index in range(2_000)}
    hostile.update(dict.fromkeys(ALLOWED_HEADERS, "L" * 100000))
    captured = capture_headers(hostile)
    assert captured is not None
    assert len(json.dumps(captured)) <= MAX_TOTAL_CHARS


# --------------------------------------------------------------- degenerate


@pytest.mark.parametrize("headers", [None, {}])
def test_missing_headers_produce_none(headers) -> None:
    assert capture_headers(headers) is None


def test_blank_and_non_string_values_do_not_crash() -> None:
    odd: dict[str, str] = {"user-agent": "", "   ": "x"}
    # A non-str value is not reachable through Starlette's typed Headers, but
    # ``capture_headers`` guards against it and that guard must be exercised.
    odd["accept"] = cast(str, None)
    assert capture_headers(odd) is None


def test_unlisted_only_request_still_records_names() -> None:
    assert capture_headers({"x-only": "v"}) == {UNLISTED_NAMES_KEY: "x-only"}


# ------------------------------------------------------------- leak test


def test_no_secret_value_reaches_the_stored_row(store, monkeypatch) -> None:
    """THE LEAK TEST: assert on the whole row, not on remembered fields."""
    monkeypatch.setattr(
        "my_claude_code.api.request_capture.store_from_settings", lambda _s: store
    )
    capture = build_capture(
        Settings(),
        _request(),
        request_id="req_leak",
        endpoint="/v1/messages",
        protocol="anthropic",
        headers={
            **SECRET_HEADERS,
            "user-agent": "claude-cli/2.0.0",
            "anthropic-version": "2023-06-01",
        },
    )
    capture.finish_success("done")
    store.close()

    row = store.get_request("req_leak")
    assert row is not None
    blob = _serialised_row(row)
    for secret in SECRET_VALUES:
        assert secret not in blob, f"leaked value for one of {sorted(SECRET_HEADERS)}"
    assert "Bearer" not in blob
    # Names may appear; values may not.
    assert "authorization" in blob
    assert row["headers"]["user-agent"] == "claude-cli/2.0.0"


def test_round_trip_through_the_real_store(store, monkeypatch) -> None:
    monkeypatch.setattr(
        "my_claude_code.api.request_capture.store_from_settings", lambda _s: store
    )
    headers = {
        "User-Agent": "claude-cli/2.0.0",
        "Anthropic-Beta": "oauth-2025-04-20",
        "X-Unknown": "ignored-value",
    }
    expected = capture_headers(headers)
    capture = build_capture(
        Settings(),
        _request(),
        request_id="req_round",
        endpoint="/v1/messages",
        protocol="anthropic",
        headers=headers,
    )
    capture.finish_success("done")
    store.close()

    row = store.get_request("req_round")
    assert row is not None
    assert row["headers"] == expected
    assert row["headers"]["user-agent"] == "claude-cli/2.0.0"
    assert row["headers"][UNLISTED_NAMES_KEY] == "x-unknown"
    assert "ignored-value" not in _serialised_row(row)


def test_capture_without_headers_leaves_the_column_null(store, monkeypatch) -> None:
    monkeypatch.setattr(
        "my_claude_code.api.request_capture.store_from_settings", lambda _s: store
    )
    capture = build_capture(
        Settings(),
        _request(),
        request_id="req_none",
        endpoint="/v1/messages",
        protocol="anthropic",
    )
    capture.finish_success("done")
    store.close()
    row = store.get_request("req_none")
    assert row is not None
    assert row["headers"] is None


# --------------------------------------------------- write-time attribution


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"user-agent": "claude-cli/2.0.0 (external, cli)"}, "claude"),
        ({"user-agent": "opencode2/0.3.1"}, "opencode2"),
        # Explicit beats inferred: this row would read as a curl one-liner.
        ({"user-agent": "curl/8.4.0", "x-mcc-harness": "droid"}, "droid"),
        # Nothing recognisable is still an answer, never a NULL.
        ({"user-agent": "something-nobody-has-seen/1"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_a_new_row_is_attributed_to_a_harness_at_write_time(
    store, monkeypatch, headers, expected
) -> None:
    """Every row written from here on carries a harness, so NULL keeps meaning
    "written before the column existed" for the historical backfill."""
    monkeypatch.setattr(
        "my_claude_code.api.request_capture.store_from_settings", lambda _s: store
    )
    capture = build_capture(
        Settings(),
        _request(),
        request_id="req_harness",
        endpoint="/v1/messages",
        protocol="anthropic",
        headers=headers,
    )
    capture.finish_success("done")
    store.close()

    row = store.get_request("req_harness")
    assert row is not None
    assert row["harness"] == expected
