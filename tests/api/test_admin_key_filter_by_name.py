"""Filtering requests by a NAMED key returns exactly the rows the mask did.

7.78.1 makes the dashboard's Key filter offer a named key by its name. The
name is a display join (``credential_names.json`` against a hash of the
secret); a request row stores only the masked ``key_label`` it was written
with, and the store matches it with ``key_label = ?``. So the page turns the
name back into the mask before it asks, and these tests pin the server half of
that bargain through the real routes: the rows one mask selects do not depend
on whether, when, or how often the key was named -- a key named after its rows
were written, and a key renamed mid-history, still select every one of them.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.config.credentials import mask_key_label
from my_claude_code.core.request_log import (
    RequestRecord,
    default_request_log_path,
    get_request_log_store,
)
from tests.api.support import create_test_app

ENV_KEY = "NVIDIA_NIM_API_KEY"
KEY_A = "nvapi-test-named-key-1111"
KEY_B = "nvapi-test-unnamed-key-2222"
KEY_C = "nvapi-test-renamed-key-3333"
KEYS = {"A": KEY_A, "B": KEY_B, "C": KEY_C}


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in (ENV_KEY, "MCC_ENV_FILE", "MODEL", "HOST", "PORT"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_test_app(), client=("127.0.0.1", 50000))
    applied = client.post(
        "/admin/api/config/apply", json={"values": {ENV_KEY: ",".join(KEYS.values())}}
    )
    assert applied.status_code == 200, applied.text
    return client


def _write_batch(batch: int) -> list[str]:
    """Rows as the capture path writes them: the mask, never the key or a name."""

    store = get_request_log_store(default_request_log_path())
    assert store is not None
    ids = []
    for index, (letter, secret) in enumerate(KEYS.items()):
        for n in range(2):
            row_id = f"b{batch}-{letter}-{n}"
            store.enqueue(
                RequestRecord(
                    id=row_id,
                    endpoint="/v1/messages",
                    protocol="anthropic",
                    provider="nvidia_nim",
                    resolved_model="m1",
                    ts_epoch=1_790_000_000.0 + batch * 100 + index * 10 + n,
                    status="success",
                    key_index=index,
                    key_label=mask_key_label(secret),
                )
            )
            ids.append(row_id)
    store.close()
    return ids


def _name(client: TestClient, letter: str, name: str) -> None:
    rows = client.get(f"/admin/api/credentials/{ENV_KEY}/keys").json()["rows"]
    row = rows["ABC".index(letter)]
    response = client.put(
        f"/admin/api/credentials/{ENV_KEY}/keys/{row['id']}/name", json={"name": name}
    )
    assert response.status_code == 200, response.text


def _selected(client: TestClient, key: str) -> tuple[list[str], dict, str]:
    response = client.get(
        "/admin/api/requests", params={"key": key, "limit": 200, "local": "all"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return sorted(row["id"] for row in body["rows"]), body["key_names"], response.text


def test_a_mask_selects_the_same_rows_however_the_key_was_named(
    monkeypatch, tmp_path
) -> None:
    client = _client(monkeypatch, tmp_path)
    masks = {letter: mask_key_label(secret) for letter, secret in KEYS.items()}

    _write_batch(1)
    unnamed = {letter: _selected(client, mask)[0] for letter, mask in masks.items()}
    # A is named after its first rows were written; C gets a name it will lose.
    _name(client, "A", "Work laptop")
    _name(client, "C", "Team old")
    _write_batch(2)
    mid = {letter: _selected(client, mask)[0] for letter, mask in masks.items()}
    _name(client, "C", "Team shared")
    _write_batch(3)
    final = {}
    bodies = []
    for letter, mask in masks.items():
        final[letter], names, text = _selected(client, mask)
        bodies.append(text)

    for letter in KEYS:
        expected = sorted(
            f"b{batch}-{letter}-{n}" for batch in (1, 2, 3) for n in range(2)
        )
        # Named (A), unnamed (B), renamed mid-history (C): every row of the key,
        # including the ones written before its name or under its old name.
        assert final[letter] == expected, letter
        assert set(unnamed[letter]) <= set(mid[letter]) <= set(final[letter])
    assert unnamed["A"] == ["b1-A-0", "b1-A-1"]
    assert mid["C"] == ["b1-C-0", "b1-C-1", "b2-C-0", "b2-C-1"]
    # The name index the page renders from follows the rename; the rows do not
    # need to, because they never carried a name.
    assert names == {masks["A"]: "Work laptop", masks["C"]: "Team shared"}
    # A name is not a stored dimension: asking the store for one finds nothing.
    # This is why the page translates the box's text back to the mask.
    assert _selected(client, "Work laptop")[0] == []
    assert _selected(client, "Team old")[0] == []
    # No raw key in any answer, and none in the log file.
    for text in bodies:
        for secret in KEYS.values():
            assert secret not in text
    raw = b"".join(
        path.read_bytes()
        for path in default_request_log_path().parent.glob("requests.db*")
        if path.is_file()
    )
    for secret in KEYS.values():
        assert secret.encode() not in raw
    for name in ("Work laptop", "Team old", "Team shared"):
        assert name.encode() not in raw
