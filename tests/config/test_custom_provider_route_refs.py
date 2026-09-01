"""Deleting erases the refs; disabling pauses them. Neither may break Settings."""

import pytest

from my_claude_code.config.admin.route_refs import (
    updates_pausing_provider,
    updates_removing_provider,
    updates_unpausing,
)
from my_claude_code.config.provider_registry import ProviderRegistry
from my_claude_code.config.settings import Settings


@pytest.fixture
def routed(monkeypatch, tmp_path) -> Settings:
    """One custom provider, named by a primary route and two chains."""
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        display_name="Acme",
        base_url="https://api.acme.example/v1",
        api_keys=("sk-acme-aaaa1111bbbb",),
    )
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)
    monkeypatch.setenv("MODEL", "custom_acme/m1")
    monkeypatch.setenv("MODEL_FALLBACKS", "custom_acme/m2,nvidia_nim/keep")
    monkeypatch.setenv("MODEL_OPUS_FALLBACKS", "nvidia_nim/keep,custom_acme/m3")
    return Settings()


def test_delete_clears_the_primary_and_prunes_the_chains(routed: Settings) -> None:
    updates, removed = updates_removing_provider(routed, "custom_acme")

    assert updates == {
        "MODEL": "",
        "MODEL_FALLBACKS": "nvidia_nim/keep",
        "MODEL_OPUS_FALLBACKS": "nvidia_nim/keep",
    }
    assert set(removed) == {
        "MODEL=custom_acme/m1",
        "MODEL_FALLBACKS=custom_acme/m2",
        "MODEL_OPUS_FALLBACKS=custom_acme/m3",
    }


def test_delete_leaves_a_provider_nothing_routes_to_alone(routed: Settings) -> None:
    updates, removed = updates_removing_provider(routed, "custom_other")

    assert updates == {}
    assert removed == ()


def test_disable_pauses_every_chain_entry_including_the_primary(
    routed: Settings,
) -> None:
    updates, added = updates_pausing_provider(routed, "custom_acme")

    assert updates == {
        "MODEL_PAUSED": "custom_acme/m1,custom_acme/m2",
        "MODEL_OPUS_PAUSED": "custom_acme/m3",
    }
    assert added == (
        ("MODEL_PAUSED", "custom_acme/m1"),
        ("MODEL_PAUSED", "custom_acme/m2"),
        ("MODEL_OPUS_PAUSED", "custom_acme/m3"),
    )


def test_disable_does_not_claim_a_pause_the_operator_already_made(
    monkeypatch, routed: Settings
) -> None:
    monkeypatch.setenv("MODEL_PAUSED", "custom_acme/m1")
    settings = Settings()

    updates, added = updates_pausing_provider(settings, "custom_acme")

    assert updates == {
        "MODEL_PAUSED": "custom_acme/m1,custom_acme/m2",
        "MODEL_OPUS_PAUSED": "custom_acme/m3",
    }
    # Only the ones it added. Re-enabling must not lift the hand-made pause.
    assert added == (
        ("MODEL_PAUSED", "custom_acme/m2"),
        ("MODEL_OPUS_PAUSED", "custom_acme/m3"),
    )


def test_enable_lifts_exactly_the_pauses_the_disable_added(
    monkeypatch, routed: Settings
) -> None:
    monkeypatch.setenv("MODEL_PAUSED", "custom_acme/m1,custom_acme/m2")
    settings = Settings()

    updates = updates_unpausing(settings, (("MODEL_PAUSED", "custom_acme/m2"),))

    assert updates == {"MODEL_PAUSED": "custom_acme/m1"}


def test_enable_writes_nothing_when_its_pauses_are_already_gone(
    routed: Settings,
) -> None:
    assert updates_unpausing(routed, (("MODEL_PAUSED", "custom_acme/m2"),)) == {}


def test_settings_still_builds_with_the_provider_disabled(
    monkeypatch, tmp_path
) -> None:
    """The reproduction from the spec, now the other way round.

    ``registry.update(id, enabled=False)`` is exactly what
    ``PATCH /admin/api/custom-providers/{id}`` performs. Before 6.25.0 the very
    next ``Settings()`` raised ``ValidationError`` with one error per route
    naming the provider.
    """
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        display_name="Acme",
        base_url="https://api.acme.example/v1",
        api_keys=("sk-acme-aaaa1111bbbb",),
    )
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)
    monkeypatch.setenv("MODEL", "custom_acme/m1")
    monkeypatch.setenv("MODEL_OPUS_FALLBACKS", "custom_acme/m1")

    registry.update("custom_acme", enabled=False)

    settings = Settings()
    assert settings.model == "custom_acme/m1"
    assert updates_pausing_provider(settings, "custom_acme")[1]
