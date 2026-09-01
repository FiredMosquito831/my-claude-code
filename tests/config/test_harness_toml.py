"""The TOML writer Kimi Code's generated config goes through.

Two things have to hold and neither is obvious from the emitter's shape: what
it writes must be re-readable by ``tomllib`` (the parser Kimi's own
``tomlkit`` agrees with on every construct MCC emits), and the bytes must be
stable so an unchanged refresh does not rewrite the file.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

from my_claude_code.config.harness_toml import (
    kimi_base_url,
    toml_document_bytes,
    with_kimi_credentials,
    write_toml_document_atomically_if_changed,
)
from my_claude_code.config.harnesses import (
    KIMI_API_KEY_SENTINEL,
    KIMI_BASE_URL_SENTINEL,
)


def _provider(document: Mapping[str, object]) -> dict[str, object]:
    """Return the ``providers.mcc`` table, whatever the mapping's static type."""

    providers = document["providers"]
    assert isinstance(providers, Mapping)
    flat = {str(key): value for key, value in providers.items()}
    entry = flat["mcc"]
    assert isinstance(entry, Mapping)
    return {str(key): value for key, value in entry.items()}


DOCUMENT: dict[str, object] = {
    "default_thinking": True,
    "providers": {
        "mcc": {
            "type": "anthropic",
            "base_url": KIMI_BASE_URL_SENTINEL,
            "api_key": KIMI_API_KEY_SENTINEL,
        }
    },
    "models": {
        "mcc/openrouter/anthropic/claude-sonnet-4.5": {
            "provider": "mcc",
            "model": "openrouter/anthropic/claude-sonnet-4.5",
            "max_context_size": 200_000,
            "capabilities": ["image_in", "thinking"],
        }
    },
    "_mcc_defaulted": {"mcc/openrouter/silent": ["max_context_size"]},
}


def test_what_is_written_parses_back_to_what_was_asked_for() -> None:
    assert tomllib.loads(toml_document_bytes(DOCUMENT).decode("utf-8")) == DOCUMENT


def test_a_slashed_model_key_is_quoted_rather_than_written_bare() -> None:
    """A bare key with a slash is not TOML, and the failure is a parse error."""

    text = toml_document_bytes(DOCUMENT).decode("utf-8")

    assert '[models."mcc/openrouter/anthropic/claude-sonnet-4.5"]' in text
    assert "[providers.mcc]" in text


def test_a_scalar_never_lands_inside_a_table_that_followed_it() -> None:
    """Root scalars come first, or TOML silently reassigns them.

    ``default_thinking`` written after ``[providers.mcc]`` would parse as
    ``providers.mcc.default_thinking`` and nothing would report it.
    """

    text = toml_document_bytes(DOCUMENT).decode("utf-8")

    assert text.index("default_thinking = true") < text.index("[providers.mcc]")
    assert tomllib.loads(text)["default_thinking"] is True


def test_the_bytes_are_lf_terminated_and_stable() -> None:
    once = toml_document_bytes(DOCUMENT)

    assert once == toml_document_bytes(DOCUMENT)
    assert b"\r\n" not in once
    assert once.endswith(b"\n")


def test_an_unchanged_document_is_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "kimi-code-config.toml"

    assert write_toml_document_atomically_if_changed(path, DOCUMENT) is True
    stamp = path.stat().st_mtime_ns
    assert write_toml_document_atomically_if_changed(path, DOCUMENT) is False
    assert path.stat().st_mtime_ns == stamp
    assert not list(tmp_path.glob("*.fcc-tmp"))


def test_a_value_the_emitter_cannot_encode_is_refused_not_guessed() -> None:
    with pytest.raises(TypeError):
        toml_document_bytes({"models": {"x": {"when": object()}}})


def test_the_credentials_are_resolved_only_where_the_placeholders_stand() -> None:
    resolved = with_kimi_credentials(
        DOCUMENT, proxy_root_url="http://127.0.0.1:8199", api_key="secret-token"
    )

    assert _provider(resolved)["base_url"] == "http://127.0.0.1:8199"
    assert _provider(resolved)["api_key"] == "secret-token"
    # The source document is left alone: the serialiser's output is shared
    # with the dashboard route and the launcher's stderr summary.
    assert _provider(DOCUMENT)["api_key"] == KIMI_API_KEY_SENTINEL


def test_resolving_twice_changes_nothing_the_second_time() -> None:
    once = with_kimi_credentials(
        DOCUMENT, proxy_root_url="http://127.0.0.1:8199", api_key="secret-token"
    )
    twice = with_kimi_credentials(
        once, proxy_root_url="http://127.0.0.1:9999", api_key="other-token"
    )

    assert twice == once


def test_the_base_url_is_the_proxy_root_and_never_carries_v1() -> None:
    """Kimi's client is the *official* Anthropic SDK, which owns the ``/v1``.

    A base URL ending in ``/v1`` produced ``POST /v1/v1/messages`` and a 404
    against a real launch. Every other harness wants the opposite, because
    they go through Vercel's ``@ai-sdk/anthropic``, which appends a bare
    ``/messages``.
    """

    assert kimi_base_url("http://127.0.0.1:8082") == "http://127.0.0.1:8082"
    assert kimi_base_url("http://127.0.0.1:8082/") == "http://127.0.0.1:8082"
    assert kimi_base_url("http://127.0.0.1:8082/v1") == "http://127.0.0.1:8082"
    assert kimi_base_url("http://127.0.0.1:8082/v1/") == "http://127.0.0.1:8082"
