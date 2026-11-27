"""Operator routing and parameter overrides for media models (8.61.0).

Dedicated store ``~/.mcc/media_overrides.json``, kept strictly separate from
``~/.mcc/media_request_fields.json`` (user decision Q6):

* ``media_request_fields.json`` is publish-only (shown in ``/v1/media/models``
  schemas and never changes a request or route);
* ``media_overrides.json`` holds live routing overrides (``preferred_surface``,
  ``surface``, ``pin_surface``) and parameter defaults/unsets per provider or
  per ``(provider/model, operation)``.

Document shape::

    {
      "providers": {
        "gemini": {
          "image_generate": {"preferred_surface": "gemini_generate_content_image"}
        }
      },
      "models": {
        "gemini/gemini-2.5-flash-image": {
          "image_generate": {
            "preferred_surface": "gemini_generate_content_image",
            "pin_surface": false,
            "param_defaults": {"aspect_ratio": "1:1"},
            "unset_params": ["response_format"]
          }
        }
      }
    }

Missing or unreadable files safely degrade to empty overrides.
Leaf module: ``config`` imports nothing first-party outside itself.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.media_surfaces import MEDIA_OPERATIONS
from my_claude_code.config.model_overrides import normalize_override_key
from my_claude_code.config.paths import media_overrides_path

PROVIDERS_KEY = "providers"
MODELS_KEY = "models"

_VALID_OPERATIONS: frozenset[str] = frozenset(MEDIA_OPERATIONS)
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SURFACE_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


@dataclass(frozen=True, slots=True)
class MediaOperationOverride:
    """Operator routing and parameter overrides for one media operation."""

    preferred_surface: str | None = None
    pin_surface: bool = False
    surface: str | None = None
    param_defaults: Mapping[str, Any] = field(default_factory=dict)
    unset_params: tuple[str, ...] = ()

    @property
    def pinned_surface(self) -> str | None:
        """The explicitly pinned surface key/shape, if any."""

        if self.surface is not None:
            return self.surface
        if self.pin_surface and self.preferred_surface is not None:
            return self.preferred_surface
        return None

    @property
    def is_empty(self) -> bool:
        """Whether this override states nothing at all."""

        return (
            self.preferred_surface is None
            and not self.pin_surface
            and self.surface is None
            and not self.param_defaults
            and not self.unset_params
        )

    def as_row(self) -> dict[str, Any]:
        """Render back to the on-disk JSON shape."""

        row: dict[str, Any] = {}
        if self.surface is not None:
            row["surface"] = self.surface
        if self.preferred_surface is not None:
            row["preferred_surface"] = self.preferred_surface
        if self.pin_surface or self.preferred_surface is not None:
            row["pin_surface"] = self.pin_surface
        if self.param_defaults:
            row["param_defaults"] = dict(self.param_defaults)
        if self.unset_params:
            row["unset_params"] = list(self.unset_params)
        return row


#: ``operation -> MediaOperationOverride``
type MediaOperationOverrides = Mapping[str, MediaOperationOverride]


@dataclass(frozen=True, slots=True)
class MediaOverridesDocument:
    """Resolved media overrides table, keyed by folded provider id and ref."""

    providers: Mapping[str, MediaOperationOverrides] = field(default_factory=dict)
    models: Mapping[str, MediaOperationOverrides] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether this document states no overrides at all."""

        return not self.providers and not self.models

    def for_provider(self, provider_id: str) -> MediaOperationOverrides:
        """Overrides keyed on one provider id; empty when none."""

        return self.providers.get(normalize_override_key(provider_id), {})

    def for_model(self, ref: str) -> MediaOperationOverrides:
        """Overrides keyed on one folded ``provider/model`` ref; empty when none."""

        return self.models.get(normalize_override_key(ref), {})

    def as_document(self) -> dict[str, Any]:
        """Render back to the on-disk JSON shape."""

        return {
            PROVIDERS_KEY: {
                provider_id: {
                    op: override.as_row() for op, override in operations.items()
                }
                for provider_id, operations in self.providers.items()
            },
            MODELS_KEY: {
                ref: {op: override.as_row() for op, override in operations.items()}
                for ref, operations in self.models.items()
            },
        }


EMPTY_MEDIA_OVERRIDES = MediaOverridesDocument()


