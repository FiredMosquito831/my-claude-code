"""Provider model-list response parsing helpers."""

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ModelDefaultParameters,
    ModelDefaultParameterValue,
    ModelReasoningCapability,
    ProviderModelDeclaration,
)
from my_claude_code.application.model_metadata import (
    ProviderModelInfo as _ProviderModelInfo,
)
from my_claude_code.core.reasoning import EFFORT_BY_VALUE, ReasoningEffort

type ModelListScalar = str | bool
type RequiredPathValues = tuple[
    tuple[tuple[str, ...], tuple[ModelListScalar, ...]], ...
]


class ModelListResponseError(ValueError):
    """A provider model-list response cannot be parsed safely."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def model_infos_from_ids(
    model_ids: Iterable[str], *, supports_thinking: bool | None = None
) -> frozenset[_ProviderModelInfo]:
    """Build unknown-capability model metadata from plain provider model ids."""
    return frozenset(
        _ProviderModelInfo(model_id=model_id, supports_thinking=supports_thinking)
        for model_id in model_ids
        if model_id.strip()
    )


def extract_openai_model_infos(
    payload: Any,
    *,
    provider_name: str,
    collection_field: str | None = "data",
    id_field: str = "id",
    aliases_field: str | None = None,
    required_path_values: RequiredPathValues = (),
    required_null_field: str | None = None,
    required_sequence_items: tuple[tuple[str, str], ...] = (),
    exclude_missing_sequence_fields: bool = False,
    tags_field: str | None = None,
    thinking_tag: str = "reasoning",
    non_thinking_tag: str | None = None,
    thinking_boolean_path: tuple[str, ...] | None = None,
) -> frozenset[_ProviderModelInfo]:
    """Extract routable IDs from an OpenAI-compatible model-list response."""
    model_infos: dict[str, _ProviderModelInfo] = {}
    item_location = collection_field or "root-array"
    for item in model_list_items(
        payload,
        provider_name=provider_name,
        collection_field=collection_field,
    ):
        model_id = _field(item, id_field)
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(
                provider_name,
                f"expected every {item_location} item to include {id_field}",
            )
        included = True
        for path, allowed_values in required_path_values:
            path_value = _path(item, path)
            matching_types = tuple(
                allowed
                for allowed in allowed_values
                if type(path_value) is type(allowed)
            )
            if path_value is _MISSING or not matching_types:
                expected_types = "/".join(
                    dict.fromkeys(_scalar_type_name(value) for value in allowed_values)
                )
                raise _malformed(
                    provider_name,
                    f"expected every {item_location} item to include "
                    f"{'.'.join(path)} as {expected_types}",
                )
            if path_value not in matching_types:
                included = False

        if required_null_field is not None:
            if not _has_field(item, required_null_field):
                raise _malformed(
                    provider_name,
                    f"expected every {item_location} item to include "
                    f"{required_null_field}",
                )
            if _field(item, required_null_field) is not None:
                included = False

        missing_sequence_field = False
        for field_name, required_item in required_sequence_items:
            values = _field(item, field_name)
            if values is None and exclude_missing_sequence_fields:
                missing_sequence_field = True
                continue
            if not _is_sequence(values) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise _malformed(
                    provider_name,
                    f"expected every {item_location} item to include "
                    f"{field_name} string array",
                )
            if required_item not in values:
                included = False

        if missing_sequence_field:
            continue

        supports_thinking: bool | None = None
        if tags_field is not None:
            tags_value = _field(item, tags_field)
            if not _is_sequence(tags_value) or any(
                not isinstance(tag, str) or not tag.strip() for tag in tags_value
            ):
                raise _malformed(
                    provider_name,
                    f"expected every {item_location} item to include "
                    f"{tags_field} string array",
                )
            tags = frozenset(tags_value)
            if thinking_tag in tags:
                supports_thinking = True
            elif non_thinking_tag is not None and non_thinking_tag in tags:
                supports_thinking = False

        if thinking_boolean_path is not None:
            capability = _path(item, thinking_boolean_path)
            if capability is not _MISSING:
                if not isinstance(capability, bool):
                    raise _malformed(
                        provider_name,
                        f"expected {'.'.join(thinking_boolean_path)} to be boolean",
                    )
                supports_thinking = capability

        if not included:
            continue

        declared = declared_from_row(item)
        supported_parameters = published_parameters_from_row(item)
        model_infos.setdefault(
            model_id,
            record_with_declared(
                _ProviderModelInfo(
                    model_id=model_id,
                    supports_thinking=supports_thinking,
                    supported_parameters=supported_parameters,
                    declared=declared,
                )
            ),
        )
        if aliases_field is not None:
            aliases = _field(item, aliases_field)
            if not _is_sequence(aliases):
                raise _malformed(
                    provider_name,
                    f"expected every {item_location} item to include "
                    f"{aliases_field} array",
                )
            for alias in aliases:
                if not isinstance(alias, str) or not alias.strip():
                    raise _malformed(
                        provider_name,
                        f"expected every {aliases_field} item to be a model id",
                    )
                # An alias is the same row under another name, so it states
                # what its row states.
                model_infos.setdefault(
                    alias,
                    record_with_declared(
                        _ProviderModelInfo(
                            model_id=alias,
                            supports_thinking=supports_thinking,
                            supported_parameters=supported_parameters,
                            declared=declared,
                        )
                    ),
                )

    if not model_infos:
        raise _malformed(provider_name, "response did not include any model ids")
    return frozenset(model_infos.values())


def extract_tool_capable_model_infos(
    payload: Any, *, provider_name: str
) -> frozenset[_ProviderModelInfo]:
    """Extract tool-capable models with ``supported_parameters`` metadata."""
    data = model_list_items(payload, provider_name=provider_name)

    model_infos: set[_ProviderModelInfo] = set()
    for item in data:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(provider_name, "expected every data item to include id")

        supported_parameters = _field(item, "supported_parameters")
        if not _is_sequence(supported_parameters):
            continue
        supported_parameter_names = {
            param for param in supported_parameters if isinstance(param, str)
        }
        if supported_parameter_names.isdisjoint({"tools", "tool_choice"}):
            continue
        model_infos.add(
            _openrouter_dialect_model_info(
                item,
                model_id=model_id,
                supported_parameter_names=supported_parameter_names,
                read_vision=False,
            )
        )

    return frozenset(model_infos)


def model_list_items(
    payload: Any,
    *,
    provider_name: str,
    collection_field: str | None = "data",
) -> tuple[Any, ...]:
    """Return a validated OpenAI-shaped model-list data array."""
    data = payload if collection_field is None else _field(payload, collection_field)
    if not _is_sequence(data):
        location = (
            "root array"
            if collection_field is None
            else (f"top-level {collection_field} array")
        )
        raise _malformed(
            provider_name,
            f"expected {location}",
        )
    return tuple(data)


def validate_model_list_page(
    payload: Any,
    *,
    provider_name: str,
    expected_page: int,
    current_page_path: tuple[str, ...],
    total_pages_path: tuple[str, ...],
    max_pages: int,
    expected_total_pages: int | None = None,
) -> int:
    """Validate numbered pagination metadata and return the total page count."""
    current_page = _path(payload, current_page_path)
    if type(current_page) is not int:
        raise _malformed(
            provider_name,
            f"expected {'.'.join(current_page_path)} to be an integer",
        )
    if current_page != expected_page:
        raise _malformed(
            provider_name,
            f"expected {'.'.join(current_page_path)} to be {expected_page}",
        )

    total_pages = _path(payload, total_pages_path)
    if type(total_pages) is not int:
        raise _malformed(
            provider_name,
            f"expected {'.'.join(total_pages_path)} to be an integer",
        )
    if total_pages < 1 or total_pages > max_pages:
        raise _malformed(
            provider_name,
            f"expected {'.'.join(total_pages_path)} between 1 and {max_pages}",
        )
    if expected_total_pages is not None and total_pages != expected_total_pages:
        raise _malformed(
            provider_name,
            f"expected {'.'.join(total_pages_path)} to remain {expected_total_pages}",
        )
    return total_pages


def merge_model_list_pages(
    payloads: Iterable[Any],
    *,
    provider_name: str,
    collection_field: str | None,
) -> tuple[Any, ...] | dict[str, tuple[Any, ...]]:
    """Combine complete model-list pages before strict record parsing."""
    merged: list[Any] = []
    for payload in payloads:
        merged.extend(
            model_list_items(
                payload,
                provider_name=provider_name,
                collection_field=collection_field,
            )
        )

    items = tuple(merged)
    if collection_field is None:
        return items
    return {collection_field: items}


def extract_openai_model_ids(payload: Any, *, provider_name: str) -> frozenset[str]:
    """Extract model ids from an OpenAI-compatible ``/models`` response."""
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_ids: set[str] = set()
    for item in data:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(provider_name, "expected every data item to include id")
        model_ids.add(model_id)

    if not model_ids:
        raise _malformed(provider_name, "response did not include any model ids")
    return frozenset(model_ids)


def extract_openrouter_tool_model_ids(
    payload: Any, *, provider_name: str
) -> frozenset[str]:
    """Extract OpenRouter model ids that advertise tool-use support."""
    return frozenset(
        info.model_id
        for info in extract_openrouter_tool_model_infos(
            payload, provider_name=provider_name
        )
    )


def extract_openrouter_tool_model_infos(
    payload: Any, *, provider_name: str
) -> frozenset[_ProviderModelInfo]:
    """Extract OpenRouter tool-capable model ids with thinking capability metadata."""
    data = _field(payload, "data")
    if not _is_sequence(data):
        raise _malformed(provider_name, "expected top-level data array")

    model_infos: set[_ProviderModelInfo] = set()
    for item in data:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise _malformed(provider_name, "expected every data item to include id")

        supported_parameters = _field(item, "supported_parameters")
        if not _is_sequence(supported_parameters):
            continue
        supported_parameter_names = {
            param for param in supported_parameters if isinstance(param, str)
        }
        if supported_parameter_names.isdisjoint({"tools", "tool_choice"}):
            continue
        model_infos.add(
            _openrouter_dialect_model_info(
                item,
                model_id=model_id,
                supported_parameter_names=supported_parameter_names,
                read_vision=True,
            )
        )

    return frozenset(model_infos)


def openrouter_row_model_info(item: Any) -> _ProviderModelInfo | None:
    """One OpenRouter-dialect row, read exactly as the provider reads its own (7.84.0).

    The same record :func:`extract_openrouter_tool_model_infos` builds for a
    row, minus its tool-capable filter: OpenRouter's live list as a ladder rung
    describes every model it serves, and a model that takes no tools is still
    a model whose modalities, window and prices it states. ``None`` for a row
    with no usable id, and for a row this cannot read at all -- never raises,
    because one odd row in a 665-row list is not worth losing the other 664.
    A row that publishes no ``supported_parameters`` list is read with an
    empty one, so its record's thinking flag is ``False``; a caller that must
    not read that silence as "no" checks :func:`published_parameters_from_row`.
    """

    try:
        model_id = _field(item, "id")
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        supported = _field(item, "supported_parameters")
        names = (
            {value for value in supported if isinstance(value, str)}
            if _is_sequence(supported)
            else set()
        )
        return _openrouter_dialect_model_info(
            item,
            model_id=model_id,
            supported_parameter_names=names,
            read_vision=True,
        )
    except Exception:
        return None


def listed_price_per_million(value: Any) -> float | None:
    """One OpenRouter-dialect listed price in USD per million tokens (7.84.0).

    The unit rule every listed price on the record already follows (a decimal
    STRING is USD per token, a JSON number is USD per million), for the rates
    the record has no field for -- ``pricing.input_cache_read``,
    ``input_cache_write``, ``internal_reasoning``. Never raises; a negative or
    unreadable value states nothing and ``0`` is a stated free price.
    """

    try:
        return _price(value, "by_type")
    except Exception:
        return None


# The OpenRouter dialect publishes "none" inside ``reasoning.supported_efforts``
# alongside real effort levels. It is not an effort level -- it is the gateway
# saying reasoning can be switched off -- so it is deliberately never mapped
# onto a ``ReasoningEffort`` member: there is none, and inventing one would
# make "think as little as possible" and "do not think at all" the same
# request. It is read as a toggle signal instead; see ``_openrouter_reasoning``.
_REASONING_OFF_EFFORT = "none"


def _openrouter_dialect_model_info(
    item: Any,
    *,
    model_id: str,
    supported_parameter_names: set[str],
    read_vision: bool,
) -> _ProviderModelInfo:
    """Build one model record from an OpenRouter-dialect ``/models`` entry.

    Every field a gateway does not publish stays ``None``. A thin payload that
    carries only ``context_length`` therefore yields ``None`` -- unknown -- for
    the rest, never ``False``.

    The dialect's own readings below are byte-identical to every release before
    7.83.0. What they leave unset -- today the two listed prices, which this
    dialect publishes as ``pricing.prompt``/``pricing.completion`` -- is filled
    from the generic reader's statement of the same row.
    """
    top_provider = _field(item, "top_provider")
    record = _ProviderModelInfo(
        model_id=model_id,
        supports_thinking="reasoning" in supported_parameter_names,
        supports_vision=_openrouter_accepts_images(item) if read_vision else None,
        # ``top_provider.context_length`` is the routed deployment's own window
        # and is the more specific of the two; the top-level value is the
        # model's nominal one. Prefer the specific, fall back to the nominal.
        context_length=(
            _positive_int_or_none(_field(top_provider, "context_length"))
            or _positive_int_or_none(_field(item, "context_length"))
        ),
        max_output_tokens=_positive_int_or_none(
            _field(top_provider, "max_completion_tokens")
        ),
        supported_parameters=frozenset(supported_parameter_names),
        default_parameters=_default_parameters(_field(item, "default_parameters")),
        reasoning_capability=_openrouter_reasoning(
            _field(item, "reasoning"),
            supports_thinking="reasoning" in supported_parameter_names,
            supported_parameter_names=supported_parameter_names,
        ),
        declared=declared_from_row(item),
    )
    return record_with_declared(record)


def _positive_int_or_none(value: Any) -> int | None:
    """Read a positive integer; anything else -- including 0 -- is unreported.

    A limit of zero is never a real limit. Upstream feeds publish it for models
    the field does not apply to, so it must read as absent rather than as a
    ceiling that would forbid all output.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _default_parameters(value: Any) -> ModelDefaultParameters | None:
    """Read a gateway's declared per-model default parameters.

    ``None`` when the block is absent or not an object; an empty tuple when the
    gateway published an empty object, which is its statement that it pins
    nothing.
    """
    if not isinstance(value, Mapping):
        return None
    pinned: list[tuple[str, ModelDefaultParameterValue]] = []
    for name, pinned_value in value.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(pinned_value, bool | int | float | str):
            pinned.append((name, pinned_value))
    return tuple(sorted(pinned, key=lambda entry: entry[0]))


