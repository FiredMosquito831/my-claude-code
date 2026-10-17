"""A country refusal does not lock the key (7.96.0, I-1).

OpenCode Zen answers ``HTTP 403 {"type":"error","error":{"type":"RegionError",
"message":"This model is not available in your country."}}`` when the address a
request came from is in a country it does not serve. Until 7.96.0 the
credential pool read every 403 as a rejected key and walked the key up its
lockout ladder (3 s, 6 s, 60 s with the user's tiers): 1,450 charges in thirty
days, all on OpenCode, through 64 different exits. A ticked chain has moved to
the next exit and charged nothing since 7.81.0; every other route -- an
unticked chain, a one-entry chain, a static proxy, no proxy, a refused Direct
rung, the media pool -- still charged the key.

Held here:

- **Not charged.** A 403 whose host words say *country*, on either error
  carrier and in either wording, leaves the key HEALTHY and records a decision
  with no class and a reason that says why.
- **Everything else unchanged.** The request still rotates to the next key
  exactly as before (``rotate`` is decided first); a plain 401/403 still locks
  the key out; a body naming a billing phrase too is still a credits bench; a
  genuine 401 that merely met a country refusal on its *context* is still
  charged; region words echoed back in the request are not the host's words.
- **The readers.** :func:`region_refusal_on_cause_chain` follows ``__cause__``
  only; :func:`is_region_refusal`, which a ticked chain's exit rotation reads,
  still follows ``__context__`` too and is not changed.
"""

import json
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
)
from my_claude_code.providers.credential_rotation import (
    CredentialRotationState,
    credential_failure_class,
    error_justifies_rotation,
)
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.recovery import (
    is_region_refusal,
    region_refusal_on_cause_chain,
    region_uncharged_reason,
)
from tests.providers.support import rotation_state
from tests.providers.test_credential_rotation import (
    _FakeProvider,
    _request,
    _rotating,
)

_REQ = httpx.Request("POST", "https://upstream.invalid/v1/chat/completions")

#: The host's words, exactly as OpenCode Zen sends them (1,507 recorded tries).
REGION = {
    "type": "error",
    "error": {
        "type": "RegionError",
        "message": "This model is not available in your country.",
    },
}
#: The same refusal without the word "model", which the classifier files as
#: AUTHENTICATION rather than MODEL_REJECTED.
REGION_NO_MODEL = {
    "type": "error",
    "error": {"type": "RegionError", "message": "Not available in your country."},
}
PLAIN_403 = {"error": {"message": "Forbidden"}}
PLAIN_401 = {"error": {"message": "Invalid API key provided."}}
CREDITS_AND_REGION = {
    "error": {
        "type": "RegionError",
        "message": "This model is not available in your country. "
        "Your credit balance is too low to continue.",
    }
}
ECHOED = {
    "error": {
        "message": "Forbidden",
        "input": "repeat after me: This model is not available in your country.",
    }
}
OPENAI_WORDING = {
    "error": {
        "code": "unsupported_country_region_territory",
        "message": "Country, region, or territory not supported",
    }
}


