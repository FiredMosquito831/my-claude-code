"""User-set request parameters, per provider and per model.

Since 5.61.0 the proxy invents nothing: every sampling field is unset unless a
client asked for it, because a "sensible" default that cannot be switched off
is an invented limit (WORKING-NOTES 53). That left no way for a user to say
"send ``top_p: 0.95`` to this one model, which pins it" -- the only remaining
lever was editing the code.

This module is that lever. ``~/.fcc/model_overrides.json``::

    {
      "providers": {"nvidia_nim": {"top_p": 0.95}},
      "models": {"nvidia_nim/moonshotai/kimi-k3": {"top_p": 0.95,
                                                   "temperature": null}}
    }

Three states per parameter, and the middle one is the point of the file:

============  =========================================================
key absent    inherit -- provider level, then nothing at all; the body
              is left exactly as the provider built it
value `null`  **force unset** -- remove the key from the body even if a
              provider postprocessor put it there
a value       force that value
============  =========================================================

Precedence is model over provider over untouched, decided per parameter rather
than per level: a ``null`` on the model beats a value on the provider, which is
the only way to say "this provider generally, except here".

**The allow-list is a security boundary, not a convenience.** Whatever lands in
these dicts is written straight into an upstream request body, so only the
parameters named in :data:`ALLOWED_OVERRIDE_PARAMETERS` are honoured and
everything else is dropped with a log line. A typo must not be able to inject
an arbitrary key.

Two families are excluded on purpose:

* **reasoning and thinking fields** belong to the reasoning pipeline, which
  resolves effort and budget against the model's declared capability and its
  answer allowance. A raw override there would silently defeat that whole
  chain, so those fields are not overridable here at all.
* **``max_tokens``** is owned by ``application.output_tokens``, which clamps it
  to the model's published output limit, falls back when nothing published one,
  applies the operator ceiling, and leaves room in the context window. A value
  forced into the body afterwards would skip all four -- and asking a
  16,384-output model for 100k is a guaranteed upstream 400, which is exactly
  what that module exists to prevent.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.paths import model_overrides_path

PROVIDERS_KEY = "providers"
MODELS_KEY = "models"

# Real request fields only. Adding to this list makes a parameter settable;
# nothing else about it has to change.
ALLOWED_OVERRIDE_PARAMETERS: frozenset[str] = frozenset(
    {
        "frequency_penalty",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "temperature",
        "top_k",
        "top_p",
    }
)

# Named rather than merely absent, so a user who sets one is told why it did
# nothing instead of watching it be ignored alongside their typos.
OWNED_ELSEWHERE_PARAMETERS: dict[str, str] = {
    "include_reasoning": "the reasoning pipeline owns thinking parameters",
    "max_completion_tokens": "application.output_tokens owns the output budget",
    "max_tokens": "application.output_tokens owns the output budget",
    "reasoning": "the reasoning pipeline owns thinking parameters",
    "reasoning_content": "the reasoning pipeline owns thinking parameters",
    "reasoning_effort": "the reasoning pipeline owns thinking parameters",
    "thinking": "the reasoning pipeline owns thinking parameters",
}

# A JSON object is the one shape rejected: every allowed parameter is a scalar
# or, for ``stop``, a list of strings, and an object here would mean the user is
# describing something this file does not model.
_ALLOWED_VALUE_TYPES = (bool, int, float, str, list)


def normalize_override_key(raw: str) -> str:
    """Fold one provider id or model ref into its lookup form."""

    return raw.strip().casefold()


def model_ref_for(provider_id: str, model: str) -> str:
    """Build the ``provider/model`` ref an override's ``models`` key names.

    Providers hand over the upstream model id, which for a gateway is already
    slash-separated (``moonshotai/kimi-k3``). The guard against re-prefixing an
    id that already carries its provider is defensive: a caller that passed a
    full ref would otherwise produce ``nvidia_nim/nvidia_nim/...`` and match
    nothing, which is the hardest kind of override bug to see.
    """

    model = (model or "").strip()
    provider_id = (provider_id or "").strip()
    if not provider_id:
        return model
    if not model:
        return provider_id
    prefix = f"{normalize_override_key(provider_id)}/"
    if normalize_override_key(model).startswith(prefix):
        return model
    return f"{provider_id}/{model}"


@dataclass(frozen=True, slots=True)
class ModelParameterOverrides:
    """Resolved override table, keyed by folded provider id and model ref."""

    providers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    models: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether this table can change any request at all."""

        return not self.providers and not self.models

    @classmethod
    def from_document(cls, document: object) -> Self:
        """Build a table from a parsed JSON document, ignoring what it cannot use."""

        if not isinstance(document, Mapping):
            logger.warning(
                "MODEL OVERRIDES: top-level JSON value is not an object; ignoring it"
            )
            return cls()
        return cls(
            providers=_parse_section(document.get(PROVIDERS_KEY), PROVIDERS_KEY),
            models=_parse_section(document.get(MODELS_KEY), MODELS_KEY),
        )

    def resolve(self, provider_id: str, model_ref: str) -> dict[str, Any]:
        """Return the parameters to force for one model, merged per parameter.

        A ``None`` value in the result means "force unset". An absent key means
        nothing was said, which is not the same thing.
        """

        resolved = dict(self.providers.get(normalize_override_key(provider_id), {}))
        resolved.update(self.models.get(normalize_override_key(model_ref), {}))
        return resolved

    def as_document(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Render back to the on-disk shape."""

        return {
            PROVIDERS_KEY: {key: dict(value) for key, value in self.providers.items()},
            MODELS_KEY: {key: dict(value) for key, value in self.models.items()},
        }


EMPTY_MODEL_OVERRIDES = ModelParameterOverrides()


def _parse_section(section: object, section_name: str) -> dict[str, dict[str, Any]]:
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        logger.warning(
            "MODEL OVERRIDES: '{}' is not an object; ignoring it", section_name
        )
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in section.items():
        key = normalize_override_key(str(raw_key))
        if not key:
            continue
        if not isinstance(raw_value, Mapping):
            logger.warning(
                "MODEL OVERRIDES: '{}.{}' is not an object; ignoring it",
                section_name,
                key,
            )
            continue
        parameters = _parse_parameters(raw_value, f"{section_name}.{key}")
        if parameters:
            parsed[key] = parameters
    return parsed


def _parse_parameters(raw: Mapping[Any, Any], where: str) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for raw_name, value in raw.items():
        name = str(raw_name).strip()
        if name in OWNED_ELSEWHERE_PARAMETERS:
            logger.warning(
                "MODEL OVERRIDES: '{}.{}' is not overridable here -- {}",
                where,
                name,
                OWNED_ELSEWHERE_PARAMETERS[name],
            )
            continue
        if name not in ALLOWED_OVERRIDE_PARAMETERS:
            logger.warning(
                "MODEL OVERRIDES: '{}.{}' is not a known request parameter; ignoring it",
                where,
                name,
            )
            continue
        if value is not None and not isinstance(value, _ALLOWED_VALUE_TYPES):
            logger.warning(
                "MODEL OVERRIDES: '{}.{}' has an unsupported value type {}; ignoring it",
                where,
                name,
                type(value).__name__,
            )
            continue
        parameters[name] = value
    return parameters


def load_model_overrides(path: Path | None = None) -> ModelParameterOverrides:
    """Read the override file, treating every failure as "no overrides".

    A malformed file must never stop the proxy from starting or from serving a
    request: the worst honest outcome is that the user's overrides do not apply
    and a log line says so.
    """

    resolved_path = path if path is not None else model_overrides_path()
    try:
        raw = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_MODEL_OVERRIDES
    except OSError as exc:
        logger.warning("MODEL OVERRIDES: cannot read {}: {}", resolved_path, exc)
        return EMPTY_MODEL_OVERRIDES

    if not raw.strip():
        return EMPTY_MODEL_OVERRIDES

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("MODEL OVERRIDES: cannot parse {}: {}", resolved_path, exc)
        return EMPTY_MODEL_OVERRIDES

    return ModelParameterOverrides.from_document(document)


def save_model_overrides(
    overrides: ModelParameterOverrides, path: Path | None = None
) -> None:
    """Write the override file atomically and drop the cache."""

    resolved_path = path if path is not None else model_overrides_path()
    write_json_document_atomically(resolved_path, overrides.as_document())
    reset_model_overrides_cache()


# (path, mtime_ns, size) -> parsed table. Overrides are read on the request
# path, so re-parsing a JSON file per request would be a real cost; keying the
# cache on the file's own stat means an edit is picked up without a restart.
_CACHE_SIGNATURE: tuple[str, int, int] | None = None
_CACHED_OVERRIDES: ModelParameterOverrides = EMPTY_MODEL_OVERRIDES


def reset_model_overrides_cache() -> None:
    """Forget the cached table, so the next read goes back to disk."""

    global _CACHE_SIGNATURE, _CACHED_OVERRIDES
    _CACHE_SIGNATURE = None
    _CACHED_OVERRIDES = EMPTY_MODEL_OVERRIDES


def current_model_overrides(path: Path | None = None) -> ModelParameterOverrides:
    """Return the override table, re-reading only when the file has changed."""

    global _CACHE_SIGNATURE, _CACHED_OVERRIDES
    resolved_path = path if path is not None else model_overrides_path()
    try:
        stat = resolved_path.stat()
    except OSError:
        reset_model_overrides_cache()
        return EMPTY_MODEL_OVERRIDES

    signature = (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    if signature != _CACHE_SIGNATURE:
        _CACHED_OVERRIDES = load_model_overrides(resolved_path)
        _CACHE_SIGNATURE = signature
    return _CACHED_OVERRIDES


def apply_model_parameter_overrides(
    body: dict[str, Any],
    *,
    provider_id: str,
    model_ref: str,
    overrides: ModelParameterOverrides,
) -> dict[str, Any]:
    """Force this model's overrides onto an already-built request body.

    Returns what was applied, so the caller can log it and a future admin page
    or request-log row can surface it. An empty return means the body was not
    touched at all -- which is the case for every request until somebody writes
    the file.
    """

    if overrides.is_empty or not provider_id:
        return {}
    applied: dict[str, Any] = {}
    for name, value in overrides.resolve(provider_id, model_ref).items():
        if value is None:
            # Only a key that was actually there counts as applied: forcing off
            # something nobody set changed nothing and should not read as an
            # override having fired.
            if name in body:
                del body[name]
                applied[name] = None
            continue
        body[name] = value
        applied[name] = value
    if applied:
        logger.debug(
            "MODEL OVERRIDES: applied {} to '{}'",
            dict(sorted(applied.items())),
            model_ref,
        )
    return applied