def _openrouter_reasoning(
    reasoning: Any,
    *,
    supports_thinking: bool,
    supported_parameter_names: set[str] | None = None,
) -> ModelReasoningCapability | None:
    """Map a gateway ``reasoning`` block onto the neutral capability record.

    A block that omits ``supported_efforts`` yields a capability whose
    ``supported_efforts`` is ``None`` (known model, unknown vocabulary), which
    stays distinct from a published-but-empty vocabulary.

    With no block at all, the gateway's own ``supported_parameters`` list is
    still a statement about this model and is read as one. It was parsed and
    stored already and only ever consulted for ``supports_thinking``, which is
    how ``nous_portal`` publishing ``reasoning_effort`` for ``tencent/hy3:free``
    and not for ``meituan/longcat-2.0:free`` -- one gateway, two dialects, per
    model -- went unnoticed by everything downstream. ``None`` comes back only
    when the gateway says nothing about reasoning at all.
    """
    if not isinstance(reasoning, Mapping):
        return _reasoning_from_supported_parameters(supported_parameter_names)
    mandatory = _bool_or_none(reasoning.get("mandatory"))
    raw_efforts = reasoning.get("supported_efforts")
    supported_efforts: frozenset[ReasoningEffort] | None = None
    supports_effort: bool | None = None
    can_switch_off: bool | None = None
    if _is_sequence(raw_efforts):
        published = {value for value in raw_efforts if isinstance(value, str)}
        supported_efforts = frozenset(
            EFFORT_BY_VALUE[value] for value in published if value in EFFORT_BY_VALUE
        )
        supports_effort = bool(supported_efforts)
        if _REASONING_OFF_EFFORT in published:
            can_switch_off = True
    # ``mandatory`` is the gateway's own statement about whether thinking can be
    # turned off, so it settles the toggle question in both directions; a
    # published "none" effort can only ever confirm it.
    if mandatory is not None and not can_switch_off:
        can_switch_off = not mandatory
    if (
        supports_effort is None
        and supported_parameter_names is not None
        and "reasoning_effort" in supported_parameter_names
    ):
        # The block is silent about an effort knob and the parameter list is
        # not. A published block wins every field it states; this one does not
        # state this field.
        supports_effort = True
    return ModelReasoningCapability(
        can_reason=supports_thinking,
        supports_effort_control=supports_effort,
        supports_toggle_control=can_switch_off,
        supports_budget_control=_bool_or_none(reasoning.get("supports_max_tokens")),
        supported_efforts=supported_efforts,
        mandatory=mandatory,
        default_enabled=_bool_or_none(reasoning.get("default_enabled")),
    )