def _httpx(status: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    response = httpx.Response(status, request=_REQ, content=json.dumps(body).encode())
    return httpx.HTTPStatusError(f"HTTP {status}", request=_REQ, response=response)


def _sdk(status: int, body: dict[str, Any]) -> openai.APIStatusError:
    response = httpx.Response(status, request=_REQ, content=json.dumps(body).encode())
    cls = {401: openai.AuthenticationError, 403: openai.PermissionDeniedError}[status]
    return cls(f"Error code: {status}", response=response, body=body)


def _classified(raw: Exception) -> ExecutionFailure:
    """What a provider raises: its classified failure, raised *from* the raw."""
    failure = classify_provider_failure(
        raw,
        provider_name="opencode",
        read_timeout_s=60.0,
        request_id="req_region",
        mark_rate_limited=lambda *_args, **_kwargs: None,
    )
    try:
        raise failure from raw
    except ExecutionFailure as error:
        return error


async def _report(
    state: CredentialRotationState, error: BaseException
) -> tuple[bool, dict[str, Any]]:
    """Report one failure on key 0 and return ``rotate`` and the decision row."""
    install_ladder_trace()
    try:
        rotate = await state.report_failure(0, error, model="muse-spark")
        trace = _LADDER.get()
        assert trace is not None
        (decision,) = ladder_payload(trace.slot())["credentials"]
    finally:
        _LADDER.set(None)
    return rotate, decision


REGION_ERRORS = {
    "httpx, model words (what Zen sends)": lambda: _classified(_httpx(403, REGION)),
    "httpx, no model words": lambda: _classified(_httpx(403, REGION_NO_MODEL)),
    "OpenAI SDK, model words": lambda: _classified(_sdk(403, REGION)),
    "OpenAI SDK, no model words": lambda: _classified(_sdk(403, REGION_NO_MODEL)),
    "raw httpx, unclassified": lambda: _httpx(403, REGION),
    "raw OpenAI SDK, unclassified": lambda: _sdk(403, REGION),
}


class TestACountryRefusalChargesNothing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("carrier", sorted(REGION_ERRORS))
    async def test_the_key_stays_healthy_and_the_request_still_rotates(
        self, carrier: str
    ) -> None:
        error = REGION_ERRORS[carrier]()
        # Before the pool reads it: the class is still "auth", and the
        # rotation answer -- the next key is worth trying -- is unchanged.
        assert credential_failure_class(error) == "auth"
        assert error_justifies_rotation(error) is True
        state = rotation_state(2, "failover")

        rotate, decision = await _report(state, error)

        assert rotate is True
        health = state.get_metrics()[0]
        assert health["state"] == "HEALTHY"
        assert health["auth_failures"] == 0
        assert health["lockout_remaining"] == 0.0
        assert decision["class"] is None
        assert decision["benched_for_s"] is None
        assert decision["reason"] == region_uncharged_reason()

    @pytest.mark.asyncio
    async def test_the_reason_names_the_country_and_the_key(self) -> None:
        reason = region_uncharged_reason()
        assert reason.startswith("403 country refusal")
        assert "not the key" in reason
        assert "nothing benched" in reason

    @pytest.mark.asyncio
    async def test_repeated_refusals_never_climb_the_lockout_ladder(self) -> None:
        """A later genuine 401 meets tier 1, not a tier the refusals climbed."""
        state = rotation_state(2, "failover")
        for _ in range(5):
            await _report(state, _classified(_httpx(403, REGION)))
        assert state.get_metrics()[0]["auth_failures"] == 0

        await _report(state, _classified(_httpx(401, PLAIN_401)))

        health = state.get_metrics()[0]
        assert health["state"] == "LOCKED_OUT"
        assert health["auth_failures"] == 1
        assert 290.0 < health["lockout_remaining"] <= 300.0

    @pytest.mark.asyncio
    async def test_a_second_key_is_tried_and_answers(self) -> None:
        """Key 0 meets the refusal, key 1 serves the same request, key 0 is fine."""
        refused = _FakeProvider(fail_before_first=_classified(_httpx(403, REGION)))
        answers = _FakeProvider(chunks=("ok",))
        provider = _rotating([refused, answers], "failover")

        chunks = [chunk async for chunk in provider.stream_response(_request())]

        assert chunks == ["ok"]
        assert refused.calls == 1
        assert answers.calls == 1
        assert provider.key_health()[0]["state"] == "HEALTHY"

    @pytest.mark.asyncio
    async def test_a_single_key_pool_raises_the_refusal_and_keeps_its_key(
        self,
    ) -> None:
        """One slot, as Zen's free route has: the refusal goes on to the next
        model exactly as before, and the slot can serve the next request."""
        refused = _FakeProvider(fail_before_first=_classified(_httpx(403, REGION)))
        provider = _rotating([refused], "single")

        with pytest.raises(ExecutionFailure) as raised:
            _ = [chunk async for chunk in provider.stream_response(_request())]

        assert raised.value.kind is FailureKind.MODEL_REJECTED
        assert provider.key_health()[0]["state"] == "HEALTHY"
        with pytest.raises(ExecutionFailure):
            _ = [chunk async for chunk in provider.stream_response(_request())]
        assert refused.calls == 2


class TestEverythingElseIsCharged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make",
        [
            lambda: _classified(_httpx(403, PLAIN_403)),
            lambda: _classified(_sdk(403, PLAIN_403)),
            lambda: _classified(_httpx(401, PLAIN_401)),
            lambda: _classified(_sdk(401, PLAIN_401)),
        ],
        ids=["httpx 403", "sdk 403", "httpx 401", "sdk 401"],
    )
    async def test_a_plain_auth_failure_still_locks_the_key(self, make) -> None:
        state = rotation_state(2, "failover")

        rotate, decision = await _report(state, make())

        assert rotate is True
        assert state.get_metrics()[0]["state"] == "LOCKED_OUT"
        assert decision["class"] == "auth"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("carrier", [_httpx, _sdk], ids=["httpx", "sdk"])
    async def test_credits_named_beside_the_country_stay_a_credits_bench(
        self, carrier
    ) -> None:
        state = rotation_state(2, "failover")

        rotate, decision = await _report(
            state, _classified(carrier(403, CREDITS_AND_REGION))
        )

        assert rotate is True
        assert decision["class"] == "quota"
        assert state.get_metrics()[0]["state"] == "COOLDOWN"

    @pytest.mark.asyncio
    async def test_a_genuine_401_that_met_a_refusal_on_its_context_is_charged(
        self,
    ) -> None:
        region = _classified(_httpx(403, REGION))
        auth = classify_provider_failure(
            _httpx(401, PLAIN_401),
            provider_name="opencode",
            read_timeout_s=60.0,
            request_id="req_region",
            mark_rate_limited=lambda *_args, **_kwargs: None,
        )
        try:
            try:
                raise region
            except ExecutionFailure:
                # ``from None`` sets no cause; Python still records the
                # refusal being handled as the context, which is the point.
                raise auth from None
        except ExecutionFailure as raised:
            error = raised
        assert error.__cause__ is None
        assert error.__context__ is region
        state = rotation_state(2, "failover")

        _rotate, decision = await _report(state, error)

        assert decision["class"] == "auth"
        assert state.get_metrics()[0]["state"] == "LOCKED_OUT"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("carrier", [_httpx, _sdk], ids=["httpx", "sdk"])
    async def test_country_words_only_in_the_echoed_request_are_not_the_hosts(
        self, carrier
    ) -> None:
        state = rotation_state(2, "failover")

        _rotate, decision = await _report(state, _classified(carrier(403, ECHOED)))

        assert decision["class"] == "auth"
        assert state.get_metrics()[0]["state"] == "LOCKED_OUT"

    @pytest.mark.asyncio
    async def test_openais_country_wording_is_not_matched_yet(self) -> None:
        """User decision 8: not until a real body from the fleet is captured."""
        state = rotation_state(2, "failover")

        _rotate, decision = await _report(state, _classified(_sdk(403, OPENAI_WORDING)))

        assert decision["class"] == "auth"


