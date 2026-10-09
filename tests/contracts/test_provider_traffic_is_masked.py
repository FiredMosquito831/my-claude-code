"""A provider reached through a proxy chain never sees this computer's address.

The leak contract of ``specs/PR-EXIT-ROTATION-AND-REPEAT-MODELS-SPEC.md`` C.4,
and the grid every later masking fix fills in. One row per kind of traffic MCC
sends to a provider host; each row drives its traffic against the rig in
``tests/support/masking_harness.py`` -- a fake provider host that records the
peer of every connection, and two SOCKS5 proxies and an HTTP proxy that record
the onward socket and the requested name of every tunnel -- and asserts where
it came from, and that the proxy, not this computer, resolved the name.

Filled by PR-2 (7.78.7): the three the Providers card's buttons send -- the
capability probe, the client-identity probe that rides on it, and a custom
provider's reasoning-dialect probe (leak C-1). Four questions each:

* chain healthy, Direct fallback off: everything through the first exit;
* first exit unreachable: through the next one, never the dead one;
* every exit unhealthy, Direct fallback off: nothing sent, and the answer
  names the setting;
* every exit unhealthy, Direct fallback on: this computer's address, which is
  the one case the user allowed it (2026-10-06 23:03).

Filled by PR-3 (7.78.8): real traffic through the provider tree -- a request on
each of the three upstream doors (Chat Completions, Responses, Anthropic
Messages), streamed and not; a same-exit retry; a fallback to another model of
the same provider; the discovery sweep; the Test button; the credential health
probe; a media image whose answer MCC downloads. Their questions are below,
with the three fail-closed causes (every entry paused, every address removed,
a chain file that cannot be read at start-up -- leaks C-2 and C-5, which went
out from this computer until 7.78.8).

Filled by 7.79.2 (PR-4, PR-5, PR-6 in one change):

* PR-4 -- Direct fallback ON uses this computer's address only once EVERY
  proxy of the chain is unhealthy (the user's decision of 2026-10-06 23:03):
  a chain with nothing usable is refused with it on too; every exit
  unhealthy goes direct; one unhealthy exit among healthy ones never does;
  a loop that ends with a healthy exit left (its live-failure bound spent)
  withholds Direct and the request moves on; the switch bound never ends
  in Direct.
* PR-5 -- the token refresh rows: Claude and ChatGPT sign-ins refresh
  through the exit of the leg about to send (and the cards' Refresh buttons
  through the chain's exit), and a Vertex refresh through a ``socks5://``
  entry has the proxy, not this computer, resolve the token host.
* PR-6 -- the request log names the address that carried a request: a
  one-entry chain's entry, a static proxy, and Direct carried by the system
  proxy.

Every traffic class in the grid is now driven; none is a placeholder.

Filled by 7.84.0 (K-OR): OpenRouter's live model list, which MCC fetches for
every provider's metadata ladder, is traffic to a provider host too. It is
fetched through the ``open_router`` provider's chain with the one exit a
Providers-card probe would take, so it is asked the probe questions above.

Filled by 7.81.0 ("Keep trying exits until one answers", PR-7/8/9): a ticked
chain moves a request an exit refused (a free-usage 429, a country refusal) to
the next exit -- through proxies only, the same bytes on every exit, never
retried on the exit that refused; at its switch limit it never goes direct and
the next model answers; with every exit remembered spent it sends nothing when
Direct fallback is off and goes direct only when it is on (the 7.79.2 rule
sees the memory); media does the same.
"""

import asyncio
import base64
import contextlib
import dataclasses
import functools
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.credentials import Credentials as GoogleCredentials

from my_claude_code.application.model_metadata import ResponseSurface
from my_claude_code.application.ports import PooledCredentialPort
from my_claude_code.config.constants import CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE
from my_claude_code.config.credential_names import credential_fingerprint
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.proxy_exit_memory import (
    EXIT_MEMORY,
    MEDIA_EXIT_MEMORY,
    SPENT,
)
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health
from my_claude_code.core.request_log import store_from_settings
from my_claude_code.providers.anthropic_oauth import credentials as claude_creds
from my_claude_code.providers.chatgpt_oauth import credentials as chatgpt_creds
from my_claude_code.providers.media import proxy_pool as media_proxy_pool
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from my_claude_code.providers.openai_chat import response_surface
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_SESSION_HEADER,
)
from my_claude_code.providers.runtime import openrouter_catalogue
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.openrouter_catalogue import (
    OPENROUTER_PROVIDER_ID,
    fetch_openrouter_live,
    openrouter_live_cache_path,
    read_openrouter_live_rows,
    refresh_openrouter_live,
)
from my_claude_code.providers.runtime.proxy_rotating import ProxyRotatingProvider
from my_claude_code.providers.vertex.auth import GoogleAccessTokenProvider
from my_claude_code.runtime.application import ApplicationRuntime
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager
from tests.api.support import create_test_app, provider_manager_for_app
from tests.support.masking_harness import (
    MaskingRig,
    SeenRequest,
    closed_port,
    start_masking_rig,
)

#: The custom provider every custom-card row probes.
CUSTOM_NAME = "Mask Co"
#: The built-in provider whose profile declares a client identity -- the only
#: kind the identity probe runs for. Its base URL is a catalogue constant, so
#: the row points the descriptor at the rig rather than at the real host.
IDENTITY_PROVIDER = "opencode"
MODEL = "rig-model"
#: The body of both identity-probe requests (``identity_probe._probe_body``).
_PLAIN_BODY = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16,
    "stream": False,
}


#: The one row the rig serves as OpenRouter's live model list (7.84.0).
_LIVE_LIST = {
    "data": [
        {
            "id": "rig/model",
            "architecture": {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
            "supported_parameters": ["tools"],
        }
    ]
}


def _rig_answers(request: SeenRequest) -> tuple[int, bytes]:
    """What the fake host says, chosen so every probe learns something."""

    if request.method == "GET" and request.path.endswith("/models"):
        return 200, json.dumps(_LIVE_LIST).encode()
    body = json.loads(request.body or b"{}")
    content = (body.get("messages") or [{}])[0].get("content")
    if body.get("max_tokens") == 2_000_000_000:
        message = "max_tokens must be less than or equal to 8192"
    elif isinstance(content, list):
        message = "this model does not support image input"
    elif body.get("reasoning_effort") == "bogus_value":
        message = "reasoning_effort must be one of: low, medium, high"
    elif OPENCODE_SESSION_HEADER not in request.headers:
        message = f"missing {OPENCODE_SESSION_HEADER} header"
    else:
        return 200, b'{"id":"rig","object":"chat.completion","choices":[]}'
    return 400, json.dumps({"error": {"message": message}}).encode()


@dataclass
class ProbeWorld:
    """One runtime, one custom provider and the rig, wired together."""

    rig: MaskingRig
    runtime: ApplicationRuntime
    custom_id: str
    write_chains: Callable[[ProxyChains], None]

    def chain_everything(
        self, *, direct_fallback: bool, policy: str = "failover"
    ) -> None:
        """Give both probed providers the rig's three proxies, in order."""

        proxies = {
            f"px_{index}": ProxyEndpoint(url=url)
            for index, url in enumerate(self.rig.proxy_urls)
        }
        chain = ProxyChain(
            enabled=True,
            policy=policy,
            entries=tuple(ProxyChainEntry(proxy=key) for key in proxies),
            direct_fallback=direct_fallback,
        )
        self.write_chains(
            ProxyChains(
                proxies=proxies,
                chains={
                    self.custom_id: chain,
                    IDENTITY_PROVIDER: chain,
                    OPENROUTER_PROVIDER_ID: chain,
                },
            )
        )

    def mark_unreachable(self, *indexes: int) -> None:
        """Put entries on the reachability ladder, as a failed dial would."""

        for index in indexes:
            PROXY_REACHABILITY.note_failure(
                mask_proxy_label(self.rig.proxy_urls[index]), "rig: refused"
            )


@pytest.fixture
def world(tmp_path, monkeypatch) -> Iterator[ProbeWorld]:
    rig = start_masking_rig(_rig_answers)
    chains_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: chains_path
    )
    reset_proxy_chains_cache()
    reset_proxy_health()
    # The suite refuses this download everywhere else; the row below drives
    # the real one, against the rig.
    monkeypatch.setattr(
        openrouter_catalogue, "fetch_openrouter_live", fetch_openrouter_live
    )

    registry = get_provider_registry()
    entry = registry.add(
        display_name=CUSTOM_NAME,
        base_url=rig.host.base_url(),
        api_keys=("sk-rigmask-0000aaaa1111bbbb",),
    )
    descriptors = dict(registry.all_descriptors())
    descriptors[IDENTITY_PROVIDER] = dataclasses.replace(
        PROVIDER_CATALOG[IDENTITY_PROVIDER], default_base_url=rig.host.base_url()
    )
    monkeypatch.setattr(registry, "all_descriptors", lambda: descriptors)

    settings = Settings.model_validate(
        {"OPENCODE_API_KEY": "sk-rigzen-0000aaaa1111bbbb"}
    )
    runtime = ApplicationRuntime(ProviderRuntimeManager(settings), transcriber=None)
    monkeypatch.setattr(
        runtime,
        "cached_model_ids",
        lambda: {entry.provider_id: frozenset({MODEL})},
    )
    # The dialect probe republishes the generation so the next request spells
    # the new word; there is no generation worth building in this test.
    monkeypatch.setattr(runtime.provider_manager, "replace", AsyncMock(return_value=1))
    try:
        yield ProbeWorld(
            rig=rig,
            runtime=runtime,
            custom_id=entry.provider_id,
            write_chains=lambda chains: save_proxy_chains(chains, chains_path),
        )
    finally:
        rig.close()
        reset_proxy_chains_cache()
        reset_proxy_health()


