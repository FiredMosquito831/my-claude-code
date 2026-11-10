"""Anthropic Messages API model-catalog parsing."""

from collections.abc import Mapping, Sequence
from typing import Any

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.providers.model_listing import (
    ModelListParse,
    ModelListResponseError,
    SkippedModelRow,
    declared_from_row,
    listing_date_field,
    model_list_parse,
    published_parameters_from_row,
    published_row_text,
    record_with_declared,
)


def parse_anthropic_model_list(
    payload: Any,
    *,
    provider_name: str,
) -> ModelListParse:
    """Parse Anthropic's ``GET /v1/models`` page into model metadata.

    Anthropic reports ``id``, ``display_name`` and ``created_at`` per entry and
    publishes neither context length, pricing, nor per-model capability flags.
    Every optional field is therefore left unset rather than guessed: a ``None``
    reads as "not reported", which is what lets the models.dev enrichment fill
    it, and what keeps vision routing from diverting a model whose image
    support is merely unknown. The generic reader still runs on every row
    (7.83.0), so a number or flag the list does publish one day is kept at the
    provider rung exactly as every other list's is.
    """
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_infos: set[ProviderModelInfo] = set()
    skipped: list[SkippedModelRow] = []
    # 7.85.0: Anthropic's ``created_at`` is the day it released the model,
    # read as that model's publication date where the list's values vary.
    date_field = listing_date_field(data)
    for index, item in enumerate(data):
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            # 8.18.0: one unreadable row is skipped and recorded, never raised
            # -- the same partial-probe rule the generic parser follows.
            skipped.append(
                SkippedModelRow(
                    index=index,
                    model_id=None,
                    reason=f"{provider_name} model-list row has no id",
                )
            )
            continue
        # The same generic reader every listing parser calls: whatever this
        # row states about the model's kind is kept, as published, and any
        # number or flag it states fills the record at the provider rung.
        model_infos.add(
            record_with_declared(
                ProviderModelInfo(
                    model_id=model_id.strip(),
                    supported_parameters=published_parameters_from_row(item),
                    declared=declared_from_row(item, listing_date=date_field),
                    # 7.86.0: the row itself, for the "Everything known" view.
                    published_row=published_row_text(item),
                )
            )
        )

    if not model_infos:
        raise _malformed(provider_name, "response did not include any models")
    return model_list_parse(provider_name, model_infos, skipped, len(data))


def extract_anthropic_model_infos(
    payload: Any,
    *,
    provider_name: str,
) -> frozenset[ProviderModelInfo]:
    """The records Anthropic's ``GET /v1/models`` page yielded.

    The one-value form every caller has always got; since 8.18.0 an unreadable
    row is skipped and reported rather than raised, and
    :func:`parse_anthropic_model_list` is the same walk with the skips beside
    the records.
    """

    return parse_anthropic_model_list(payload, provider_name=provider_name).infos


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
