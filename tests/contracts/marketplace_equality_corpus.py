"""Every outbound call MCC makes, for the 7.109.0 marketplace equality contract.

Shared by ``test_marketplace_equality.py`` and by the one-off run that wrote
``marketplace_equality_baseline.json`` **from the v7.108.0 code (b0a7b1bd),
before the marketplace existed** -- which is why this module imports nothing
7.109.0 added. Run as a script it writes the baseline::

    <base>/.venv/Scripts/python.exe tests/contracts/marketplace_equality_corpus.py OUT.json

with HOME, USERPROFILE, APPDATA, LOCALAPPDATA and MCC_CONFIG_DIR pointed at an
empty scratch folder, so no override file, learned fact or models.dev copy of
the machine is read.

For each of the 432 requests of ``test_anthropic_foreign_thinking_equality``:

* every OpenAI-shaped Chat host MCC builds -- every profile in
  ``OPENAI_CHAT_PROFILES`` and every special Chat-family provider -- is driven
  through its real ``preflight_stream`` and ``stream_response`` up to the SDK
  call, and the call's keyword arguments are recorded: the body (``body``) and
  the whole call, ``extra_headers`` included (``call``);
* the Messages and the Responses doors of the three hosts that declare them
  (OpenCode Zen, OpenCode Go, a custom provider declaring both) are driven up
  to the HTTP send, and the request's body bytes and every header MCC set are
  recorded.

The OpenCode identity's clock and random tail are pinned, so its session and
message ids are the same on every run.
"""

import asyncio
import hashlib
import json
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from my_claude_code.application.model_metadata import ResponseSurface
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OpenAIChatProvider,
    create_openai_chat_provider,
    profile_with_declared_surfaces,
)
from my_claude_code.providers.openai_chat import client_identity
from my_claude_code.providers.runtime import factory
from tests.contracts.test_anthropic_foreign_thinking_equality import corpus
from tests.providers.support import passthrough_rate_limiter

#: The model id every host is asked for. One id: what varies is the host.
MODEL = "corpus/model-x"
#: The special (non-profile) Chat-family providers, by catalogue id.
SPECIAL_CHAT_HOSTS: tuple[str, ...] = (
    "cloudflare",
    "deepseek",
    "gemini",
    "github_models",
    "kilo",
    "lmstudio",
    "mistral",
    "nous_portal",
    "nvidia_nim",
    "open_router",
    "vertex",
)
#: The hosts whose Messages and Responses doors are driven.
SURFACE_HOSTS: tuple[str, ...] = ("opencode", "opencode_go", "corpus_custom")
#: The clock the OpenCode identity reads while the corpus runs.
_PINNED_TIME = 1_792_800_000.0


class _Stop(Exception):
    """Raised by the fake send once the call is recorded."""


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patch(owner: object, name: str, value: object) -> None:
    vars(owner)[name] = value


@contextmanager
def pinned_identity() -> Iterator[None]:
    """The OpenCode identity's ids, made the same on every run."""

    saved_time = client_identity.time
    saved_suffix = client_identity._random_suffix
    _patch(client_identity, "time", types.SimpleNamespace(time=lambda: _PINNED_TIME))
    _patch(
        client_identity,
        "_random_suffix",
        lambda: "R" * client_identity.ID_SUFFIX_CHARS,
    )
    with client_identity._session_ids_lock:
        client_identity._session_ids.clear()
    _patch(client_identity, "_last_mint_ms", 0)
    _patch(client_identity, "_mint_counter", 0)
    try:
        yield
    finally:
        _patch(client_identity, "time", saved_time)
        _patch(client_identity, "_random_suffix", saved_suffix)
        with client_identity._session_ids_lock:
            client_identity._session_ids.clear()


def _config() -> ProviderConfig:
    return ProviderConfig(api_key="sk-test", base_url="https://corpus.invalid/v1")


def chat_hosts() -> dict[str, Callable[[], Any]]:
    """Every Chat-family host this build constructs, by catalogue id."""

    settings = Settings.model_validate(
        {"CLOUDFLARE_ACCOUNT_ID": "corpus-account", "VERTEX_PROJECT_ID": "corpus"}
    )
    hosts: dict[str, Callable[[], Any]] = {}
    for provider_id in sorted(OPENAI_CHAT_PROFILES):
        hosts[provider_id] = lambda provider_id=provider_id: create_openai_chat_provider(
            provider_id,
            _config(),
            passthrough_rate_limiter(),
            profile=OPENAI_CHAT_PROFILES[provider_id],
        )
    for provider_id in SPECIAL_CHAT_HOSTS:
        make = factory._SPECIAL_PROVIDER_FACTORIES[provider_id]
        hosts[provider_id] = lambda make=make: make(
            _config(), settings, passthrough_rate_limiter()
        )
    return hosts


