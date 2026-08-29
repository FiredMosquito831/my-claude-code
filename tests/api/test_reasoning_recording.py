"""Both reasoning policies -- requested and applied -- reach the request log.

Per-model gating (v5.33.0) silently changed what the existing ``reasoning``
column meant: it used to hold what was asked for, and now holds what was sent.
Those are identical whenever nothing was clamped and differ exactly when the
interesting thing happened, so a single column cannot answer "how often did the
model's capability change what we sent". These tests pin the pair.
"""

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.export import (
    REQUEST_FIELD_IDS,
    request_detail_columns,
    request_detail_derived_columns,
)
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.core.request_log import (
    RequestLogStore,
    RequestRecord,
    get_request_log_store,
)
from tests.api.support import create_test_app

EFFORT_UP_TO_HIGH = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supported_efforts=frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
    ),
)
CANNOT_REASON = ModelReasoningCapability(can_reason=False)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _settings(preference: ReasoningPreference) -> Settings:
    settings = Settings()
    settings.model = "nvidia_nim/a-model"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.reasoning_policy = preference
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    return settings


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-3-opus",
        max_tokens=4096,
        messages=[Message(role="user", content="hello")],
    )


def _recorded(
    store: RequestLogStore,
    preference: ReasoningPreference,
    capability: ModelReasoningCapability | None,
) -> dict:
    routed = ModelRouter(
        _settings(preference),
        reasoning_capability_lookup=lambda _p, _m: capability,
        output_limit_lookup=lambda _p, _m: 8192,
    ).resolve_messages_request(_request())
    capture = RequestCapture(
        store,
        request_id="req_reasoning",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=False,
        requested_model="claude-3-opus",
        input_text="hello",
        params={"max_tokens": 4096},
    )
    capture.set_routing(routed)
    capture.finish_success("ok")
    store.close()
    row = store.get_request("req_reasoning")
    assert row is not None
    return row


def test_an_ungated_request_records_both_policies_equal(store) -> None:
    row = _recorded(store, ReasoningPreference.HIGH, EFFORT_UP_TO_HIGH)

    assert row["reasoning"] == "control=on,effort=high"
    assert row["requested_reasoning"] == "control=on,effort=high"


def test_a_clamped_effort_records_the_requested_and_the_applied(store) -> None:
    row = _recorded(store, ReasoningPreference.MAX, EFFORT_UP_TO_HIGH)

    assert row["requested_reasoning"] == "control=on,effort=max"
    assert row["reasoning"] == "control=on,effort=high"
    assert row["reasoning"] != row["requested_reasoning"]


def test_a_suppressed_request_records_the_effort_that_was_dropped(store) -> None:
    row = _recorded(store, ReasoningPreference.MAX, CANNOT_REASON)

    assert row["requested_reasoning"] == "control=on,effort=max"
    assert row["reasoning"] == "control=default"


def test_an_unknown_model_records_both_policies_equal(store) -> None:
    """R0: no capability row means nothing is gated, so nothing differs."""

    row = _recorded(store, ReasoningPreference.MAX, None)

    assert row["reasoning"] == "control=on,effort=max"
    assert row["requested_reasoning"] == "control=on,effort=max"


def test_an_adaptive_tier_is_recorded_legibly(store) -> None:
    row = _recorded(store, ReasoningPreference.ADAPTIVE, None)

    assert row["requested_reasoning"] == "control=adaptive"
    assert row["reasoning"] == "control=adaptive"


def test_an_adaptive_tier_on_a_non_reasoning_model_records_the_suppression(
    store,
) -> None:
    row = _recorded(store, ReasoningPreference.ADAPTIVE, CANNOT_REASON)

    assert row["requested_reasoning"] == "control=adaptive"
    assert row["reasoning"] == "control=default"


def _record(request_id: str, **overrides) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "a-model",
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def test_requested_reasoning_is_added_to_a_pre_existing_database(tmp_path) -> None:
    """``CREATE TABLE IF NOT EXISTS`` is a no-op; live databases predate this."""

    path = tmp_path / "requests.db"
    seed = RequestLogStore(path, max_rows=100)
    seed.enqueue(_record("old", reasoning="control=on,effort=max"))
    seed.close()

    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("ALTER TABLE requests DROP COLUMN requested_reasoning")
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()
    assert "requested_reasoning" not in columns

    reopened = RequestLogStore(path, max_rows=100)
    reopened.enqueue(
        _record(
            "new",
            reasoning="control=on,effort=high",
            requested_reasoning="control=on,effort=max",
        )
    )
    reopened.close()

    conn = sqlite3.connect(path)
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()
    assert "requested_reasoning" in columns

    old_row = reopened.get_request("old")
    new_row = reopened.get_request("new")
    assert old_row is not None and new_row is not None
    # NULL, never backfilled: "we do not know what was requested" is a
    # different fact from "the request was sent unchanged".
    assert old_row["requested_reasoning"] is None
    assert old_row["reasoning"] == "control=on,effort=max"
    assert new_row["requested_reasoning"] == "control=on,effort=max"
    assert new_row["reasoning"] == "control=on,effort=high"


def test_the_requested_reasoning_migration_is_idempotent(tmp_path) -> None:
    """Reopening a database that already has the column must not raise."""

    path = tmp_path / "requests.db"
    for _ in range(3):
        store = RequestLogStore(path, max_rows=100)
        store.close()

    conn = sqlite3.connect(path)
    try:
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")]
    finally:
        conn.close()
    assert columns.count("requested_reasoning") == 1