# ------------------------------------------------------------------ drivers


async def _capability_probe(world: ProbeWorld) -> dict[str, Any]:
    return await world.runtime.probe_provider_capabilities(world.custom_id)


async def _identity_probe(world: ProbeWorld) -> dict[str, Any]:
    payload = await world.runtime.probe_provider_capabilities(
        IDENTITY_PROVIDER, (MODEL,)
    )
    if payload.get("status") != "not_sent":
        # The identity probe is the two plain requests, with and without the
        # session header; every capability probe before them adds a field.
        plain = [
            seen
            for seen in world.rig.host.requests
            if json.loads(seen.body) == _PLAIN_BODY
        ]
        assert len(plain) == 2, [seen.headers for seen in plain]
        assert [OPENCODE_SESSION_HEADER in seen.headers for seen in plain] == [
            True,
            False,
        ]
    return payload


async def _dialect_probe(world: ProbeWorld) -> dict[str, Any]:
    return await world.runtime.probe_custom_provider_dialect(world.custom_id)


async def _openrouter_live_list(world: ProbeWorld) -> dict[str, Any]:
    """The background fetch of OpenRouter's live model list (7.84.0)."""

    path = openrouter_live_cache_path()
    outcome = await refresh_openrouter_live(
        world.runtime.settings,
        path,
        url=f"{world.rig.host.base_url()}/models?output_modalities=all",
    )
    if outcome.status != "not_sent":
        assert outcome.status == "fetched", outcome
        stored = read_openrouter_live_rows(path)
        assert stored is not None and stored[0] == _LIVE_LIST["data"]
    else:
        assert read_openrouter_live_rows(path) is None
    return {
        "status": outcome.status,
        "detail": outcome.detail,
        "proxy_exit": outcome.proxy_exit,
    }


Driver = Callable[[ProbeWorld], Awaitable[dict[str, Any]]]


# ------------------------------------------------- real traffic (PR-3 rows)
#
# The rows a request, a retry, a fallback, a listing, a health probe and a
# media call drive. Each goes through the real provider tree the factory
# builds -- from the app's own ``/v1/messages`` and media routes where there is
# one, from the runtime's own call where there is not -- against custom
# providers pointed at the rig, every one behind the same three-exit chain.
# Each lives under its own path on the host, so the host's log says which
# provider a request was for.

CHAT_NAME = "Mask Chat"
RESPONSES_NAME = "Mask Responses"
MESSAGES_NAME = "Mask Messages"
MEDIA_NAME = "Mask Media"
PAIR_NAME = "Mask Pair"
#: No chain at all: where a refused request falls back to.
PLAIN_NAME = "Plain Co"
RIG_KEY = "sk-rigmask-0000aaaa1111bbbb"
SECOND_KEY = "sk-rigmask-2222cccc3333dddd"
#: A model the host says does not exist: the fallback row's first entry.
REJECTED_MODEL = "rig-rejected"
MEDIA_MODEL = "rig-image"
PICTURE = b"\x89PNG\r\n\x1a\n" + b"\x07" * 64
GEMINI_IMAGE_PATH = "/v1beta/models/mcc-image:generateContent"
#: Every refused answer names the setting and where it lives.
REFUSAL_WORDS = ("Direct fallback", "Proxying page")


def _sse(events: list[tuple[str | None, dict[str, Any] | str]]) -> bytes:
    lines: list[str] = []
    for event, data in events:
        if event is not None:
            lines.append(f"event: {event}")
        lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


_CHAT_STREAM = _sse(
    [
        (
            None,
            {
                "id": "chatcmpl-rig",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "masked"},
                        "finish_reason": None,
                    }
                ],
            },
        ),
        (
            None,
            {
                "id": "chatcmpl-rig",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        ),
        (None, "[DONE]"),
    ]
)
_RESPONSE = {
    "id": "resp_rig",
    "object": "response",
    "model": MODEL,
    "status": "completed",
    "output": [
        {
            "type": "message",
            "id": "msg_rig",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "masked", "annotations": []}],
        }
    ],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
}
_RESPONSES_STREAM = _sse(
    [
        (
            "response.created",
            {
                "type": "response.created",
                "response": _RESPONSE | {"status": "in_progress", "output": []},
            },
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": "msg_rig",
                "output_index": 0,
                "content_index": 0,
                "delta": "masked",
            },
        ),
        ("response.completed", {"type": "response.completed", "response": _RESPONSE}),
    ]
)
_MESSAGES_STREAM = _sse(
    [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_rig",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "masked"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
)


@dataclass
class Outcome:
    """What the client (or the card) was told."""

    ok: bool
    text: str


@dataclass
class TrafficWorld:
    """The rig, six custom providers and the chain file, wired together."""

    rig: MaskingRig
    chains_path: Path
    ids: dict[str, str]
    chat_failures_left: int = 0
    #: A proxy address nothing listens on, for a dead exit.
    dead_url: str = ""
    #: 7.81.0: what the host answers a chat or image request arriving through
    #: the rig's exit N -- the host reads which proxy carried it from the
    #: peer it sees, the way a provider metering per address does.
    exit_answers: dict[int, tuple[int, bytes]] = dataclasses.field(default_factory=dict)

    def exit_of(self, request: SeenRequest) -> int | None:
        """Which of the rig's proxies carried ``request``, if any did."""

        for index, proxy in enumerate(self.rig.proxies):
            if request.peer in proxy.outbound:
                return index
        return None

    @property
    def masked_ids(self) -> tuple[str, ...]:
        """Every provider the chain covers: all but :data:`PLAIN_NAME`'s."""

        return tuple(pid for name, pid in self.ids.items() if name != PLAIN_NAME)

    def answer(
        self, request: SeenRequest
    ) -> tuple[int, bytes] | tuple[int, bytes, str]:
        path = request.path
        if path.endswith("/models"):
            # The rejected model is listed: it exists in the catalogue and
            # fails only when asked, which is what makes a fallback walk to it.
            return 200, json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model"}
                        for model in (MODEL, REJECTED_MODEL)
                    ],
                }
            ).encode()
        if path.endswith("/files/kite.png"):
            return 200, PICTURE, "image/png"
        if self.exit_answers and (
            path.endswith("/images/generations") or path.endswith("/chat/completions")
        ):
            carried = self.exit_of(request)
            answer = None if carried is None else self.exit_answers.get(carried)
            if answer is not None:
                return answer
        if path.endswith("/images/generations"):
            url = f"{self.rig.host.base_url(path='')}/files/kite.png"
            return 200, json.dumps({"created": 1, "data": [{"url": url}]}).encode()
        body = json.loads(request.body or b"{}")
        if body.get("model") == REJECTED_MODEL:
            message = f"The model `{REJECTED_MODEL}` does not exist"
            return 404, json.dumps({"error": {"message": message}}).encode()
        if path.endswith("/chat/completions"):
            if self.chat_failures_left > 0:
                self.chat_failures_left -= 1
                return 503, b'{"error":{"message":"rig: try again"}}'
            return 200, _CHAT_STREAM, "text/event-stream"
        if path.endswith("/responses"):
            return 200, _RESPONSES_STREAM, "text/event-stream"
        if path.endswith("/messages"):
            return 200, _MESSAGES_STREAM, "text/event-stream"
        return 404, b'{"error":{"message":"rig: no such path"}}'

    # -- the chain file, in every state a row is asked about ---------------

    def write(
        self,
        *,
        direct_fallback: bool = False,
        paused: bool = False,
        removed: bool = False,
        dead_first: bool = False,
        on: tuple[str, ...] | None = None,
        max_switches: int | None = None,
        until_served: bool = False,
    ) -> None:
        """The rig's three exits, in order, on every masked provider.

        ``paused`` pauses every entry; ``removed`` leaves the addresses out of
        the catalogue, so the read drops every entry naming one -- the two
        ways a chain ends up with nothing usable in it. ``dead_first`` puts a
        proxy nobody listens on ahead of them (7.79.2).
        """

        proxies = {
            f"px_{index}": ProxyEndpoint(url=url)
            for index, url in enumerate(self.rig.proxy_urls)
        }
        if dead_first:
            proxies = {"px_dead": ProxyEndpoint(url=self.dead_url)} | proxies
        extra: dict[str, Any] = {}
        if on is not None:
            extra["on"] = on
        if max_switches is not None:
            extra["max_switches"] = max_switches
        chain = ProxyChain(
            enabled=True,
            policy="failover",
            entries=tuple(ProxyChainEntry(proxy=key, paused=paused) for key in proxies),
            direct_fallback=direct_fallback,
            # Always named: the box is on by default since 7.81.1, so a row
            # about the earlier rotation stores it switched off (``false``)
            # and a ticked row stores it the way the store does (no key).
            until_served=until_served,
            **extra,
        )
        save_proxy_chains(
            ProxyChains(
                proxies={} if removed else proxies,
                chains=dict.fromkeys(self.masked_ids, chain),
            ),
            self.chains_path,
        )
        reset_proxy_chains_cache()

    def corrupt(self, *, direct_fallback: bool = False) -> None:
        """A healthy chain saved, then the file broken -- and a fresh process."""

        self.write(direct_fallback=direct_fallback)
        self.chains_path.write_text("{ this is not json", encoding="utf-8")
        reset_proxy_chains_cache()

    def settings(self, **values: str) -> Settings:
        return Settings.model_validate(
            {
                # ``model`` has no env alias: the field's own name is its key.
                "model": f"{self.ids[CHAT_NAME]}/{MODEL}",
                "MODEL_IMAGE": f"{self.ids[MEDIA_NAME]}/{MEDIA_MODEL}",
                "MODEL_IMAGE_FALLBACKS": "",
                "MEDIA_FALLBACK_ON_UNDOWNLOADABLE": "true",
                "PROVIDER_RETRY_BACKOFF_BASE_SECONDS": "0",
                "PROVIDER_RETRY_BACKOFF_MAX_SECONDS": "0",
                "PROVIDER_RETRY_BACKOFF_JITTER_SECONDS": "0",
            }
            | values
        )

    @contextlib.contextmanager
    def client(self, **values: str) -> Iterator[TestClient]:
        app = create_test_app(self.settings(**values), media=MediaRegistry())
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            yield client

    def requests_for(self, name: str) -> list[SeenRequest]:
        """What the host saw for ``name``: each provider lives under its slug."""

        prefix = f"/{_slug(name)}/"
        return [seen for seen in self.rig.host.requests if seen.path.startswith(prefix)]


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")


