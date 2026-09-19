"""A reorder is only a feature if the pool is actually built in the new order.

Everything else in this PR is a display or a store. This file is the one that
says the thing the user asked for happened: after a reorder, slot 0 -- the slot
``failover`` serves first and ``single`` serves exclusively -- holds the key
that was moved to the top, and it holds it through the *unchanged* rotation
machinery.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.api.support import create_test_app

ENV_KEY = "NVIDIA_NIM_API_KEY"
PROVIDER_ID = "nvidia_nim"
KEY_A = "sk-alpha-key-1111"
KEY_B = "sk-bravo-key-2222"
KEY_C = "sk-charlie-key-3333"


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in (ENV_KEY, "MCC_ENV_FILE", "MODEL", "HOST", "PORT"):
        monkeypatch.delenv(name, raising=False)
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _pool_labels(value: str) -> tuple[str, ...]:
    # No rotation policy is set: with more than one key the default already is
    # ``failover``, which is the policy the order matters most to.
    settings = Settings(nvidia_nim_api_key=value)
    provider = create_provider(PROVIDER_ID, settings)
    assert isinstance(provider, RotatingProvider)
    return tuple(row["key_label"] for row in provider.key_health())


def test_the_pool_is_built_from_the_env_value_in_order() -> None:
    assert _pool_labels(f"{KEY_A},{KEY_B},{KEY_C}") == (
        mask_key_label(KEY_A),
        mask_key_label(KEY_B),
        mask_key_label(KEY_C),
    )


def test_reorder_changes_which_key_failover_serves_first(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    client.post(
        "/admin/api/config/apply",
        json={"values": {ENV_KEY: f"{KEY_A},{KEY_B},{KEY_C}"}},
    )
    rows = client.get(f"/admin/api/credentials/{ENV_KEY}/keys").json()["rows"]
    ids = [row["id"] for row in rows]

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": [ids[1], ids[2], ids[0]]},
    )
    assert response.status_code == 200, response.text

    written = ""
    for line in (tmp_path / ".mcc" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{ENV_KEY}="):
            written = line.split("=", 1)[1]
    assert written == f"{KEY_B},{KEY_C},{KEY_A}"

    # And the pool the rotation engine is handed is built in that order, so
    # slot 0 -- the one ``failover`` picks and ``single`` never leaves -- is
    # now the key that was moved to the top.
    labels = _pool_labels(written)
    assert labels[0] == mask_key_label(KEY_B)
    assert labels == (
        mask_key_label(KEY_B),
        mask_key_label(KEY_C),
        mask_key_label(KEY_A),
    )


def test_a_reorder_changes_nothing_about_the_pool_but_its_order() -> None:
    """Same set, same count, same secrets: only the sequence moved."""

    before = _pool_labels(f"{KEY_A},{KEY_B},{KEY_C}")
    after = _pool_labels(f"{KEY_C},{KEY_A},{KEY_B}")

    assert sorted(before) == sorted(after)
    assert len(before) == len(after) == 3
    assert before != after
