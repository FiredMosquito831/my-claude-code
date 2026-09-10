"""What MCC tells OpenCode Zen and Go about the client calling them.

The header set is the whole point of the feature, so it is asserted here at
three levels: the declaration on the profile, the bytes the SDK would put on
the wire, and the promise that no other provider's bytes moved.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from openai._models import FinalRequestOptions

from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.client_identity import (
    ClientIdentity,
    conversation_key_from_messages,
    derived_session_id,
    identity_headers_for_body,
    new_message_id,
)
from my_claude_code.providers.openai_chat.identity_enforcement import (
    free_quota_reset,
    observe_identity_enforcement,
)
from my_claude_code.providers.openai_chat.opencode_identity import (
    MCC_CLIENT_VALUE,
    OPENCODE_CLIENT_HEADER,
    OPENCODE_HEADER_ORDER,
    OPENCODE_PROJECT_HEADER,
    OPENCODE_REQUEST_HEADER,
    OPENCODE_SESSION_HEADER,
    USER_AGENT_HEADER,
    identity_wire_record,
    opencode_constant_headers,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    FACT_CLIENT_IDENTITY_REQUIRED,
    PROVIDER_WIDE_MODEL_ID,
    learned_fact_store,
)

OPENCODE_PROVIDERS = ("opencode", "opencode_go")


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _provider(provider_id: str):
    return create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test-key", base_url="https://example.invalid/v1"),
        ProviderRateLimiter(rate_limit=1, rate_window=60),
    )


def _outbound_headers(provider_id: str, extra: dict[str, str] | None = None):
    """The headers the openai SDK would actually put on one request."""
    client = _provider(provider_id)._client
    request = client._build_request(
        FinalRequestOptions.construct(
            method="post",
            url="/chat/completions",
            json_data={"model": "m", "messages": []},
            headers=extra or {},
        )
    )
    return request.headers


# ------------------------------------------------------------- declaration


@pytest.mark.parametrize("provider_id", OPENCODE_PROVIDERS)
def test_the_opencode_profile_declares_the_five_client_identity_headers(
    provider_id: str,
) -> None:
    identity = OPENAI_CHAT_PROFILES[provider_id].client_identity
    assert identity is not None
    assert identity.order == (
        OPENCODE_PROJECT_HEADER,
        OPENCODE_SESSION_HEADER,
        OPENCODE_REQUEST_HEADER,
        OPENCODE_CLIENT_HEADER,
        USER_AGENT_HEADER,
    )


def test_the_generic_profile_declares_no_client_identity() -> None:
    """Nineteen other hosts are told nothing about who is calling."""
    declaring = {
        provider_id
        for provider_id, profile in OPENAI_CHAT_PROFILES.items()
        if profile.client_identity is not None
    }
    assert declaring == set(OPENCODE_PROVIDERS)


def test_opencode_zen_and_go_share_one_declaration() -> None:
    """The vendor's requirement landed on Go first and the limiter reads Zen."""
    assert (
        OPENAI_CHAT_PROFILES["opencode"].client_identity
        is OPENAI_CHAT_PROFILES["opencode_go"].client_identity
    )


# ------------------------------------------------------------------- bytes


@pytest.mark.parametrize("provider_id", OPENCODE_PROVIDERS)
def test_the_constant_headers_reach_the_sdk_as_default_headers(
    provider_id: str,
) -> None:
    headers = _outbound_headers(provider_id)
    assert headers["user-agent"].startswith("opencode/")
    assert headers[OPENCODE_CLIENT_HEADER] == "cli"
    assert headers[OPENCODE_PROJECT_HEADER] == "global"
    assert headers[OPENCODE_SESSION_HEADER].startswith("ses_f")
    assert headers[OPENCODE_REQUEST_HEADER].startswith("msg_")


