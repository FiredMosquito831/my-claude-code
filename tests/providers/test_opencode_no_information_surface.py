"""The door a multi-surface gateway knocks on when nobody has an opinion.

The defect this holds shut: a fresh or keyless install has no models.dev cache
on its first runs -- and neither has any install whose fetch failed -- so for
every Zen model the override, the learned facts and the registry all say
nothing, and the resolver fell through to ``DEFAULT_SURFACE``, a constant
written for the 39 providers that have exactly one surface. Zen has three. The
Models page then explained the choice with "this provider's only surface",
which is false for the only provider family that can reach that row.

What these tests hold:

* the profile declares its own no-information door, as data beside the
  surfaces it can speak -- never a model-name branch and never a provider-id
  branch in the resolver;
* that door is Chat Completions for both OpenCode profiles, because five of
  Zen's seven free models are behind it and two are not, and the two are
  carried by the surface rung in one probe;
* every profile that declares nothing keeps ``DEFAULT_SURFACE`` and the
  ``default`` source, byte for byte;
* the Models page says which of the two silences produced the answer: no
  registry entry for this model, or no cached registry at all.
"""

from typing import Any

import pytest

from my_claude_code.api.model_admin import (
    SURFACE_SOURCE_LABELS,
    response_surface_payload,
)
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    resolve_response_surface,
)
from my_claude_code.providers.openai_chat.response_surface import (
    DEFAULT_SURFACE,
    catalogue_surface,
    registry_silence,
    remember_response_surface,
)
from my_claude_code.providers.runtime import models_dev

OPENCODE = OPENAI_CHAT_PROFILES["opencode"]
#: A profile that declares no surfaces at all -- which is 39 of the 41.
GENERIC = OPENAI_CHAT_PROFILES["groq"]


def _write_registry(**models: object) -> None:
    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(
        {"opencode": {"npm": "@ai-sdk/openai-compatible", "models": dict(models)}},
        path,
    )
    models_dev.reset_models_dev_payload_cache()


def _no_registry_at_all() -> None:
    path = models_dev.models_dev_cache_path()
    if path.exists():
        path.unlink()
    models_dev.reset_models_dev_payload_cache()


def _resolve_opencode(model: str) -> Any:
    return resolve_response_surface(
        "opencode",
        model,
        registry_provider=OPENCODE.surface_registry_provider,
        declared=OPENCODE.response_surfaces,
        no_information=OPENCODE.no_information_surface,
    )


def _resolve_generic(model: str) -> Any:
    return resolve_response_surface(
        "groq",
        model,
        registry_provider=GENERIC.surface_registry_provider,
        declared=GENERIC.response_surfaces,
        no_information=GENERIC.no_information_surface,
    )


# --------------------------------------------------------------------------
# the declaration
# --------------------------------------------------------------------------


def test_both_opencode_profiles_declare_a_no_information_door() -> None:
    for provider_id in ("opencode", "opencode_go"):
        profile = OPENAI_CHAT_PROFILES[provider_id]
        assert profile.no_information_surface is ResponseSurface.CHAT_COMPLETIONS
        assert profile.no_information_surface in profile.response_surfaces


def test_no_other_profile_declares_one() -> None:
    """The blast radius, asserted rather than assumed."""

    declaring = {
        provider_id
        for provider_id, profile in OPENAI_CHAT_PROFILES.items()
        if profile.no_information_surface is not None
    }
    assert declaring == {"opencode", "opencode_go"}


# --------------------------------------------------------------------------
# the resolver table: {override, learned, registry, none} x two profiles
# --------------------------------------------------------------------------


def test_an_override_still_outranks_the_profiles_door(monkeypatch) -> None:
    from my_claude_code.config import model_overrides as overrides_module

    table = overrides_module.ModelParameterOverrides.from_document(
        {"models": {"opencode/m": {"response_surface": "responses"}}}
    )
    monkeypatch.setattr(
        "my_claude_code.providers.openai_chat.response_surface.current_model_overrides",
        lambda: table,
    )
    _no_registry_at_all()
    resolved = _resolve_opencode("m")
    assert resolved.surface is ResponseSurface.RESPONSES
    assert resolved.source is ResponseSurfaceSource.OVERRIDE


def test_a_learned_fact_still_outranks_the_profiles_door() -> None:
    _no_registry_at_all()
    remember_response_surface("opencode", "m", ResponseSurface.RESPONSES)
    resolved = _resolve_opencode("m")
    assert resolved.surface is ResponseSurface.RESPONSES
    assert resolved.source is ResponseSurfaceSource.LEARNED


def test_a_registry_entry_still_outranks_the_profiles_door() -> None:
    _write_registry(**{"m": {"provider": {"npm": "@ai-sdk/openai"}}})
    resolved = _resolve_opencode("m")
    assert resolved.surface is ResponseSurface.RESPONSES
    assert resolved.source is ResponseSurfaceSource.REGISTRY


def test_with_nothing_at_all_the_profiles_door_answers_and_says_so() -> None:
    _no_registry_at_all()
    resolved = _resolve_opencode("m")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.PROFILE
    assert resolved.detail == "no cached copy of the vendor's registry yet"
    assert resolved.label == "chat_completions (profile)"


def test_a_cached_registry_that_simply_does_not_name_this_model() -> None:
    """The other silence, and it must not be spelled like the first."""

    _write_registry(**{"other": {}})
    resolved = _resolve_opencode("m")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.PROFILE
    assert resolved.detail == "no registry entry for this model"


@pytest.mark.parametrize("registry", [True, False])
def test_a_generic_profile_is_byte_identical_either_way(registry: bool) -> None:
    if registry:
        _write_registry(**{"m": {}})
    else:
        _no_registry_at_all()
    resolved = _resolve_generic("m")
    assert resolved.surface is DEFAULT_SURFACE
    assert resolved.source is ResponseSurfaceSource.DEFAULT
    assert resolved.detail == ""
    # And the Models page still draws no surface row for it at all.
    assert catalogue_surface("groq", "m") is None


def test_the_two_silences_are_two_sentences() -> None:
    _no_registry_at_all()
    assert registry_silence("opencode") == "no cached copy of the vendor's registry yet"
    _write_registry(**{"m": {}})
    assert registry_silence("opencode") == "no registry entry for this model"
    # A profile that publishes no registry bucket has no silence to report.
    assert registry_silence("") == ""


# --------------------------------------------------------------------------
# what the operator reads
# --------------------------------------------------------------------------


def test_the_models_page_names_the_door_and_the_silence_behind_it() -> None:
    _no_registry_at_all()
    payload = response_surface_payload("opencode", "m")
    assert payload is not None
    assert payload["value"] == "chat_completions"
    assert payload["source"] == "profile"
    assert payload["source_label"] == "this provider's declared first door"
    assert payload["note"] == "no cached copy of the vendor's registry yet"


def test_the_only_surface_sentence_is_never_said_about_a_gateway() -> None:
    """The wording defect itself: three doors must not read as one."""

    _no_registry_at_all()
    payload = response_surface_payload("opencode", "m")
    assert payload is not None
    assert (
        payload["source_label"] != SURFACE_SOURCE_LABELS[ResponseSurfaceSource.DEFAULT]
    )
    assert len(payload["offered"]) == 3