@pytest.fixture
def traffic(tmp_path, monkeypatch) -> Iterator[TrafficWorld]:
    rig = start_masking_rig()
    chains_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: chains_path
    )
    reset_proxy_chains_cache()
    reset_proxy_health()
    registry = get_provider_registry()
    ids: dict[str, str] = {}
    shapes: tuple[
        tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...
    ] = (
        # name, keys, chat surfaces declared, media operations declared
        (CHAT_NAME, (RIG_KEY,), (), ()),
        (RESPONSES_NAME, (RIG_KEY,), ("responses",), ()),
        (MESSAGES_NAME, (RIG_KEY,), ("messages",), ()),
        (MEDIA_NAME, (RIG_KEY,), (), ("image_generate",)),
        (PAIR_NAME, (RIG_KEY, SECOND_KEY), (), ()),
        (PLAIN_NAME, (RIG_KEY,), (), ()),
    )
    for name, keys, surfaces, media in shapes:
        entry = registry.add(
            display_name=name,
            base_url=rig.host.base_url(path=f"/{_slug(name)}/v1"),
            api_keys=keys,
            surfaces=surfaces or None,
            media_operations=media or None,
        )
        ids[name] = entry.provider_id
    world = TrafficWorld(
        rig=rig,
        chains_path=chains_path,
        ids=ids,
        dead_url=f"http://127.0.0.1:{closed_port()}",
    )
    rig.host.responder = world.answer
    # Which door each model is served on, as an operator states it in
    # ``model_overrides.json``: with nothing stated a custom host is asked on
    # Chat Completions, and these two rows are about the other two doors.
    doors = {
        ids[RESPONSES_NAME]: ResponseSurface.RESPONSES,
        ids[MESSAGES_NAME]: ResponseSurface.MESSAGES,
    }
    monkeypatch.setattr(
        response_surface,
        "override_surface",
        lambda provider_id, _model_id: doors.get(provider_id),
    )
    try:
        yield world
    finally:
        rig.close()
        reset_proxy_chains_cache()
        reset_proxy_health()


def _app_of(client: TestClient) -> FastAPI:
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def _portal(client: TestClient) -> BlockingPortal:
    """The app's own event loop, so a runtime call shares its providers."""

    portal = client.portal
    assert portal is not None, "only inside `with TestClient(...)`"
    return portal


def _messages_body(model: str, *, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }


def _answered(response: Any) -> Outcome:
    text = response.text
    return Outcome(
        ok=response.status_code == 200 and "event: error" not in text,
        text=text,
    )


def _both_ways(world: TrafficWorld, name: str) -> Outcome:
    """One request on ``name``'s model, streamed and not, from the client."""

    outcomes: list[Outcome] = []
    with world.client() as client:
        for stream in (True, False):
            response = client.post(
                "/v1/messages",
                json=_messages_body(f"{world.ids[name]}/{MODEL}", stream=stream),
            )
            outcomes.append(_answered(response))
    if all(outcome.ok for outcome in outcomes):
        assert len(world.requests_for(name)) >= 2, world.rig.host.requests
    return Outcome(
        ok=all(outcome.ok for outcome in outcomes),
        text="\n".join(outcome.text for outcome in outcomes),
    )


def _chat_completions(world: TrafficWorld) -> Outcome:
    outcome = _both_ways(world, CHAT_NAME)
    if outcome.ok:
        assert {seen.path for seen in world.requests_for(CHAT_NAME)} == {
            f"/{_slug(CHAT_NAME)}/v1/chat/completions"
        }
    return outcome


def _responses(world: TrafficWorld) -> Outcome:
    outcome = _both_ways(world, RESPONSES_NAME)
    if outcome.ok:
        assert {seen.path for seen in world.requests_for(RESPONSES_NAME)} == {
            f"/{_slug(RESPONSES_NAME)}/v1/responses"
        }
    return outcome


def _anthropic_messages(world: TrafficWorld) -> Outcome:
    outcome = _both_ways(world, MESSAGES_NAME)
    if outcome.ok:
        assert {seen.path for seen in world.requests_for(MESSAGES_NAME)} == {
            f"/{_slug(MESSAGES_NAME)}/v1/messages"
        }
    return outcome


def _same_exit_retry(world: TrafficWorld) -> Outcome:
    """A 503 and then a 200: the leg's own retry, on the exit it already has."""

    world.chat_failures_left = 1
    with world.client() as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{world.ids[CHAT_NAME]}/{MODEL}", stream=False),
            )
        )
    if outcome.ok:
        assert len(world.requests_for(CHAT_NAME)) == 2
    return outcome


