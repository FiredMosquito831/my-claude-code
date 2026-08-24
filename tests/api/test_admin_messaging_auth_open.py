"""GET /admin/api/config exposes messaging_auth_open for the dashboard.

The field lists platform ids that are selected by config, have their bot
token set, and would therefore start WITHOUT an operator/channel allowlist.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.api.support import create_test_app

_MESSAGING_KEYS = (
    "FCC_ENV_FILE",
    "MESSAGING_PLATFORM",
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_TELEGRAM_USER_ID",
    "DISCORD_BOT_TOKEN",
    "ALLOWED_DISCORD_CHANNELS",
)


def _isolated_client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in _MESSAGING_KEYS:
        monkeypatch.delenv(key, raising=False)
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def test_messaging_auth_open_present_and_empty_by_default(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)

    response = client.get("/admin/api/config")

    assert response.status_code == 200
    assert response.json()["messaging_auth_open"] == []


def test_open_telegram_is_listed(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == ["telegram"]


def test_locked_telegram_is_not_listed(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "42")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == []


def test_blank_telegram_user_id_counts_as_open(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "  ")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == ["telegram"]


def test_open_discord_is_listed(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "discord")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == ["discord"]


def test_locked_discord_is_not_listed(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "discord")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_DISCORD_CHANNELS", "111,222")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == []


def test_platform_without_bot_token_is_never_listed(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "telegram")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == []


def test_disabled_platform_is_never_listed(monkeypatch, tmp_path):
    client = _isolated_client(monkeypatch, tmp_path)
    monkeypatch.setenv("MESSAGING_PLATFORM", "none")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    response = client.get("/admin/api/config")

    assert response.json()["messaging_auth_open"] == []
