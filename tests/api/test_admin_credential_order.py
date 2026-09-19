"""Reordering and naming one credential pool, through the real routes.

The feature's whole promise is in these tests: the order the pool is stored in
is the order failover reads, a name never leaves the machine, and no mutation
is ever addressed by a position the caller might be wrong about.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config.credential_names import (
    credential_fingerprint,
    env_pool_id,
    pool_names,
)
from my_claude_code.config.credentials import mask_key_label
from tests.api.support import create_test_app

ENV_KEY = "NVIDIA_NIM_API_KEY"
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


def _seed(client: TestClient, *keys: str) -> None:
    response = client.post(
        "/admin/api/config/apply",
        json={"values": {ENV_KEY: ",".join(keys)}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True


def _rows(client: TestClient) -> list[dict]:
    listed = client.get(f"/admin/api/credentials/{ENV_KEY}/keys")
    assert listed.status_code == 200, listed.text
    return listed.json()["rows"]


def _env_value(tmp_path: Path) -> str:
    text = (tmp_path / ".mcc" / ".env").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(f"{ENV_KEY}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{ENV_KEY} not in {text}")


def test_the_key_listing_carries_an_id_a_name_and_both_masks(
    monkeypatch, tmp_path
) -> None:
    """The card mask and the analytics mask differ, so the row carries both."""

    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)

    rows = _rows(client)

    assert [row["index"] for row in rows] == [0, 1]
    assert [row["id"] for row in rows] == [
        credential_fingerprint(KEY_A),
        credential_fingerprint(KEY_B),
    ]
    assert rows[0]["masked"] == "sk-alp…1111"
    assert rows[0]["key_label"] == mask_key_label(KEY_A) == "sk-a…1111"
    assert rows[0]["name"] == ""
    # Two different masks, and neither of them is the key.
    assert KEY_A not in json.dumps(rows)


def test_reordering_writes_the_pool_in_the_new_order(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B, KEY_C)
    ids = [row["id"] for row in _rows(client)]

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": [ids[2], ids[0], ids[1]]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["count"] == 3
    assert _env_value(tmp_path) == f"{KEY_C},{KEY_A},{KEY_B}"
    assert [row["id"] for row in _rows(client)] == [ids[2], ids[0], ids[1]]


def test_reorder_preserves_the_env_files_quoting_and_blank_rules(
    monkeypatch, tmp_path
) -> None:
    """A comma-joined pool round-trips unquoted, exactly as an add leaves it."""

    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    before = _env_value(tmp_path)
    ids = [row["id"] for row in _rows(client)]

    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": list(reversed(ids))},
    )

    after = _env_value(tmp_path)
    assert before == f"{KEY_A},{KEY_B}"
    assert after == f"{KEY_B},{KEY_A}"
    assert '"' not in after


def test_reorder_does_not_require_a_restart(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": list(reversed(ids))},
    )

    restart = response.json().get("restart") or {}
    assert restart.get("required") in (False, None), restart


def test_reorder_keeps_every_name_attached_to_its_key(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B, KEY_C)
    ids = [row["id"] for row in _rows(client)]
    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[1]}/name",
        json={"name": "Personal"},
    )

    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": [ids[1], ids[0], ids[2]]},
    )

    rows = _rows(client)
    assert rows[0]["id"] == ids[1]
    assert rows[0]["name"] == "Personal"
    assert [row["name"] for row in rows] == ["Personal", "", ""]


def test_reorder_rejects_a_body_that_is_not_a_permutation_of_the_pool(
    monkeypatch, tmp_path
) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]

    short = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order", json={"order": [ids[0]]}
    )
    unknown = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": [ids[0], "sha256:deadbeefdeadbeef"]},
    )

    assert short.status_code == 409
    assert unknown.status_code == 409
    # Nothing was written by either refusal.
    assert _env_value(tmp_path) == f"{KEY_A},{KEY_B}"


def test_reorder_rejects_a_duplicate_id(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": [ids[0], ids[0]]},
    )

    assert response.status_code == 409
    assert _env_value(tmp_path) == f"{KEY_A},{KEY_B}"


def test_reorder_refuses_a_credential_from_a_locked_source(
    monkeypatch, tmp_path
) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]
    monkeypatch.setenv(ENV_KEY, f"{KEY_A},{KEY_B}")

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": list(reversed(ids))},
    )

    assert response.status_code == 409
    assert "process environment" in response.json()["detail"]


def test_naming_a_key_does_not_rebuild_the_provider_generation(
    monkeypatch, tmp_path
) -> None:
    """A name is not configuration, so it must not cost a provider rebuild."""

    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]
    env_before = (tmp_path / ".mcc" / ".env").read_bytes()

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[0]}/name",
        json={"name": "Work"},
    )

    assert response.status_code == 200
    assert response.json() == {"env_key": ENV_KEY, "id": ids[0], "name": "Work"}
    # The apply path rewrites ``.env`` on every config change. It was not
    # taken: the file is byte-identical.
    assert (tmp_path / ".mcc" / ".env").read_bytes() == env_before
    assert "restart" not in response.json()


def test_a_name_never_reaches_the_env_file(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]

    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[0]}/name",
        json={"name": "Work laptop"},
    )

    env_text = (tmp_path / ".mcc" / ".env").read_text(encoding="utf-8")
    assert "Work laptop" not in env_text
    store = tmp_path / ".mcc" / "credential_names.json"
    assert "Work laptop" in store.read_text(encoding="utf-8")
    assert KEY_A not in store.read_text(encoding="utf-8")


def test_an_empty_name_clears_it(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A)
    ids = [row["id"] for row in _rows(client)]
    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[0]}/name", json={"name": "Work"}
    )

    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[0]}/name", json={"name": ""}
    )

    assert _rows(client)[0]["name"] == ""


def test_naming_an_unknown_key_is_a_404(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A)

    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/sha256:0000000000000000/name",
        json={"name": "Nope"},
    )

    assert response.status_code == 404


def test_a_key_can_be_named_as_it_is_added(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A)

    response = client.post(
        f"/admin/api/credentials/{ENV_KEY}/keys",
        json={"key": KEY_B, "name": "Spare"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Spare"
    assert [row["name"] for row in _rows(client)] == ["", "Spare"]


def test_a_multi_key_paste_ignores_the_name(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A)

    response = client.post(
        f"/admin/api/credentials/{ENV_KEY}/keys",
        json={"key": f"{KEY_B},{KEY_C}", "name": "Spare"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == ""
    assert [row["name"] for row in _rows(client)] == ["", "", ""]


def test_delete_refuses_when_the_supplied_id_does_not_match_the_index(
    monkeypatch, tmp_path
) -> None:
    """The stale-tab hazard an editable order creates, closed."""

    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]
    # A second tab reorders underneath the first.
    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order",
        json={"order": list(reversed(ids))},
    )

    response = client.delete(f"/admin/api/credentials/{ENV_KEY}/keys/0?id={ids[0]}")

    assert response.status_code == 409
    assert "key list changed" in response.json()["detail"]
    assert _env_value(tmp_path) == f"{KEY_B},{KEY_A}"


def test_delete_without_an_id_still_works_for_an_older_dashboard(
    monkeypatch, tmp_path
) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)

    response = client.delete(f"/admin/api/credentials/{ENV_KEY}/keys/0")

    assert response.status_code == 200
    assert _env_value(tmp_path) == KEY_B


def test_deleting_a_key_drops_its_name(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]
    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[0]}/name", json={"name": "Work"}
    )

    client.delete(f"/admin/api/credentials/{ENV_KEY}/keys/0?id={ids[0]}")

    store = tmp_path / ".mcc" / "credential_names.json"
    assert pool_names(env_pool_id(ENV_KEY), store) == {}


def test_key_order_endpoints_are_loopback_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_KEY, raising=False)
    remote = TestClient(create_test_app(), client=("10.0.0.5", 50000))

    order = remote.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/order", json={"order": []}
    )
    name = remote.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/sha256:00/name", json={"name": "x"}
    )

    assert order.status_code == 403
    assert name.status_code == 403


def test_the_request_stats_payload_carries_the_name_index(
    monkeypatch, tmp_path
) -> None:
    """The dashboard renders a name from this map; the store cannot supply it."""

    client = _client(monkeypatch, tmp_path)
    _seed(client, KEY_A, KEY_B)
    ids = [row["id"] for row in _rows(client)]
    client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{ids[0]}/name", json={"name": "Work"}
    )

    stats = client.get("/admin/api/requests/stats")

    assert stats.status_code == 200
    body = stats.json()
    if body.get("enabled") is False:
        return
    assert body["key_names"] == {mask_key_label(KEY_A): "Work"}