def _fallback_within_the_provider(world: TrafficWorld) -> Outcome:
    """The route's first model does not exist; its fallback, same provider, does."""

    chat = world.ids[CHAT_NAME]
    with world.client(
        model=f"{chat}/{REJECTED_MODEL}", MODEL_FALLBACKS=f"{chat}/{MODEL}"
    ) as client:
        outcome = _answered(
            client.post(
                "/v1/messages", json=_messages_body("claude-sonnet-4-5", stream=False)
            )
        )
    if outcome.ok:
        models = [
            json.loads(seen.body).get("model")
            for seen in world.requests_for(CHAT_NAME)
            if seen.path.endswith("/chat/completions")
        ]
        assert REJECTED_MODEL in models and MODEL in models, models
    return outcome


def _discovery_sweep(world: TrafficWorld) -> Outcome:
    """The sweep's own per-provider call: ``list_model_infos`` on the tree."""

    with world.client() as client:
        manager = provider_manager_for_app(_app_of(client))
        result = _portal(client).call(
            functools.partial(
                manager.refresh_provider_models, world.ids[CHAT_NAME], attempts=1
            )
        )
    failure = result.failure_for(world.ids[CHAT_NAME])
    return Outcome(ok=failure is None, text="" if failure is None else failure.message)


def _test_button(world: TrafficWorld) -> Outcome:
    with world.client() as client:
        payload = client.post(
            f"/admin/api/providers/{world.ids[CHAT_NAME]}/test"
        ).json()
    return Outcome(ok=bool(payload.get("ok")), text=str(payload.get("message", "")))


def _credential_health_probe(world: TrafficWorld) -> Outcome:
    """What the executor sends after a 429: one request on one named key."""

    request = MessagesRequest.model_validate(_messages_body(MODEL, stream=True))

    async def probe(manager: ProviderRuntimeManager) -> Outcome:
        lease = await manager.acquire()
        try:
            provider = lease.resolve_provider(world.ids[PAIR_NAME])
            assert isinstance(provider, PooledCredentialPort)
            stream = provider.stream_on_credential(1, request)
            try:
                async for _chunk in stream:
                    pass
            except ExecutionFailure as exc:
                return Outcome(ok=False, text=exc.message)
            return Outcome(ok=True, text="")
        finally:
            await lease.release()

    with world.client() as client:
        outcome = _portal(client).call(probe, provider_manager_for_app(_app_of(client)))
    if outcome.ok:
        # The key the probe named, and only that one.
        auths = {seen.headers.get("authorization") for seen in world.rig.host.requests}
        assert auths == {f"Bearer {SECOND_KEY}"}, auths
    return outcome


