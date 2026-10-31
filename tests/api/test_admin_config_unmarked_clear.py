"""The server refuses to blank a setting the request did not clear (7.121.3).

Up to 7.121.0 the dashboard's `<select>` could not hold a saved value that was
not among its options, so the control rendered blank, the page reported that
blank as an edit, and one Save deleted the line: ``""`` means "unset" to this
save path, and nothing asked whether anybody meant it. 7.121.1 fixed the page;
this release makes the server say no on its own, because a page is not the only
thing that can post a blank -- a dashboard left open across the upgrade, or a
script.

The contract, and what each test below proves:

* a request that would blank a setting it did not name in ``cleared`` is
  refused whole, with **409** and a sentence naming the setting, before
  anything is read or written -- ``.env`` and ``.env.previous`` byte-identical,
  and the valid half of a mixed body not written either;
* a setting named in ``cleared`` is unset exactly as 7.121.2 did it (the line
  goes, the repo ``.env``/code default shows through again);
* a body that clears nothing is untouched (no ``cleared`` needed);
* the typed routes that deliberately write a blank -- removing the last key of
  a pool, lifting the last pause, an emptied visibility pair -- still work,
  because their request shape is the intent and none of them goes through the
  flat map.
"""

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.admin import sources
from my_claude_code.config.admin.persistence import (
    UnmarkedClearError,
    cleared_keys_in,
    refuse_unmarked_clears,
)
from tests.api.support import create_test_app

SEED = (
    "DEEPSEEK_API_KEY=sk-test-fake-0001\n"
    "LOG_LEVEL=debug\n"
    "DDGS_BACKEND=brave,mojeek\n"
    "PROXY_CHECK_ENABLED=True\n"
    "MODEL_FABLE_PAUSED=nvidia_nim/vendor/fable-b\n"
    "MCC_SMOKE_MARKER=keep-me\n"
)

#: Every key a save touches in this module is cleared from the process
#: environment: ``load_value_state`` treats the process environment as its
#: highest layer, so an exported copy would decide the answer instead of the
#: fixture file.
_ISOLATED_KEYS = (
    "DEEPSEEK_API_KEY",
    "LOG_LEVEL",
    "DDGS_BACKEND",
    "PROXY_CHECK_ENABLED",
    "DDGS_REGION",
    "MODEL_FABLE",
    "MODEL_FABLE_FALLBACKS",
    "MODEL_FABLE_PAUSED",
    "MODEL_VISIBILITY_ALLOW",
    "MODEL_VISIBILITY_DENY",
    "MCC_ENV_FILE",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / ".mcc"
    config.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config))
    monkeypatch.chdir(tmp_path)
    for key in _ISOLATED_KEYS:
        monkeypatch.delenv(key, raising=False)
    sources.clear_env_parse_cache()
    path = config / ".env"
    path.write_text(SEED, encoding="utf-8")
    return path


@pytest.fixture
def client(managed: Path) -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _live_lines(path: Path) -> dict[str, str]:
    """The file's `KEY=value` lines, so a commented placeholder cannot match."""

    lines: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key and key == key.upper() and not line.startswith("#"):
            lines[key] = value
    return lines


# ---------------------------------------------------------------- the guard


def test_a_blank_nobody_cleared_is_refused_and_names_the_key(
    managed: Path, client: TestClient
) -> None:
    """The DDG-6 shape: blanks a page produced because it could not show them."""

    before = _sha(managed)

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"LOG_LEVEL": "", "DDGS_BACKEND": ""}},
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail.startswith("Not saved:")
    # Names every setting it would have removed a line for ...
    assert "LOG_LEVEL" in detail
    assert "DDGS_BACKEND" in detail
    # ... says nothing was changed, and says what to do about it.
    assert "Nothing was changed" in detail
    assert "Reload the dashboard" in detail
    # Never a byte of the file, and never a secret.
    assert "sk-test-fake" not in response.text
    assert _sha(managed) == before
    assert not (managed.parent / ".env.previous").exists()
    assert _live_lines(managed)["LOG_LEVEL"] == "debug"
    assert _live_lines(managed)["DDGS_BACKEND"] == "brave,mojeek"


