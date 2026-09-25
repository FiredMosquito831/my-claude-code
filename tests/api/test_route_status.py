"""The Model Config rail hint: which providers cannot serve a request right now.

Pinned here: the three answers the page draws (no credential to sign in with,
no key, every key benched), that a healthy provider is absent, that nothing is
built, refreshed or written to find out, and that the config payload the page
already loads carries it.
"""

import os
import time
from unittest.mock import MagicMock

import pytest

from my_claude_code.api import route_status
from my_claude_code.api.route_status import (
    ALL_BENCHED,
    NO_CREDENTIALS,
    benched_until,
    config_changed_at,
    credential_problems,
)
from my_claude_code.config.constants import (
    ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.runtime.rotating import RotatingProvider
from tests.api.support import create_test_app
from tests.api.test_admin import _clear_process_config, _local_client, _set_home

NOW = 1_800_000_000.0


def _remote(provider_id: str, status: str = "configured") -> dict:
    return {
        "provider_id": provider_id,
        "display_name": provider_id.upper(),
        "kind": "remote",
        "status": status,
    }


@pytest.fixture
def empty_stores(monkeypatch):
    """No stored sign-in anywhere; the tests opt into one explicitly."""

    monkeypatch.setattr(route_status, "load_chatgpt_accounts", lambda **_: [])
    monkeypatch.setattr(route_status, "load_accounts", lambda **_: [])
    monkeypatch.setattr(route_status, "load_claude_code_tokens", lambda: None)


def test_a_sign_in_provider_with_no_stored_account_cannot_serve(empty_stores) -> None:
    """The 2026-09-20 case: the setting holds the managed-store reference, and
    the store it points at has nothing in it."""

    values = {
        "CHATGPT_OAUTH_ACCESS_TOKEN": {
            "value": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
        }
    }

    problems = credential_problems(
        [_remote("chatgpt_oauth")], values, lambda _: None, now=NOW
    )

    assert problems == {
        "chatgpt_oauth": {
            "state": NO_CREDENTIALS,
            "action": "sign_in",
            "display_name": "CHATGPT_OAUTH",
        }
    }


def test_a_stored_chatgpt_account_or_a_raw_token_can_serve(
    monkeypatch, empty_stores
) -> None:
    reference = {
        "CHATGPT_OAUTH_ACCESS_TOKEN": {
            "value": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
        }
    }
    raw = {"CHATGPT_OAUTH_ACCESS_TOKEN": {"value": "a-pasted-token"}}

    assert (
        credential_problems([_remote("chatgpt_oauth")], raw, lambda _: None, now=NOW)
        == {}
    )

    monkeypatch.setattr(route_status, "load_chatgpt_accounts", lambda **_: [object()])
    assert (
        credential_problems(
            [_remote("chatgpt_oauth")], reference, lambda _: None, now=NOW
        )
        == {}
    )


def test_the_account_listings_are_read_without_migrating(monkeypatch) -> None:
    """Opening a page must never rewrite a credential store."""

    seen: list[dict] = []

    def record(**kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(route_status, "load_chatgpt_accounts", record)
    monkeypatch.setattr(route_status, "load_accounts", record)
    monkeypatch.setattr(route_status, "load_claude_code_tokens", lambda: None)

    credential_problems(
        [_remote("chatgpt_oauth"), _remote("anthropic_oauth")],
        {},
        lambda _: None,
        now=NOW,
    )

    assert seen == [{"migrate": False}, {"migrate": False}]


def test_claude_subscription_follows_credential_viability(
    monkeypatch, empty_stores
) -> None:
    values = {
        "ANTHROPIC_OAUTH_ACCESS_TOKEN": {
            "value": ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE
        }
    }
    provider = [_remote("anthropic_oauth", status="missing_key")]

    assert (
        credential_problems(provider, values, lambda _: None, now=NOW)[
            "anthropic_oauth"
        ]["action"]
        == "sign_in"
    )

    # Claude Code's own file counts, exactly as the executor's loader counts it.
    monkeypatch.setattr(route_status, "load_claude_code_tokens", lambda: "tokens")
    monkeypatch.setattr(
        route_status, "credential_viability", lambda tokens: (tokens == "tokens", "")
    )
    assert credential_problems(provider, values, lambda _: None, now=NOW) == {}


def test_an_unreadable_store_is_a_store_with_nothing_usable(monkeypatch) -> None:
    def boom(**_):
        raise OSError("unreadable")

    monkeypatch.setattr(route_status, "load_chatgpt_accounts", boom)

    problems = credential_problems(
        [_remote("chatgpt_oauth")], {}, lambda _: None, now=NOW
    )

    assert problems["chatgpt_oauth"]["state"] == NO_CREDENTIALS


def test_a_key_provider_with_no_key_asks_for_one() -> None:
    problems = credential_problems(
        [_remote("nvidia_nim", status="missing_key")], {}, lambda _: None, now=NOW
    )

    assert problems["nvidia_nim"]["action"] == "add_key"


def _pool(*states: str, wait: float) -> dict:
    return {"slots": [{"state": state} for state in states], "wait": wait}


def test_every_key_benched_reports_when_the_first_one_returns() -> None:
    """``until`` is the runtime's own shortest wait, not a re-derivation."""

    health = {"nvidia_nim": _pool("COOLDOWN", "LOCKED_OUT", "CIRCUIT_OPEN", wait=120.0)}

    problems = credential_problems([_remote("nvidia_nim")], {}, health.get, now=NOW)

    assert problems["nvidia_nim"] == {
        "state": ALL_BENCHED,
        "until": NOW + 120,
        "keys": 3,
        "display_name": "NVIDIA_NIM",
    }


@pytest.mark.parametrize(
    ("states", "wait"),
    [
        ((), 0.0),
        # A healthy key throttled by the client-side limiter is not a bench.
        (("HEALTHY", "COOLDOWN"), 4.0),
        # The engine can hand a key out now (a forced-single pool, or a bench
        # that has just run out): the entry is still tried.
        (("LOCKED_OUT", "LOCKED_OUT"), 0.0),
        (("COOLDOWN",), None),
    ],
)
def test_a_pool_the_runtime_can_still_serve_from_is_not_benched(states, wait) -> None:
    assert benched_until([{"state": state} for state in states], wait, NOW) is None


def _rotating(policy: str) -> tuple[RotatingProvider, CredentialRotationState]:
    state = CredentialRotationState(
        2, policy, rate_limit_seconds=60.0, lockout_tiers=(300.0, 3600.0, 86400.0)
    )
    provider = RotatingProvider(
        ProviderConfig(
            api_key="sk-one",
            base_url="http://127.0.0.1:9",
            api_keys=("sk-one", "sk-two"),
            credential_rotation=policy,
        ),
        [_leaf(), _leaf()],
        state,
    )
    return provider, state


def _leaf() -> MagicMock:
    leaf = MagicMock(spec=BaseProvider)
    leaf.throttle_remaining.return_value = 0.0  # no client-side limiter wait
    return leaf


class _AuthError(Exception):
    status_code = 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "locked", "benched"),
    [
        ("round_robin", (0, 1), True),
        # The default policy serves key 0 whatever its health, so the entry is
        # still tried: seen on the scratch server as the same locked-out key
        # sent four requests in a row. Not "benched", then.
        ("single", (0, 1), False),
        # One key still serves: the entry is tried, so there is nothing to say.
        ("round_robin", (0,), False),
    ],
)
async def test_the_hint_follows_the_real_rotation_engine(
    policy, locked, benched
) -> None:
    """End to end through a real pool: 401s lock keys out, and the hint agrees
    with the provider's own ``throttle_remaining`` about whether it can serve."""

    provider, state = _rotating(policy)
    for index in locked:
        await state.report_failure(index, _AuthError())
    pool = {"slots": provider.key_health(), "wait": provider.throttle_remaining()}

    problems = credential_problems(
        [_remote("nvidia_nim")], {}, {"nvidia_nim": pool}.get
    )

    assert ("nvidia_nim" in problems) is benched
    if benched:
        assert problems["nvidia_nim"]["keys"] == 2
        assert problems["nvidia_nim"]["until"] > time.time() + 290


