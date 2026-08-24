"""Tests for messaging platform factory."""

import logging
from unittest.mock import MagicMock, patch

from my_claude_code.messaging.platforms.factory import (
    MessagingPlatformOptions,
    create_messaging_components,
)
from my_claude_code.messaging.platforms.ports import MessagingStartupNotice


class TestCreateMessagingComponents:
    """Tests for create_messaging_components factory function."""

    def test_telegram_with_token(self):
        """Create Telegram platform when bot_token is provided."""
        mock_runtime = MagicMock()
        mock_runtime.name = "telegram"
        mock_runtime.outbound = MagicMock()
        limiter = MagicMock()
        transcriber = MagicMock()
        with (
            patch(
                "my_claude_code.messaging.platforms.factory.MessagingRateLimiter",
                return_value=limiter,
            ) as limiter_cls,
            patch(
                "my_claude_code.messaging.platforms.telegram.TELEGRAM_AVAILABLE", True
            ),
            patch(
                "my_claude_code.messaging.platforms.telegram.TelegramRuntime",
                return_value=mock_runtime,
            ) as runtime_cls,
        ):
            result = create_messaging_components(
                "telegram",
                MessagingPlatformOptions(
                    telegram_bot_token="test_token",
                    allowed_telegram_user_id="12345",
                    telegram_proxy_url="socks5://127.0.0.1:1080",
                    transcriber=transcriber,
                    messaging_rate_limit=7,
                    messaging_rate_window=2.5,
                ),
            )

        assert result is not None
        assert result.runtime is mock_runtime
        assert result.outbound is mock_runtime.outbound
        assert result.voice_cancellation is mock_runtime
        assert result.startup_notice == MessagingStartupNotice(
            chat_id="12345",
            transport_label="Bot API",
        )
        limiter_cls.assert_called_once_with(
            rate_limit=7,
            rate_window=2.5,
            log_error_details=False,
        )
        runtime_cls.assert_called_once_with(
            bot_token="test_token",
            allowed_user_id="12345",
            telegram_proxy_url="socks5://127.0.0.1:1080",
            limiter=limiter,
            transcriber=transcriber,
            log_raw_messaging_content=False,
            log_api_error_tracebacks=False,
        )

    def test_telegram_without_token(self):
        """Return None when no bot_token for Telegram."""
        result = create_messaging_components("telegram")
        assert result is None

    def test_telegram_empty_token(self):
        """Return None when bot_token is empty string."""
        result = create_messaging_components(
            "telegram", MessagingPlatformOptions(telegram_bot_token="")
        )
        assert result is None

    def test_discord_with_token(self):
        """Create Discord platform when discord_bot_token is provided."""
        mock_runtime = MagicMock()
        mock_runtime.name = "discord"
        mock_runtime.outbound = MagicMock()
        limiter = MagicMock()
        transcriber = MagicMock()
        with (
            patch(
                "my_claude_code.messaging.platforms.factory.MessagingRateLimiter",
                return_value=limiter,
            ) as limiter_cls,
            patch("my_claude_code.messaging.platforms.discord.DISCORD_AVAILABLE", True),
            patch(
                "my_claude_code.messaging.platforms.discord.DiscordRuntime",
                return_value=mock_runtime,
            ) as runtime_cls,
        ):
            result = create_messaging_components(
                "discord",
                MessagingPlatformOptions(
                    discord_bot_token="test_token",
                    allowed_discord_channels="123,456",
                    transcriber=transcriber,
                    messaging_rate_limit=3,
                    messaging_rate_window=4.5,
                ),
            )

        assert result is not None
        assert result.runtime is mock_runtime
        assert result.outbound is mock_runtime.outbound
        assert result.voice_cancellation is mock_runtime
        assert result.startup_notice is None
        limiter_cls.assert_called_once_with(
            rate_limit=3,
            rate_window=4.5,
            log_error_details=False,
        )
        runtime_cls.assert_called_once_with(
            bot_token="test_token",
            allowed_channel_ids="123,456",
            limiter=limiter,
            transcriber=transcriber,
            log_raw_messaging_content=False,
            log_api_error_tracebacks=False,
        )

    def test_discord_without_token(self):
        """Return None when no discord_bot_token for Discord."""
        result = create_messaging_components("discord")
        assert result is None

    def test_discord_empty_token(self):
        """Return None when discord_bot_token is empty string."""
        result = create_messaging_components(
            "discord",
            MessagingPlatformOptions(
                discord_bot_token="",
                allowed_discord_channels="123",
            ),
        )
        assert result is None

    def test_unknown_platform(self):
        """Return None for unknown platform types."""
        result = create_messaging_components("slack")
        assert result is None

    def test_unknown_platform_with_kwargs(self):
        """Return None for unknown platform even with kwargs."""
        result = create_messaging_components(
            "slack", MessagingPlatformOptions(telegram_bot_token="token")
        )
        assert result is None

    def test_separate_factory_calls_construct_distinct_limiters(self):
        """Each selected platform runtime owns a new limiter instance."""
        runtime = MagicMock(name="runtime")
        runtime.name = "telegram"
        runtime.outbound = MagicMock()
        with (
            patch(
                "my_claude_code.messaging.platforms.telegram.TelegramRuntime",
                return_value=runtime,
            ) as runtime_cls,
            patch(
                "my_claude_code.messaging.platforms.telegram.TELEGRAM_AVAILABLE", True
            ),
        ):
            first = create_messaging_components(
                "telegram",
                MessagingPlatformOptions(telegram_bot_token="one"),
            )
            second = create_messaging_components(
                "telegram",
                MessagingPlatformOptions(telegram_bot_token="two"),
            )

        assert first is not None
        assert second is not None
        assert first.startup_notice is None
        assert second.startup_notice is None
        first_limiter = runtime_cls.call_args_list[0].kwargs["limiter"]
        second_limiter = runtime_cls.call_args_list[1].kwargs["limiter"]
        assert first_limiter is not second_limiter
        assert runtime_cls.call_args_list[0].kwargs["transcriber"] is None
        assert runtime_cls.call_args_list[1].kwargs["transcriber"] is None


