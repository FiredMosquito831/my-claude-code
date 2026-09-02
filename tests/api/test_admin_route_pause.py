"""The immediate write behind the Pause button on Model Config.

Pausing is not a form field: there is no Apply, because a route the operator
has just switched off has to stop being tried now rather than after a save
they might not make. That makes the write a read-derive-write on a list, which
is exactly the shape #223 fixed for visibility -- so it goes through the same
locked path, and these prove it does.
"""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from tests.api.support import create_test_app

PAUSE_ENDPOINT = "/admin/api/config/route-pause"


def _local_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _isolated_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in (
        "MODEL",
        "MODEL_OPUS",
        "MODEL_OPUS_FALLBACKS",
        "MODEL_OPUS_PAUSED",
        "OPEN_ROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def _app(**overrides: str):
    settings = Settings()
    settings.model = "open_router/routed"
    settings.model_opus = "open_router/opus-primary"
    settings.model_opus_fallbacks = "open_router/one,open_router/two"
    settings.open_router_api_key = "open-router-key"
    for name, value in overrides.items():
        setattr(settings, name, value)
    return create_test_app(settings)


def test_the_pause_route_refuses_a_non_loopback_client():
    """The admin surface is loopback-only and this route must not be a hole."""

    remote = TestClient(_app(), client=("203.0.113.10", 50000))
    response = remote.post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/one",
            "paused": True,
        },
    )

    assert response.status_code == 403


def test_pausing_a_model_writes_one_key_and_round_trips(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app())

    paused = client.post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/one",
            "paused": True,
        },
    )

    assert paused.status_code == 200
    assert paused.json()["paused_key"] == "MODEL_OPUS_PAUSED"
    assert paused.json()["paused_value"] == "open_router/one"

    env = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8")
    assert "MODEL_OPUS_PAUSED=open_router/one" in env

    resumed = client.post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/one",
            "paused": False,
        },
    )

    assert resumed.json()["paused_value"] == ""
    assert "MODEL_OPUS_PAUSED=open_router/one" not in (
        tmp_path / ".fcc" / ".env"
    ).read_text(encoding="utf-8")


def test_a_pause_write_touches_no_other_setting(monkeypatch, tmp_path):
    """The Q20 claim on the server side: an unsaved drag elsewhere survives."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app())

    client.post(
        "/admin/api/config/apply",
        json={"values": {"MODEL_OPUS_FALLBACKS": "open_router/one,open_router/two"}},
    )
    before = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8")

    client.post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/two",
            "paused": True,
        },
    )
    after = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8")

    added = set(after.splitlines()) - set(before.splitlines())
    assert added == {"MODEL_OPUS_PAUSED=open_router/two"}


def test_a_pause_write_happens_inside_one_config_lock(monkeypatch, tmp_path):
    """One gesture, one commit, one lock -- the same claim the bulk route makes."""

    _isolated_home(monkeypatch, tmp_path)
    app = _app()
    runtime = app.state.services.admin
    calls: list[int] = []
    original = runtime.apply_admin_config_with

    async def counted(build):
        calls.append(1)
        return await original(build)

    runtime.apply_admin_config_with = counted

    _local_client(app).post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/one",
            "paused": True,
        },
    )

    assert calls == [1]


def test_a_failed_pause_write_does_not_claim_it_was_honored(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    app = _app()

    async def refuse(build):
        return {"errors": ["nope"]}

    app.state.services.admin.apply_admin_config_with = refuse
    body = (
        _local_client(app)
        .post(
            PAUSE_ENDPOINT,
            json={
                "model_key": "MODEL_OPUS",
                "model_ref": "open_router/one",
                "paused": True,
            },
        )
        .json()
    )

    assert body["errors"] == ["nope"]
    assert "paused" not in body
    assert "paused_value" not in body


def test_a_pause_on_something_that_is_not_a_route_is_refused(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)

    response = _local_client(_app()).post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS_FALLBACKS",
            "model_ref": "open_router/one",
            "paused": True,
        },
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_two_overlapping_pause_writes_both_survive(monkeypatch, tmp_path):
    """The #223 race, driven rather than asserted structurally.

    Two callers that each read the pause list and then wrote a full
    replacement derived from what they read would lose one of the two edits.
    Nothing in this suite drove two concurrent writers before; this does.
    """

    _isolated_home(monkeypatch, tmp_path)
    app = _app()
    runtime = app.state.services.admin
    # Start the app's runtime by hand: the write path is exercised directly
    # rather than through TestClient, which serialises requests for us and so
    # could never observe the race.
    from my_claude_code.api.admin_routes import PAUSE_KEY_FOR_ROUTE
    from my_claude_code.config.model_refs import (
        format_model_ref_list,
        parse_model_ref_list,
    )

    key = PAUSE_KEY_FOR_ROUTE["MODEL_OPUS"]

    def builder(model_ref: str):
        def build(settings):
            current = list(parse_model_ref_list(settings.model_opus_paused or ""))
            if model_ref not in current:
                current.append(model_ref)
            return {key: format_model_ref_list(tuple(current))}

        return build

    await asyncio.gather(
        runtime.apply_admin_config_with(builder("open_router/one")),
        runtime.apply_admin_config_with(builder("open_router/two")),
    )

    env = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8")
    line = next(row for row in env.splitlines() if row.startswith(f"{key}="))
    assert "open_router/one" in line
    assert "open_router/two" in line


def test_a_paused_ref_is_pruned_when_it_leaves_the_chain(monkeypatch, tmp_path):
    """A pause list that outlives the ref it names grows stale forever."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app())

    client.post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/two",
            "paused": True,
        },
    )
    assert "MODEL_OPUS_PAUSED=open_router/two" in (
        tmp_path / ".fcc" / ".env"
    ).read_text(encoding="utf-8")

    client.post(
        "/admin/api/config/apply",
        json={"values": {"MODEL_OPUS_FALLBACKS": "open_router/one"}},
    )

    # The rendered file carries a commented placeholder for every field, so
    # look for a live line rather than for the key anywhere in the text.
    lines = (tmp_path / ".fcc" / ".env").read_text(encoding="utf-8").splitlines()
    assert not [row for row in lines if row.startswith("MODEL_OPUS_PAUSED=")]