def _reasoning_from_supported_parameters(
    names: set[str] | None,
) -> ModelReasoningCapability | None:
    """Read capability off a ``supported_parameters`` list with no block beside it.

    A field name is a weaker statement than a ``reasoning`` block and says
    nothing about a vocabulary, so only the flags it genuinely implies are set
    and ``supported_efforts`` stays unknown. ``can_reason`` is ``True`` rather
    than the plain ``"reasoning" in names``: a gateway that parses
    ``reasoning_effort`` is a gateway that reasons, and reporting ``False``
    there would suppress reasoning outright on exactly the models this is meant
    to enable.
    """
    if not names:
        return None
    lists_effort = "reasoning_effort" in names
    lists_reasoning = "reasoning" in names
    if not (lists_effort or lists_reasoning):
        return None
    return ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True if lists_effort else None,
        # OpenRouter's ``reasoning`` object carries both ``enabled`` and
        # ``max_tokens``, so listing it states two channels at once.
        supports_toggle_control=True if lists_reasoning else None,
        supports_budget_control=True if lists_reasoning else None,
    )


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _openrouter_accepts_images(item: Any) -> bool | None:
    """Read image support from an OpenRouter-dialect ``architecture`` block.

    A gateway that omits the block entirely tells us nothing, so the answer is
    unknown rather than False — reporting False would divert requests away from
    a model that may well handle them.
    """
    architecture = _field(item, "architecture")
    if architecture is None:
        return None
    modalities = _field(architecture, "input_modalities")
    if not _is_sequence(modalities):
        return None
    return any(
        isinstance(modality, str) and modality.strip().lower() == "image"
        for modality in modalities
    )


