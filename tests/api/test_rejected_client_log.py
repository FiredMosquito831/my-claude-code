"""One WARNING per distinct rejected ``/v1/*`` client, never the token (item 6).

INVESTIGATION-SELF-INFLICTED-LOAD.md §3 / decision 11: a client retrying with
a wrong proxy token roughly once a second was invisible until the 401 itself.
``RejectedClientTracker`` logs the first rejection from a client it has not
seen this window, counts the rest silently, and logs at most one summary line
per :data:`SUMMARY_WINDOW_SECONDS` -- after which the "seen" set is forgotten
entirely, so a client that is still retrying gets a fresh WARNING.

A loguru sink added in-process would see each record twice whenever an
earlier test in the same worker left stdlib-to-loguru interception installed
(``config/logging_config.py``'s ``InterceptHandler``), so the module's
``logger`` is stubbed with a ``MagicMock`` instead, the same way
``tests/runtime/test_listener_guard.py`` does it.
"""

import contextlib
from unittest.mock import patch

from starlette.requests import Request

from my_claude_code.api import rejected_client_log as rcl
from my_claude_code.api.dependencies import require_proxy_auth
from my_claude_code.config.settings import Settings


def _client(remote: str = "203.0.113.5", ua: str = "curl/8.0") -> rcl.RejectedClient:
    return rcl.RejectedClient(
        remote_address=remote, user_agent=ua, auth_header_names=("authorization",)
    )


def test_the_first_rejection_from_a_client_logs_one_warning() -> None:
    tracker = rcl.RejectedClientTracker()
    with patch.object(rcl, "logger") as stub:
        tracker.note_rejection(_client())

    assert stub.warning.call_count == 1
    args = stub.warning.call_args.args
    assert args[0].startswith("REJECTED CLIENT:")
    assert "203.0.113.5" in args
    assert "curl/8.0" in args


def test_a_repeat_from_the_same_client_in_the_same_window_logs_nothing_new() -> None:
    tracker = rcl.RejectedClientTracker()
    client = _client()
    tracker.note_rejection(client)

    with patch.object(rcl, "logger") as stub:
        tracker.note_rejection(client)
        tracker.note_rejection(client)

    stub.warning.assert_not_called()


def test_a_different_client_in_the_same_window_gets_its_own_warning() -> None:
    tracker = rcl.RejectedClientTracker()
    tracker.note_rejection(_client(remote="203.0.113.5"))

    with patch.object(rcl, "logger") as stub:
        tracker.note_rejection(_client(remote="198.51.100.9"))

    assert stub.warning.call_count == 1
    assert "198.51.100.9" in stub.warning.call_args.args


def test_the_window_rolling_over_logs_one_summary_and_forgets_the_client(
    monkeypatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(rcl.time, "monotonic", lambda: now)
    tracker = rcl.RejectedClientTracker()
    client = _client()
    tracker.note_rejection(client)
    tracker.note_rejection(client)  # suppressed, but counted

    now = 1_000.0 + rcl.SUMMARY_WINDOW_SECONDS + 1.0
    with patch.object(rcl, "logger") as stub:
        tracker.note_rejection(client)  # window rolled over

    calls = stub.warning.call_args_list
    summaries = [c for c in calls if c.args[0].startswith("REJECTED CLIENT SUMMARY:")]
    assert len(summaries) == 1, calls
    assert summaries[0].args[1] == 2  # total rejected this window
    # The client was forgotten by the rollover, so this same rejection is also
    # reported fresh.
    fresh = [c for c in calls if c.args[0].startswith("REJECTED CLIENT:")]
    assert len(fresh) == 1, calls


def test_the_token_itself_never_appears_in_any_captured_record() -> None:
    settings = Settings.model_construct(anthropic_auth_token="super-secret-token-xyz")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "client": ("203.0.113.7", 54321),
            "headers": [
                (b"authorization", b"Bearer wrong-token-value"),
                (b"user-agent", b"claude-code/1.0"),
            ],
        }
    )

    with patch.object(rcl, "logger") as stub, contextlib.suppress(Exception):
        require_proxy_auth(request, settings)

    for call in stub.warning.call_args_list:
        rendered = " ".join(str(part) for part in call.args) + str(call.kwargs)
        assert "super-secret-token-xyz" not in rendered
        assert "wrong-token-value" not in rendered