def test_the_next_plan_after_a_pause_apply_honours_it(monkeypatch, tmp_path):
    """The whole point of the immediate write, asserted end to end.

    The POST is not allowed to return until the paused set the router reads is
    the new one: ``replace`` publishes the generation synchronously before it
    schedules anything, so a plan resolved after the response has seen it.
    This is also the guard on the 6.35.1 speed fix -- suppressing the provider
    sweep must not suppress the generation swap that makes a pause visible.
    """

    _isolated_home(monkeypatch, tmp_path)
    app = _app()
    client = _local_client(app)
    manager = app.state.services.admin.provider_manager
    request = MessagesRequest.model_validate(
        {"model": "claude-opus-4", "messages": [{"role": "user", "content": "hi"}]}
    )

    # The route has to live in the managed env file, not just in the Settings
    # object this app was built with: every apply rebuilds Settings from disk,
    # so an in-memory-only chain would vanish on the first write.
    applied = client.post(
        "/admin/api/config/apply",
        json={
            "values": {
                "OPENROUTER_API_KEY": "open-router-key",
                "MODEL_OPUS": "open_router/opus-primary",
                "MODEL_OPUS_FALLBACKS": "open_router/one,open_router/two",
            }
        },
    )
    assert applied.json()["errors"] == []

    before = ModelRouter(manager.current_settings()).resolve_messages_plan(request)
    assert before.model_refs() == (
        "open_router/opus-primary",
        "open_router/one",
        "open_router/two",
    )
    assert before.paused_refs == frozenset()

    response = client.post(
        PAUSE_ENDPOINT,
        json={
            "model_key": "MODEL_OPUS",
            "model_ref": "open_router/one",
            "paused": True,
        },
    )

    assert response.status_code == 200
    plan = ModelRouter(manager.current_settings()).resolve_messages_plan(request)

    # The ref keeps its place in the chain -- that is what makes it reportable.
    assert "open_router/one" in plan.model_refs()
    assert plan.paused_refs == frozenset({"open_router/one"})
    # And it is not offered to the executor.
    refs = plan.model_refs()
    usable = RouteHealthRegistry().usable_indexes(refs, paused=plan.paused_refs)
    assert refs.index("open_router/one") not in usable