#: Where a ``/models`` row states what a model accepts and produces, as pairs
#: of paths read together, first pair that states wins. Data, not branches:
#: the reader reads whatever shape a provider publishes and never asks which
#: provider it is.
#:
#: - ``architecture.input_modalities``/``output_modalities``: the OpenRouter
#:   dialect (OpenRouter, Nous Portal, Kilo);
#: - ``input_modalities``/``output_modalities``: Novita, chutes, zenmux, xAI;
#: - ``modalities.input``/``modalities.output``: Vercel's AI Gateway.
_MODALITY_PAIR_PATHS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("architecture", "input_modalities"), ("architecture", "output_modalities")),
    (("input_modalities",), ("output_modalities",)),
    (("modalities", "input"), ("modalities", "output")),
)
#: The OpenRouter dialect's one-string summary, ``"text+image->text"``. Read
#: only when no row path above states a list pair, because the lists are the
#: richer statement and the string is their abbreviation.
_MODALITY_SUMMARY_PATH: tuple[str, ...] = ("architecture", "modality")
_MODALITY_SUMMARY_ARROW = "->"
#: A provider's one-word model type: Novita ``model_type``, Vercel and
#: together ``type``, deepinfra ``reported_type``. First that states wins.
_MODEL_TYPE_PATHS: tuple[tuple[str, ...], ...] = (
    ("model_type",),
    ("type",),
    ("reported_type",),
)
#: The endpoints a provider says serve a model: new-api's
#: ``supported_endpoint_types``, Command Code's and LiteLLM proxies'
#: ``supported_endpoints``, Novita's ``endpoints``. First that states wins.
_ENDPOINT_PATHS: tuple[tuple[str, ...], ...] = (
    ("supported_endpoint_types",),
    ("supported_endpoints",),
    ("endpoints",),
)

