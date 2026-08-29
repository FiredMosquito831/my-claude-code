"""Settings parsing tests for request log configuration."""

from my_claude_code.config.settings import Settings


def test_request_log_defaults() -> None:
    settings = Settings()
    assert settings.request_log_enabled is True
    assert settings.request_log_capture_bodies is True
    assert settings.request_log_max_rows == 50_000


def test_request_log_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    monkeypatch.setenv("REQUEST_LOG_CAPTURE_BODIES", "false")
    monkeypatch.setenv("REQUEST_LOG_MAX_ROWS", "1234")
    settings = Settings()
    assert settings.request_log_enabled is False
    assert settings.request_log_capture_bodies is False
    assert settings.request_log_max_rows == 1234


def test_the_wire_body_cap_defaults_and_overrides(monkeypatch) -> None:
    assert Settings().request_log_wire_body_max_chars == 8000
    monkeypatch.setenv("REQUEST_LOG_WIRE_BODY_MAX_CHARS", "1234")
    assert Settings().request_log_wire_body_max_chars == 1234