def test_a_mixed_body_is_refused_whole(managed: Path, client: TestClient) -> None:
    """One real edit cannot carry an accidental blank past the guard.

    The save has been all-or-nothing since 7.69.7; its refusal is too, so the
    valid half is not written either -- otherwise the next Save would succeed
    and the blank would still be in the page.
    """

    before = _sha(managed)

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"DDGS_REGION": "uk-en", "LOG_LEVEL": ""}},
    )

    assert response.status_code == 409, response.text
    assert "LOG_LEVEL" in response.json()["detail"]
    assert "DDGS_REGION" not in response.json()["detail"]
    assert _sha(managed) == before
    assert "DDGS_REGION" not in _live_lines(managed)
    assert _live_lines(managed)["LOG_LEVEL"] == "debug"
    assert not (managed.parent / ".env.previous").exists()


def test_a_page_that_sends_no_cleared_list_at_all_is_refused(
    managed: Path, client: TestClient
) -> None:
    """The point of the guard: a stale page (<= 7.121.2) is exactly that client.

    It posts only ``values``, and its blanks are the ones that wiped settings.
    """

    response = client.post(
        "/admin/api/config/apply", json={"values": {"LOG_LEVEL": ""}}
    )

    assert response.status_code == 409, response.text
    assert _live_lines(managed)["LOG_LEVEL"] == "debug"


def test_the_same_request_through_validate_is_refused_the_same_way(
    managed: Path, client: TestClient
) -> None:
    """A preview must not read "valid" for a body the save would refuse."""

    before = _sha(managed)

    response = client.post(
        "/admin/api/config/validate", json={"values": {"LOG_LEVEL": ""}}
    )

    assert response.status_code == 409, response.text
    assert "LOG_LEVEL" in response.json()["detail"]
    assert _sha(managed) == before