def surface_host(provider_id: str) -> OpenAIChatProvider:
    if provider_id == "corpus_custom":
        return create_openai_chat_provider(
            "corpus_custom",
            _config(),
            passthrough_rate_limiter(),
            profile=profile_with_declared_surfaces(
                GENERIC_OPENAI_PROFILE, ("chat_completions", "responses", "messages")
            ),
        )
    return create_openai_chat_provider(
        provider_id,
        _config(),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )


async def _drain(stream: Any) -> None:
    try:
        async for _event in stream:
            pass
    except Exception:
        return


async def chat_call(
    provider: Any, request: MessagesRequest, policy: Any
) -> dict[str, str]:
    """Drive one Chat request to the SDK call and record what it was handed."""

    seen: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        seen.append(kwargs)
        raise _Stop()

    _patch(provider._client.chat.completions, "create", create)
    try:
        provider.preflight_stream(request, reasoning=policy)
    except Exception as exc:
        return {"refused": type(exc).__name__}
    await _drain(provider.stream_response(request, reasoning=policy))
    if not seen:
        return {"refused": "no call"}
    call = seen[0]
    body = {key: value for key, value in call.items() if key != "extra_headers"}
    return {"body": digest(body), "call": digest(call)}


def _set_headers(client: httpx.AsyncClient, request: httpx.Request) -> list[list[str]]:
    """Every header MCC set: the client's own defaults are left out."""

    defaults = {name.lower(): value for name, value in client.headers.items()}
    kept: list[list[str]] = []
    for name, value in request.headers.multi_items():
        lowered = name.lower()
        if lowered in ("content-length", "host"):
            continue
        if defaults.get(lowered) == value:
            continue
        kept.append([lowered, value])
    return kept


async def door_call(
    provider: OpenAIChatProvider,
    surface: ResponseSurface,
    request: MessagesRequest,
    policy: Any,
) -> dict[str, str]:
    """Drive one request to one door's HTTP send and record the request."""

    seen: list[httpx.Request] = []
    client = (
        provider._messages._provider._client
        if surface is ResponseSurface.MESSAGES
        else provider._responses._client
    )

    async def send(sent: httpx.Request, **_kwargs: Any) -> Any:
        seen.append(sent)
        raise _Stop()

    _patch(client, "send", send)
    try:
        stream = provider._stream_on_surface(
            surface,
            f"{surface.value} (corpus)",
            provider.with_stand_ins(request),
            input_tokens=0,
            request_id=None,
            reasoning=policy,
        )
    except Exception as exc:
        return {"refused": type(exc).__name__}
    await _drain(stream)
    if not seen:
        return {"refused": "no call"}
    sent = seen[0]
    return {
        "body": hashlib.sha256(sent.content).hexdigest(),
        "headers": digest(_set_headers(client, sent)),
    }


async def _snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {}
    requests = corpus()
    for host, make in sorted(chat_hosts().items()):
        try:
            provider = make()
        except Exception as exc:
            out[f"chat/{host}"] = {"unbuildable": type(exc).__name__}
            continue
        out[f"chat/{host}/default_headers"] = digest(
            dict(getattr(provider, "_default_headers", {}) or {})
        )
        for key, (request, policy) in sorted(requests.items()):
            routed = request.model_copy(update={"model": MODEL}, deep=True)
            out[f"chat/{host}/{key}"] = await chat_call(provider, routed, policy)
    for host in SURFACE_HOSTS:
        provider = surface_host(host)
        for surface in (ResponseSurface.MESSAGES, ResponseSurface.RESPONSES):
            for key, (request, policy) in sorted(requests.items()):
                routed = request.model_copy(update={"model": MODEL}, deep=True)
                out[f"{surface.value}/{host}/{key}"] = await door_call(
                    provider, surface, routed, policy
                )
    return out


def snapshot() -> dict[str, Any]:
    """``door/host/fixture/policy/thinking -> record`` for the whole corpus."""

    with pinned_identity():
        return asyncio.run(_snapshot())


def record_digest(record: Any) -> str:
    """One record, as the baseline stores it: 12 hex of its sha256."""

    return digest(record)[:12]


def compact(records: dict[str, Any]) -> dict[str, Any]:
    """The baseline document: per door and host, one digest per corpus key.

    The corpus keys are stored once, in order; each group holds the record
    digests in that order, so a moved call is located by its index.
    """

    keys = sorted(corpus())
    groups: dict[str, Any] = {}
    for name, value in records.items():
        door, host, *rest = name.split("/", 2)
        group = groups.setdefault(f"{door}/{host}", {})
        if not rest or rest[0] == "default_headers":
            group["extra"] = record_digest(value)
    for name, group in groups.items():
        if f"{name}/{keys[0]}" in records:
            group["calls"] = [record_digest(records[f"{name}/{key}"]) for key in keys]
    return {"keys": keys, "count": len(records), "groups": groups}


def main(argv: list[str]) -> int:
    target = Path(argv[1])
    records = snapshot()
    target.write_text(
        json.dumps(compact(records), separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
