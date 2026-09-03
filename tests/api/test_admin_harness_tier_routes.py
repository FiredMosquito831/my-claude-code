"""The two routes behind the Coding agents card's Tiers section.

The first non-GET route on this module. It writes a JSON document rather than
settings keys, so it cannot go through ``/admin/api/config/apply`` -- that
route's flat env-key-to-string map has no way to express ``(harness, tier)``,
and widening it is what would break the dirty-state diff on every other page.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

PRIMARY = "nvidia_nim/primary"
OVERRIDE = "open_router/override"


@pytest.fixture(autouse=True)
def _isolate_tier_store(monkeypatch, tmp_path: Path):
    """Never the real ``~/.fcc``: this route writes a file."""

    from my_claude_code.api import admin_harness_routes
    from my_claude_code.config import harness_tiers

    path = tmp_path / "harness_tiers.json"
    monkeypatch.setattr(harness_tiers, "harness_tiers_path", lambda: path)
    monkeypatch.setattr(
        admin_harness_routes,
        "current_harness_tiers",
        lambda: harness_tiers.load_harness_tiers(path),
    )
    harness_tiers.reset_harness_tiers_cache()
    yield path
    harness_tiers.reset_harness_tiers_cache()


def _client() -> TestClient:
    # Loopback, as every admin route requires: the check is on the client host
    # and TestClient's default is not one.
    return TestClient(
        create_test_app(Settings(model=PRIMARY)), client=("127.0.0.1", 50000)
    )


def test_the_get_reports_every_tier_and_what_it_resolves_to() -> None:
    """The card cannot say "same as global Sonnet, currently X" without this."""

    payload = _client().get("/admin/api/harness-tiers").json()

    assert [tier["id"] for tier in payload["tiers"]] == [
        "best",
        "good",
        "medium",
        "cheap",
        "vision",
    ]
    assert [tier["ref"] for tier in payload["tiers"]] == [
        "mcc/best",
        "mcc/good",
        "mcc/medium",
        "mcc/cheap",
        "mcc/vision",
    ]
    # Every tier is unset on this install, so every one of them collapses onto
    # MODEL -- and the payload says so rather than hiding it.
    for tier in payload["tiers"]:
        assert tier["global"]["primary"] == PRIMARY

    opencode = payload["harnesses"]["opencode"]
    assert opencode["best"]["override"] is False
    assert opencode["best"]["resolved"]["primary"] == PRIMARY
    assert opencode["best"]["resolved"]["source"] == "global"
    # Claude Code has no generated picker, so it has no tiers to override.
    assert "claude" not in payload["harnesses"]


def test_a_post_writes_one_entry_and_returns_the_refreshed_state(
    _isolate_tier_store: Path,
) -> None:
    client = _client()

    payload = client.post(
        "/admin/api/harness-tiers",
        json={
            "harness": "opencode",
            "tier": "best",
            "override": True,
            "model": OVERRIDE,
            "fallbacks": [PRIMARY],
            "paused": [PRIMARY],
        },
    ).json()

    assert payload["harnesses"]["opencode"]["best"]["override"] is True
    assert payload["harnesses"]["opencode"]["best"]["resolved"]["primary"] == OVERRIDE
    assert payload["harnesses"]["opencode"]["best"]["resolved"]["source"] == "override"
    # And only that agent moved.
    assert payload["harnesses"]["crush"]["best"]["resolved"]["primary"] == PRIMARY

    assert json.loads(_isolate_tier_store.read_text(encoding="utf-8")) == {
        "harnesses": {
            "opencode": {
                "best": {
                    "model": OVERRIDE,
                    "fallbacks": [PRIMARY],
                    "paused": [PRIMARY],
                }
            }
        }
    }


def test_override_false_deletes_the_entry(_isolate_tier_store: Path) -> None:
    """ "Revert to global" removes it rather than emptying it."""

    client = _client()
    client.post(
        "/admin/api/harness-tiers",
        json={
            "harness": "opencode",
            "tier": "best",
            "override": True,
            "model": OVERRIDE,
        },
    )

    payload = client.post(
        "/admin/api/harness-tiers",
        json={"harness": "opencode", "tier": "best", "override": False},
    ).json()

    assert payload["harnesses"]["opencode"]["best"]["override"] is False
    assert json.loads(_isolate_tier_store.read_text(encoding="utf-8")) == {
        "harnesses": {}
    }


def test_an_override_pointing_at_another_tier_is_refused() -> None:
    """Rejected at the door, where the operator can still see what they typed."""

    response = _client().post(
        "/admin/api/harness-tiers",
        json={
            "harness": "opencode",
            "tier": "best",
            "override": True,
            "model": "mcc/cheap",
        },
    )

    assert response.status_code == 400
    assert "never at another tier" in response.json()["detail"]


def test_an_unknown_agent_or_tier_is_refused() -> None:
    client = _client()

    assert (
        client.post(
            "/admin/api/harness-tiers",
            json={"harness": "not-an-agent", "tier": "best", "override": True},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/admin/api/harness-tiers",
            json={"harness": "opencode", "tier": "turbo", "override": True},
        ).status_code
        == 400
    )


def test_two_writes_to_different_tiers_both_survive(_isolate_tier_store: Path) -> None:
    """Read and write are one critical section.

    Two edits landing together would each derive their document from a base read
    before the other committed, and the second would silently drop the first --
    the race ``apply_admin_config_with`` closes for the env-var settings.
    """

    client = _client()
    client.post(
        "/admin/api/harness-tiers",
        json={
            "harness": "opencode",
            "tier": "best",
            "override": True,
            "model": OVERRIDE,
        },
    )
    client.post(
        "/admin/api/harness-tiers",
        json={"harness": "crush", "tier": "cheap", "override": True, "model": OVERRIDE},
    )

    document = json.loads(_isolate_tier_store.read_text(encoding="utf-8"))
    assert set(document["harnesses"]) == {"opencode", "crush"}


def test_both_routes_are_loopback_only() -> None:
    """Every admin route is, and the first non-GET one on this module must be.

    The write is the reason this matters: a remote POST here would rewrite a
    file that decides which model every coding agent on this machine routes to.
    """

    remote = TestClient(
        create_test_app(Settings(model=PRIMARY)), client=("203.0.113.10", 50000)
    )

    assert remote.get("/admin/api/harness-tiers").status_code == 403
    assert (
        remote.post(
            "/admin/api/harness-tiers",
            json={"harness": "opencode", "tier": "best", "override": False},
        ).status_code
        == 403
    )
