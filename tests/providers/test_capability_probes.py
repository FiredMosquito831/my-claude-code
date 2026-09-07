"""What a probe may conclude, and -- more importantly -- what it may not.

A probe outranks the provider's own catalogue, which is only safe because of
two rules: it may only ever narrow, and a 200 proves nothing. Both are tested
here, because both are the difference between "measured this deployment" and
"guessed confidently".
"""

import httpx
import pytest

from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.providers.runtime.capability_probes import (
    ALL_PROBES,
    DEFAULT_PROBES,
    IMPOSSIBLE_OUTPUT_TOKENS,
    MAX_MODELS_PER_PROBE_RUN,
    OPTIONAL_PROBES,
    PROBE_FACT_KINDS,
    probe_model_capabilities,
)
from my_claude_code.providers.runtime.reasoning_probe import PROBE_MAX_TOKENS


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_error(message: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message}})


@pytest.mark.asyncio
async def test_output_cap_probe_reads_the_stated_maximum() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return _json_error("max_completion_tokens must be less than or equal to 40960")

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("output_cap",), client=client
        )

    assert outcome.status == "learned"
    assert outcome.value == 40960
    # Both spellings in one body, so an OpenAI-compatible host names whichever
    # of them it owns.
    assert seen[0]["max_tokens"] == IMPOSSIBLE_OUTPUT_TOKENS
    assert seen[0]["max_completion_tokens"] == IMPOSSIBLE_OUTPUT_TOKENS
    assert seen[0]["stream"] is False


@pytest.mark.asyncio
async def test_output_cap_probe_records_nothing_on_200() -> None:
    """A host that accepted two billion tokens has told us nothing at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("output_cap",), client=client
        )

    assert outcome.status == "ignored"
    assert outcome.value is None


@pytest.mark.asyncio
async def test_vision_probe_learns_from_a_modality_400() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_error("this model does not support image_url content")

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("vision",), client=client
        )

    assert outcome.status == "learned"
    assert outcome.value is True


@pytest.mark.asyncio
async def test_a_400_about_something_else_teaches_nothing() -> None:
    """An unrelated rejection is 'unknown', never a verdict about the field."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_error("top_p is immutable for this model")

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("vision",), client=client
        )

    assert outcome.status == "unknown"


@pytest.mark.asyncio
async def test_tool_probe_learns_from_a_tools_400() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_error("tool_choice is not supported by this deployment")

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("tool_calls",), client=client
        )

    assert outcome.status == "learned"


@pytest.mark.asyncio
async def test_stream_usage_probe_learns_from_the_sdk_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_error("stream_options.include_usage is not supported")

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("stream_usage",), client=client
        )

    assert outcome.status == "learned"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_402_403_before_validation_is_unprobeable(status: int) -> None:
    """Nothing was measured, so nothing is claimed -- and nothing else is asked."""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, json={"error": {"message": "no"}})

    async with _client(handler) as client:
        outcomes = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=ALL_PROBES, client=client
        )

    assert len(outcomes) == 1
    assert outcomes[0].status == "unknown"
    assert outcomes[0].detail == f"unprobeable ({status})"
    # One request, not four: every further probe would get the same non-answer
    # from the same credential.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_probe_stores_no_response_text() -> None:
    secret = "Bearer sk-do-not-store-me and a whole transcript besides"

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_error(f"max_tokens must be <= 4096 -- {secret}")

    async with _client(handler) as client:
        (outcome,) = await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("output_cap",), client=client
        )

    payload = outcome.as_payload()
    assert secret not in str(payload)
    # A status word and at most an HTTP code, exactly as reasoning_probe does.
    assert payload["detail"] == "400 named a maximum"


@pytest.mark.asyncio
async def test_a_probe_sends_a_tiny_budget_except_where_it_is_the_question() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        await probe_model_capabilities(
            "https://host/v1", "key", "m", probes=("vision",), client=client
        )

    assert bodies[0]["max_tokens"] == PROBE_MAX_TOKENS


def test_probe_only_narrows() -> None:
    """Every shipped probe's ``learned`` value removes or lowers. Never adds.

    ``output_cap`` writes a number that is only ever applied as a clamp, and
    every other probe writes a bare negative. There is no probe whose success
    asserts a capability, which is the whole reason a probe may outrank the
    catalogue.
    """

    for probe, fact_kind in PROBE_FACT_KINDS.items():
        assert probe == "output_cap" or fact_kind.endswith("_unsupported")


def test_the_shipped_probe_set_is_the_cheap_half() -> None:
    """Tool calling and streamed usage are implemented but shipped off.

    They are the two a *correct* host answers by generating billable output
    tokens, which is a different bargain from reading a refusal.
    """

    assert DEFAULT_PROBES == ("output_cap", "vision")
    assert OPTIONAL_PROBES == ("tool_calls", "stream_usage")
    assert set(ALL_PROBES) == set(PROBE_FACT_KINDS)


def test_a_press_is_bounded() -> None:
    assert MAX_MODELS_PER_PROBE_RUN == 25


def test_a_probe_outranks_the_catalogue_but_stays_authoritative() -> None:
    """Tier 0 is above tier 1, and the two range checks still hold."""

    assert ResolutionTier.PROBED_DEPLOYMENT < ResolutionTier.PROVIDER_EXACT
    assert ResolutionTier.PROBED_DEPLOYMENT.is_authoritative
    assert not ResolutionTier.PROBED_DEPLOYMENT.is_approximate
    assert not ResolutionTier.PROBED_DEPLOYMENT.is_reference
    # The existing rungs did not move; renumbering them is what would break a
    # stored tier integer.
    assert int(ResolutionTier.PROVIDER_EXACT) == 1
    assert int(ResolutionTier.FALLBACK_DEFAULT) == 11


def test_no_probe_from_a_served_request() -> None:
    """Nothing on the request path may reach the probe module.

    Enforced by import, not by intention: a provider that imported it would
    show up here.
    """

    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "my_claude_code"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("runtime/") or "capability_probes" in relative:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "capability_probes" in (
                node.module or ""
            ):
                offenders.append(f"{relative}:{node.lineno}")
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{relative}:{node.lineno}"
                    for alias in node.names
                    if "capability_probes" in alias.name
                )

    assert offenders == [], (
        "a probe is an operator action; these modules import it: "
        + ", ".join(offenders)
    )
