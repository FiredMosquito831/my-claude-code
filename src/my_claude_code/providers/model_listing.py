"""Provider model-list response parsing helpers."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from my_claude_code.application.model_metadata import (
    ModelDefaultParameters,
    ModelDefaultParameterValue,
    ModelReasoningCapability,
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

        model_infos.setdefault(
            model_id,
            _ProviderModelInfo(model_id=model_id, supports_thinking=supports_thinking),
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
                model_infos.setdefault(
                    alias,
                    _ProviderModelInfo(
                        model_id=alias, supports_thinking=supports_thinking
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
    """
    top_provider = _field(item, "top_provider")
    return _ProviderModelInfo(
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
        ),
    )


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
    reasoning: Any, *, supports_thinking: bool
) -> ModelReasoningCapability | None:
    """Map a gateway ``reasoning`` block onto the neutral capability record.

    ``None`` when the gateway publishes no block at all -- unknown. A block
    that omits ``supported_efforts`` yields a capability whose
    ``supported_efforts`` is ``None`` (known model, unknown vocabulary), which
    stays distinct from a published-but-empty vocabulary.
    """
    if not isinstance(reasoning, Mapping):
        return None
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
    return ModelReasoningCapability(
        can_reason=supports_thinking,
        supports_effort_control=supports_effort,
        supports_toggle_control=can_switch_off,
        supports_budget_control=_bool_or_none(reasoning.get("supports_max_tokens")),
        supported_efforts=supported_efforts,
        mandatory=mandatory,
        default_enabled=_bool_or_none(reasoning.get("default_enabled")),
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
