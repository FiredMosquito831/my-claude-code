"""Admin config value state and API response assembly."""

import os
from collections.abc import Iterable, Mapping
from typing import Any

from my_claude_code.config.limits import LIMIT_RANGES
from my_claude_code.config.model_refs import (
    format_model_ref_list,
    parse_model_ref_list,
)
from my_claude_code.config.paths import managed_env_path
from my_claude_code.config.provider_catalog import PROVIDER_GROUPS
from my_claude_code.config.settings import BLANK_MEANS_UNSET_FIELDS

from .manifest import (
    FIELD_BY_KEY,
    FIELDS,
    SECTIONS,
    ConfigFieldSpec,
    ConfigOptionSpec,
)
from .sources import (
    configured_env_files,
    dotenv_values_from_file,
    explicit_env_path,
    is_locked_source,
    repo_env_path,
    template_values,
)
from .status import provider_config_status

MASKED_SECRET = "********"
ValueState = dict[str, dict[str, Any]]


def normalize_for_env(value: Any) -> str:
    """Normalize a submitted admin value for dotenv persistence."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def normalize_field_value(field: ConfigFieldSpec, value: Any) -> str:
    """Normalize a submitted admin value for its field type.

    ``Settings`` canonicalises a fallback chain when it loads it, so persisting
    the raw submission would leave the dashboard showing a value that differs
    from the one actually in effect. Normalizing on the way in keeps the stored
    value and the running value the same string.
    """

    normalized = normalize_for_env(value)
    if field.field_type == "model_chain":
        return format_model_ref_list(parse_model_ref_list(normalized))
    return normalized


# Field types whose value is a free string: an empty one is a legal value the
# Settings model already stores as "" or maps to None, so writing ``KEY=`` for
# them says "deliberately nothing" rather than "unparseable".
_BLANK_TOLERANT_FIELD_TYPES = frozenset(
    {"text", "secret", "model", "optional_model", "model_chain", "textarea"}
)


def blank_is_accepted(field: ConfigFieldSpec) -> bool:
    """Return whether ``KEY=`` is a value this field's Settings type accepts.

    Clearing a field normally means removing its line, so the layer underneath
    (the repo ``.env`` or the code default) shows through again. When the repo
    ``.env`` sets the key that is not enough -- only an explicit empty value in
    the managed file outranks it -- and an explicit empty value is only safe
    where a validator turns it back into the default. A plain ``bool`` or
    ``int`` has no such validator and would refuse to start the server.
    """

    if field.field_type in _BLANK_TOLERANT_FIELD_TYPES:
        return True
    attr = field.settings_attr
    if attr is None:
        return False
    return attr in LIMIT_RANGES or attr in BLANK_MEANS_UNSET_FIELDS


# One route: the setting holding its primary model, the setting holding its
# fallback chain, and the setting holding the refs paused on it. Pause is a
# property of a route rather than of a model -- the same ref paused on Opus
# keeps serving Sonnet -- so every rail on Model Config owns its own list.
ROUTE_PAUSE_KEYS: tuple[tuple[str, str, str], ...] = (
    ("MODEL", "MODEL_FALLBACKS", "MODEL_PAUSED"),
    ("MODEL_FABLE", "MODEL_FABLE_FALLBACKS", "MODEL_FABLE_PAUSED"),
    ("MODEL_OPUS", "MODEL_OPUS_FALLBACKS", "MODEL_OPUS_PAUSED"),
    ("MODEL_SONNET", "MODEL_SONNET_FALLBACKS", "MODEL_SONNET_PAUSED"),
    ("MODEL_HAIKU", "MODEL_HAIKU_FALLBACKS", "MODEL_HAIKU_PAUSED"),
    ("MODEL_VISION", "MODEL_VISION_FALLBACKS", "MODEL_VISION_PAUSED"),
)

PAUSE_KEY_FOR_ROUTE: dict[str, str] = {
    model_key: paused_key for model_key, _chain, paused_key in ROUTE_PAUSE_KEYS
}


def prune_paused_refs(
    values: dict[str, str],
    effective: Mapping[str, str] | None = None,
    touched: Iterable[str] = (),
) -> None:
    """Drop paused refs a route no longer names, in place.

    A pause list that outlives the ref it names grows stale forever: the row
    the user paused is gone from the rail, nothing on the page can unpause it,
    and re-adding that model later would silently bring back a switch the user
    never set this time. Pruning on write is the only moment both halves of
    the route are known, so it happens here rather than in the browser.

    ``values`` holds only what the *managed* file carries, and a route may
    legitimately be configured in the repo ``.env`` or the template instead.
    Judging a pause against the managed half alone would delete every pause on
    such an install the moment anything was saved, so ``effective`` supplies
    what is actually in force and ``values`` only overrides it.

    Only routes this save actually edited are pruned. Pruning every route on
    every write would make the check depend on how completely the value state
    can see a route -- and a route the dashboard cannot see the whole of would
    silently lose its pauses on an unrelated save.
    """

    edited = set(touched)
    base: dict[str, str] = dict(effective or {})
    base.update(values)
    for model_key, chain_key, paused_key in ROUTE_PAUSE_KEYS:
        raw = values.get(paused_key)
        if not raw:
            continue
        if model_key not in edited and chain_key not in edited:
            continue
        live = set(parse_model_ref_list(base.get(model_key, "")))
        live.update(parse_model_ref_list(base.get(chain_key, "")))
        kept = [ref for ref in parse_model_ref_list(raw) if ref in live]
        if kept:
            values[paused_key] = format_model_ref_list(tuple(kept))
        else:
            values.pop(paused_key, None)


def display_value(field: ConfigFieldSpec, value: str) -> str:
    """Return the Admin UI display value for a raw config value."""

    if field.secret and value:
        return MASKED_SECRET
    return value


def load_value_state() -> ValueState:
    """Load effective admin field values and their sources."""

    values = template_values()
    sources = {key: "template" if key in values else "default" for key in FIELD_BY_KEY}

    for source, path in configured_env_files():
        file_values = dotenv_values_from_file(path)
        for key, value in file_values.items():
            if key in FIELD_BY_KEY:
                values[key] = value
                sources[key] = source

    for key in FIELD_BY_KEY:
        if key in os.environ:
            values[key] = os.environ[key]
            sources[key] = "process"

    return {
        key: {
            "value": values.get(key, ""),
            "source": sources.get(key, "default"),
        }
        for key in FIELD_BY_KEY
    }


def load_config_response() -> dict[str, Any]:
    """Return manifest and current config values for the admin UI."""

    state = load_value_state()
    fields: list[dict[str, Any]] = []
    for field in FIELDS:
        entry = state[field.key]
        source = entry["source"]
        raw_value = entry["value"]
        fields.append(
            {
                "key": field.key,
                "label": field.label,
                "section": field.section_id,
                "provider": field.provider or None,
                "type": field.field_type,
                "value": display_value(field, raw_value),
                "effective": display_value(field, raw_value),
                "default": field.default,
                # A line in the managed file is the only thing that means "the
                # user chose this". Anything else is a value the code or the
                # template supplied, and offering to reset it would be noise.
                "set": source == "managed_env",
                "configured": bool(str(raw_value).strip()),
                "source": source,
                "locked": is_locked_source(source),
                "secret": field.secret,
                "advanced": field.advanced,
                "restart_required": field.restart_required,
                "session_sensitive": field.session_sensitive,
                "options": [
                    (
                        {"value": option.value, "label": option.label}
                        if isinstance(option, ConfigOptionSpec)
                        else {"value": option, "label": option}
                    )
                    for option in field.options
                ],
                "description": field.description,
                "minimum": field.minimum,
                "maximum": field.maximum,
                "range_hint": field.range_hint,
            }
        )

    return {
        "sections": [
            {
                "id": section.section_id,
                "label": section.label,
                "description": section.description,
                "advanced": section.advanced,
            }
            for section in SECTIONS
        ],
        "provider_groups": [
            {
                "id": group.group_id,
                "label": group.label,
                "description": group.description,
            }
            for group in PROVIDER_GROUPS
        ],
        "fields": fields,
        "paths": {
            "managed": str(managed_env_path()),
            "repo": str(repo_env_path()),
            "explicit": str(explicit_env_path()) if explicit_env_path() else None,
        },
        "provider_status": provider_config_status(state),
    }