def _media_image_with_result_download(world: TrafficWorld) -> Outcome:
    """A picture the host answers with a link, fetched by MCC before answering."""

    with world.client() as client:
        response = client.post(
            GEMINI_IMAGE_PATH,
            json={
                "contents": [{"role": "user", "parts": [{"text": "a red kite"}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            },
        )
    outcome = Outcome(ok=response.status_code == 200, text=response.text)
    if outcome.ok:
        paths = [seen.path for seen in world.rig.host.requests]
        assert f"/{_slug(MEDIA_NAME)}/v1/images/generations" in paths, paths
        assert "/files/kite.png" in paths, paths
    return outcome


TrafficDriver = Callable[[TrafficWorld], Outcome]


@dataclass(frozen=True)
class TrafficClass:
    name: str
    #: The change that drives this row: ``PR-2`` / ``PR-3`` for the filled
    #: rows, else the one that will (the PR split in the spec).
    filled_by: str
    #: A Providers-card probe (PR-2): answers with the card's payload.
    driver: Driver | None = None
    #: Real traffic through the provider tree (PR-3): answers what the client
    #: or the card was told.
    traffic: TrafficDriver | None = None
    #: A token refresh (PR-5): drives one through the leg (and, for a
    #: sign-in, through the card's Refresh button) against the rig.
    refresh: RefreshDriver | None = None


TRAFFIC: tuple[TrafficClass, ...] = (
    TrafficClass("probe_capabilities", "PR-2", _capability_probe),
    TrafficClass("identity_probe", "PR-2", _identity_probe),
    TrafficClass("dialect_probe", "PR-2", _dialect_probe),
    TrafficClass("openrouter_live_model_list", "PR-KOR", _openrouter_live_list),
    TrafficClass("chat_completions", "PR-3", traffic=_chat_completions),
    TrafficClass("responses", "PR-3", traffic=_responses),
    TrafficClass("anthropic_messages", "PR-3", traffic=_anthropic_messages),
    TrafficClass("same_exit_retry", "PR-3", traffic=_same_exit_retry),
    TrafficClass(
        "fallback_to_a_model_of_the_same_provider",
        "PR-3",
        traffic=_fallback_within_the_provider,
    ),
    TrafficClass("discovery_sweep", "PR-3", traffic=_discovery_sweep),
    TrafficClass("test_button", "PR-3", traffic=_test_button),
    TrafficClass("credential_health_probe", "PR-3", traffic=_credential_health_probe),
    TrafficClass(
        "media_image_with_result_download",
        "PR-3",
        traffic=_media_image_with_result_download,
    ),
    TrafficClass(
        "oauth_token_refresh_claude", "PR-5", refresh=lambda w: _claude_refresh(w)
    ),
    TrafficClass(
        "oauth_token_refresh_chatgpt", "PR-5", refresh=lambda w: _chatgpt_refresh(w)
    ),
    TrafficClass(
        "vertex_token_refresh_socks5", "PR-5", refresh=lambda w: _vertex_refresh(w)
    ),
)


def _placeholder(row: TrafficClass) -> Any:
    return pytest.param(
        row,
        id=row.name,
        marks=pytest.mark.skip(
            reason=f"placeholder: {row.name} is driven from {row.filled_by}"
        ),
    )


def _rows() -> list[Any]:
    """The Providers card's probes, asked the probe questions."""

    return [pytest.param(row, id=row.name) for row in TRAFFIC if row.driver]


def _traffic_rows() -> list[Any]:
    """Every other traffic class, asked the real-traffic questions.

    A row nobody drives yet stays a strict ``skip`` naming who will, so the
    grid says what is covered and what is not.
    """

    return [
        pytest.param(row, id=row.name) if row.traffic else _placeholder(row)
        for row in TRAFFIC
        if row.driver is None and row.refresh is None
    ]


def _drive(row: TrafficClass) -> Driver:
    assert row.driver is not None
    return row.driver


def _send(row: TrafficClass) -> TrafficDriver:
    assert row.traffic is not None
    return row.traffic


# -------------------------------------------------------------------- rows


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
@pytest.mark.local_serial
async def test_a_chained_provider_is_reached_only_through_its_proxies(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=False)

    payload = await _drive(row)(world)

    world.rig.assert_masked()
    first, *others = world.rig.proxies
    assert first.targets, "the first exit in the chain's order carried nothing"
    assert all(not proxy.targets for proxy in others)
    assert payload["proxy_exit"] == mask_proxy_label(first.url)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
@pytest.mark.local_serial
async def test_an_unreachable_exit_is_skipped_for_the_next_one(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=False)
    world.mark_unreachable(0)

    await _drive(row)(world)

    world.rig.assert_masked()
    dead, second, third = world.rig.proxies
    assert dead.accepted == 0
    assert second.targets
    assert not third.targets


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
@pytest.mark.local_serial
async def test_no_usable_exit_and_direct_fallback_off_sends_nothing(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=False)
    world.mark_unreachable(0, 1, 2)

    payload = await _drive(row)(world)

    world.rig.assert_nothing_sent()
    assert payload["status"] == "not_sent"
    assert "Direct fallback is off" in payload["detail"]
    assert "Proxying page" in payload["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", _rows())
@pytest.mark.local_serial
async def test_no_usable_exit_and_direct_fallback_on_goes_direct(
    world: ProbeWorld, row: TrafficClass
) -> None:
    world.chain_everything(direct_fallback=True)
    world.mark_unreachable(0, 1, 2)

    payload = await _drive(row)(world)

    assert world.rig.host.peers
    assert world.rig.direct_peers() == world.rig.host.peers
    assert all(proxy.accepted == 0 for proxy in world.rig.proxies)
    assert payload["proxy_exit"] == "direct"


def test_every_row_names_who_fills_it() -> None:
    """The grid is the plan: a placeholder says which change will drive it."""

    names = [row.name for row in TRAFFIC]
    assert len(names) == len(set(names))
    for row in TRAFFIC:
        assert row.filled_by.startswith("PR-")
        # A probe-shaped row: the Providers card's (PR-2) and, since 7.84.0,
        # the background fetch of OpenRouter's live list, which takes the one
        # exit a probe takes.
        assert (row.driver is not None) == (row.filled_by in {"PR-2", "PR-KOR"})
        assert (row.traffic is not None) == (row.filled_by == "PR-3")
        assert (row.refresh is not None) == (row.filled_by == "PR-5")
    # 7.79.2: no placeholder is left.
    assert all(row.driver or row.traffic or row.refresh for row in TRAFFIC)


# ------------------------------------------------- real traffic (PR-3 rows)
#
# Three questions per row, plus the two that prove nothing else moved:
#
# * chain healthy, Direct fallback off: the host sees it, and only from the
#   chain's first exit, by name;
# * the chain has nothing usable -- every entry paused, every address removed,
#   or the chain file unreadable at start -- and Direct fallback off: not one
#   connection reaches the host or any proxy, and the answer names the setting
#   and the page (7.78.8; before it, each of these went out from this
#   computer's own address);
# * the same chain with Direct fallback ON: exactly what every release did
#   before -- this computer's address -- because the user's rule for that
#   case is a later change of its own (PR-4), not this one.

#: How each fail-closed row breaks the chain.
NO_USABLE_ENTRY: dict[str, Callable[[TrafficWorld, bool], None]] = {
    "all_paused": lambda world, direct: world.write(
        paused=True, direct_fallback=direct
    ),
    "all_removed": lambda world, direct: world.write(
        removed=True, direct_fallback=direct
    ),
    "chain_file_unreadable": lambda world, direct: world.corrupt(
        direct_fallback=direct
    ),
}


@pytest.mark.parametrize("row", _traffic_rows())
@pytest.mark.local_serial
def test_real_traffic_reaches_the_host_only_through_the_chain(
    traffic: TrafficWorld, row: TrafficClass
) -> None:
    traffic.write()

    outcome = _send(row)(traffic)

    assert outcome.ok, outcome.text
    traffic.rig.assert_masked()
    first, *others = traffic.rig.proxies
    assert first.targets, "the first exit in the chain's order carried nothing"
    assert all(not proxy.targets for proxy in others)


@pytest.mark.parametrize("cause", sorted(NO_USABLE_ENTRY))
@pytest.mark.parametrize("row", _traffic_rows())
@pytest.mark.local_serial
def test_no_usable_entry_and_direct_fallback_off_sends_nothing(
    traffic: TrafficWorld, row: TrafficClass, cause: str
) -> None:
    NO_USABLE_ENTRY[cause](traffic, False)

    outcome = _send(row)(traffic)

    traffic.rig.assert_nothing_sent()
    assert not outcome.ok, outcome.text
    for words in REFUSAL_WORDS:
        assert words in outcome.text, outcome.text


@pytest.mark.parametrize("cause", sorted(NO_USABLE_ENTRY))
@pytest.mark.parametrize("row", _traffic_rows())
@pytest.mark.local_serial
def test_no_usable_entry_and_direct_fallback_on_sends_nothing_too(
    traffic: TrafficWorld, row: TrafficClass, cause: str
) -> None:
    """PR-4: paused, removed or unreadable is not "every proxy unhealthy".

    Up to 7.79.1 each of these went out from this computer's own address
    when Direct fallback was on.
    """

    NO_USABLE_ENTRY[cause](traffic, True)

    outcome = _send(row)(traffic)

    traffic.rig.assert_nothing_sent()
    assert not outcome.ok, outcome.text
    for words in REFUSAL_WORDS:
        assert words in outcome.text, outcome.text


@pytest.mark.local_serial
def test_a_refused_request_moves_on_to_the_next_model(traffic: TrafficWorld) -> None:
    """Refused exactly as any unavailable provider is: the chain carries on.

    The route's first model is on a provider whose chain has nothing usable
    and Direct fallback off; its fallback is on a provider with no chain. The
    first is never dialled -- not through a proxy, not directly -- and the
    second answers the client.
    """

    traffic.write(paused=True)
    masked = traffic.ids[CHAT_NAME]
    plain = traffic.ids[PLAIN_NAME]

    with traffic.client(
        model=f"{masked}/{MODEL}", MODEL_FALLBACKS=f"{plain}/{MODEL}"
    ) as client:
        response = client.post(
            "/v1/messages", json=_messages_body("claude-sonnet-4-5", stream=False)
        )

    assert response.status_code == 200, response.text
    assert traffic.requests_for(CHAT_NAME) == []
    assert len(traffic.requests_for(PLAIN_NAME)) == 1
    assert all(not proxy.targets for proxy in traffic.rig.proxies)


@pytest.mark.local_serial
def test_a_refusal_reaches_the_client_as_a_503_naming_the_setting(
    traffic: TrafficWorld,
) -> None:
    """With nothing behind it, the refusal is the answer: 503, and why."""

    traffic.write(paused=True)
    with traffic.client() as client:
        response = client.post(
            "/v1/messages",
            json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
        )

    assert response.status_code == 503, response.text
    message = response.json()["error"]["message"]
    assert message.startswith(f"Not sent: {CHAT_NAME}'s proxy chain has no usable")
    assert "all 3 entries are paused" in message
    assert "Direct fallback is off" in message
    assert f"Proxying page -> {CHAT_NAME}" in message
    traffic.rig.assert_nothing_sent()


@pytest.mark.local_serial
def test_a_static_proxy_still_carries_a_chain_with_nothing_usable(
    traffic: TrafficWorld,
) -> None:
    """A provider's own stored proxy stands in, as it always has: masked.

    The fail-closed rule is about this computer's own address. A chain with
    nothing usable stands aside for the provider's static proxy exactly as
    before (``<PROVIDER>_PROXY``, or a custom provider's stored one), and that
    goes out through the proxy -- not refused.
    """

    entry = get_provider_registry().add(
        display_name="Static Co",
        base_url=traffic.rig.host.base_url(path="/static_co/v1"),
        api_keys=(RIG_KEY,),
        proxy=traffic.rig.proxy_urls[2],
    )
    traffic.ids["Static Co"] = entry.provider_id
    traffic.write(paused=True)

    with traffic.client() as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{entry.provider_id}/{MODEL}", stream=False),
            )
        )

    assert outcome.ok, outcome.text
    assert traffic.requests_for("Static Co")
    traffic.rig.assert_masked()
    first, second, third = traffic.rig.proxies
    assert third.targets and not first.targets and not second.targets


# =========================================== 7.79.2: PR-4 Direct fallback rows
#
# The user's decision of 2026-10-06 23:03: Direct fallback ON uses this
# computer's address only once EVERY proxy of the chain is unhealthy. Not after
# a number of failed exits in one request, not when the switch bound is spent,
# not for paused / removed / unreadable (the fail-closed rows above), and not
# for a listing while a proxy answers.

#: The rows that are requests through the chain's rotation. A model listing --
#: the sweep and the Test button -- asks the chain's first entry and is never
#: rotated, so it never reaches the fallback at all.
REQUEST_ROWS = frozenset(
    {
        "chat_completions",
        "responses",
        "anthropic_messages",
        "same_exit_retry",
        "fallback_to_a_model_of_the_same_provider",
        "credential_health_probe",
        "media_image_with_result_download",
    }
)


def _mark_unhealthy(world: TrafficWorld, *indexes: int) -> None:
    """Put exits on the reachability ladder in both books, as failed dials would."""

    for index in indexes:
        label = mask_proxy_label(world.rig.proxy_urls[index])
        PROXY_REACHABILITY.note_failure(label, "rig: refused")
        media_proxy_pool.MEDIA_PROXY_REACHABILITY.note_failure(label, "rig: refused")


def _last_attempts(world: TrafficWorld) -> list[dict[str, Any]]:
    """The newest request's attempts, as the request log stored them."""

    store = store_from_settings(world.settings())
    assert store is not None
    deadline = time.monotonic() + 10.0
    while True:
        rows, _total = store.list_requests(limit=1)
        if rows:
            row = store.get_request(rows[0]["id"])
            if row is not None and row["route_attempts"]:
                return sorted(
                    row["route_attempts"], key=lambda attempt: attempt["attempt"]
                )
        assert time.monotonic() < deadline, "the request was never logged"
        time.sleep(0.05)


def _dial_proxies(attempt: dict[str, Any]) -> list[str | None]:
    ladder = (attempt.get("params") or {}).get("ladder") or {}
    return [dial.get("proxy") for dial in ladder.get("dials") or []]


@pytest.mark.parametrize("row", _traffic_rows())
@pytest.mark.local_serial
def test_every_exit_unhealthy_and_direct_fallback_on_goes_direct(
    traffic: TrafficWorld, row: TrafficClass
) -> None:
    """The one case the user allowed this computer's address."""

    traffic.write(direct_fallback=True)
    _mark_unhealthy(traffic, 0, 1, 2)

    outcome = _send(row)(traffic)

    assert outcome.ok, outcome.text
    if row.name in REQUEST_ROWS:
        assert traffic.rig.host.peers
        assert traffic.rig.direct_peers() == traffic.rig.host.peers
        assert all(not proxy.targets for proxy in traffic.rig.proxies)
    else:
        # A listing asks the first entry, as it always has: masked.
        traffic.rig.assert_masked()


@pytest.mark.parametrize("row", _traffic_rows())
@pytest.mark.local_serial
def test_one_unhealthy_exit_among_healthy_never_goes_direct(
    traffic: TrafficWorld, row: TrafficClass
) -> None:
    traffic.write(direct_fallback=True)
    _mark_unhealthy(traffic, 0)

    outcome = _send(row)(traffic)

    assert outcome.ok, outcome.text
    traffic.rig.assert_masked()
    if row.name in REQUEST_ROWS:
        first, second, _third = traffic.rig.proxies
        assert not first.targets, "a request went through the unhealthy exit"
        assert second.targets


@pytest.mark.local_serial
def test_a_healthy_exit_left_withholds_direct_and_the_request_moves_on(
    traffic: TrafficWorld,
) -> None:
    """The live-failure bound ends the loop at the dead exit; three are healthy.

    Up to 7.79.1 the request then went out from this computer. Now Direct is
    withheld, nothing is dialled, and the route's next model answers.
    """

    traffic.write(direct_fallback=True, dead_first=True)
    masked = traffic.ids[CHAT_NAME]
    plain = traffic.ids[PLAIN_NAME]

    with traffic.client(
        model=f"{masked}/{MODEL}",
        MODEL_FALLBACKS=f"{plain}/{MODEL}",
        PROXY_MAX_LIVE_FAILURES="1",
    ) as client:
        response = client.post(
            "/v1/messages", json=_messages_body("claude-sonnet-4-5", stream=False)
        )

    assert response.status_code == 200, response.text
    assert traffic.requests_for(CHAT_NAME) == []
    assert len(traffic.requests_for(PLAIN_NAME)) == 1
    # The plain provider has no chain: its one request is the only peer.
    assert traffic.rig.direct_peers() == traffic.rig.host.peers
    assert len(traffic.rig.host.peers) == 1
    assert all(not proxy.targets for proxy in traffic.rig.proxies)


@pytest.mark.local_serial
def test_a_withheld_direct_is_a_503_and_the_log_names_no_direct_dial(
    traffic: TrafficWorld,
) -> None:
    traffic.write(direct_fallback=True, dead_first=True)

    with traffic.client(PROXY_MAX_LIVE_FAILURES="1") as client:
        response = client.post(
            "/v1/messages",
            json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
        )

    assert response.status_code == 503, response.text
    message = response.json()["error"]["message"]
    for phrase in (
        "Not sent from this computer's own address",
        f"3 of 4 proxies in {CHAT_NAME}'s chain are not unhealthy",
        "Direct fallback uses this computer's address only once every proxy",
        f"Proxying page -> {CHAT_NAME} -> Direct fallback",
    ):
        assert phrase in message, message
    traffic.rig.assert_nothing_sent()
    dead = mask_proxy_label(traffic.dead_url)
    attempt = _last_attempts(traffic)[0]
    assert _dial_proxies(attempt) == [dead]
    assert attempt["proxy_label"] in (dead, None)


@pytest.mark.local_serial
def test_media_withholds_direct_the_same_way(traffic: TrafficWorld) -> None:
    traffic.write(direct_fallback=True, dead_first=True)
    with traffic.client(PROXY_MAX_LIVE_FAILURES="1") as client:
        response = client.post(
            GEMINI_IMAGE_PATH,
            json={
                "contents": [{"role": "user", "parts": [{"text": "a red kite"}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            },
        )

    assert response.status_code != 200, response.text
    assert "Direct fallback" in response.text, response.text
    traffic.rig.assert_nothing_sent()


@pytest.mark.local_serial
def test_the_switch_bound_never_ends_in_direct(traffic: TrafficWorld) -> None:
    """Every exit answers 503 and the chain switches on it, once: then it ends."""

    traffic.write(direct_fallback=True, on=("upstream", "overloaded"), max_switches=1)
    traffic.chat_failures_left = 10_000

    with traffic.client() as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
            )
        )

    assert not outcome.ok
    traffic.rig.assert_masked()
    used = [proxy for proxy in traffic.rig.proxies if proxy.targets]
    assert len(used) == 2, [proxy.url for proxy in used]


# ================================================ 7.79.2: PR-6 log label rows


@pytest.mark.local_serial
def test_the_log_names_a_one_entry_chain_s_entry(traffic: TrafficWorld) -> None:
    """A chain left with one usable entry is that entry: named in the log."""

    proxies = {
        f"px_{index}": ProxyEndpoint(url=url)
        for index, url in enumerate(traffic.rig.proxy_urls)
    }
    save_proxy_chains(
        ProxyChains(
            proxies=proxies,
            chains={
                traffic.ids[CHAT_NAME]: ProxyChain(
                    enabled=True,
                    entries=(
                        ProxyChainEntry(proxy="px_0", paused=True),
                        ProxyChainEntry(proxy="px_1"),
                    ),
                )
            },
        ),
        traffic.chains_path,
    )
    reset_proxy_chains_cache()

    with traffic.client() as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
            )
        )

    assert outcome.ok, outcome.text
    traffic.rig.assert_masked()
    label = mask_proxy_label(traffic.rig.proxy_urls[1])
    attempt = _last_attempts(traffic)[0]
    assert attempt["proxy_label"] == label
    assert _dial_proxies(attempt) == [label]