# -- 7.83.0: the row's own numbers and flags, for fields the record carries --
#
# Each table is read in order and the first path that states a usable value
# wins, exactly as the three tables above. A value in a shape this cannot read
# -- a string where a number belongs, zero, a negative -- states nothing and
# the next path is tried, as a ``None`` rung defers on the ladder.

#: The routed deployment's own context window. ``top_provider.context_length``
#: first because the OpenRouter dialect's parser already prefers it to the
#: nominal top-level value, so the two can never disagree about one row.
#:
#: - ``top_provider.context_length``, ``context_length``: the OpenRouter
#:   dialect (OpenRouter, Nous Portal, Kilo); Command Code;
#: - ``context_window``: HyperCharm, Vercel's AI Gateway, Groq;
#: - ``context_size``: Novita;
#: - ``max_context_length``: Mistral (its documented field; no keyless copy).
_CONTEXT_LENGTH_PATHS: tuple[tuple[str, ...], ...] = (
    ("top_provider", "context_length"),
    ("context_length",),
    ("context_window",),
    ("context_size",),
    ("max_context_length",),
)
#: The deployment's own ceiling on generated tokens.
#:
#: - ``top_provider.max_completion_tokens``: the OpenRouter dialect;
#: - ``max_output_tokens``: Novita, HyperCharm;
#: - ``max_completion_tokens``: Groq;
#: - ``max_tokens``: Vercel's AI Gateway, which publishes it beside
#:   ``context_window`` as the generation limit.
_OUTPUT_LIMIT_PATHS: tuple[tuple[str, ...], ...] = (
    ("top_provider", "max_completion_tokens"),
    ("max_output_tokens",),
    ("max_completion_tokens",),
    ("max_tokens",),
)

