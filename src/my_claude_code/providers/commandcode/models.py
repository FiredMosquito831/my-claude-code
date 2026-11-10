"""Command Code model-catalog parsing and protocol routing."""

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


def is_anthropic_messages_model(model_id: str) -> bool:
    """Return whether Command Code documents this ID as a Claude model."""
    return model_id.strip().lower().startswith("claude-")


def parse_commandcode_model_list(
    payload: Any,
    *,
    provider_name: str,
) -> ModelListParse:
    """Parse Command Code's OpenAI-shaped model list with context metadata.

    A row this cannot read is skipped and recorded rather than raised (8.18.0):
    the same partial-probe rule the generic parser follows, so one odd row
    never discards the rest of the list.
    """
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_infos: set[ProviderModelInfo] = set()
    skipped: list[SkippedModelRow] = []
    # 7.85.0: Command Code's ``created`` is one stamp on every row today, so
    # this finds no date; the rule is the listing's, never the provider's.
    date_field = listing_date_field(data)
    for index, item in enumerate(data):
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            skipped.append(
                SkippedModelRow(
                    index=index,
                    model_id=None,
                    reason=f"{provider_name} model-list row has no id",
                )
            )
            continue
        context_length = _field(item, "context_length")
        if context_length is not None and (
            not isinstance(context_length, int)
            or isinstance(context_length, bool)
            or context_length <= 0
        ):
            skipped.append(
                SkippedModelRow(
                    index=index,
                    model_id=model_id,
                    reason=(
                        f"{provider_name} model-list row states a"
                        " context_length that is not a positive integer"
                    ),
                )
            )
            continue
        model_infos.add(
            # The same generic reader every listing parser calls (7.83.0): a
            # number or flag the row states beyond its own ``context_length``
            # fills the record at the provider rung.
            record_with_declared(
                ProviderModelInfo(
                    model_id=model_id,
                    context_length=context_length,
                    supported_parameters=published_parameters_from_row(item),
                    # ``supported_endpoints`` -- which of the three doors serve
                    # this model, in Command Code's own words. Recorded, never
                    # routed on: ``is_anthropic_messages_model`` still decides.
                    declared=declared_from_row(item, listing_date=date_field),
                    # 7.86.0: the row itself, for the "Everything known" view.
                    published_row=published_row_text(item),
                )
            )
        )

    if not model_infos:
        raise _malformed(provider_name, "response did not include any models")
    return model_list_parse(provider_name, model_infos, skipped, len(data))


def extract_commandcode_model_infos(
    payload: Any,
    *,
    provider_name: str,
) -> frozenset[ProviderModelInfo]:
    """The records Command Code's OpenAI-shaped model list yielded.

    The one-value form every caller has always got; since 8.18.0 an unreadable
    row is skipped and reported rather than raised, and
    :func:`parse_commandcode_model_list` is the same walk with the skips beside
    the records.
    """

    return parse_commandcode_model_list(payload, provider_name=provider_name).infos


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
