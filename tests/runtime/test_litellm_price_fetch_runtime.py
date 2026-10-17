"""LiteLLM's price table is fetched while its source is on, and never otherwise (7.97.0).

Until 7.97.0 ``COST_SOURCE_LITELLM_ENABLED=true`` did nothing on a fresh
install: ``refresh_litellm_cache`` (one conditional GET, the integrity check,
the pinned commit as the floor) existed and nothing called it, so the source
read ``litellm-prices.json`` only if that file was already on disk. The table
is now fetched in the background after a catalogue sweep when it is due -- the
source on and no copy fresher than ``LITELLM_CACHE_TTL_SECONDS`` on disk --
one download at a time, and turning the source on fetches at once. Off, which
is what ships, nothing is fetched.

The download itself is proven against a real HTTP server on this machine
serving a generated table; no test reaches the real URL.
"""

import asyncio
import functools
import http.server
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime import litellm_prices
from my_claude_code.providers.runtime.litellm_prices import (
    LITELLM_MIN_ENTRIES,
    fetch_litellm_prices,
    litellm_cache_path,
    litellm_rate_card,
    litellm_refresh_is_due,
    read_litellm_cache,
    refresh_litellm_cache,
    write_litellm_cache,
)
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

_PRICED = {
    "litellm_provider": "anthropic",
    "input_cost_per_token": 3e-06,
    "output_cost_per_token": 1.5e-05,
}


def _table(entries: int = LITELLM_MIN_ENTRIES + 50) -> dict[str, Any]:
    """A table that passes LiteLLM's own integrity floor, one real-shaped row."""

    table: dict[str, Any] = {
        f"filler-model-{index}": {
            "litellm_provider": "filler",
            "input_cost_per_token": 1e-06,
        }
        for index in range(entries - 1)
    }
    table["claude-sonnet-4-5"] = _PRICED
    return table


def _settings(**update: Any) -> Settings:
    return Settings.model_validate({"OPENCODE_FREE_TIER_CREDENTIAL": "key", **update})


# --------------------------------------------------------------------------
# When a fetch is due
# --------------------------------------------------------------------------


def test_nothing_is_due_while_the_source_is_off(tmp_path: Path) -> None:
    absent = tmp_path / "litellm-prices.json"
    assert litellm_refresh_is_due(_settings(), absent) is False
    assert (
        litellm_refresh_is_due(_settings(COST_SOURCE_LITELLM_ENABLED="false"), absent)
        is False
    )


def test_due_when_on_and_absent_or_past_its_freshness(tmp_path: Path) -> None:
    on = _settings(COST_SOURCE_LITELLM_ENABLED="true")
    path = tmp_path / "litellm-prices.json"
    assert litellm_refresh_is_due(on, path) is True
    write_litellm_cache(_table(), path)
    assert litellm_refresh_is_due(on, path) is False
    # read_litellm_cache's own freshness rule, from the same mtime.
    cached = read_litellm_cache(path)
    assert cached is not None and cached.fresh is True
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    assert litellm_refresh_is_due(on, path) is True
    cached = read_litellm_cache(path)
    assert cached is not None and cached.fresh is False
    # 0 revalidates every pass, as the setting's help says.
    write_litellm_cache(_table(), path)
    zero = _settings(COST_SOURCE_LITELLM_ENABLED="true", LITELLM_CACHE_TTL_SECONDS=0)
    assert litellm_refresh_is_due(zero, path) is True


# --------------------------------------------------------------------------
# The runtime schedules it
# --------------------------------------------------------------------------


def _recording_refresh(monkeypatch: pytest.MonkeyPatch, result: bool = True):
    calls: list[Path | None] = []

    async def refresh(path: Path | None = None) -> bool:
        calls.append(path)
        return result

    monkeypatch.setattr(
        "my_claude_code.runtime.provider_manager.refresh_litellm_cache", refresh
    )
    return calls