@pytest.mark.local_serial
def test_the_log_names_a_static_proxy(traffic: TrafficWorld) -> None:
    entry = get_provider_registry().add(
        display_name="Static Log Co",
        base_url=traffic.rig.host.base_url(path="/static_log_co/v1"),
        api_keys=(RIG_KEY,),
        proxy=traffic.rig.proxy_urls[2],
    )

    with traffic.client() as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{entry.provider_id}/{MODEL}", stream=False),
            )
        )

    assert outcome.ok, outcome.text
    traffic.rig.assert_masked()
    label = mask_proxy_label(traffic.rig.proxy_urls[2])
    attempt = _last_attempts(traffic)[0]
    assert attempt["proxy_label"] == label
    assert _dial_proxies(attempt) == [label]


@pytest.mark.local_serial
def test_the_log_names_direct_carried_by_the_system_proxy(
    traffic: TrafficWorld, monkeypatch
) -> None:
    """Direct fallback, every exit unhealthy, and the OS proxy carries it.

    The rig's HTTP proxy plays the operating system's proxy: httpx really goes
    through it (a client with no proxy of its own follows the system's), and the
    log says so instead of ``direct``.
    """

    system = {"http": traffic.rig.proxy_urls[1]}
    monkeypatch.setattr("httpx._utils.getproxies", lambda: dict(system))
    monkeypatch.setattr(
        "my_claude_code.config.system_proxy.getproxies", lambda: dict(system)
    )
    traffic.write(direct_fallback=True)
    _mark_unhealthy(traffic, 0, 1, 2)

    with traffic.client() as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
            )
        )

    assert outcome.ok, outcome.text
    carrier = traffic.rig.proxies[1]
    assert traffic.rig.host.peers
    assert set(traffic.rig.host.peers) <= set(carrier.outbound)
    label = f"direct via system proxy {mask_proxy_label(carrier.url)}"
    attempt = _last_attempts(traffic)[0]
    assert attempt["proxy_label"] == label
    assert _dial_proxies(attempt) == [label]


# ============================================== 7.79.2: PR-5 token refresh rows

FRESH_CLAUDE = "sk-ant-oat01-rigfresh-0000"
FRESH_CHATGPT_EXP = 9_999_999_999
VERTEX_TOKEN = "ya29.rig-fresh"


def _jwt(claims: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({})}.{part(claims)}."


