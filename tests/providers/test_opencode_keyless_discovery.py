"""A keyless install sees Zen's free models on the Models page.

7.34.0 gave an install with no ``OPENCODE_API_KEY`` the free Zen models: a
request for one goes out as ``Bearer public``, which is what the vendor's own
unauthenticated client sends. The Models page did not follow. Discovery asks
"does this provider have a credential" of the settings alone, Zen's answer
with no key was "no", and so the hourly sweep skipped it, the cache stayed
empty, and the card for the provider that had just become usable without a key
listed nothing at all.

The fix is the credential question, asked once:
``credential_or_public`` -- identity for the other forty providers, and for a
Zen install that has a key.

What these tests hold:

* Zen is discovered with no key, and ``opencode_go`` -- a paid subscription
  endpoint where ``public`` buys nothing -- is not;
* the opt-out ``OPENCODE_FREE_TIER_CREDENTIAL=key`` takes it back out, which
  is 7.33.0;
* no other provider's answer moves, with or without a key.
"""

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.discovery import (
    model_cache_provider_ids_for_settings,
)
from my_claude_code.providers.runtime.opencode_credentials import (
    credential_or_public,
)


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def _make(**env: str) -> Settings:
        monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("MODEL", "nvidia_nim/test-model")
        monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings()

    return _make


def test_zen_is_discovered_with_no_key_at_all(settings_for) -> None:
    ids = model_cache_provider_ids_for_settings(settings_for())

    assert "opencode" in ids


def test_the_paid_go_endpoint_is_not(settings_for) -> None:
    """``public`` buys nothing on a subscription endpoint, so it is left alone."""

    ids = model_cache_provider_ids_for_settings(settings_for())

    assert "opencode_go" not in ids


def test_the_opt_out_takes_it_back_out(settings_for) -> None:
    ids = model_cache_provider_ids_for_settings(
        settings_for(OPENCODE_FREE_TIER_CREDENTIAL="key")
    )

    assert "opencode" not in ids


def test_a_configured_key_is_still_what_is_discovered_on(settings_for) -> None:
    ids = model_cache_provider_ids_for_settings(
        settings_for(OPENCODE_API_KEY="sk-operator")
    )

    assert "opencode" in ids
    assert credential_or_public("opencode", "sk-operator") == "sk-operator"


def test_every_other_provider_answers_exactly_as_it_did(settings_for) -> None:
    """The blast radius: one provider id moves, and only in one direction."""

    settings = settings_for(GROQ_API_KEY="", OPENAI_API_KEY="")
    ids = set(model_cache_provider_ids_for_settings(settings))

    assert "groq" not in ids
    assert "openai" not in ids
    assert credential_or_public("groq", "") == ""
    assert credential_or_public("openai", "") == ""
