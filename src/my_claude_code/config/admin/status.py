"""Provider configuration status for the Admin UI."""

from collections.abc import Mapping
from typing import Any

from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.provider_catalog import (
    CUSTOM_PROVIDER_GROUP,
    PROVIDER_CATALOG,
)
from my_claude_code.config.provider_registry import get_provider_registry

from .manifest import FIELDS
from .provider_manifest import credential_env_owner


def _credential_sharers(credential_env: str, provider_id: str) -> list[dict[str, str]]:
    """Every *other* provider that draws on the same credential."""
    return [
        {"provider_id": other.provider_id, "display_name": other.display_name}
        for other in PROVIDER_CATALOG.values()
        if other.credential_env == credential_env and other.provider_id != provider_id
    ]


def provider_config_status(
    state: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return provider configuration status without making network calls."""
    statuses: list[dict[str, Any]] = []
    for provider_id, descriptor in PROVIDER_CATALOG.items():
        if descriptor.local:
            base_url = ""
            if descriptor.base_url_attr is not None:
                base_url = _value_for_settings_attr(state, descriptor.base_url_attr)
            statuses.append(
                {
                    "provider_id": provider_id,
                    "display_name": descriptor.display_name,
                    "group": descriptor.group,
                    "kind": "local",
                    "status": "missing_url" if not base_url.strip() else "unknown",
                    "label": "Missing URL" if not base_url.strip() else "Not checked",
                    "base_url": base_url or descriptor.default_base_url or "",
                }
            )
            continue

        # ``credential_env`` is optional on the descriptor because local
        # runtimes have none and Application Default Credential providers
        # (e.g. Vertex AI) have no key either. A remote provider with no
        # configuration attributes could not be configured at all.
        configuration_attrs = descriptor.configuration_attrs()
        missing_attrs = tuple(
            attr
            for attr in configuration_attrs
            if not str(_value_for_settings_attr(state, attr)).strip()
        )
        configured = not missing_attrs
        credential_env = descriptor.credential_env
        value = (
            str(state.get(credential_env, {}).get("value", ""))
            if credential_env is not None
            else ""
        )
        # Only one provider owns the editable field for a shared credential, so
        # every other provider on that key has to be told where it lives --
        # otherwise its card offers no way to add a key and looks broken.
        owner_id = credential_env_owner(credential_env) if credential_env else None
        owner = PROVIDER_CATALOG.get(owner_id) if owner_id else None
        statuses.append(
            {
                "provider_id": provider_id,
                "display_name": descriptor.display_name,
                "group": descriptor.group,
                "kind": "remote",
                "status": "configured" if configured else "missing_key",
                "label": "Configured" if configured else "Missing key",
                "credential_env": credential_env,
                "credential_owner_id": owner_id,
                "credential_owner_name": owner.display_name if owner else None,
                "credential_shared_with": (
                    _credential_sharers(credential_env, provider_id)
                    if credential_env
                    else []
                ),
                # How many keys are in the pool. Secret values are masked to a
                # constant before they reach the client, so the Admin UI cannot
                # derive this itself, and fetching it per provider would mean
                # one request per provider on every page load.
                "key_count": len(parse_credential_keys(value)),
            }
        )
    for entry in get_provider_registry().list_custom():
        if not entry.enabled:
            status, label = "disabled", "Disabled"
        elif entry.api_keys:
            status, label = "configured", "Configured"
        else:
            status, label = "missing_key", "Missing key"
        statuses.append(
            {
                "provider_id": entry.provider_id,
                "display_name": entry.display_name,
                "group": CUSTOM_PROVIDER_GROUP,
                "kind": "remote",
                "key_count": len(entry.api_keys),
                "status": status,
                "label": label,
                "custom": True,
                "base_url": entry.base_url,
                # The "Rotation, per pool" readout is built from whatever
                # declares a rotation policy. A custom pool rotates, benches
                # and cools down on exactly the same machinery as a static one
                # (B12); omitting this field was the only reason it never
                # appeared in the list.
                "credential_rotation": entry.credential_rotation,
            }
        )
    return statuses


def _value_for_settings_attr(
    state: Mapping[str, Mapping[str, Any]], settings_attr: str
) -> str:
    for field in FIELDS:
        if field.settings_attr == settings_attr:
            return str(state.get(field.key, {}).get("value", field.default))
    return ""
