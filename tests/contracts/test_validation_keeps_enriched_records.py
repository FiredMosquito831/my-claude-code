"""Startup validation keeps the enriched catalogue record (7.97.0).

The configured-model check runs behind readiness and caches the provider lists
it fetched. It cached them RAW -- straight from ``list_model_infos()`` -- over
the records a sweep had already enriched from models.dev through
``discovery.cache_enriched_model_infos``, the single cache-and-publish seam. So
for every provider named in the configuration, a model's window, output limit,
modalities and prices read "unknown" from the moment validation finished until
the next sweep.

Held the way the metadata-ladder equality contract holds the ladder
(``test_metadata_ladder_equality.py``): on its committed catalogue copy -- the
trimmed real listing rows of ten providers and models.dev trimmed to the rows
they reach -- every record the catalogue holds after the sweep is byte-for-byte
the record it holds after validation ran, for the configured providers and for
everyone else.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.model_refs import configured_chat_model_refs
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import BaseProvider
from my_claude_code.providers.runtime.discovery import cache_enriched_model_infos
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache
from my_claude_code.providers.runtime.validation import ConfiguredModelValidator
from tests.contracts.test_metadata_ladder_equality import (
    MODELS_DEV_PATH,
    ROWS_PATH,
    _document,
    _parse,
)

#: Two configured models on two providers whose committed rows models.dev
#: describes, so enrichment has something to fill.
CONFIGURED = {
    "MODEL": "nvidia_nim/moonshotai/kimi-k3",
    "MODEL_OPUS": "nous_portal/anthropic/claude-sonnet-5",
}


class _Listing:
    """A provider double that answers the raw listing, as a real one does."""

    def __init__(self, infos: Iterable[ProviderModelInfo]) -> None:
        self._infos = frozenset(infos)

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        return self._infos


def _rows() -> dict[str, dict[str, Any]]:
    return json.loads(ROWS_PATH.read_text(encoding="utf-8"))


def _records(cache: ProviderModelCache) -> str:
    """Every stored record, canonical JSON, provider by provider, in list order."""

    by_provider = cache.cached_model_infos_by_provider()
    return json.dumps(
        {
            provider_id: [_document(info) for info in infos]
            for provider_id, infos in sorted(by_provider.items())
        },
        sort_keys=True,
    )


def _settings() -> Settings:
    """Exactly the two configured refs: no tier, chain or key from any layer."""

    return Settings.model_construct(
        model=CONFIGURED["MODEL"],
        model_mythos=None,
        model_fable=None,
        model_opus=CONFIGURED["MODEL_OPUS"],
        model_sonnet=None,
        model_haiku=None,
        model_vision=None,
        model_fallbacks="",
        model_mythos_fallbacks="",
        model_fable_fallbacks="",
        model_opus_fallbacks="",
        model_sonnet_fallbacks="",
        model_haiku_fallbacks="",
        model_vision_fallbacks="",
        opencode_free_tier_credential="key",
        log_api_error_tracebacks=False,
    )


@pytest.mark.asyncio
async def test_validation_leaves_every_swept_record_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    write_models_dev_cache(json.loads(MODELS_DEV_PATH.read_text(encoding="utf-8")))
    rows = _rows()
    raw = {provider_id: _parse(provider_id, rows[provider_id]) for provider_id in rows}

    cache = ProviderModelCache(raw)
    for provider_id in sorted(raw):
        await cache_enriched_model_infos(
            provider_id, raw[provider_id], cache.cache_model_infos, quiet=True
        )
    swept = _records(cache)

    # Not vacuous: models.dev fills something the configured rows left empty.
    swept_records = cache.cached_model_infos_by_provider()
    filled = []
    for ref in CONFIGURED.values():
        provider_id, _, model_id = ref.partition("/")
        stored = next(i for i in swept_records[provider_id] if i.model_id == model_id)
        listed = next(i for i in raw[provider_id] if i.model_id == model_id)
        if _document(stored) != _document(listed):
            filled.append(ref)
    assert filled == list(CONFIGURED.values())

    settings = _settings()
    assert {ref.provider_id for ref in configured_chat_model_refs(settings)} == {
        "nvidia_nim",
        "nous_portal",
    }
    validator = ConfiguredModelValidator(
        settings,
        lambda provider_id: cast(BaseProvider, _Listing(raw[provider_id])),
        cache,
    )
    await validator.validate_configured_models()

    assert _records(cache) == swept
