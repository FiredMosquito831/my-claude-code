"""Per-coding-agent tier overrides: ``~/.fcc/harness_tiers.json``.

The five tier aliases (``core/tier_refs``) point at the global Claude routes by
default, so ``mcc/medium`` from OpenCode and ``claude-sonnet-5`` from Claude Code
resolve to the same ``MODEL_SONNET`` chain. This file is the one lever that
breaks that tie: "Best means *this* in Crush, and the global thing everywhere
else".

::

    {
      "harnesses": {
        "opencode": {
          "best":  {"model": "open_router/x-ai/grok-5",
                    "fallbacks": ["nous_portal/tencent/hy3"],
                    "paused": []},
          "cheap": {"fallbacks": ["open_router/z/cheap-1"]}
        }
      }
    }

**Pointer semantics, three states per (harness, tier)**, the same shape
``model_overrides.json`` documents and for the same reason -- the middle one is
the point of the file:

==========================  =================================================
harness or tier key absent  **inherit** -- resolve the global tier chain. The
                            default for every harness and every tier.
entry present, no `model`   inherit the global primary, but honour this
                            harness's own ``fallbacks`` and ``paused``
``model`` set               this harness's own chain leads
==========================  =================================================

A JSON document rather than settings fields, deliberately. Thirteen harnesses
carry a catalogue; 13 x 5 tiers x 3 keys is 195 new ``Settings`` fields, each
needing its own ``ConfigFieldSpec`` and its own consumer-contract hop. That is
not a manifest, it is a second product. ``~/.fcc/model_overrides.json`` and
``~/.fcc/rtk.json`` already set the precedent for "structured per-key config
that is not an env var", and the dashboard rule is "settable on the dashboard",
which the Coding agents card's Tiers section satisfies directly.

**A tier may never point at a tier.** Any ref in the ``mcc/`` namespace is
dropped with a log line: an override resolving to ``mcc/best`` would either loop
or resolve through a provider id that does not exist, and neither failure is
visible from the file that caused it.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.constants import MODEL_TIER_NAMES, TIER_NAMESPACE
from my_claude_code.config.harnesses import harness_ids
from my_claude_code.config.paths import harness_tiers_path

HARNESSES_KEY = "harnesses"
MODEL_KEY = "model"
FALLBACKS_KEY = "fallbacks"
PAUSED_KEY = "paused"


@dataclass(frozen=True, slots=True)
class HarnessTierOverride:
    """What one coding agent says about one tier.

    ``model`` is ``None`` for the middle state: "keep the global primary, but
    these are my fallbacks". An entry where all three are empty is kept rather
    than dropped, because the dashboard writes one the moment the operator
    presses Override and before they have typed anything -- and a store that
    silently forgot it would make the toggle bounce back.
    """

    model: str | None = None
    fallbacks: tuple[str, ...] = ()
    paused: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether this entry changes nothing about the global chain."""

        return self.model is None and not self.fallbacks and not self.paused

    def as_document(self) -> dict[str, Any]:
        """Render back to the on-disk shape, omitting what was never said."""

        document: dict[str, Any] = {}
        if self.model is not None:
            document[MODEL_KEY] = self.model
        if self.fallbacks:
            document[FALLBACKS_KEY] = list(self.fallbacks)
        if self.paused:
            document[PAUSED_KEY] = list(self.paused)
        return document


EMPTY_OVERRIDE = HarnessTierOverride()