@pytest.mark.parametrize("provider_id", OPENCODE_PROVIDERS)
def test_the_identity_headers_go_out_in_the_clients_own_order(
    provider_id: str,
) -> None:
    """The four vendor headers keep the client's own relative order.

    Not all five, and the difference is worth writing down rather than
    quietly asserting less. ``User-Agent`` is a header the HTTP client sets
    for itself, and replacing its value keeps the slot the client gave it, so
    it lands earlier in the request than the client this proxy identifies as
    puts it. Nothing observable depends on that: HTTP field order carries no
    meaning, and the host's own check is a lookup by name. What *is* asserted
    is that the ordering comes from the declaration rather than from which
    half of the identity happened to fill a value -- which is the property
    that would silently rot if the two halves ever drifted apart.
    """
    identity = OPENAI_CHAT_PROFILES[provider_id].client_identity
    assert identity is not None
    per_request = identity.headers_for("some-conversation")
    sent = _outbound_headers(provider_id, per_request)
    emitted = [name.decode("ascii").lower() for name, _ in sent.raw]
    vendor = [name for name in OPENCODE_HEADER_ORDER if name.startswith("x-opencode")]
    positions = [emitted.index(name.lower()) for name in vendor]
    assert positions == sorted(positions)
    assert len(positions) == 4
    assert emitted.count("user-agent") == 1


def test_opencode_go_sends_the_session_header_too() -> None:
    """G1 is a Go requirement first: the vendor made it mandatory there."""
    assert _outbound_headers("opencode_go")[OPENCODE_SESSION_HEADER]


@pytest.mark.parametrize("provider_id", OPENCODE_PROVIDERS)
def test_the_credential_never_appears_in_any_identity_header(
    provider_id: str,
) -> None:
    identity = OPENAI_CHAT_PROFILES[provider_id].client_identity
    assert identity is not None
    values = identity.headers_for("k").values()
    assert not any("sk-test-key" in value for value in values)
    sent = _outbound_headers(provider_id)
    for name in OPENCODE_HEADER_ORDER:
        assert "sk-test-key" not in sent[name]


def test_no_header_value_is_derived_from_the_model_name() -> None:
    """The anti-special-casing guard: the identity is a property of the host."""
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    free = identity_headers_for_body(
        identity,
        {"model": "some-model-free", "messages": [{"role": "user", "content": "x"}]},
        None,
    )
    paid = identity_headers_for_body(
        identity,
        {"model": "a-paid-model", "messages": [{"role": "user", "content": "x"}]},
        None,
    )
    del free[OPENCODE_REQUEST_HEADER], paid[OPENCODE_REQUEST_HEADER]
    assert free == paid


# ------------------------------------------------------- the conversation


def test_the_session_header_is_mirrored_from_the_inbound_harness() -> None:
    """A client that named its conversation decides what MCC calls it."""
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    body = {"messages": [{"role": "user", "content": "unrelated"}]}
    mirrored = identity_headers_for_body(identity, body, "client-session-42")
    assert mirrored[OPENCODE_SESSION_HEADER] == derived_session_id("client-session-42")


def test_the_session_header_falls_back_to_a_stable_derived_id() -> None:
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    body = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ]
    }
    first = identity_headers_for_body(identity, body, None)
    assert first[OPENCODE_SESSION_HEADER] == derived_session_id(
        conversation_key_from_messages(body["messages"])
    )


def test_the_same_conversation_produces_the_same_session_header_across_turns() -> None:
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    turn_one = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ]
    }
    turn_two = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "and again"},
        ]
    }
    assert (
        identity_headers_for_body(identity, turn_one, None)[OPENCODE_SESSION_HEADER]
        == identity_headers_for_body(identity, turn_two, None)[OPENCODE_SESSION_HEADER]
    )


def test_two_conversations_do_not_share_a_session_header() -> None:
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    one = {"messages": [{"role": "user", "content": "first question"}]}
    two = {"messages": [{"role": "user", "content": "different question"}]}
    assert (
        identity_headers_for_body(identity, one, None)[OPENCODE_SESSION_HEADER]
        != identity_headers_for_body(identity, two, None)[OPENCODE_SESSION_HEADER]
    )


def test_the_request_header_is_new_on_every_call() -> None:
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    body = {"messages": [{"role": "user", "content": "hello"}]}
    seen = {
        identity_headers_for_body(identity, body, None)[OPENCODE_REQUEST_HEADER]
        for _ in range(8)
    }
    assert len(seen) == 8