def test_healthy_and_local_providers_are_absent() -> None:
    status = [
        _remote("open_router"),
        {"provider_id": "ollama", "kind": "local", "status": "missing_url"},
    ]
    health = {"open_router": _pool("HEALTHY", wait=0.0)}

    assert credential_problems(status, {}, health.get, now=NOW) == {}


def test_config_changed_at_is_the_settings_file_mtime(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("MODEL=p/m\n", encoding="utf-8")
    os.utime(env, (NOW, NOW))

    assert config_changed_at(str(env)) == NOW
    assert config_changed_at(str(tmp_path / "missing")) is None
    assert config_changed_at(None) is None


def test_the_config_payload_carries_route_status(
    monkeypatch, tmp_path, empty_stores
) -> None:
    """Joined onto the payload the page already loads -- no request of its own
    -- with the live bench state of a provider the server has already built."""

    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    monkeypatch.delenv("CHATGPT_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_ACCESS_TOKEN", raising=False)
    benched = MagicMock()
    benched.key_health.return_value = [{"state": "COOLDOWN"}]
    benched.throttle_remaining.return_value = 900.0
    app = create_test_app(providers={"nvidia_nim": benched})
    client = _local_client(app)
    assert (
        client.post(
            "/admin/api/config/apply",
            json={"values": {"NVIDIA_NIM_API_KEY": "nvapi-one-key"}},
        ).status_code
        == 200
    )

    payload = client.get("/admin/api/config").json()

    status = payload["route_status"]
    assert status["providers"]["chatgpt_oauth"]["state"] == NO_CREDENTIALS
    assert status["providers"]["nvidia_nim"]["state"] == ALL_BENCHED
    assert status["providers"]["nvidia_nim"]["keys"] == 1
    assert isinstance(status["config_changed_at"], float)
    # Every key the payload had before is still there, unchanged in shape.
    assert {"fields", "sections", "provider_status", "paths"} <= set(payload)


def test_a_failing_pool_reader_still_serves_the_payload(
    monkeypatch, tmp_path, empty_stores
) -> None:
    _set_home(monkeypatch, tmp_path)
    _clear_process_config(monkeypatch)
    broken = MagicMock(spec=BaseProvider)
    broken.key_health = MagicMock(side_effect=RuntimeError("boom"))
    app = create_test_app(providers={"nvidia_nim": broken})

    response = _local_client(app).get("/admin/api/config")

    assert response.status_code == 200
    assert "route_status" in response.json()


def test_opencode_zen_without_a_key_is_not_flagged() -> None:
    """Zen's zero-cost models answer on the shared public credential."""

    status = [
        _remote("opencode", status="missing_key"),
        _remote("opencode_go", status="missing_key"),
    ]

    assert list(credential_problems(status, {}, lambda _: None, now=NOW)) == [
        "opencode_go"
    ]