class TestMessagingAuthWarnings:
    """Platforms starting without an allowlist warn loudly; locked ones stay quiet."""

    def test_telegram_without_allowlist_warns_loudly(self, caplog):
        runtime = MagicMock(name="runtime")
        runtime.name = "telegram"
        with (
            patch(
                "my_claude_code.messaging.platforms.telegram.TELEGRAM_AVAILABLE",
                True,
            ),
            patch(
                "my_claude_code.messaging.platforms.telegram.TelegramRuntime",
                return_value=runtime,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = create_messaging_components(
                "telegram",
                MessagingPlatformOptions(telegram_bot_token="token"),
            )

        assert result is not None
        assert "ALLOWED_TELEGRAM_USER_ID" in caplog.text
        assert "act as the operator" in caplog.text

    def test_telegram_with_allowlist_stays_quiet(self, caplog):
        runtime = MagicMock(name="runtime")
        runtime.name = "telegram"
        with (
            patch(
                "my_claude_code.messaging.platforms.telegram.TELEGRAM_AVAILABLE",
                True,
            ),
            patch(
                "my_claude_code.messaging.platforms.telegram.TelegramRuntime",
                return_value=runtime,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = create_messaging_components(
                "telegram",
                MessagingPlatformOptions(
                    telegram_bot_token="token",
                    allowed_telegram_user_id="42",
                ),
            )

        assert result is not None
        assert "ALLOWED_TELEGRAM_USER_ID" not in caplog.text

    def test_telegram_blank_user_id_counts_as_unconfigured(self, caplog):
        runtime = MagicMock(name="runtime")
        runtime.name = "telegram"
        with (
            patch(
                "my_claude_code.messaging.platforms.telegram.TELEGRAM_AVAILABLE",
                True,
            ),
            patch(
                "my_claude_code.messaging.platforms.telegram.TelegramRuntime",
                return_value=runtime,
            ),
            caplog.at_level(logging.WARNING),
        ):
            create_messaging_components(
                "telegram",
                MessagingPlatformOptions(
                    telegram_bot_token="token",
                    allowed_telegram_user_id="  ",
                ),
            )

        assert "ALLOWED_TELEGRAM_USER_ID" in caplog.text

    def test_discord_without_allowlist_warns_loudly(self, caplog):
        runtime = MagicMock(name="runtime")
        runtime.name = "discord"
        with (
            patch(
                "my_claude_code.messaging.platforms.discord.DISCORD_AVAILABLE",
                True,
            ),
            patch(
                "my_claude_code.messaging.platforms.discord.DiscordRuntime",
                return_value=runtime,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = create_messaging_components(
                "discord",
                MessagingPlatformOptions(discord_bot_token="token"),
            )

        assert result is not None
        assert "ALLOWED_DISCORD_CHANNELS" in caplog.text
        assert "act as the operator" in caplog.text

    def test_discord_with_allowlist_stays_quiet(self, caplog):
        runtime = MagicMock(name="runtime")
        runtime.name = "discord"
        with (
            patch(
                "my_claude_code.messaging.platforms.discord.DISCORD_AVAILABLE",
                True,
            ),
            patch(
                "my_claude_code.messaging.platforms.discord.DiscordRuntime",
                return_value=runtime,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = create_messaging_components(
                "discord",
                MessagingPlatformOptions(
                    discord_bot_token="token",
                    allowed_discord_channels="111,222",
                ),
            )

        assert result is not None
        assert "ALLOWED_DISCORD_CHANNELS" not in caplog.text

    def test_discord_degenerate_channel_list_still_warns(self, caplog):
        runtime = MagicMock(name="runtime")
        runtime.name = "discord"
        with (
            patch(
                "my_claude_code.messaging.platforms.discord.DISCORD_AVAILABLE",
                True,
            ),
            patch(
                "my_claude_code.messaging.platforms.discord.DiscordRuntime",
                return_value=runtime,
            ),
            caplog.at_level(logging.WARNING),
        ):
            create_messaging_components(
                "discord",
                MessagingPlatformOptions(
                    discord_bot_token="token",
                    allowed_discord_channels=" , ",
                ),
            )

        assert "ALLOWED_DISCORD_CHANNELS" in caplog.text

    def test_no_warning_when_platform_skipped_for_missing_token(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert create_messaging_components("telegram") is None
            assert create_messaging_components("discord") is None

        assert "ALLOWED_TELEGRAM_USER_ID" not in caplog.text
        assert "ALLOWED_DISCORD_CHANNELS" not in caplog.text