def test_the_minted_ids_have_the_clients_own_shape() -> None:
    """``ses_`` + 12 hex + 14 base62, descending; ``msg_`` the same, ascending."""
    session = derived_session_id("anything")
    message = new_message_id()
    assert len(session) == len("ses_") + 26
    assert len(message) == len("msg_") + 26
    assert session.startswith("ses_f")
    assert all(char in "0123456789abcdef" for char in session[4:16])
    assert all(char in "0123456789abcdef" for char in message[4:16])
    assert int(message[4:16], 16) > 1_700_000_000_000


# ------------------------------------------------------------- the opt-out


def test_the_truthful_opt_out_names_mcc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_CLIENT_IDENTITY", "mcc")
    get_settings.cache_clear()
    try:
        headers = opencode_constant_headers()
    finally:
        get_settings.cache_clear()
    assert headers[USER_AGENT_HEADER].startswith("my-claude-code/")
    assert headers[OPENCODE_CLIENT_HEADER] == MCC_CLIENT_VALUE
    assert headers[OPENCODE_PROJECT_HEADER] == MCC_CLIENT_VALUE


def test_the_opt_out_keeps_the_three_functional_headers() -> None:
    """Changing whose name is on the request never drops what the host asked for."""
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    truthful = ClientIdentity(
        order=identity.order,
        constant=lambda: {
            OPENCODE_PROJECT_HEADER: "mcc",
            OPENCODE_CLIENT_HEADER: "mcc",
            USER_AGENT_HEADER: "my-claude-code/9.9.9",
        },
        session_header=identity.session_header,
        request_header=identity.request_header,
    )
    assert set(truthful.headers_for("k")) == set(identity.headers_for("k"))


def test_an_unknown_identity_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="OPENCODE_CLIENT_IDENTITY"):
        _settings(OPENCODE_CLIENT_IDENTITY="chrome")


def test_a_cleared_field_falls_back_to_the_shipped_default() -> None:
    assert _settings(OPENCODE_CLIENT_IDENTITY="").opencode_client_identity == "opencode"


# ------------------------------------------------------------ the log row


def test_the_wire_record_keeps_names_and_never_a_correlation_id() -> None:
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    sent = identity.headers_for("a-conversation")
    record = identity_wire_record(sent)
    assert record["names"] == sorted(OPENCODE_HEADER_ORDER)
    assert record["user-agent"] == sent[USER_AGENT_HEADER]
    assert record["x-opencode-client"] == "cli"
    serialised = json.dumps(record)
    assert sent[OPENCODE_SESSION_HEADER] not in serialised
    assert sent[OPENCODE_REQUEST_HEADER] not in serialised


# ------------------------------------------------- the passive observation


class _Refusal(Exception):
    def __init__(self, body: object) -> None:
        super().__init__("refused")
        self.body = body


def _free_limit_body(now: datetime) -> dict[str, object]:
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {
        "type": "FreeUsageLimitError",
        "retryAfter": int((midnight - now).total_seconds()),
    }


def test_a_free_quota_429_is_recognised_by_its_midnight_reset() -> None:
    now = datetime(2026, 9, 10, 16, 45, tzinfo=UTC)
    error = _Refusal(_free_limit_body(now))
    assert free_quota_reset(error, now=now) == pytest.approx(26_100, abs=2)


def test_an_ordinary_rate_limit_is_not_the_free_quota() -> None:
    now = datetime(2026, 9, 10, 16, 45, tzinfo=UTC)
    assert (
        free_quota_reset(
            _Refusal({"type": "RateLimitError", "retryAfter": 30}), now=now
        )
        is None
    )
    assert (
        free_quota_reset(
            _Refusal({"type": "FreeUsageLimitError", "retryAfter": 30}), now=now
        )
        is None
    )


def test_the_observation_records_one_provider_wide_fact() -> None:
    now = datetime(2026, 9, 10, 16, 45, tzinfo=UTC)
    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert observe_identity_enforcement(
        "opencode", identity, _Refusal(_free_limit_body(now)), now=now
    )
    facts = learned_fact_store().facts_for_provider("opencode")
    assert [(f.model_id, f.fact_kind, f.value, f.source) for f in facts] == [
        (PROVIDER_WIDE_MODEL_ID, FACT_CLIENT_IDENTITY_REQUIRED, True, "observation")
    ]


