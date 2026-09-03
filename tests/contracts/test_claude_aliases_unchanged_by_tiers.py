"""Claude Code must not notice that the coding-agent tiers exist.

It is the one harness with no generated catalogue: it self-discovers through
``/v1/models`` and speaks the eight ``claude-*`` protocol names. Those names,
what they resolve to, and the probe auto-response that echoes a resolved model
back are all pinned here, because "we added five aliases and Claude Code started
answering differently" is the failure this feature could most easily cause and
least easily be blamed for.
"""

from typing import Any

from my_claude_code.api.model_catalog import (
    SUPPORTED_CLAUDE_MODELS,
    build_models_list_response,
)
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.harness_tiers import HarnessTierOverride, HarnessTiers
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import Message, MessagesRequest
from my_claude_code.core.tier_refs import is_tier_ref
from tests.application.test_catalogue_model import FakeRuntime

PRIMARY = "nvidia_nim/primary"
OPUS = "open_router/opus"
SONNET = "open_router/sonnet"
HAIKU = "open_router/haiku"
OVERRIDE = "commandcode/override"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "model": PRIMARY,
        "MODEL_OPUS": OPUS,
        "MODEL_SONNET": SONNET,
        "MODEL_HAIKU": HAIKU,
    }
    values.update(overrides)
    return Settings(**values)


def test_claude_aliases_are_unchanged_by_the_harness_tiers() -> None:
    """Every alias, with an override loaded for the agent Claude Code is not.

    The harness argument is read for exactly one thing -- which chain a tier
    alias resolves through -- so a misattributed agent can never move a Claude
    alias. Asserted with a ``claude`` override present as well, since that is
    the case where a leak would actually show.
    """

    tiers = HarnessTiers(
        harnesses={"opencode": {"best": HarnessTierOverride(model=OVERRIDE)}}
    )
    router = ModelRouter(_settings(), harness_tiers=lambda: tiers)

    for harness in (None, "claude", "opencode"):
        assert router.resolve("claude-fable-5", harness=harness).provider_model_ref == (
            PRIMARY
        )
        assert router.resolve("claude-opus-5", harness=harness).provider_model_ref == (
            OPUS
        )
        assert (
            router.resolve("claude-sonnet-5", harness=harness).provider_model_ref
            == SONNET
        )
        assert (
            router.resolve(
                "claude-haiku-4-5-20251001", harness=harness
            ).provider_model_ref
            == HAIKU
        )


def test_the_eight_claude_protocol_names_are_still_listed_verbatim() -> None:
    """Claude Code cannot name a model that is not in this list."""

    settings = _settings()
    runtime = FakeRuntime(settings=settings)

    ids = {entry.id for entry in build_models_list_response(settings, runtime).data}

    for model in SUPPORTED_CLAUDE_MODELS:
        assert model.id in ids
    assert not any(is_tier_ref(model.id) for model in SUPPORTED_CLAUDE_MODELS)


def test_the_probe_auto_response_echoes_the_resolved_model_for_a_tier_too() -> None:
    """The 6.9.0 rule needs no change, and this is what says so.

    ``try_probe_auto_response`` fires after routing has already rewritten
    ``request.model`` to the resolved provider model, so a tier alias echoes the
    real id exactly as a Claude alias does. If routing ever stopped rewriting
    it, a probe would start replying "mcc/best" -- a model name no upstream has
    ever heard of.
    """

    router = ModelRouter(_settings(), harness_tiers=HarnessTiers)
    request = MessagesRequest(
        model="mcc/cheap",
        max_tokens=16,
        messages=[Message(role="user", content="hi")],
    )

    routed = router.resolve_messages_plan(request, harness="opencode").primary

    assert routed.request.model == "haiku"
    assert routed.resolved.provider_model_ref == HAIKU
    # The alias the client sent is not lost: it is what the request log stores.
    assert routed.resolved.original_model == "mcc/cheap"