# ------------------------------------------------------- dashboard / export ---


@pytest.fixture
def admin_client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def gated_store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.enqueue(
        _record(
            "clamped",
            reasoning="control=on,effort=high",
            requested_reasoning="control=on,effort=max",
            thinking_chars=100,
        )
    )
    store.close()
    yield store


def test_the_detail_payload_carries_both_policies(admin_client, gated_store) -> None:
    """The dashboard detail modal reads this endpoint; the field must reach it."""

    payload = admin_client.get("/admin/api/requests/clamped").json()

    assert payload["reasoning"] == "control=on,effort=high"
    assert payload["requested_reasoning"] == "control=on,effort=max"


def test_an_export_carries_the_requested_policy_in_the_cell(
    admin_client, gated_store
) -> None:
    """Assert on the data cell: a header-only check hides an empty export."""

    rows = admin_client.get(
        "/admin/api/export",
        params={"format": "json", "scope": "requests", "fields": "thinking"},
    ).json()

    assert [row["requested_reasoning"] for row in rows] == ["control=on,effort=max"]
    assert [row["reasoning"] for row in rows] == ["control=on,effort=high"]


def test_every_exportable_request_column_exists_in_the_schema(tmp_path) -> None:
    """A registry naming a column the table lacks breaks the whole export."""

    store = RequestLogStore(tmp_path / "requests.db")
    store.close()
    conn = sqlite3.connect(tmp_path / "requests.db")
    try:
        schema = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()

    columns = set(request_detail_columns(REQUEST_FIELD_IDS))
    derived = set(request_detail_derived_columns(REQUEST_FIELD_IDS))
    assert columns - derived <= schema
    assert "requested_reasoning" in columns


def test_client_adaptive_is_recorded_without_changing_the_wire() -> None:
    """A client asking for adaptive is legible in the log, and only in the log.

    ``adaptive`` is not representable on providers with no adaptive channel, so
    the resolved control stays ``on`` and every encoder keeps sending its
    thinking request. Only the recorded *requested* string carries the client's
    own wording.
    """
    from my_claude_code.api.request_capture import (
        _client_thinking_type,
        _describe_reasoning,
    )
    from my_claude_code.application.reasoning import resolve_reasoning_policy
    from my_claude_code.config.reasoning import ReasoningPreference
    from my_claude_code.core.anthropic.models import MessagesRequest, ThinkingConfig
    from my_claude_code.core.reasoning import ReasoningControl
    from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES

    adaptive = MessagesRequest(
        model="m",
        max_tokens=4096,
        messages=[],
        thinking=ThinkingConfig(type="adaptive"),
    )
    enabled = MessagesRequest(
        model="m", max_tokens=4096, messages=[], thinking=ThinkingConfig(type="enabled")
    )

    policy = resolve_reasoning_policy(adaptive, ReasoningPreference.CLIENT)
    # The resolved control must NOT move: that is what keeps the wire stable.
    assert policy.control is ReasoningControl.ON
    assert policy.requests_reasoning is True

    described = _describe_reasoning(
        policy, client_thinking_type=_client_thinking_type(adaptive)
    )
    assert described is not None and "client=adaptive" in described

    enabled_policy = resolve_reasoning_policy(enabled, ReasoningPreference.CLIENT)
    enabled_described = _describe_reasoning(
        enabled_policy, client_thinking_type=_client_thinking_type(enabled)
    )
    assert enabled_described is not None and "client=adaptive" not in enabled_described

    # And the emitted body is identical for both, on every representative
    # encoder family -- the recording note cannot reach a provider.
    for name in ("groq", "fireworks", "zai", "featherless", "gemini"):
        profile = OPENAI_CHAT_PROFILES.get(name)
        if profile is None:
            continue
        body_a: dict[str, object] = {"model": "m", "messages": []}
        body_b: dict[str, object] = {"model": "m", "messages": []}
        profile.apply_reasoning(body_a, adaptive, policy)
        profile.apply_reasoning(body_b, enabled, enabled_policy)
        assert body_a == body_b, name


def test_a_provider_adaptation_merges_with_the_routing_verdict(store) -> None:
    """One row, one verdict: routing's and the provider's are combined.

    Routing clamps before the request leaves; the provider strips after the
    host has already refused it. Both messages are kept, under the more severe
    of the two kinds, so the row never under-represents what happened.
    """
    from my_claude_code.core.reasoning import ReasoningAdaptationKind
    from my_claude_code.core.wire_capture import record_reasoning_adaptation

    routed = ModelRouter(
        _settings(ReasoningPreference.MAX),
        reasoning_capability_lookup=lambda _p, _m: EFFORT_UP_TO_HIGH,
        output_limit_lookup=lambda _p, _m: 8192,
    ).resolve_messages_request(_request())
    capture = RequestCapture(
        store,
        request_id="req_merged",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=False,
        requested_model="claude-3-opus",
        input_text="hello",
        params={"max_tokens": 4096},
    )
    capture.set_routing(routed)
    # Routing recorded a CLAMPED verdict above; the provider layer now reports
    # that the host refused the field outright.
    record_reasoning_adaptation(
        ReasoningAdaptationKind.SUPPRESSED, "XAI rejected 'reasoning_effort' for m"
    )
    capture.finish_success("ok")
    store.close()

    row = store.get_request("req_merged")
    assert row is not None
    assert row["reasoning_adaptation_kind"] == "suppressed"
    assert "XAI rejected 'reasoning_effort' for m" in row["reasoning_adaptation"]
