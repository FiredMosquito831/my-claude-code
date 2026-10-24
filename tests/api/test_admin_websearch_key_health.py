"""The Web Search key list's health projection and per-key Reset (7.113.0).

The page reads one shape and one state spelling -- the provider pool's
snapshot -- from whichever pool serves today's route:

* no chain (or a chain without the provider): the provider's own ``KeyPool``,
  exactly the snapshot the endpoint has always returned;
* a chain listing the provider: the chain's books (``SearchKeyBooks``, the
  model path's ``CredentialRotationState``), projected into that shape.

Reset restores one key in that same pool: the books through
``CredentialRotationState.reset_key`` (called, never edited), the default
pool through the same engine restore.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.web_tools import search_key_health
from my_claude_code.api.web_tools.search_books import (
    cached_search_key_books,
    search_key_books,
)
from my_claude_code.api.web_tools.search_key_health import (
    STATE_SPELLING,
    books_health,
    chain_entries,
)
from my_claude_code.api.web_tools.search_providers import (
    cached_key_pool_snapshot,
    reset_runtime_providers,
    runtime_provider,
)
from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.config.settings import Settings
from my_claude_code.core.credential_rotation import PoolHealthState
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers.credential_rotation import (
    QUOTA_BENCH_REASON,
    CredentialRotationState,
)
from my_claude_code.websearch.registry import build_provider
from tests.api.support import create_test_app
from tests.support.websearch_credentials import forget_web_search_credentials

KEYS = ("exa-fake-aaaa1111bbbb", "exa-fake-cccc2222dddd")
ROUTE = "/admin/api/websearch/credentials/EXA_API_KEY/keys"


@pytest.fixture(autouse=True)
def _forget_cached_providers():
    asyncio.run(reset_runtime_providers())
    yield
    asyncio.run(reset_runtime_providers())


def _home(monkeypatch, tmp_path: Path, env: str) -> TestClient:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    forget_web_search_credentials(monkeypatch)
    for key in ("WEB_SEARCH_PROVIDER", "WEB_SEARCH_CHAIN", "WEB_SEARCH_CHAIN_PAUSED"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".mcc" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(env, "utf-8")
    # The running settings are the same lines: the key list is read from the
    # file, the route (the chain) from the settings the server runs with.
    values = dict(line.split("=", 1) for line in env.splitlines() if "=" in line)
    settings = Settings.model_validate(values)
    return TestClient(create_test_app(settings), client=("127.0.0.1", 50000))


def _settings(**env: str) -> Settings:
    return Settings.model_validate({"EXA_API_KEY": ",".join(KEYS), **env})


def _auth_failure() -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.AUTHENTICATION,
        status_code=401,
        message="exa rejected the key",
        retryable=False,
    )


def _rate_limit(wait: float) -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="slow down",
        retryable=True,
        retry_after_seconds=wait,
    )


def _frozen(health: dict[str, Any]) -> dict[str, Any]:
    """A health shape without its countdown, which moves between two reads."""

    return {
        **health,
        "keys": [
            {k: v for k, v in key.items() if k != "state_remaining_seconds"}
            for key in health["keys"]
        ],
    }


def _books(**env: str):
    settings = _settings(**env)
    provider = build_provider(settings, "exa")
    return asyncio.run(search_key_books(settings, provider))


def test_the_spelling_covers_every_pool_state_once() -> None:
    assert set(STATE_SPELLING) == {state.name for state in PoolHealthState}
    assert sorted(STATE_SPELLING.values()) == sorted(
        state.value for state in PoolHealthState
    )
    assert all(STATE_SPELLING[state.name] == state.value for state in PoolHealthState)


def test_chain_entries_read_the_chain_with_its_repeats() -> None:
    assert chain_entries(_settings()) == ()
    assert chain_entries(_settings(WEB_SEARCH_CHAIN="exa,tavily,exa")) == (
        "exa",
        "tavily",
        "exa",
    )


def test_with_no_chain_the_list_is_the_pool_snapshot_it_always_was(
    monkeypatch, tmp_path
) -> None:
    client = _home(monkeypatch, tmp_path, f"EXA_API_KEY={','.join(KEYS)}\n")
    unused = client.get(ROUTE).json()
    assert unused["health"] is None
    assert unused["key_health"] == {"pool": "default", "keys": None}

    provider = asyncio.run(runtime_provider(_settings(), "exa"))
    provider.key_pool.report_rate_limit(0, retry_after_seconds=60.0)
    provider.key_pool.report_failure(1, kind="quota", message="no credits")
    body = client.get(ROUTE).json()
    # The very snapshot the endpoint returned before 7.113.0 (only the
    # countdown moves between two reads).
    snapshot = cached_key_pool_snapshot("exa")
    assert snapshot is not None
    assert _frozen(body["health"]) == _frozen(snapshot)
    assert [key["state"] for key in body["health"]["keys"]] == [
        "cooldown",
        "locked_out",
    ]
    facts = body["key_health"]["keys"]
    assert body["key_health"]["pool"] == "default"
    assert [fact["reason"] for fact in facts] == ["rate_limited", "out_of_credits"]
    assert facts[1]["lockout_tier"] == 1
    assert facts[1]["lockout_tiers"] == 5
    assert 59 < facts[0]["remaining_s"] <= 60


def test_with_a_chain_the_list_projects_the_books_into_the_same_shape(
    monkeypatch, tmp_path
) -> None:
    client = _home(
        monkeypatch,
        tmp_path,
        f"EXA_API_KEY={','.join(KEYS)}\nWEB_SEARCH_CHAIN=exa,tavily\n",
    )
    unused = client.get(ROUTE).json()
    assert unused["health"] is None
    assert unused["key_health"] == {"pool": "chain", "keys": None}

    books = _books(WEB_SEARCH_CHAIN="exa,tavily")
    asyncio.run(books.state.report_failure(0, _auth_failure()))
    asyncio.run(books.state.report_failure(1, _rate_limit(30.0)))
    body = client.get(ROUTE).json()
    health = body["health"]
    assert _frozen(health) == _frozen(books_health(books)[0])
    # One spelling: the model metrics' LOCKED_OUT / COOLDOWN, as the page reads.
    assert [key["state"] for key in health["keys"]] == ["locked_out", "cooldown"]
    assert {key["state"] for key in books.state.get_metrics()} == {
        "LOCKED_OUT",
        "COOLDOWN",
    }
    assert set(health["keys"][0]) == {
        "index",
        "key_label",
        "state",
        "state_remaining_seconds",
        "requests",
        "successes",
        "failures",
        "consecutive_failures",
        "rate_limits",
        "lockouts",
        "last_error",
    }
    # Not measured by the model metrics: None, never a zero.
    assert health["keys"][0]["successes"] is None
    assert health["keys"][0]["consecutive_failures"] is None
    facts = body["key_health"]
    assert facts["pool"] == "chain"
    assert [fact["reason"] for fact in facts["keys"]] == ["rejected", "rate_limited"]
    tiers = len(books.policy.lockout_tiers)
    assert facts["keys"][0]["lockout_tier"] == 1
    assert facts["keys"][0]["lockout_tiers"] == tiers
    assert "exa-fake-aaaa1111bbbb" not in client.get(ROUTE).text


def test_a_credits_bench_on_the_books_reads_out_of_credits() -> None:
    books = _books(WEB_SEARCH_CHAIN="exa")
    asyncio.run(
        books.state.report_failure(
            0,
            ExecutionFailure(
                kind=FailureKind.QUOTA,
                status_code=402,
                message="insufficient credits",
                retryable=False,
                retry_after_seconds=60.0,
            ),
        )
    )
    metrics = books.state.get_metrics()
    assert metrics[0]["state"] == "COOLDOWN"
    assert metrics[0]["cooldown_reason"] == QUOTA_BENCH_REASON
    _, facts = books_health(books)
    assert facts[0]["reason"] == "out_of_credits"


def test_books_built_for_other_keys_are_not_shown(monkeypatch, tmp_path) -> None:
    client = _home(
        monkeypatch,
        tmp_path,
        "EXA_API_KEY=exa-fake-eeee3333ffff\nWEB_SEARCH_CHAIN=exa\n",
    )
    _books(WEB_SEARCH_CHAIN="exa")
    assert cached_search_key_books("exa") is not None
    body = client.get(ROUTE).json()
    assert body["health"] is None
    assert body["key_health"] == {"pool": "chain", "keys": None}


def test_a_provider_outside_the_chain_shows_its_own_pool(monkeypatch, tmp_path) -> None:
    client = _home(
        monkeypatch,
        tmp_path,
        f"EXA_API_KEY={','.join(KEYS)}\nWEB_SEARCH_CHAIN=tavily,ddgs\n",
    )
    assert client.get(ROUTE).json()["key_health"]["pool"] == "default"


def test_reset_on_the_chain_calls_the_model_pools_reset_key(
    monkeypatch, tmp_path
) -> None:
    client = _home(
        monkeypatch,
        tmp_path,
        f"EXA_API_KEY={','.join(KEYS)}\nWEB_SEARCH_CHAIN=exa\n",
    )
    books = _books(WEB_SEARCH_CHAIN="exa")
    asyncio.run(books.state.report_failure(0, _auth_failure()))
    calls: list[int] = []
    original = CredentialRotationState.reset_key

    async def spy(self: CredentialRotationState, index: int) -> bool:
        calls.append(index)
        return await original(self, index)

    monkeypatch.setattr(CredentialRotationState, "reset_key", spy)
    response = client.post(
        f"{ROUTE}/0/reset?id={credential_fingerprint(KEYS[0])}",
        headers={"Origin": "http://127.0.0.1:8082"},
    )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert calls == [0]
    assert body["pool"] == "chain"
    assert body["reset"] is True
    assert books.state.get_metrics()[0]["state"] == "HEALTHY"
    assert body["health"]["keys"][0]["state"] == "healthy"
    assert body["key_health"]["keys"][0]["reason"] is None


def test_reset_without_a_chain_restores_the_providers_own_pool(
    monkeypatch, tmp_path
) -> None:
    client = _home(monkeypatch, tmp_path, f"EXA_API_KEY={','.join(KEYS)}\n")
    nothing = client.post(f"{ROUTE}/1/reset").json()
    assert nothing["reset"] is None
    provider = asyncio.run(runtime_provider(_settings(), "exa"))
    provider.key_pool.report_failure(1, kind="auth", message="rejected")
    body = client.post(f"{ROUTE}/1/reset").json()
    assert body["pool"] == "default"
    assert body["reset"] is True
    assert provider.key_pool.health_at(1).state is PoolHealthState.HEALTHY
    assert body["key_health"]["keys"][1]["reason"] is None


def test_reset_refuses_a_changed_list_and_a_missing_key(monkeypatch, tmp_path) -> None:
    client = _home(monkeypatch, tmp_path, f"EXA_API_KEY={','.join(KEYS)}\n")
    assert client.post(f"{ROUTE}/5/reset").status_code == 404
    assert client.post(f"{ROUTE}/0/reset?id=sha256:nope").status_code == 409
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    assert remote.post(f"{ROUTE}/0/reset").status_code == 403
    foreign = client.post(f"{ROUTE}/0/reset", headers={"Origin": "https://evil.test"})
    assert foreign.status_code == 403


def test_the_projection_never_reads_a_state_it_does_not_spell(monkeypatch) -> None:
    """Dropping a state from the mapping would fail loudly, not render it."""

    books = _books(WEB_SEARCH_CHAIN="exa")
    asyncio.run(books.state.report_failure(0, _auth_failure()))
    monkeypatch.setattr(
        search_key_health,
        "STATE_SPELLING",
        {k: v for k, v in STATE_SPELLING.items() if k != "LOCKED_OUT"},
    )
    with pytest.raises(KeyError):
        books_health(books)
