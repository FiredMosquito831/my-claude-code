"""Managed env persistence, validation preview, and rendering.

The managed file records **choices**, never defaults. A field the user has
never touched is rendered as a commented placeholder (``# KEY=``) carrying the
code default, so the file says what was decided and nothing more. Consequences
of that contract, in the order they matter:

* Saving one field cannot write a value for any other field.
* A default that changes in a later release reaches every install that never
  overrode it, because there is no stored value in the way.
* Clearing a field removes its line, and the layer underneath (the repo
  ``.env``, else the code default) shows through again. The one exception is a
  key the repo ``.env`` also sets: only an explicit ``KEY=`` outranks it, and
  that is written only where the Settings type accepts a blank -- otherwise the
  line is dropped and the save reports a warning.
* A value equal to the default is still kept once it was chosen: an explicit
  choice has to survive a later change of that default.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from my_claude_code.config.paths import managed_env_path
from my_claude_code.config.settings import Settings

from .manifest import FIELD_BY_KEY, FIELDS, SECTIONS, ConfigFieldSpec
from .sources import dotenv_values_from_file, is_locked_source, repo_env_path
from .validation import settings_from_values
from .values import (
    MASKED_SECRET,
    blank_is_accepted,
    load_value_state,
    normalize_field_value,
    normalize_for_env,
    prune_paused_refs,
)


@dataclass(frozen=True, slots=True)
class PreparedAdminUpdate:
    """Validated Admin update ready for an atomic managed-file commit."""

    target_values: dict[str, str]
    settings: Settings | None
    errors: tuple[str, ...]
    pending_fields: tuple[str, ...]
    path: Path
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.settings is not None

    def validation_response(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "env_preview": render_env_file(
                self.target_values,
                mask_secrets=True,
                preserved=unmanaged_env_values(),
            ),
        }

    def applied_response(self) -> dict[str, Any]:
        if not self.valid:
            return self.validation_response() | {
                "applied": False,
                "pending_fields": [],
            }
        return {
            "applied": True,
            "valid": True,
            "errors": [],
            "warnings": list(self.warnings),
            "env_preview": render_env_file(
                self.target_values,
                mask_secrets=True,
                preserved=unmanaged_env_values(),
            ),
            "path": str(self.path),
            "pending_fields": list(self.pending_fields),
        }


def target_values_with_updates(
    updates: Mapping[str, Any],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return the managed env values after applying admin updates, and warnings.

    The result holds only keys somebody set: what the managed file already
    carried, plus what this save changed, minus what this save cleared. It
    starts from the file rather than from the template because the template
    carries defaults, and a default written into the file is indistinguishable
    from a choice for the rest of the file's life.
    """

    state = load_value_state()
    managed_values = dotenv_values_from_file(managed_env_path())
    values = {
        key: value for key, value in managed_values.items() if key in FIELD_BY_KEY
    }

    # First write on an install that configured itself through a repo .env:
    # those are real choices and are migrated into the managed file. Template
    # and code defaults are not choices, so they are deliberately not seeded.
    if not managed_values:
        for key, entry in state.items():
            if entry["source"] == "repo_env":
                values[key] = str(entry["value"])

    repo_values = dotenv_values_from_file(repo_env_path())
    warnings: list[str] = []
    for key, value in updates.items():
        field = FIELD_BY_KEY.get(key)
        if field is None:
            continue
        if is_locked_source(state[key]["source"]):
            continue
        if field.secret and value == MASKED_SECRET:
            continue
        normalized = normalize_field_value(field, value)
        if normalized == "" and not field.secret:
            _unset_field(field, values, repo_values, warnings)
            continue
        values[key] = normalized
    # A route's pause list is only meaningful next to the route it belongs to,
    # and this is the first point where both halves of every rail are known.
    prune_paused_refs(
        values,
        {key: str(entry["value"]) for key, entry in state.items()},
        updates.keys(),
    )
    return values, tuple(warnings)


def _unset_field(
    field: ConfigFieldSpec,
    values: dict[str, str],
    repo_values: Mapping[str, str],
    warnings: list[str],
) -> None:
    """Clear one field, choosing between removing the line and blanking it."""

    if field.key not in repo_values:
        # Nothing underneath to mask: dropping the line is the honest way to
        # say "unset", and it lets a future change of the default through.
        values.pop(field.key, None)
        return
    if blank_is_accepted(field):
        values[field.key] = ""
        return
    # A bool or an int with no blank validator cannot be written as ``KEY=``
    # without stopping the server from starting. Say so instead of pretending.
    values.pop(field.key, None)
    warnings.append(
        f"{field.key}: cannot be blanked while the repo .env sets it; remove it there"
    )


def effective_values_for_validation(
    target_values: Mapping[str, str],
) -> dict[str, str]:
    """Return values validated after preserving locked external sources."""

    values = dict(target_values)
    for key, entry in load_value_state().items():
        if is_locked_source(entry["source"]):
            values[key] = str(entry["value"])
    return values