@dataclass
class RefreshWorld:
    """The rig, the token stores and the chain file, for the refresh rows."""

    rig: MaskingRig
    chains_path: Path
    chatgpt_store: Path
    #: Whether a chain was written: the Vertex row reads it to decide whether
    #: its refresh has a proxy at all.
    proxied: bool = False

    def answer(self, request: SeenRequest) -> tuple[int, bytes]:
        if request.path.endswith("/oauth/token"):
            body = json.loads(request.body or b"{}")
            if body.get("client_id") == chatgpt_creds.CODEX_OAUTH_CLIENT_ID:
                return 200, json.dumps(
                    {
                        "access_token": _jwt(
                            {"exp": FRESH_CHATGPT_EXP, "chatgpt_account_id": "acct"}
                        ),
                        "refresh_token": "rig-chatgpt-refresh-2",
                        "expires_in": 3600,
                    }
                ).encode()
            return 200, json.dumps(
                {
                    "access_token": FRESH_CLAUDE,
                    "refresh_token": "sk-ant-ort01-rigfresh-0000",
                    "expires_in": 3600,
                }
            ).encode()
        if request.path.endswith("/vertex/token"):
            return 200, b"{}"
        return 400, b'{"error":{"message":"rig: not part of the refresh rows"}}'

    def chain(self, *, direct_fallback: bool = False) -> None:
        proxies = {
            f"px_{index}": ProxyEndpoint(url=url)
            for index, url in enumerate(self.rig.proxy_urls)
        }
        chain = ProxyChain(
            enabled=True,
            policy="failover",
            entries=tuple(ProxyChainEntry(proxy=key) for key in proxies),
            direct_fallback=direct_fallback,
            oauth_acknowledged=True,
        )
        save_proxy_chains(
            ProxyChains(
                proxies=proxies,
                chains=dict.fromkeys(("anthropic_oauth", "chatgpt_oauth"), chain),
            ),
            self.chains_path,
        )
        reset_proxy_chains_cache()
        self.proxied = True

    def settings(self) -> Settings:
        return Settings.model_validate(
            {
                "CHATGPT_OAUTH_BASE_URL": self.rig.host.base_url(path="/chatgpt"),
                # The stored sign-in, not a pasted token.
                "CHATGPT_OAUTH_ACCESS_TOKEN": CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
            }
        )

    def token_posts(self) -> list[SeenRequest]:
        return [
            seen
            for seen in self.rig.host.requests
            if seen.path.endswith(("/oauth/token", "/vertex/token"))
        ]

    def press(self, button: str) -> tuple[int, str]:
        """The card's Refresh button, as the dashboard presses it."""

        app = create_test_app(self.settings())
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            if button == "claude":
                response = client.post("/admin/api/anthropic-oauth/refresh", json={})
            else:
                account = chatgpt_creds.load_chatgpt_accounts(
                    auth_path=self.chatgpt_store
                )[0]
                response = client.post(
                    f"/admin/api/chatgpt-oauth/accounts/{account.id}/refresh",
                    json={},
                )
        return response.status_code, response.text


@pytest.fixture
def refresh_world(tmp_path, monkeypatch) -> Iterator[RefreshWorld]:
    rig = start_masking_rig()
    chains_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: chains_path
    )
    reset_proxy_chains_cache()
    reset_proxy_health()
    token_base = rig.host.base_url(path="")
    # The token endpoints, pointed at the rig. The real hosts are blocked in
    # the suite anyway (tests/support/token_host_block.py).
    monkeypatch.setattr(claude_creds, "TOKEN_URL", f"{token_base}/v1/oauth/token")
    monkeypatch.setattr(
        claude_creds, "LEGACY_TOKEN_URL", f"{token_base}/v1/oauth/token"
    )
    monkeypatch.setattr(
        chatgpt_creds, "CODEX_OAUTH_TOKEN_URL", f"{token_base}/oauth/token"
    )
    monkeypatch.setattr(
        claude_creds, "managed_store_path", lambda: tmp_path / "anthropic_oauth.json"
    )
    monkeypatch.setattr(
        "my_claude_code.config.credential_names.credential_names_path",
        lambda: tmp_path / "credential_names.json",
    )
    claude_creds._REFRESH_LOCKS.clear()
    chatgpt_store = tmp_path / "auth" / "chatgpt-oauth.json"
    monkeypatch.setattr(chatgpt_creds, "chatgpt_oauth_auth_path", lambda: chatgpt_store)
    # Two expired credentials MCC signed in itself (NATIVE): a refresh is due.
    claude_creds.add_or_update_account(
        claude_creds.OAuthTokens(
            access_token="sk-ant-oat01-rigold-0000",
            refresh_token="sk-ant-ort01-rigold-0000",
            expires_at=int(time.time()) - 600,
            account_uuid="uuid-rig",
            scopes=("user:inference",),
        ),
        origin=ORIGIN_MCC,
    )
    chatgpt_creds.store_managed_chatgpt_oauth_tokens(
        {
            "access_token": _jwt({"exp": int(time.time()) - 600}),
            "refresh_token": "rig-chatgpt-refresh-1",
            "id_token": _jwt({"chatgpt_account_id": "acct"}),
            "account_id": "acct",
        },
        auth_path=chatgpt_store,
    )
    world = RefreshWorld(rig=rig, chains_path=chains_path, chatgpt_store=chatgpt_store)
    rig.host.responder = world.answer
    try:
        yield world
    finally:
        rig.close()
        reset_proxy_chains_cache()
        reset_proxy_health()


def _first_leg(provider: Any) -> Any:
    """The leg a request to this provider goes out through first."""

    if isinstance(provider, ProxyRotatingProvider):
        return provider._pool.get(0)
    return provider


def _claude_refresh(world: RefreshWorld) -> Outcome:
    """A request finds the credential expired: the leg refreshes it, now."""

    async def run() -> Outcome:
        provider = create_provider("anthropic_oauth", world.settings())
        try:
            tokens = await _first_leg(provider)._oauth.current_tokens(purpose="request")
        finally:
            await provider.cleanup()
        return Outcome(ok=tokens.access_token == FRESH_CLAUDE, text="")

    return asyncio.run(run())


def _chatgpt_refresh(world: RefreshWorld) -> Outcome:
    """The leg's own request path resolves the credential: expired, so refreshed."""

    async def run() -> Outcome:
        provider = create_provider("chatgpt_oauth", world.settings())
        request = MessagesRequest.model_validate(_messages_body("gpt-5.5", stream=True))
        try:
            stream = _first_leg(provider).stream_response(request)
            with contextlib.suppress(Exception):
                async for _chunk in stream:
                    pass
        finally:
            await provider.cleanup()
        record = chatgpt_creds.load_chatgpt_accounts(auth_path=world.chatgpt_store)[0]
        return Outcome(
            ok=record.tokens.get("refresh_token") == "rig-chatgpt-refresh-2",
            text=str(sorted(record.tokens)),
        )

    return asyncio.run(run())


class _RigGoogleCredentials(GoogleCredentials):
    """ADC that refreshes against the rig, through whatever session it is handed."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url

    def refresh(self, request: Any) -> None:
        response = request(url=self._url, method="POST", body=b"grant_type=rig")
        assert response.status == 200
        self.token = VERTEX_TOKEN


def _vertex_refresh(world: RefreshWorld) -> Outcome:
    """Vertex refreshes through a ``socks5://`` entry: the proxy resolves the host."""

    url = f"{world.rig.host.base_url(path='')}/vertex/token"
    provider = GoogleAccessTokenProvider(
        lambda: _RigGoogleCredentials(url),
        proxy=world.rig.proxy_urls[0] if world.proxied else None,
    )
    token = asyncio.run(provider())
    return Outcome(ok=token == VERTEX_TOKEN, text=token)


RefreshDriver = Callable[[RefreshWorld], Outcome]


def _refresh_rows() -> list[Any]:
    return [pytest.param(row, id=row.name) for row in TRAFFIC if row.refresh]


def _refresh(row: TrafficClass) -> RefreshDriver:
    assert row.refresh is not None
    return row.refresh


@pytest.mark.parametrize("row", _refresh_rows())
@pytest.mark.local_serial
def test_a_token_refresh_leaves_through_the_leg_s_exit(
    refresh_world: RefreshWorld, row: TrafficClass
) -> None:
    refresh_world.chain()

    outcome = _refresh(row)(refresh_world)

    assert outcome.ok, outcome.text
    assert refresh_world.token_posts(), refresh_world.rig.host.requests
    # Through the chain's first exit only, and the proxy -- not this
    # computer -- resolved the token host (the Vertex row's socks5:// entry
    # is the C-8 case: requests resolves locally unless told socks5h).
    refresh_world.rig.assert_masked()
    first, *others = refresh_world.rig.proxies
    assert first.targets
    assert all(not proxy.targets for proxy in others)