def test_the_observation_stores_no_response_text() -> None:
    now = datetime(2026, 9, 10, 16, 45, tzinfo=UTC)
    body = _free_limit_body(now)
    body["message"] = "You have hit the secret internal limit of 4271 requests"
    observe_identity_enforcement(
        "opencode",
        OPENAI_CHAT_PROFILES["opencode"].client_identity,
        _Refusal(body),
        now=now,
    )
    for fact in learned_fact_store().facts_for_provider("opencode"):
        assert "secret internal limit" not in fact.evidence
        assert len(fact.evidence) <= 160


def test_a_profile_without_an_identity_observes_nothing() -> None:
    now = datetime(2026, 9, 10, 16, 45, tzinfo=UTC)
    assert not observe_identity_enforcement(
        "groq", None, _Refusal(_free_limit_body(now)), now=now
    )
    assert learned_fact_store().all_facts() == ()


# ------------------------------------------------------- the wire capture


@pytest.mark.asyncio
async def test_the_wire_row_records_the_identity_names_and_no_credential() -> None:
    """5.68.0 stopped recording headers because of bearer tokens.

    These five are the exception, and a narrow one: the names always, the two
    values that are published constants, and nothing else. The credential
    never travelled in any of them, and this asserts it in the place a reader
    would actually look.
    """
    from my_claude_code.core.wire_capture import install_wire_trace

    provider = _provider("opencode")
    trace = install_wire_trace()

    async def refuse(**_kwargs):
        raise RuntimeError("no upstream in a test")

    provider._client.chat.completions.create = refuse
    with pytest.raises(RuntimeError):
        await provider._create_stream(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )

    recorded = trace.requests[trace.current_attempt].params["client_identity"]
    assert recorded["names"] == sorted(OPENCODE_HEADER_ORDER)
    assert recorded["user-agent"].startswith("opencode/")
    assert "sk-test-key" not in json.dumps(recorded)
    assert "ses_" not in json.dumps(recorded)


@pytest.mark.asyncio
async def test_a_provider_without_an_identity_records_no_identity_row() -> None:
    from my_claude_code.core.wire_capture import install_wire_trace

    provider = _provider("groq")
    trace = install_wire_trace()

    async def refuse(**_kwargs):
        raise RuntimeError("no upstream in a test")

    provider._client.chat.completions.create = refuse
    with pytest.raises(RuntimeError):
        await provider._create_stream(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )

    assert "client_identity" not in trace.requests[trace.current_attempt].params


@pytest.mark.asyncio
async def test_the_harness_header_never_reaches_the_wire() -> None:
    """Which coding agent sent a request is between the user and MCC.

    ``x-mcc-harness`` is written into the document MCC generates *for* a
    harness so the harness sends it *to* MCC. It has never gone upstream, and
    this asserts it against the real transport rather than against a reading
    of the code: a client that shouts its harness id at MCC still produces a
    request that says nothing about it.
    """
    import httpx

    from my_claude_code.core.client_fingerprint import install_fingerprint

    install_fingerprint(
        {
            "x-mcc-harness": "opencode",
            "x-mcc-harness-version": "1.18.30",
            "x-session-id": "a-real-conversation",
            "user-agent": "opencode/1.18.30",
        }
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": []})

    from openai import AsyncOpenAI

    identity = OPENAI_CHAT_PROFILES["opencode"].client_identity
    assert identity is not None
    provider = _provider("opencode")
    provider._client = AsyncOpenAI(
        api_key="sk-test-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
        default_headers=identity.default_headers(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await provider._create_stream(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert seen, "the probe transport was never reached"
    sent = seen[0].headers
    assert "x-mcc-harness" not in sent
    assert "x-mcc-harness-version" not in sent
    # The conversation was used, and reduced: the client's own id is not on the wire.
    assert sent["x-opencode-session"] == derived_session_id("a-real-conversation")
    assert "a-real-conversation" not in str(sent)