#: How a price path's value is denominated.
#:
#: ``"by_type"`` follows the two conventions the lists actually use under the
#: same names: a decimal written as a STRING is USD per token (OpenRouter's
#: published convention, which Nous Portal, Kilo and Vercel's AI Gateway copy:
#: ``"0.0000027"``), and one written as a JSON NUMBER is USD per million tokens
#: (HyperCharm: ``1.31``). ``"per_million"`` is a path whose own name says so,
#: whatever the type (Novita's ``price_per_m_decimal``: ``"0.15"``).
type PriceUnit = Literal["by_type", "per_million"]

#: Where a row lists its price per uncached input token, and per output token:
#: (input path, output path, unit). Each half is read on its own, first path
#: that states wins, because the record holds two separate prices and the
#: ladder fills each one separately.
#:
#: - ``pricing.prompt``/``completion``: the OpenRouter dialect (strings);
#: - ``pricing.prompt.price_per_m_decimal``: Novita, whose ``pricing.prompt``
#:   is an object rather than a value (the integer ``input_token_price_per_m``
#:   beside it is in ten-thousandths of a dollar and reads ``0`` on the rows that
#:   carry no ``pricing`` object at all -- two of them billed by tier -- so it is
#:   deliberately not read: that ``0`` is not a free price);
#: - ``pricing.input``/``output``: HyperCharm (numbers), Vercel (strings).
_PRICE_PATHS: tuple[tuple[tuple[str, ...], tuple[str, ...], PriceUnit], ...] = (
    (("pricing", "prompt"), ("pricing", "completion"), "by_type"),
    (
        ("pricing", "prompt", "price_per_m_decimal"),
        ("pricing", "completion", "price_per_m_decimal"),
        "per_million",
    ),
    (("pricing", "input"), ("pricing", "output"), "by_type"),
)
_PER_MILLION = Decimal(1_000_000)

#: Lists of capability words a row may publish: Novita ``features``
#: (``function-calling``, ``reasoning``, ``structured-outputs``), chutes
#: ``supported_features`` (``tools``, ``reasoning``), Vercel and deepinfra
#: ``tags`` (``tool-use``, ``reasoning``; deepinfra also ``non-reasoning``).
_CAPABILITY_WORD_PATHS: tuple[tuple[str, ...], ...] = (
    ("features",),
    ("supported_features",),
    ("tags",),
)
#: Words that state the model reasons, and the one word that states it does
#: not. A word list states nothing by leaving a word out: tag lists are not
#: exhaustive, and reading an absence as "no" would suppress reasoning on a
#: model whose provider simply did not tag it.
_REASONING_WORDS = frozenset({"reasoning"})
_NON_REASONING_WORDS = frozenset({"non-reasoning"})
#: Words that state the model takes tool calls.
_TOOL_CALL_WORDS = frozenset(
    {
        "function-calling",
        "function_calling",
        "tool-use",
        "tool_use",
        "tool-calling",
        "tool_calling",
        "tools",
    }
)
#: Explicit booleans, which state either answer: featherless
#: ``capabilities.reasoning`` (the path its profile already reads), Mistral
#: ``capabilities.function_calling`` (its documented field; no keyless copy).
_REASONING_BOOLEAN_PATHS: tuple[tuple[str, ...], ...] = (("capabilities", "reasoning"),)
_TOOL_CALL_BOOLEAN_PATHS: tuple[tuple[str, ...], ...] = (
    ("capabilities", "function_calling"),
)
#: A ``reasoning`` object states the model reasons only when it lists an
#: effort level or makes thinking mandatory: OpenRouter publishes
#: ``{"mandatory": false}`` alone for models that do not reason at all.
#: ``effort_levels``: HyperCharm; ``supported_efforts``: the OpenRouter dialect.
_REASONING_EFFORT_LIST_FIELDS: tuple[str, ...] = ("effort_levels", "supported_efforts")
#: Request parameters whose presence in a row's ``supported_parameters`` means
#: the gateway parses a reasoning control -- the same two names
#: ``_reasoning_from_supported_parameters`` reads for the OpenRouter dialect.
_REASONING_PARAMETERS = frozenset({"reasoning", "reasoning_effort"})