def test_a_cleared_key_is_unset_exactly_as_before(
    managed: Path, client: TestClient
) -> None:
    """Q6 kept: "Default (…)" still clears, and one rolling copy still holds it."""

    before = managed.read_bytes()

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"LOG_LEVEL": ""}, "cleared": ["LOG_LEVEL"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    lines = _live_lines(managed)
    assert "LOG_LEVEL" not in lines
    # Every other line is byte-identical, the unmanaged one included.
    assert lines["DDGS_BACKEND"] == "brave,mojeek"
    assert lines["MCC_SMOKE_MARKER"] == "keep-me"
    assert (managed.parent / ".env.previous").read_bytes() == before


def test_a_cleared_key_that_also_carries_a_value_is_saved_as_given(
    managed: Path, client: TestClient
) -> None:
    """The list permits a blank; it never forces one.

    A page can name a key in ``cleared`` and send a value for it (someone types
    into a control they had emptied): the value wins, because ``values`` is
    unchanged and the list only ever unlocks the blank.
    """

    response = client.post(
        "/admin/api/config/apply",
        json={"values": {"LOG_LEVEL": "INFO"}, "cleared": ["LOG_LEVEL"]},
    )

    assert response.status_code == 200, response.text
    assert _live_lines(managed)["LOG_LEVEL"] == "INFO"


def test_a_body_that_clears_nothing_needs_no_list(
    managed: Path, client: TestClient
) -> None:
    before = _sha(managed)

    response = client.post(
        "/admin/api/config/apply", json={"values": {"DDGS_REGION": "uk-en"}}
    )

    assert response.status_code == 200, response.text
    assert _live_lines(managed)["DDGS_REGION"] == "uk-en"
    assert _sha(managed) != before


def test_an_empty_body_and_an_empty_list_are_the_same_request(
    managed: Path, client: TestClient
) -> None:
    """The frozen desktop tray restarts the server with exactly this body."""

    response = client.post(
        "/admin/api/config/apply", json={"values": {}, "cleared": []}
    )

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True


# ----------------------------------------------------------- what a blank is


@pytest.mark.parametrize(
    ("key", "value", "is_clear"),
    [
        ("LOG_LEVEL", "", True),
        ("LOG_LEVEL", None, True),
        # A model chain of nothing but separators normalises to the same blank
        # the save would unset, so the guard reads it as one too.
        ("MODEL_FABLE_FALLBACKS", " , ", True),
        ("LOG_LEVEL", "INFO", False),
        ("LOG_LEVEL", "None", False),  # a literal text value, not a clear
    ],
)
def test_the_guard_reads_a_blank_exactly_as_the_save_does(
    key: str, value: object, is_clear: bool
) -> None:
    """One definition: the guard cannot disagree with the write it guards."""

    assert cleared_keys_in({key: value}) == ([key] if is_clear else [])


def test_a_secrets_blank_is_a_value_never_a_clear() -> None:
    """A credential with nothing in it is stored as such, as it always was."""

    assert cleared_keys_in({"DEEPSEEK_API_KEY": ""}) == []
    refuse_unmarked_clears({"DEEPSEEK_API_KEY": ""})


def test_a_key_that_is_not_a_setting_is_ignored_by_the_guard() -> None:
    """Unknown keys are dropped by the route before the save, as they always were."""

    assert cleared_keys_in({"NOT_A_MANIFEST_KEY": ""}) == []


def test_the_guard_is_a_no_op_when_every_blank_is_marked() -> None:
    refuse_unmarked_clears(
        {"LOG_LEVEL": "", "DDGS_BACKEND": ""}, ["LOG_LEVEL", "DDGS_BACKEND"]
    )


def test_the_refusal_lists_the_unmarked_keys_in_request_order() -> None:
    with pytest.raises(UnmarkedClearError) as raised:
        refuse_unmarked_clears(
            {"DDGS_BACKEND": "", "LOG_LEVEL": "", "PROXY_CHECK_ENABLED": ""},
            ["LOG_LEVEL"],
        )

    assert raised.value.keys == ("DDGS_BACKEND", "PROXY_CHECK_ENABLED")


# ---------------------------------------- the typed routes keep their clears


def test_removing_the_last_key_of_a_pool_still_writes_what_it_always_did(
    managed: Path, client: TestClient
) -> None:
    """DELETE, not a flat map: the request shape is the intent (7.121.3 audit).

    The pool's value is now empty, and the save writes it because this route
    never goes through the guarded map. A credential is a secret, so an empty
    pool is the value ``KEY=`` -- not a clear -- and the guard says so too; the
    line's presence is 7.121.2's answer, byte for byte.
    """

    listed = client.get("/admin/api/credentials/DEEPSEEK_API_KEY/keys")
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 1

    response = client.delete("/admin/api/credentials/DEEPSEEK_API_KEY/keys/0")

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert response.json()["count"] == 0
    assert _live_lines(managed)["DEEPSEEK_API_KEY"] == ""
    assert _live_lines(managed)["LOG_LEVEL"] == "debug"


def test_lifting_the_last_pause_still_blanks_the_list(
    managed: Path, client: TestClient
) -> None:
    response = client.post(
        "/admin/api/config/route-pause",
        json={
            "model_key": "MODEL_FABLE",
            "model_ref": "nvidia_nim/vendor/fable-b",
            "paused": False,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert "MODEL_FABLE_PAUSED" not in _live_lines(managed)


def test_an_emptied_visibility_pair_still_blanks_both_lists(
    managed: Path, client: TestClient
) -> None:
    response = client.post(
        "/admin/api/model-admin/visibility", json={"allow": "", "deny": ""}
    )

    assert response.status_code == 200, response.text
    lines = _live_lines(managed)
    assert "MODEL_VISIBILITY_ALLOW" not in lines
    assert "MODEL_VISIBILITY_DENY" not in lines