@pytest.mark.asyncio
async def test_a_sweep_fetches_the_table_when_the_source_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _recording_refresh(monkeypatch)
    manager = ProviderRuntimeManager(_settings(COST_SOURCE_LITELLM_ENABLED="true"))
    published: list[str] = []
    monkeypatch.setattr(
        manager, "_publish_model_catalog", lambda: published.append("p")
    )
    try:
        await manager.refresh_model_list_cache()
        swept = len(published)
        task = manager._litellm_task
        assert task is not None
        await task
        assert calls == [litellm_cache_path()]
        # A refreshed table republishes the kind lists that read its words.
        assert len(published) == swept + 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_no_fetch_when_off_fresh_or_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _recording_refresh(monkeypatch)
    off = ProviderRuntimeManager(_settings())
    try:
        await off.refresh_model_list_cache()
        await asyncio.sleep(0)
        assert off._litellm_task is None
        assert calls == []
    finally:
        await off.close()

    write_litellm_cache(_table(), litellm_cache_path())
    fresh = ProviderRuntimeManager(_settings(COST_SOURCE_LITELLM_ENABLED="true"))
    try:
        await fresh.refresh_model_list_cache()
        await asyncio.sleep(0)
        assert calls == []
        # Past its freshness it is due again -- once at a time.
        old = time.time() - 2 * 86400
        os.utime(litellm_cache_path(), (old, old))
        gate = asyncio.Event()

        async def slow(path: Path | None = None) -> bool:
            calls.append(path)
            await gate.wait()
            return False

        monkeypatch.setattr(
            "my_claude_code.runtime.provider_manager.refresh_litellm_cache", slow
        )
        fresh.schedule_litellm_refresh()
        fresh.schedule_litellm_refresh()
        await asyncio.sleep(0)
        assert len(calls) == 1
        gate.set()
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_a_close_cancels_a_fetch_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def hang(path: Path | None = None) -> bool:
        started.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(
        "my_claude_code.runtime.provider_manager.refresh_litellm_cache", hang
    )
    manager = ProviderRuntimeManager(_settings(COST_SOURCE_LITELLM_ENABLED="true"))
    manager.schedule_litellm_refresh()
    task = manager._litellm_task
    assert task is not None
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(manager.close(), 10)
    assert task.cancelled()
    # Closed: nothing new is scheduled.
    manager.schedule_litellm_refresh()
    assert manager._litellm_task is None


@pytest.mark.asyncio
async def test_switching_the_source_on_fetches_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _settings()
    runtime = ApplicationRuntime(ProviderRuntimeManager(previous), transcriber=None)
    scheduled: list[Settings | None] = []
    monkeypatch.setattr(
        runtime.provider_manager,
        "schedule_litellm_refresh",
        lambda settings=None: scheduled.append(settings),
    )
    runtime._started = True
    try:
        current = _settings(COST_SOURCE_LITELLM_ENABLED="true")
        await runtime._apply_live_settings(previous, current)
        assert scheduled == [current]
        await runtime._apply_live_settings(current, current)
        assert len(scheduled) == 1
    finally:
        runtime._started = False
        await runtime.provider_manager.close()


def test_pricing_parses_the_table_once_per_file_not_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Now that a release downloads the 2.3 MB table, a priced request must not
    parse it again: the rate and media cards read the copy the kind rung keeps."""

    path = litellm_cache_path()
    write_litellm_cache(_table(), path)
    parses: list[Path | None] = []
    real = litellm_prices.read_litellm_cache

    def counting(cache_path: Path | None = None) -> Any:
        parses.append(cache_path)
        return real(cache_path)

    monkeypatch.setattr(litellm_prices, "read_litellm_cache", counting)
    for _ in range(5):
        assert litellm_rate_card("anthropic", "claude-sonnet-4-5") is not None
        litellm_prices.litellm_media_card("anthropic", "claude-sonnet-4-5")
    assert parses == [path]
    # A new file is a new parse.
    write_litellm_cache({**_table(), "extra-model": _PRICED}, path)
    assert litellm_rate_card("anthropic", "claude-sonnet-4-5") is not None
    assert len(parses) == 2


# --------------------------------------------------------------------------
# The download, against a real server on this machine
# --------------------------------------------------------------------------


class _TableHandler(http.server.BaseHTTPRequestHandler):
    body: bytes = b"{}"
    etag: str = '"t1"'
    hits: list[str]

    def do_GET(self) -> None:
        self.hits.append(self.headers.get("If-None-Match") or "")
        if self.headers.get("If-None-Match") == self.etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", self.etag)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def table_server() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []
    handler = type(
        "Handler",
        (_TableHandler,),
        {"body": json.dumps(_table()).encode("utf-8"), "hits": hits},
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host!s}:{port}/model_prices_and_context_window.json", hits
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_the_table_is_downloaded_stored_and_prices_a_request(
    monkeypatch: pytest.MonkeyPatch,
    table_server: tuple[str, list[str]],
) -> None:
    url, hits = table_server
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setattr(
        litellm_prices,
        "fetch_litellm_prices",
        functools.partial(fetch_litellm_prices, url=url),
    )
    path = litellm_cache_path()
    assert litellm_rate_card("anthropic", "claude-sonnet-4-5") is None

    assert await refresh_litellm_cache(path) is True
    stored = read_litellm_cache(path)
    assert stored is not None
    assert stored.etag == '"t1"'
    assert stored.source_url == url
    assert len(stored.index) == LITELLM_MIN_ENTRIES + 50
    card = litellm_rate_card("anthropic", "claude-sonnet-4-5")
    assert card is not None
    assert card.input_price == pytest.approx(3e-06)

    # Revalidation replays the ETag and a 304 costs no body.
    assert await refresh_litellm_cache(path) is True
    assert hits == ["", '"t1"']