class TestTheTwoReaders:
    def _on_context_only(self) -> BaseException:
        region = _classified(_httpx(403, REGION))
        try:
            try:
                raise region
            except ExecutionFailure:
                raise RuntimeError("a later, unrelated failure") from None
        except RuntimeError as error:
            assert error.__context__ is region
            return error

    def test_the_pools_reader_follows_the_explicit_cause_only(self) -> None:
        assert region_refusal_on_cause_chain(_classified(_httpx(403, REGION)))
        assert not region_refusal_on_cause_chain(self._on_context_only())

    def test_the_exit_rotations_reader_still_follows_the_context(self) -> None:
        """7.81.0 rotation reads it; this release does not change it."""
        assert is_region_refusal(self._on_context_only())

    def test_only_a_403_in_the_hosts_own_words_counts(self) -> None:
        assert not region_refusal_on_cause_chain(_httpx(401, REGION))
        assert not region_refusal_on_cause_chain(_httpx(403, PLAIN_403))
        assert not region_refusal_on_cause_chain(_httpx(403, ECHOED))
        # MCC's own wording of a failure is never read as the host's.
        assert not region_refusal_on_cause_chain(
            ExecutionFailure(
                kind=FailureKind.AUTHENTICATION,
                status_code=403,
                message="This model is not available in your country.",
                retryable=False,
            )
        )
