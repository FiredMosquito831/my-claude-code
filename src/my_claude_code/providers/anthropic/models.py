"""Anthropic Messages API model-catalog parsing."""

from collections.abc import Mapping, Sequence
from typing import Any

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.providers.model_listing import (
    ModelListResponseError,
    declared_from_row,
)


def extract_anthropic_model_infos(
    payload: Any,
    *,
    provider_name: str,
) -> frozenset[ProviderModelInfo]:
    """Parse Anthropic's ``GET /v1/models`` page into model metadata.

    Anthropic reports ``id``, ``display_name`` and ``created_at`` per entry and
    publishes neither context length, pricing, nor per-model capability flags.
    Every optional field is therefore left unset rather than guessed: a ``None``
    reads as "not reported", which is what lets the models.dev enrichment fill
    it, and what keeps vision routing from diverting a model whose image
    support is merely unknown.
    """
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_infos: set[ProviderModelInfo] = set()
    for item in data:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(provider_name, "expected every data item to include id")
        # The same generic reader every listing parser calls: whatever this
        # row states about the model's kind is kept, as published.
        model_infos.add(
            ProviderModelInfo(
                model_id=model_id.strip(), declared=declared_from_row(item)
            )
        )

    if not model_infos:
        raise _malformed(provider_name, "response did not include any models")
    return frozenset(model_infos)


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def _malformed(provider_name: str, reason: str) -> ModelListResponseError:
    return ModelListResponseError(
        f"{provider_name} model-list response is malformed: {reason}"
    )