def _clean_surface_name(raw: object, where: str, key: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning(
            "MEDIA OVERRIDES: '{}.{}' must be a surface string; ignoring it",
            where,
            key,
        )
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not _SURFACE_NAME.fullmatch(cleaned):
        logger.warning(
            "MEDIA OVERRIDES: '{}.{}' has invalid surface name {!r}; ignoring it",
            where,
            key,
            cleaned,
        )
        return None
    return cleaned


def _parse_param_defaults(raw: object, where: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        logger.warning(
            "MEDIA OVERRIDES: '{}.param_defaults' is not an object; ignoring it",
            where,
        )
        return {}
    defaults: dict[str, Any] = {}
    for raw_name, value in raw.items():
        name = str(raw_name).strip()
        if not _FIELD_NAME.fullmatch(name) or name == "model":
            logger.warning(
                "MEDIA OVERRIDES: '{}.param_defaults.{}' is not an allowed field name; ignoring it",
                where,
                name,
            )
            continue
        defaults[name] = value
    return defaults


def _parse_unset_params(raw: object, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
        logger.warning(
            "MEDIA OVERRIDES: '{}.unset_params' is not an array; ignoring it",
            where,
        )
        return ()
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            logger.warning(
                "MEDIA OVERRIDES: '{}.unset_params' entry {!r} is not a string; ignoring it",
                where,
                item,
            )
            continue
        name = item.strip()
        if not _FIELD_NAME.fullmatch(name) or name == "model":
            logger.warning(
                "MEDIA OVERRIDES: '{}.unset_params' entry {!r} is not a valid field name; ignoring it",
                where,
                item,
            )
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def _parse_operation_override(raw: object, where: str) -> MediaOperationOverride | None:
    if not isinstance(raw, Mapping):
        logger.warning("MEDIA OVERRIDES: '{}' is not an object; ignoring it", where)
        return None
    preferred = _clean_surface_name(
        raw.get("preferred_surface"), where, "preferred_surface"
    )
    surface = _clean_surface_name(raw.get("surface"), where, "surface")
    raw_pin = raw.get("pin_surface")
    if raw_pin is not None and not isinstance(raw_pin, bool):
        logger.warning(
            "MEDIA OVERRIDES: '{}.pin_surface' must be a boolean; ignoring it",
            where,
        )
        pin_surface = False
    else:
        pin_surface = bool(raw_pin)
    param_defaults = _parse_param_defaults(raw.get("param_defaults"), where)
    unset_params = _parse_unset_params(raw.get("unset_params"), where)
    override = MediaOperationOverride(
        preferred_surface=preferred,
        pin_surface=pin_surface,
        surface=surface,
        param_defaults=param_defaults,
        unset_params=unset_params,
    )
    return None if override.is_empty else override


def _parse_section(
    section: object,
    section_name: str,
    *,
    require_slash: bool,
) -> dict[str, dict[str, MediaOperationOverride]]:
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        logger.warning(
            "MEDIA OVERRIDES: '{}' is not an object; ignoring it", section_name
        )
        return {}
    parsed: dict[str, dict[str, MediaOperationOverride]] = {}
    for raw_key, raw_operations in section.items():
        key = normalize_override_key(str(raw_key))
        has_slash = "/" in key
        if not key or (require_slash and not has_slash):
            logger.warning(
                "MEDIA OVERRIDES: '{}.{}' is not a valid key; ignoring it",
                section_name,
                raw_key,
            )
            continue
        if not isinstance(raw_operations, Mapping):
            logger.warning(
                "MEDIA OVERRIDES: '{}.{}' is not an object; ignoring it",
                section_name,
                key,
            )
            continue
        ops: dict[str, MediaOperationOverride] = {}
        for raw_op, raw_override in raw_operations.items():
            op = str(raw_op).strip()
            if op not in _VALID_OPERATIONS:
                logger.warning(
                    "MEDIA OVERRIDES: '{}.{}.{}' is not a known media operation; ignoring it",
                    section_name,
                    key,
                    op,
                )
                continue
            override = _parse_operation_override(
                raw_override, f"{section_name}.{key}.{op}"
            )
            if override is not None:
                ops[op] = override
        if ops:
            parsed[key] = ops
    return parsed


def media_overrides_from_document(document: object) -> MediaOverridesDocument:
    """Build a :class:`MediaOverridesDocument` from a parsed JSON value."""

    if not isinstance(document, Mapping):
        logger.warning(
            "MEDIA OVERRIDES: top-level JSON value is not an object; ignoring it"
        )
        return EMPTY_MEDIA_OVERRIDES
    providers = _parse_section(
        document.get(PROVIDERS_KEY), PROVIDERS_KEY, require_slash=False
    )
    models = _parse_section(document.get(MODELS_KEY), MODELS_KEY, require_slash=True)
    if not providers and not models:
        return EMPTY_MEDIA_OVERRIDES
    return MediaOverridesDocument(providers=providers, models=models)


def load_media_overrides(path: Path | None = None) -> MediaOverridesDocument:
    """Read ``media_overrides.json``, degrading safely on missing/corrupt files."""

    resolved = path if path is not None else media_overrides_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_MEDIA_OVERRIDES
    except OSError as exc:
        logger.warning("MEDIA OVERRIDES: cannot read {}: {}", resolved, exc)
        return EMPTY_MEDIA_OVERRIDES
    if not raw.strip():
        return EMPTY_MEDIA_OVERRIDES
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("MEDIA OVERRIDES: cannot parse {}: {}", resolved, exc)
        return EMPTY_MEDIA_OVERRIDES
    return media_overrides_from_document(document)


def save_media_overrides(
    overrides: MediaOverridesDocument, path: Path | None = None
) -> None:
    """Write ``media_overrides.json`` atomically and drop the cache."""

    resolved = path if path is not None else media_overrides_path()
    write_json_document_atomically(resolved, overrides.as_document())
    reset_media_overrides_cache()


_CACHE_SIGNATURE: tuple[str, int, int] | None = None
_CACHED_OVERRIDES: MediaOverridesDocument = EMPTY_MEDIA_OVERRIDES


def reset_media_overrides_cache() -> None:
    """Forget the cached media overrides so the next read re-stats disk."""

    global _CACHE_SIGNATURE, _CACHED_OVERRIDES
    _CACHE_SIGNATURE = None
    _CACHED_OVERRIDES = EMPTY_MEDIA_OVERRIDES


def current_media_overrides(path: Path | None = None) -> MediaOverridesDocument:
    """Return cached media overrides, reloading only when the file changes."""

    global _CACHE_SIGNATURE, _CACHED_OVERRIDES
    resolved = path if path is not None else media_overrides_path()
    try:
        stat = resolved.stat()
    except OSError:
        reset_media_overrides_cache()
        return EMPTY_MEDIA_OVERRIDES
    signature = (str(resolved), stat.st_mtime_ns, stat.st_size)
    if signature != _CACHE_SIGNATURE:
        _CACHED_OVERRIDES = load_media_overrides(resolved)
        _CACHE_SIGNATURE = signature
    return _CACHED_OVERRIDES


def effective_media_override(
    doc: MediaOverridesDocument | None,
    target_ref: str,
    operation: str,
) -> MediaOperationOverride:
    """Merge provider-level and model-level overrides for ``(target_ref, operation)``.

    Model-level entries override provider-level entries per axis:

    * Surface routing (``surface`` / ``preferred_surface`` / ``pin_surface``):
      a model row that sets any surface field replaces the provider row's
      surface choice; a model row that only sets parameters inherits the
      provider row's surface choice.
    * ``param_defaults`` / ``unset_params``: model wins per parameter key, so a
      model's ``unset_params`` removes an inherited provider default and a
      model's ``param_defaults`` overrides an inherited provider unset.
    """

    if doc is None or doc.is_empty:
        return MediaOperationOverride()
    op = operation.strip()
    folded_ref = normalize_override_key(target_ref)
    provider_id, sep, _ = folded_ref.partition("/")
    provider_ov = doc.providers.get(provider_id, {}).get(op) if sep else None
    model_ov = doc.models.get(folded_ref, {}).get(op)
    if provider_ov is None and model_ov is None:
        return MediaOperationOverride()
    if provider_ov is None:
        assert model_ov is not None
        return model_ov
    if model_ov is None:
        return provider_ov

    model_sets_surface = (
        model_ov.surface is not None
        or model_ov.preferred_surface is not None
        or model_ov.pin_surface
    )
    if model_sets_surface:
        surface = model_ov.surface
        preferred = model_ov.preferred_surface
        pin_surface = model_ov.pin_surface
    else:
        surface = provider_ov.surface
        preferred = provider_ov.preferred_surface
        pin_surface = provider_ov.pin_surface

    merged_defaults = {
        key: value
        for key, value in provider_ov.param_defaults.items()
        if key not in model_ov.unset_params
    }
    merged_defaults.update(model_ov.param_defaults)
    merged_unset = tuple(
        dict.fromkeys(
            [
                name
                for name in provider_ov.unset_params
                if name not in model_ov.param_defaults
            ]
            + list(model_ov.unset_params)
        )
    )
    return MediaOperationOverride(
        preferred_surface=preferred,
        pin_surface=pin_surface,
        surface=surface,
        param_defaults=merged_defaults,
        unset_params=merged_unset,
    )


def with_media_override(
    doc: MediaOverridesDocument,
    target: str,
    operation: str,
    override: MediaOperationOverride | None,
) -> MediaOverridesDocument:
    """Return a copy of ``doc`` with one provider or model override set or cleared."""

    op = operation.strip()
    if op not in _VALID_OPERATIONS:
        raise ValueError(f"Unknown media operation: {operation!r}")
    folded = normalize_override_key(target)
    if not folded:
        raise ValueError("Override target must not be empty")
    providers = {key: dict(ops) for key, ops in doc.providers.items()}
    models = {key: dict(ops) for key, ops in doc.models.items()}
    bucket = models if "/" in folded else providers
    entry = dict(bucket.get(folded, {}))
    if override is None or override.is_empty:
        entry.pop(op, None)
    else:
        entry[op] = override
    if entry:
        bucket[folded] = entry
    else:
        bucket.pop(folded, None)
    return MediaOverridesDocument(providers=providers, models=models)


__all__ = [
    "EMPTY_MEDIA_OVERRIDES",
    "MODELS_KEY",
    "PROVIDERS_KEY",
    "MediaOperationOverride",
    "MediaOperationOverrides",
    "MediaOverridesDocument",
    "current_media_overrides",
    "effective_media_override",
    "load_media_overrides",
    "media_overrides_from_document",
    "reset_media_overrides_cache",
    "save_media_overrides",
    "with_media_override",
]