def validate_updates(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Validate partial admin updates and return a masked generated env preview."""

    return prepare_admin_update(updates).validation_response()


def changed_pending_fields(
    updates: Mapping[str, Any],
    *,
    settings: Settings,
) -> list[str]:
    """Return changed fields that require manual runtime action."""

    state = load_value_state()
    pending: list[str] = []
    for key, value in updates.items():
        field = FIELD_BY_KEY.get(key)
        if field is None or is_locked_source(state[key]["source"]):
            continue
        if field.secret and value == MASKED_SECRET:
            continue
        requires_restart = field.restart_required or field.session_sensitive
        if not requires_restart:
            requires_restart = _active_voice_credential(settings) == key
        if not requires_restart:
            continue
        if normalize_for_env(value) == str(state[key]["value"]):
            continue
        pending.append(key)
    return pending


def _active_voice_credential(settings: Settings) -> str | None:
    if not settings.voice_note_enabled:
        return None
    if settings.whisper_device == "nvidia_nim":
        return "NVIDIA_NIM_API_KEY"
    return "HUGGINGFACE_API_KEY"


def prepare_admin_update(updates: Mapping[str, Any]) -> PreparedAdminUpdate:
    """Validate an update and construct its prospective Settings snapshot."""

    target_values, warnings = target_values_with_updates(updates)
    effective_values = effective_values_for_validation(target_values)
    settings, errors = settings_from_values(effective_values)
    pending_fields = (
        tuple(changed_pending_fields(updates, settings=settings))
        if settings is not None
        else ()
    )
    return PreparedAdminUpdate(
        target_values=target_values,
        settings=settings,
        errors=tuple(errors),
        pending_fields=pending_fields,
        path=managed_env_path(),
        warnings=warnings,
    )


def commit_prepared_admin_update(prepared: PreparedAdminUpdate) -> dict[str, Any]:
    """Atomically persist a previously validated Admin update."""

    if not prepared.valid:
        raise ValueError("Cannot commit an invalid Admin update")

    path = prepared.path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_text(
            render_env_file(
                prepared.target_values,
                preserved=unmanaged_env_values(path),
            ),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return prepared.applied_response()


def quote_env_value(value: str) -> str:
    """Quote a value when dotenv syntax requires it."""

    if value == "":
        return ""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if any(char.isspace() for char in value) or any(
        char in value for char in ('"', "#", "=", "$")
    ):
        return f'"{escaped}"'
    return value


# Project-owned env prefixes. Keys under these prefixes (e.g. MCC_SMOKE_* and
# the pre-6.40.0 FCC_SMOKE_*) are real, settable environment variables that
# belong to this project even when no admin field or Settings alias reads them
# -- developer tooling such as smoke/ reads them straight from os.environ. A
# populated value here is a deliberate user choice and must survive a save the
# same way an admin-managed field would; an empty value configures nothing and
# is safe to drop. Both prefixes are accepted so MCC_* keys do not silently
# stop being persisted the moment the canonical name is used.
_OWNED_ENV_PREFIXES = ("MCC_", "FCC_")


def settings_env_aliases() -> frozenset[str]:
    """Env names Settings still reads.

    A key that configures nothing is stale -- a provider option that became
    fixed, say -- and rewriting it forever would be its own kind of wrong. A key
    that Settings still reads but no admin field shows is the opposite: live
    configuration this UI simply does not offer, and deleting it loses a setting
    the user chose.
    """

    from pydantic import AliasChoices

    def _alias_key(name: str, field) -> str:
        alias = field.validation_alias
        if alias is None:
            return name.upper()
        # ``AliasChoices`` lists the canonical name first; key on that.
        if isinstance(alias, AliasChoices):
            return str(alias.choices[0]).upper()
        return str(alias)

    return frozenset(
        _alias_key(name, field) for name, field in Settings.model_fields.items()
    )


def unmanaged_env_values(path: Path | None = None) -> dict[str, str]:
    """Return entries in the managed env file that no admin field owns.

    The file is rendered from the manifest, so anything the manifest does not
    know about used to disappear the next time anyone pressed Save -- silently,
    and including hand-written operational settings. Reading them back means a
    save can only change what the UI actually showed.
    """

    existing = dotenv_values_from_file(path or managed_env_path())
    managed = {field.key for field in FIELDS}
    aliases = settings_env_aliases()
    return {
        key: value
        for key, value in existing.items()
        if key not in managed
        and value is not None
        and (key in aliases or (key.startswith(_OWNED_ENV_PREFIXES) and value != ""))
    }


def render_env_file(
    values: Mapping[str, str],
    *,
    mask_secrets: bool = False,
    preserved: Mapping[str, str] | None = None,
) -> str:
    """Render a complete grouped env file.

    A field present in ``values`` is written as ``KEY=value`` -- someone chose
    it. A field that is absent is written as a commented placeholder naming its
    default, so the file still documents every setting the dashboard offers
    without any of those lines being read as configuration. ``dotenv`` sees
    only the set keys, which is exactly what the contract promises.

    ``preserved`` carries entries this UI does not manage. They are written back
    verbatim so saving a form cannot delete a setting the form never showed.
    """

    lines: list[str] = [
        "# Managed by My Claude Code /admin.",
        "# Edit in the server UI when possible.",
        "",
    ]
    fields_by_section: dict[str, list[ConfigFieldSpec]] = {
        section.section_id: [] for section in SECTIONS
    }
    for field in FIELDS:
        fields_by_section.setdefault(field.section_id, []).append(field)

    for section in SECTIONS:
        lines.append(f"# {section.label}")
        for field in fields_by_section.get(section.section_id, []):
            if field.key not in values:
                lines.append(_placeholder_line(field))
                continue
            value = values[field.key]
            if mask_secrets and field.secret and value:
                value = MASKED_SECRET
            lines.append(f"{field.key}={quote_env_value(value)}")
        lines.append("")

    if preserved:
        lines.append("# Not shown in the admin UI, kept exactly as written.")
        lines.extend(
            f"{key}={quote_env_value(preserved[key])}" for key in sorted(preserved)
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _placeholder_line(field: ConfigFieldSpec) -> str:
    """Render an unset field as a comment that names what it falls back to."""

    if field.secret or not field.default:
        return f"# {field.key}="
    return f"# {field.key}= (default: {field.default})"
