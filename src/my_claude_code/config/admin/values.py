"""Admin config value state and API response assembly."""

import os
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