def declared_from_row(item: Any) -> ProviderModelDeclaration | None:
    """What one ``/models`` row says the model is, or ``None`` if it says nothing.

    Never raises. Every path here is optional -- no profile requires it and no
    parser has ever read it -- so a row that publishes one of them in a shape
    this cannot read has simply not stated it, and discovery goes on exactly
    as it did before this reader existed. A malformed path defers to the next
    path that states the same thing, as a ``None`` rung defers on the ladder.

    The words are kept as published: lower-cased, de-duplicated, sorted, and
    otherwise untouched. Mapping them onto kinds is the job of whoever reads
    them, never of the parser that recorded them.

    Since 7.83.0 the same row's own numbers and flags are read too -- context
    window, output limit, the two listed prices (converted to the record's
    USD per million), reasoning and tool-call support -- under the same
    never-raise rule. :func:`record_with_declared` is what puts them on the
    record at the provider rung.
    """

    try:
        modalities = _declared_modalities(item)
        model_type = _first_stated(item, _MODEL_TYPE_PATHS, _word)
        endpoints = _first_stated(item, _ENDPOINT_PATHS, _words)
        context_length = _first_stated(
            item, _CONTEXT_LENGTH_PATHS, _positive_int_or_none
        )
        max_output_tokens = _first_stated(
            item, _OUTPUT_LIMIT_PATHS, _positive_int_or_none
        )
        input_price = _listed_price(item, input_half=True)
        output_price = _listed_price(item, input_half=False)
        reasoning = _declared_reasoning(item)
        tool_calls = _declared_tool_calls(item)
    except Exception:
        # Belt and braces for a payload object whose attribute access itself
        # misbehaves: an optional field is never worth a failed sweep.
        return None
    declaration = ProviderModelDeclaration(
        modalities=modalities,
        model_type=model_type,
        endpoints=endpoints,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        input_price=input_price,
        output_price=output_price,
        reasoning=reasoning,
        tool_calls=tool_calls,
    )
    if declaration == _NOTHING_DECLARED:
        return None
    return declaration


#: A row that stated nothing the reader keeps; such a row records ``None``.
_NOTHING_DECLARED = ProviderModelDeclaration()


def record_with_declared(info: _ProviderModelInfo) -> _ProviderModelInfo:
    """The provider rung, completed from what the record's own row stated (7.83.0).

    Each listing parser reads its dialect's own fields first and leaves this to
    fill only what that left unset (``None``): the row's context window,
    output limit and two listed prices into the fields of the same name, and
    its reasoning statement into ``supports_thinking``. Nothing a parser set is
    replaced -- an OpenRouter-dialect row's thinking flag stays the one its
    ``supported_parameters`` list states -- and a row that states nothing
    returns the record unchanged, the very same object.

    This is what makes a provider's own number rung 1 of the ladder: every
    lookup reads these record fields before it consults models.dev, and the
    discovery-time models.dev fill only ever writes a field still ``None``.
    """

    declared = info.declared
    if declared is None:
        return info
    filled = replace(
        info,
        supports_thinking=_first_known(info.supports_thinking, declared.reasoning),
        context_length=_first_known(info.context_length, declared.context_length),
        input_price=_first_known(info.input_price, declared.input_price),
        output_price=_first_known(info.output_price, declared.output_price),
        max_output_tokens=_first_known(
            info.max_output_tokens, declared.max_output_tokens
        ),
    )
    return info if filled == info else filled


def _first_known[T](own: T | None, declared: T | None) -> T | None:
    return declared if own is None else own


def published_parameters_from_row(item: Any) -> frozenset[str] | None:
    """A row's own ``supported_parameters`` list, or ``None`` (7.83.0).

    The OpenRouter dialect has always kept this list; a generic row that
    publishes the same field (Vercel's AI Gateway does, on 276 of 412 rows)
    now keeps it too, read exactly as the dialect reads it: every string entry,
    others skipped. ``None`` when the row has no list -- never an empty set,
    which would claim the gateway parses nothing. Never raises.
    """

    try:
        values = _field(item, "supported_parameters")
        if not _is_sequence(values):
            return None
        return frozenset(value for value in values if isinstance(value, str))
    except Exception:
        return None


def _listed_price(item: Any, *, input_half: bool) -> float | None:
    for input_path, output_path, unit in _PRICE_PATHS:
        price = _price(
            _stated_path(item, input_path if input_half else output_path), unit
        )
        if price is not None:
            return price
    return None


