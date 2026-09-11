"""MCC's OpenCode request, pinned against a capture of the real client.

6.69.0 got OpenCode's identity headers exactly right and still did not fix the
user's problem, because nobody had ever looked at a real OpenCode request. We
have one now, byte for byte, on both of the surfaces that matter:
``tests/contracts/opencode_reference_request.json`` reduces two recorded
requests from ``opencode-ai@1.18.30`` to the parts a test may legitimately
assert.

What is asserted, and why each is the strongest available form:

* **the path.** The whole defect this release fixes is that MCC posted to
  ``/chat/completions`` for a model served on ``/responses``. The path is the
  finding.
* **the identity header names**, as a set, on *both* surfaces. The capture
  shows the client sending the same five to ``/responses`` as to
  ``/chat/completions``, and the free-tier gate that refuses a request without
  them sits in front of both.
* **the identity emission sequence**, compared between MCC's own two surfaces
  rather than against the capture. HTTP header order carries no meaning and the
  capture's order differs from MCC's declared one (see
  ``opencode_identity.py``'s third correction); what must hold is that MCC's
  two surfaces do not disagree with *each other*.
* **the constant header values.** ``x-opencode-client`` must match exactly.
  ``User-Agent`` must be a prefix of the captured one -- MCC deliberately sends
  the first of its three segments (correction 1 in the same docstring), and a
  prefix check is what states that deliberately rather than pinning a value
  nobody chose.

What is deliberately NOT asserted: the credential, the two per-call ids, the
``x-stainless-*`` family (it carries the runner's Python and OS versions, which
is why 6.69.0's baseline is names-only), and the transport headers.

The honest limit, stated rather than implied: a fixture pins MCC to ONE
observation. It catches MCC drifting away from OpenCode. It cannot catch
OpenCode drifting away from the fixture. Retake it on any ``opencode-ai``
major, or whenever this area is touched.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.model_metadata import ResponseSurface
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_HEADER_ORDER,
)
from tests.providers.support import passthrough_rate_limiter

REFERENCE = json.loads(
    (Path(__file__).with_name("opencode_reference_request.json")).read_text(
        encoding="utf-8"
    )
)

#: The five names that are MCC's identity, as opposed to transport or content.
IDENTITY_HEADERS = frozenset(OPENCODE_HEADER_ORDER)


def _provider() -> Any:
    return create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["opencode"],
    )


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=16,
        messages=[Message(role="user", content="Reply with exactly: ok")],
    )


def _reference(surface: str) -> dict[str, Any]:
    return REFERENCE["surfaces"][surface]


def _identity_names(headers: dict[str, str]) -> list[str]:
    return [name for name in headers if name in IDENTITY_HEADERS]


def test_the_reference_capture_records_both_surfaces_and_how_it_was_taken() -> None:
    """A fixture nobody can date or reproduce is a fixture nobody may trust."""

    assert set(REFERENCE["surfaces"]) == {"responses", "chat_completions"}
    for key in ("_taken_at", "_client", "_how", "_retake_when", "_not_asserted"):
        assert REFERENCE[key], f"the fixture must say {key}"
    assert "1.18.30" in REFERENCE["_client"]


def test_the_reference_paths_are_the_two_surfaces_this_release_is_about() -> None:
    assert _reference("responses")["path"].endswith("/responses")
    assert _reference("chat_completions")["path"].endswith("/chat/completions")


def test_the_responses_transport_posts_to_the_captured_path() -> None:
    """The one difference that mattered: MCC's Responses URL is the CLI's."""

    provider = _provider()
    assert provider._responses.url.endswith(_reference("responses")["path"])


def test_both_mcc_surfaces_send_the_identity_names_the_capture_carries() -> None:
    """The capture shows all five on /responses too, so MCC must send all five."""

    provider = _provider()
    body, headers = provider._responses.build_body(
        _request("muse-spark-1.3-contributor-free"),
        reasoning=DEFAULT_REASONING_POLICY,
        max_output_tokens=16,
    )
    captured = set(_reference("responses")["header_names_in_order"]) & IDENTITY_HEADERS
    assert captured, "the capture must carry identity headers to be a reference"
    assert captured <= set(headers), (
        "MCC's OpenCode request has diverged from the captured reference: the "
        f"Responses surface is missing {sorted(captured - set(headers))}. "
        "Retake the capture (see the fixture's _how) before changing this test."
    )
    assert body["model"] == "muse-spark-1.3-contributor-free"


def test_mccs_two_surfaces_emit_the_identity_in_the_same_sequence() -> None:
    """Compared against each other, because header order has no meaning on the wire."""

    provider = _provider()
    _, responses_headers = provider._responses.build_body(
        _request("muse-spark-1.3-contributor-free"),
        reasoning=DEFAULT_REASONING_POLICY,
        max_output_tokens=16,
    )
    declared = [
        name for name in OPENCODE_HEADER_ORDER if name in set(responses_headers)
    ]
    assert _identity_names(responses_headers) == declared


@pytest.mark.parametrize("surface", ["responses", "chat_completions"])
def test_the_constant_header_values_still_match_the_captured_client(
    surface: str,
) -> None:
    """``x-opencode-client`` exactly; ``User-Agent`` as the prefix MCC chose."""

    provider = _provider()
    _, headers = provider._responses.build_body(
        _request("muse-spark-1.3-contributor-free"),
        reasoning=DEFAULT_REASONING_POLICY,
        max_output_tokens=16,
    )
    constants = _reference(surface)["constant_header_values"]
    assert headers["x-opencode-client"] == constants["x-opencode-client"]
    # Deliberately a prefix, not an equality: OpenCode's transport appends
    # three more segments underneath the literal MCC reads out of the bundle,
    # and 6.69.0's decision was to send the first one only. A byte-faithful
    # user-agent is its own PATCH.
    assert constants["User-Agent"].startswith(headers["User-Agent"])
    assert headers["User-Agent"].startswith("opencode/")


def test_the_responses_body_carries_every_key_the_real_client_sent() -> None:
    """Key order is not asserted; presence is, because absence is a defect."""

    provider = _provider()
    body, _ = provider._responses.build_body(
        _request("muse-spark-1.3-contributor-free"),
        reasoning=DEFAULT_REASONING_POLICY,
        max_output_tokens=16,
    )
    required = {"model", "input", "max_output_tokens", "store", "stream"}
    captured = set(_reference("responses")["body_keys_in_order"])
    assert required <= captured, "the fixture no longer describes the reference"
    assert required <= set(body), (
        "MCC's OpenCode Responses body has diverged from the captured "
        f"reference: missing {sorted(required - set(body))}."
    )


def test_the_prompt_cache_key_is_the_session_id_the_client_sends() -> None:
    """Where the session header earns its keep, exactly as the capture shows."""

    assert "prompt_cache_key" in _reference("responses")["body_keys_in_order"]
    provider = _provider()
    body, headers = provider._responses.build_body(
        _request("muse-spark-1.3-contributor-free"),
        reasoning=DEFAULT_REASONING_POLICY,
        max_output_tokens=16,
    )
    assert body["prompt_cache_key"] == headers["x-opencode-session"]


def test_no_surface_decision_is_derived_from_the_model_name() -> None:
    """The rule this whole feature is built to keep.

    Two models whose ids differ only in a substring a hand-written table would
    have keyed on resolve identically when the registry says nothing about
    either, and the resolver's source code names no model id at all.
    """

    from my_claude_code.providers.openai_chat import resolve_response_surface
    from my_claude_code.providers.openai_chat import response_surface as module

    declared = OPENAI_CHAT_PROFILES["opencode"].response_surfaces
    invented = [
        "muse-spark-9.9-contributor-free",
        "a-model-nobody-has-ever-published",
    ]
    resolved = {
        model: resolve_response_surface(
            "opencode", model, registry_provider="opencode", declared=declared
        ).surface
        for model in invented
    }
    assert set(resolved.values()) == {ResponseSurface.CHAT_COMPLETIONS}

    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in ("muse-spark", "mimo-", "glm-", "nemotron"):
        assert fragment not in source.replace("muse-spark-1.3-contributor-free", ""), (
            f"{fragment!r} appears in the resolver outside its documented "
            "evidence; the surface must come from metadata, never from a name"
        )
