"""A media rail's models are never offered to a coding agent as chat models.

``configured_chat_model_refs`` feeds discovery, ``/v1/models`` and every
harness catalogue. An image model listed there would be offered to Claude Code
or Codex as something to chat with, and every such request can only fail.
"""

from my_claude_code.application.media.rails import configured_media_model_refs
from my_claude_code.config.model_refs import configured_chat_model_refs
from my_claude_code.config.settings import Settings


def _settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    return Settings.model_validate(
        {
            "MODEL": "deepseek/deepseek-chat",
            "MODEL_IMAGE": "xai/grok-2-image",
            "MODEL_IMAGE_FALLBACKS": "together/flux,gemini/gemini-3.1-flash-image",
        }
    )


def test_media_refs_never_reach_the_chat_catalogue(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    chat = {entry.model_ref for entry in configured_chat_model_refs(settings)}
    media = set(configured_media_model_refs(settings))
    assert media == {
        "xai/grok-2-image",
        "together/flux",
        "gemini/gemini-3.1-flash-image",
    }
    assert chat.isdisjoint(media)


def test_the_media_rail_is_not_a_chat_tier() -> None:
    """``mcc/image`` must not become a chat model on /v1/messages."""
    from my_claude_code.core.tier_refs import TIER_ORDER, ModelTier

    assert "image" not in {tier.value for tier in ModelTier}
    assert all("image" not in str(tier) for tier in TIER_ORDER)
