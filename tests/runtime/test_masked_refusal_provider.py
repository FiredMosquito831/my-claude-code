"""What a refused chain is built as: a refusal, never a leaf with no proxy.

The construction half of 7.78.8's fail-closed rule. A
:class:`MaskedRefusalPlan` reaches the two construction seams -- the chat
factory's ``_create_single_provider`` and the media registry's ``_single`` --
and each builds an object that owns no client, dials nothing, and answers every
call with the 503 that names the setting. Every layer above (a key pool, an
OAuth account pool, Zen's credential split) is built exactly as it always is,
over refusals.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
)
from my_claude_code.application.routing import ResolvedModel
from my_claude_code.config.media_surfaces import MEDIA_OPERATION_IMAGE_GENERATE
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers.media.leaf import MediaLeaf
from my_claude_code.providers.media.refusal import MediaRefusalNode
from my_claude_code.providers.media.registry import MediaRegistry
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.masked_refusal import MaskedRefusalProvider
from my_claude_code.providers.runtime.opencode_credentials import (
    OpenCodeCredentialSplitProvider,
)
from my_claude_code.providers.runtime.rotating import RotatingProvider

REQUEST = MessagesRequest.model_validate(
    {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
)


@pytest.fixture
def refuse(tmp_path, monkeypatch) -> Iterator:
    """Give ``provider_ids`` a chain with every entry paused, Direct fallback off."""

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(
        "my_claude_code.config.proxy_chains.proxy_chains_path", lambda: path
    )
    reset_proxy_chains_cache()

    def write(*provider_ids: str) -> None:
        chain = ProxyChain(
            enabled=True,
            entries=(ProxyChainEntry(proxy="px_one", paused=True),),
            direct_fallback=False,
        )
        save_proxy_chains(
            ProxyChains(
                proxies={"px_one": ProxyEndpoint(url="http://198.51.100.9:8080")},
                chains=dict.fromkeys(provider_ids, chain),
            ),
            path,
        )
        reset_proxy_chains_cache()

    yield write
    reset_proxy_chains_cache()


@pytest.fixture
def clients_built(monkeypatch) -> list[Any]:
    """Every ``httpx.AsyncClient`` constructed while the test runs."""

    built: list[Any] = []
    real = httpx.AsyncClient.__init__

    def counting(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        built.append(kwargs.get("proxy"))
        real(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting)
    return built


def _assert_refused(failure: ExecutionFailure) -> None:
    assert failure.kind is FailureKind.UNAVAILABLE
    assert failure.status_code == 503
    assert failure.retryable is False
    assert "Direct fallback is off" in failure.message
    assert "Proxying page" in failure.message


def _refused_everywhere(provider: Any) -> None:
    with pytest.raises(ExecutionFailure) as preflight:
        provider.preflight_stream(REQUEST)
    _assert_refused(preflight.value)

    async def listing() -> None:
        await provider.list_model_ids()

    with pytest.raises(ExecutionFailure) as listed:
        asyncio.run(listing())
    _assert_refused(listed.value)

    async def stream() -> None:
        async for _chunk in provider.stream_response(REQUEST):
            pass

    with pytest.raises(ExecutionFailure) as streamed:
        asyncio.run(stream())
    _assert_refused(streamed.value)


def test_a_refused_chain_is_built_as_a_refusal_with_no_client(
    refuse, clients_built
) -> None:
    refuse("nvidia_nim")

    provider = create_provider(
        "nvidia_nim", Settings.model_validate({"nvidia_nim_api_key": "nim-key"})
    )

    assert isinstance(provider, MaskedRefusalProvider)
    assert clients_built == []
    _refused_everywhere(provider)
    asyncio.run(provider.cleanup())


def test_every_key_of_a_pool_is_a_refusal_and_the_pool_is_unchanged(
    refuse, clients_built
) -> None:
    refuse("nvidia_nim")

    # The field's own name: ``nvidia_nim_api_key`` has no env alias.
    provider = create_provider(
        "nvidia_nim", Settings.model_validate({"nvidia_nim_api_key": "k-one,k-two"})
    )

    assert isinstance(provider, RotatingProvider)
    assert len(provider._providers) == 2
    assert all(isinstance(leaf, MaskedRefusalProvider) for leaf in provider._providers)
    assert clients_built == []
    with pytest.raises(ExecutionFailure) as preflight:
        provider.preflight_stream(REQUEST)
    _assert_refused(preflight.value)


def test_zen_s_credential_split_refuses_on_both_sides(refuse, clients_built) -> None:
    refuse("opencode")

    provider = create_provider(
        "opencode", Settings.model_validate({"OPENCODE_API_KEY": "sk-zen-key"})
    )

    assert isinstance(provider, OpenCodeCredentialSplitProvider)
    # The shared anonymous slot is a pool of one; the operator's one key is a
    # single provider. Both are refusals, and neither built a client.
    assert isinstance(provider.public, RotatingProvider)
    assert all(
        isinstance(leaf, MaskedRefusalProvider) for leaf in provider.public._providers
    )
    assert isinstance(provider.paid, MaskedRefusalProvider)
    assert clients_built == []


def test_a_provider_with_a_usable_chain_is_not_a_refusal(refuse) -> None:
    refuse("open_router")  # someone else's chain

    provider = create_provider(
        "nvidia_nim", Settings.model_validate({"nvidia_nim_api_key": "nim-key"})
    )

    assert not isinstance(provider, MaskedRefusalProvider)


# ------------------------------------------------------------------ media


def _image_attempt(provider_id: str) -> MediaAttempt:
    request = MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        rail=MediaRail.IMAGE,
        model=f"{provider_id}/image-model",
        body={"prompt": "a red kite"},
    )
    return MediaAttempt(
        request=request,
        resolved=ResolvedModel(
            original_model=request.model,
            provider_id=provider_id,
            provider_model="image-model",
            provider_model_ref=request.model,
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )


def test_media_builds_a_refusal_node_that_answers_as_a_leaf_would(
    refuse, clients_built
) -> None:
    settings = Settings.model_validate({"AGNES_API_KEY": "agnes-key"})
    attempt = _image_attempt("agnes")
    asked = (
        attempt.request,
        MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="agnes/image-model",
            body={"prompt": "a red kite"},
            stream=True,
        ),
        MediaRequest(operation="speech", rail=MediaRail.TTS, model="agnes/voice"),
    )
    leaf = MediaRegistry().resolve("agnes", settings)  # no chain yet: a leaf
    assert isinstance(leaf, MediaLeaf)
    built_for_the_leaf = len(clients_built)
    refuse("agnes")

    node = MediaRegistry().resolve("agnes", settings)

    assert isinstance(node, MediaRefusalNode)
    assert len(clients_built) == built_for_the_leaf  # the refusal built none
    # Declared surfaces decide "skipped, uncharged" exactly as the leaf's did.
    assert [node.supports(request) for request in asked] == [
        leaf.supports(request) for request in asked
    ]
    assert node.supports(attempt.request) is True
    with pytest.raises(ExecutionFailure) as preflight:
        node.preflight(attempt)
    _assert_refused(preflight.value)
    assert node.leaf_for(0, None) is None
    asyncio.run(leaf.cleanup())


def test_a_media_job_on_a_refused_chain_is_read_through_nothing(refuse) -> None:
    refuse("agnes")
    settings = Settings.model_validate({"AGNES_API_KEY": "agnes-key"})

    client = MediaRegistry().job_client(
        settings, "agnes", key_fingerprint=None, key_index=0, proxy_label=None
    )

    assert client is None