@dataclass(frozen=True, slots=True)
class HarnessTiers:
    """Every per-harness tier override this install has been given."""

    harnesses: Mapping[str, Mapping[str, HarnessTierOverride]] = field(
        default_factory=dict
    )

    @property
    def is_empty(self) -> bool:
        """Whether this table can change any tier on any harness."""

        return not self.harnesses

    @classmethod
    def from_document(cls, document: object) -> Self:
        """Build a table from a parsed JSON document, ignoring what it cannot use."""

        if not isinstance(document, Mapping):
            logger.warning(
                "HARNESS TIERS: top-level JSON value is not an object; ignoring it"
            )
            return cls()
        section = document.get(HARNESSES_KEY)
        if section is None:
            return cls()
        if not isinstance(section, Mapping):
            logger.warning(
                "HARNESS TIERS: '{}' is not an object; ignoring it", HARNESSES_KEY
            )
            return cls()
        known = set(harness_ids())
        parsed: dict[str, dict[str, HarnessTierOverride]] = {}
        for raw_id, raw_tiers in section.items():
            harness_id = str(raw_id).strip().lower()
            if harness_id not in known:
                # The ``ALLOWED_OVERRIDE_PARAMETERS`` pattern: a typo must be
                # told about, not silently honoured against nothing.
                logger.warning(
                    "HARNESS TIERS: '{}' is not a known coding agent; ignoring it",
                    harness_id,
                )
                continue
            tiers = _parse_tiers(raw_tiers, harness_id)
            if tiers:
                parsed[harness_id] = tiers
        return cls(harnesses=parsed)

    def for_harness(self, harness_id: str | None) -> Mapping[str, HarnessTierOverride]:
        """Return every tier this harness overrides, or an empty mapping."""

        if not harness_id:
            return {}
        return self.harnesses.get(harness_id.strip().lower(), {})

    def override(self, harness_id: str | None, tier: str) -> HarnessTierOverride | None:
        """Return one ``(harness, tier)`` entry, or ``None`` for "inherit".

        ``tier`` is the tier's own name rather than ``core.tier_refs.ModelTier``
        because ``config`` is a leaf package that may not import ``core``; a
        ``StrEnum`` member passes through unchanged, so callers on the far side
        of that boundary need no conversion.
        """

        return self.for_harness(harness_id).get(str(tier))

    def with_override(
        self,
        harness_id: str,
        tier: str,
        override: HarnessTierOverride | None,
    ) -> HarnessTiers:
        """Return a copy with one entry replaced, or removed when ``None``.

        Removal is how "revert to global" is expressed: an entry present but
        empty means "override with nothing said yet", which is a different
        state from "this harness follows the global chain".
        """

        harnesses = {key: dict(value) for key, value in self.harnesses.items()}
        tiers = harnesses.setdefault(harness_id, {})
        if override is None:
            tiers.pop(str(tier), None)
        else:
            tiers[str(tier)] = override
        if not tiers:
            harnesses.pop(harness_id, None)
        return HarnessTiers(harnesses=harnesses)

    def as_document(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Render back to the on-disk shape."""

        return {
            HARNESSES_KEY: {
                harness_id: {
                    tier: override.as_document() for tier, override in tiers.items()
                }
                for harness_id, tiers in self.harnesses.items()
            }
        }


EMPTY_HARNESS_TIERS = HarnessTiers()


def _parse_tiers(raw: object, harness_id: str) -> dict[str, HarnessTierOverride]:
    if not isinstance(raw, Mapping):
        logger.warning("HARNESS TIERS: '{}' is not an object; ignoring it", harness_id)
        return {}
    parsed: dict[str, HarnessTierOverride] = {}
    for raw_tier, raw_entry in raw.items():
        tier_name = str(raw_tier).strip().lower()
        if tier_name not in MODEL_TIER_NAMES:
            logger.warning(
                "HARNESS TIERS: '{}.{}' is not a known tier; ignoring it",
                harness_id,
                tier_name,
            )
            continue
        entry = _parse_entry(raw_entry, f"{harness_id}.{tier_name}")
        if entry is not None:
            parsed[tier_name] = entry
    return parsed


def _parse_entry(raw: object, where: str) -> HarnessTierOverride | None:
    if not isinstance(raw, Mapping):
        logger.warning("HARNESS TIERS: '{}' is not an object; ignoring it", where)
        return None
    model = _clean_ref(raw.get(MODEL_KEY), f"{where}.{MODEL_KEY}")
    return HarnessTierOverride(
        model=model,
        fallbacks=_clean_refs(raw.get(FALLBACKS_KEY), f"{where}.{FALLBACKS_KEY}"),
        paused=_clean_refs(raw.get(PAUSED_KEY), f"{where}.{PAUSED_KEY}"),
    )


def _clean_ref(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning("HARNESS TIERS: '{}' is not a string; ignoring it", where)
        return None
    ref = value.strip()
    if not ref:
        return None
    if not is_valid_tier_override_ref(ref):
        logger.warning(
            "HARNESS TIERS: '{}' is not a usable provider/model ref ({!r}); "
            "ignoring it",
            where,
            ref,
        )
        return None
    return ref


def _clean_refs(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        candidates: Iterable[object] = [
            part for part in value.split(",") if part.strip()
        ]
    elif isinstance(value, list):
        candidates = value
    else:
        logger.warning("HARNESS TIERS: '{}' is not a list; ignoring it", where)
        return ()
    refs: list[str] = []
    for candidate in candidates:
        ref = _clean_ref(candidate, where)
        if ref is not None and ref not in refs:
            refs.append(ref)
    return tuple(refs)


def is_valid_tier_override_ref(ref: str) -> bool:
    """Whether a ref may be stored as a tier's model or chain entry.

    Two rules. It must be ``provider/model`` with a non-empty model half, so
    ``parse_model_name``'s ``split("/", 1)[1]`` cannot raise on it downstream.
    And it must not itself live in the ``mcc/`` namespace: a tier pointing at a
    tier is a loop, and ``_validate_provider_id`` would reject the synthetic
    provider id anyway -- one attempt later, where nobody can see the file that
    caused it. The provider id is *not* checked against the registry here: a
    route naming a provider the operator has temporarily disabled must stay
    loadable, exactly as ``_require_provider_prefixed_model_ref`` reasons for
    the env-var routes.
    """

    provider_id, separator, model_id = ref.partition("/")
    if not separator or not provider_id.strip() or not model_id.strip():
        return False
    return provider_id.strip().lower() != TIER_NAMESPACE


def load_harness_tiers(path: Path | None = None) -> HarnessTiers:
    """Read the override file, treating every failure as "no overrides".

    A malformed file must never stop the proxy from starting or from serving a
    request: the worst honest outcome is that the operator's per-harness tiers
    do not apply, every tier resolves globally, and a log line says so.
    """

    resolved_path = path if path is not None else harness_tiers_path()
    try:
        raw = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_HARNESS_TIERS
    except OSError as exc:
        logger.warning("HARNESS TIERS: cannot read {}: {}", resolved_path, exc)
        return EMPTY_HARNESS_TIERS

    if not raw.strip():
        return EMPTY_HARNESS_TIERS

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("HARNESS TIERS: cannot parse {}: {}", resolved_path, exc)
        return EMPTY_HARNESS_TIERS

    return HarnessTiers.from_document(document)


def save_harness_tiers(tiers: HarnessTiers, path: Path | None = None) -> None:
    """Write the override file atomically and drop the cache."""

    resolved_path = path if path is not None else harness_tiers_path()
    write_json_document_atomically(resolved_path, tiers.as_document())
    reset_harness_tiers_cache()


# (path, mtime_ns, size) -> parsed table. Read on the request path, so
# re-parsing a JSON file per request would be a real cost; keying the cache on
# the file's own stat means a dashboard edit is picked up without a restart --
# which is what lets the admin route write the file and nothing else.
_CACHE_SIGNATURE: tuple[str, int, int] | None = None
_CACHED_TIERS: HarnessTiers = EMPTY_HARNESS_TIERS


def reset_harness_tiers_cache() -> None:
    """Forget the cached table, so the next read goes back to disk."""

    global _CACHE_SIGNATURE, _CACHED_TIERS
    _CACHE_SIGNATURE = None
    _CACHED_TIERS = EMPTY_HARNESS_TIERS


def current_harness_tiers(path: Path | None = None) -> HarnessTiers:
    """Return the override table, re-reading only when the file has changed."""

    global _CACHE_SIGNATURE, _CACHED_TIERS
    resolved_path = path if path is not None else harness_tiers_path()
    try:
        stat = resolved_path.stat()
    except OSError:
        reset_harness_tiers_cache()
        return EMPTY_HARNESS_TIERS

    signature = (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    if signature != _CACHE_SIGNATURE:
        _CACHED_TIERS = load_harness_tiers(resolved_path)
        _CACHE_SIGNATURE = signature
    return _CACHED_TIERS


__all__ = [
    "EMPTY_HARNESS_TIERS",
    "EMPTY_OVERRIDE",
    "FALLBACKS_KEY",
    "HARNESSES_KEY",
    "MODEL_KEY",
    "PAUSED_KEY",
    "HarnessTierOverride",
    "HarnessTiers",
    "current_harness_tiers",
    "is_valid_tier_override_ref",
    "load_harness_tiers",
    "reset_harness_tiers_cache",
    "save_harness_tiers",
]