@pytest.mark.parametrize("row", _refresh_rows())
@pytest.mark.local_serial
def test_with_no_chain_a_token_refresh_is_what_it_always_was(
    refresh_world: RefreshWorld, row: TrafficClass
) -> None:
    outcome = _refresh(row)(refresh_world)

    assert outcome.ok, outcome.text
    posts = refresh_world.token_posts()
    assert posts
    assert refresh_world.rig.direct_peers() == refresh_world.rig.host.peers
    assert all(not proxy.targets for proxy in refresh_world.rig.proxies)


@pytest.mark.parametrize("button", ["claude", "chatgpt"])
@pytest.mark.local_serial
def test_the_cards_refresh_button_leaves_through_the_chain(
    refresh_world: RefreshWorld, button: str
) -> None:
    refresh_world.chain()

    status, text = refresh_world.press(button)

    assert status == 200, text
    assert refresh_world.token_posts()
    refresh_world.rig.assert_masked()
    first, *others = refresh_world.rig.proxies
    assert first.targets
    assert all(not proxy.targets for proxy in others)


@pytest.mark.parametrize("button", ["claude", "chatgpt"])
@pytest.mark.local_serial
def test_the_refresh_button_sends_nothing_when_no_exit_may_carry_it(
    refresh_world: RefreshWorld, button: str
) -> None:
    refresh_world.chain(direct_fallback=False)
    for url in refresh_world.rig.proxy_urls:
        PROXY_REACHABILITY.note_failure(mask_proxy_label(url), "rig: refused")

    status, text = refresh_world.press(button)

    assert status == 503, text
    assert "Direct fallback is off" in text
    assert "Proxying page" in text
    refresh_world.rig.assert_nothing_sent()


# ============================== 7.81.0: keep trying exits until one answers
#
# A chain with "Keep trying exits until one answers" ticked. The host answers
# by the exit that carried the request: a free-usage 429, a country refusal,
# or the ordinary answer.

FREE_USAGE_429 = (
    429,
    b'{"type":"error","error":{"type":"FreeUsageLimitError",'
    b'"message":"Rate limit exceeded. Please try again later."}}',
)
REGION_403 = (
    403,
    b'{"type":"error","error":{"type":"RegionError",'
    b'"message":"This model is not available in your country."}}',
)


#: A 429 the leaf would retry on the same exit (``RATE_LIMIT_ROUTES_AROUND_MODEL``
#: off, the user's value) sleeps a real backoff first: with the world's zero
#: backoff the frozen retry loop refuses a zero-second reactive block before
#: it ever sleeps.
BACKOFF = {
    "PROVIDER_RETRY_BACKOFF_BASE_SECONDS": "0.01",
    "PROVIDER_RETRY_BACKOFF_MAX_SECONDS": "0.01",
    "RATE_LIMIT_ROUTES_AROUND_MODEL": "false",
}


def _remember_every_exit(world: TrafficWorld, name: str) -> None:
    """Every rig exit remembered spent for ``name``'s one key (chat and media)."""

    credential = credential_fingerprint(RIG_KEY)
    for url in world.rig.proxy_urls:
        label = mask_proxy_label(url)
        for memory in (EXIT_MEMORY, MEDIA_EXIT_MEMORY):
            memory.remember(
                world.ids[name],
                credential,
                label,
                state=SPENT,
                seconds=300,
                reason="rate_limit",
            )


def _exits_used(world: TrafficWorld, name: str) -> list[int | None]:
    return [world.exit_of(seen) for seen in world.requests_for(name)]


@pytest.mark.local_serial
def test_a_ticked_chain_moves_a_refused_request_to_the_next_exit(
    traffic: TrafficWorld,
) -> None:
    traffic.write(until_served=True, max_switches=2)
    traffic.exit_answers = {0: FREE_USAGE_429, 1: REGION_403}

    with traffic.client(**BACKOFF) as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
            )
        )

    assert outcome.ok, outcome.text
    traffic.rig.assert_masked()
    # One try per exit: the 429 is not retried on the exit that refused it.
    assert _exits_used(traffic, CHAT_NAME) == [0, 1, 2]
    bodies = {seen.body for seen in traffic.requests_for(CHAT_NAME)}
    assert len(bodies) == 1, "the request body differs between exits"
    keys = {
        seen.headers.get("authorization") for seen in traffic.requests_for(CHAT_NAME)
    }
    assert len(keys) == 1
    attempt = _last_attempts(traffic)[0]
    dials = ((attempt.get("params") or {}).get("ladder") or {}).get("dials") or []
    assert [bool(dial.get("memory")) for dial in dials] == [True, True, False]
    assert dials[0]["memory"].startswith("remembered spent until ")
    assert dials[1]["memory"].startswith("remembered blocked for its country")


@pytest.mark.local_serial
def test_a_ticked_chain_at_its_switch_limit_moves_on_and_never_goes_direct(
    traffic: TrafficWorld,
) -> None:
    traffic.write(until_served=True, direct_fallback=True, max_switches=1)
    traffic.exit_answers = {0: FREE_USAGE_429, 1: FREE_USAGE_429, 2: FREE_USAGE_429}
    masked = traffic.ids[CHAT_NAME]
    plain = traffic.ids[PLAIN_NAME]

    with traffic.client(
        model=f"{masked}/{MODEL}", MODEL_FALLBACKS=f"{plain}/{MODEL}", **BACKOFF
    ) as client:
        response = client.post(
            "/v1/messages", json=_messages_body("claude-sonnet-4-5", stream=False)
        )

    assert response.status_code == 200, response.text
    # Two exits tried (one switch), the third never, and never this computer.
    assert _exits_used(traffic, CHAT_NAME) == [0, 1]
    assert len(traffic.requests_for(PLAIN_NAME)) == 1
    # The plain provider has no chain: its one request is the only direct peer.
    assert traffic.rig.direct_peers() == [traffic.requests_for(PLAIN_NAME)[0].peer]
    first = _last_attempts(traffic)[0]
    assert first["error_kind"] == "unavailable"
    assert "switch limit (1 per request) was reached" in first["error_message"]


@pytest.mark.local_serial
def test_every_exit_remembered_and_direct_fallback_off_sends_nothing(
    traffic: TrafficWorld,
) -> None:
    traffic.write(until_served=True, direct_fallback=False)
    _remember_every_exit(traffic, CHAT_NAME)

    with traffic.client(**BACKOFF) as client:
        response = client.post(
            "/v1/messages",
            json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
        )

    assert response.status_code != 200, response.text
    traffic.rig.assert_nothing_sent()
    assert f"No exit in {CHAT_NAME}'s proxy chain can carry a request" in (
        response.text
    )


@pytest.mark.local_serial
def test_every_exit_remembered_and_direct_fallback_on_goes_direct(
    traffic: TrafficWorld,
) -> None:
    """The 7.79.2 rule reads the rotation's skip state, which the memory feeds."""

    traffic.write(until_served=True, direct_fallback=True)
    _remember_every_exit(traffic, CHAT_NAME)

    with traffic.client(**BACKOFF) as client:
        outcome = _answered(
            client.post(
                "/v1/messages",
                json=_messages_body(f"{traffic.ids[CHAT_NAME]}/{MODEL}", stream=False),
            )
        )

    assert outcome.ok, outcome.text
    assert all(not proxy.targets for proxy in traffic.rig.proxies)
    assert traffic.rig.direct_peers() == traffic.rig.host.peers


@pytest.mark.local_serial
def test_a_ticked_media_chain_moves_a_refused_picture_to_the_next_exit(
    traffic: TrafficWorld,
) -> None:
    traffic.write(until_served=True)
    traffic.exit_answers = {0: FREE_USAGE_429}

    with traffic.client(**BACKOFF) as client:
        response = client.post(
            GEMINI_IMAGE_PATH,
            json={
                "contents": [{"role": "user", "parts": [{"text": "a red kite"}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            },
        )

    assert response.status_code == 200, response.text
    traffic.rig.assert_masked()
    generations = [
        traffic.exit_of(seen)
        for seen in traffic.requests_for(MEDIA_NAME)
        if seen.path.endswith("/images/generations")
    ]
    assert generations == [0, 1]