def _price(value: Any, unit: PriceUnit) -> float | None:
    """One listed price in USD per million tokens, or ``None``.

    Negative values state nothing (Kilo lists ``"-1"`` for a router whose
    price depends on the model it picks); zero is a stated free price. A
    string is converted through :class:`~decimal.Decimal` so ``"0.0000027"``
    becomes exactly ``2.7``, not ``2.6999999999999997``.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation:
            return None
        if not amount.is_finite() or amount < 0:
            return None
        if unit == "by_type":
            amount *= _PER_MILLION
        return _finite(float(amount))
    if isinstance(value, int | float):
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            return None
        return _finite(float(value))
    return None


def _finite(price: float) -> float | None:
    """A price too large to be a float (``"1e999"``) states nothing."""

    return price if math.isfinite(price) else None


def _declared_reasoning(item: Any) -> bool | None:
    stated = _first_stated(item, _REASONING_BOOLEAN_PATHS, _boolean)
    if stated is not None:
        return stated
    words = _capability_words(item)
    if words is not None:
        if words & _NON_REASONING_WORDS:
            return False
        if words & _REASONING_WORDS:
            return True
    if _reasoning_object_reasons(_field(item, "reasoning")):
        return True
    options = _field(item, "reasoning_options")
    if _is_sequence(options) and len(options) > 0:
        # Vercel's ``reasoning_options``: one entry per control the model takes.
        return True
    parameters = published_parameters_from_row(item)
    if parameters is not None and parameters & _REASONING_PARAMETERS:
        return True
    return None


def _declared_tool_calls(item: Any) -> bool | None:
    stated = _first_stated(item, _TOOL_CALL_BOOLEAN_PATHS, _boolean)
    if stated is not None:
        return stated
    words = _capability_words(item)
    if words is not None and words & _TOOL_CALL_WORDS:
        return True
    return None


def _capability_words(item: Any) -> frozenset[str] | None:
    """Every capability word the row publishes, across all its word lists."""

    found: set[str] = set()
    stated = False
    for path in _CAPABILITY_WORD_PATHS:
        words = _words(_stated_path(item, path))
        if words is not None:
            stated = True
            found.update(words)
    return frozenset(found) if stated else None


def _reasoning_object_reasons(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("mandatory") is True:
        return True
    return any(
        _is_sequence(value.get(name)) and len(value.get(name)) > 0
        for name in _REASONING_EFFORT_LIST_FIELDS
    )


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _declared_modalities(item: Any) -> DeclaredModalities | None:
    for input_path, output_path in _MODALITY_PAIR_PATHS:
        inputs = _words(_stated_path(item, input_path))
        outputs = _words(_stated_path(item, output_path))
        if inputs is not None and outputs is not None:
            return DeclaredModalities(inputs=inputs, outputs=outputs)
    return _modalities_from_summary(_stated_path(item, _MODALITY_SUMMARY_PATH))


def _modalities_from_summary(value: Any) -> DeclaredModalities | None:
    """``"text+image->text"`` as a pair, or ``None`` for anything else."""

    if not isinstance(value, str) or value.count(_MODALITY_SUMMARY_ARROW) != 1:
        return None
    left, right = value.split(_MODALITY_SUMMARY_ARROW)
    inputs = _words(left.split("+"))
    outputs = _words(right.split("+"))
    if inputs is None or outputs is None:
        return None
    return DeclaredModalities(inputs=inputs, outputs=outputs)


def _first_stated[T](
    item: Any,
    paths: tuple[tuple[str, ...], ...],
    read: Callable[[Any], T | None],
) -> T | None:
    for path in paths:
        value = read(_stated_path(item, path))
        if value is not None:
            return value
    return None


def _stated_path(item: Any, path: tuple[str, ...]) -> Any:
    current = item
    for name in path:
        current = _field(current, name)
        if current is None:
            return None
    return current


def _word(value: Any) -> str | None:
    """One published word, or ``None`` when it is not a non-empty string."""

    if not isinstance(value, str):
        return None
    word = value.strip().lower()
    return word or None


def _words(value: Any) -> tuple[str, ...] | None:
    """A published word list, or ``None`` unless every entry is a word.

    An empty list states nothing -- Nous publishes ``[]`` for models it has
    not described -- and a list with one entry that is not a word is a list
    this cannot vouch for, so neither is half a statement.
    """

    if not _is_sequence(value):
        return None
    words: set[str] = set()
    for entry in value:
        word = _word(entry)
        if word is None:
            return None
        words.add(word)
    return tuple(sorted(words)) if words else None


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _has_field(item: Any, name: str) -> bool:
    if isinstance(item, Mapping):
        return name in item
    return hasattr(item, name)


_MISSING = object()


def _path(item: Any, path: tuple[str, ...]) -> Any:
    current = item
    for name in path:
        if not _has_field(current, name):
            return _MISSING
        current = _field(current, name)
    return current


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )


def _scalar_type_name(value: ModelListScalar) -> str:
    return type(value).__name__


def _malformed(provider_name: str, reason: str) -> ModelListResponseError:
    return ModelListResponseError(
        f"{provider_name} model-list response is malformed: {reason}"
    )
