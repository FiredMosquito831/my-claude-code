"""Per-model media endpoint ladder resolution across discovery rungs (8.61.0).

Resolves an ordered tuple of :class:`MediaEndpointCandidate` entries for one
``(model_ref, operation)`` from the provider's declared ``MediaSurface`` pool:

1. Operator overrides (``~/.mcc/media_overrides.json``): pinned ``surface`` /
   ``pin_surface`` leads first unconditionally; ``preferred_surface`` leads
   ahead of unpinned defaults unless refused.
2. Learned deployment facts (``FACT_MEDIA_ENDPOINT_SURFACE`` promotes proven
   surfaces; ``FACT_MEDIA_ENDPOINT_REFUSED`` demotes refused surfaces behind
   non-refused candidates while keeping them marked ``refused=True``).
3. Shipped dated measurements (:class:`MeasuredMediaEndpointSet`), keyed
   strictly by exact ``(provider_id, model_id, operation)`` tuples.
4. Capability metadata discovery (:class:`MediaCapabilityProfile` from Rung 4,
   such as ``supported_generation_methods``), matched on wire/method traits.
5. Provider descriptor default declaration order (``surfaces_for``).

Pure: no I/O, imports ``config`` and ``core``/``application`` only.
Strictly ``IV.6``-compliant: zero model-name comparisons or vendor literals.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from my_claude_code.config.media_overrides import (
    MediaOverridesDocument,
    effective_media_override,
)
from my_claude_code.config.media_surfaces import (
    MediaSurface,
    effective_surfaces_for,
    surface_key,
)

if TYPE_CHECKING:
    from my_claude_code.application.media.capabilities import MediaCapabilityProfile

SOURCE_OPERATOR = "operator"
SOURCE_LEARNED = "learned"
SOURCE_MEASURED = "measured"
SOURCE_PROVIDER_LIST = "provider_list"
SOURCE_PROVIDER_DEFAULT = "provider_default"

FACT_MEDIA_ENDPOINT_SURFACE = "media_endpoint_surface"
FACT_MEDIA_ENDPOINT_REFUSED = "media_endpoint_refused"

_PROVIDER_WIDE_KEY = "*"
_COLON = ":"
_SLASH = "/"


@dataclass(frozen=True, slots=True)
class MediaEndpointCandidate:
    """One candidate wire surface on a model's endpoint ladder."""

    surface: MediaSurface
    source: str
    detail: str = ""
    guessed: bool = False
    refused: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedMediaEndpointLadder:
    """Ordered endpoint candidates for one ``(model_ref, operation)``."""

    model_ref: str
    operation: str
    candidates: tuple[MediaEndpointCandidate, ...]
    winner: MediaEndpointCandidate | None
    capability: MediaCapabilityProfile | None = None


@dataclass(frozen=True, slots=True)
class MeasuredMediaEndpointEntry:
    """One dated endpoint measurement keyed on an exact deployment tuple."""

    surface_key: str
    measured_at: str = ""
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class MeasuredMediaEndpointSet:
    """Exact-keyed dated endpoint measurements: ``(provider, id, op) -> entry``."""

    entries: Mapping[tuple[str, str, str], MeasuredMediaEndpointEntry | str] = field(
        default_factory=dict
    )

    def lookup(
        self, provider_key: str, item_key: str, operation: str
    ) -> MeasuredMediaEndpointEntry | None:
        """Exact (case-folded) tuple lookup; never regex or substring."""

        op = operation.strip()
        direct = self.entries.get((provider_key, item_key, op))
        if direct is None:
            folded_provider = provider_key.strip().casefold()
            folded_item = item_key.strip().casefold()
            for (prov, item, entry_op), val in self.entries.items():
                if (
                    prov.strip().casefold() == folded_provider
                    and item.strip().casefold() == folded_item
                    and entry_op.strip() == op
                ):
                    direct = val
                    break
        if direct is None:
            return None
        if isinstance(direct, str):
            return MeasuredMediaEndpointEntry(surface_key=direct)
        return direct


EMPTY_MEASURED_MEDIA_ENDPOINTS = MeasuredMediaEndpointSet()


def _surface_keys(surface: MediaSurface) -> frozenset[str]:
    keys = {surface_key(surface), surface.shape}
    if surface.surface_id:
        keys.add(surface.surface_id)
    return frozenset(keys)


def _fact_attr_or_key(fact: object, name: str, default: Any = None) -> Any:
    if isinstance(fact, Mapping):
        return fact.get(name, default)
    return getattr(fact, name, default)


def _is_fact_stale(fact: object, now: datetime) -> bool:
    if isinstance(fact, Mapping):
        return bool(fact.get("retired")) or bool(fact.get("stale"))
    if bool(getattr(fact, "retired", False)):
        return True
    checker = getattr(fact, "is_stale", None)
    if callable(checker):
        return bool(checker(now))
    return False


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip()
        return (cleaned,) if cleaned else ()
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = [
            entry.strip() for entry in value if isinstance(entry, str) and entry.strip()
        ]
        return tuple(dict.fromkeys(items))
    return ()


def _collect_learned_signals(
    learned_facts: Sequence[Mapping[str, Any] | Any],
    provider_key: str,
    item_key: str,
    full_ref: str,
    operation: str,
) -> tuple[list[str], set[str]]:
    """Extract ordered proven surface keys and refused keys from fresh facts."""

    now = datetime.now(UTC)
    folded_provider = provider_key.casefold()
    folded_item = item_key.casefold()
    folded_ref = full_ref.casefold()
    op_prefix = f"{operation}{_COLON}"

    specific_proven: list[str] = []
    wide_proven: list[str] = []
    refused: set[str] = set()

    for fact in learned_facts:
        if _is_fact_stale(fact, now):
            continue
        fact_provider = str(_fact_attr_or_key(fact, "provider_id", "") or "").strip()
        fact_item = str(_fact_attr_or_key(fact, "model_id", "") or "").strip()
        if folded_provider and fact_provider.casefold() != folded_provider:
            continue
        folded_fact_item = fact_item.casefold()
        is_wide = fact_item == _PROVIDER_WIDE_KEY
        is_match = is_wide or folded_fact_item in {folded_item, folded_ref}
        if not is_match:
            continue

        kind = str(_fact_attr_or_key(fact, "fact_kind", "") or "").strip()
        detail = str(_fact_attr_or_key(fact, "detail", "") or "").strip()
        value = _fact_attr_or_key(fact, "value", None)

        if kind == FACT_MEDIA_ENDPOINT_SURFACE:
            if detail and detail != operation:
                continue
            target_list = wide_proven if is_wide else specific_proven
            for key in _string_list(value):
                if key not in target_list:
                    target_list.append(key)
        elif kind == FACT_MEDIA_ENDPOINT_REFUSED:
            if detail.startswith(op_prefix):
                refused_key = detail[len(op_prefix) :].strip()
                if refused_key:
                    refused.add(refused_key)
            elif detail == operation and isinstance(value, str) and value.strip():
                refused.add(value.strip())

    proven = list(dict.fromkeys(specific_proven + wide_proven))
    return proven, refused


def _capability_matches_surface(
    surface: MediaSurface,
    capability: MediaCapabilityProfile | None,
) -> tuple[bool, str, str]:
    """Return ``(matched, source, detail)`` from Rung 4 capability metadata."""

    if capability is None:
        return False, "", ""
    methods = {
        method.strip().lower() for method in capability.supported_generation_methods
    }
    if not methods:
        return False, "", ""
    shape = surface.shape.lower()
    path = surface.path.lower()
    prov_entry = capability.field_provenance.get("supported_generation_methods")
    source = prov_entry[0] if prov_entry else SOURCE_PROVIDER_LIST

    if "generatecontent" in methods and (
        "generate_content" in shape or ":generatecontent" in path
    ):
        return True, source, "supported_generation_methods:generateContent"
    if "predictlongrunning" in methods and (
        shape.endswith("_lro") or ":predictlongrunning" in path
    ):
        return True, source, "supported_generation_methods:predictLongRunning"
    if "predict" in methods and ("_predict" in shape or path.endswith(":predict")):
        return True, source, "supported_generation_methods:predict"
    return False, "", ""


def _resolve_measured(
    measured: MeasuredMediaEndpointSet | Mapping[tuple[str, str, str], Any] | None,
    provider_key: str,
    item_key: str,
    operation: str,
) -> MeasuredMediaEndpointEntry | None:
    if measured is None:
        return None
    table = (
        measured
        if isinstance(measured, MeasuredMediaEndpointSet)
        else MeasuredMediaEndpointSet(entries=measured)
    )
    return table.lookup(provider_key, item_key, operation)


def resolve_media_endpoint_ladder(
    target_ref: str,
    operation: str,
    *,
    surfaces: tuple[MediaSurface, ...],
    capability_profile: MediaCapabilityProfile | None = None,
    overrides: MediaOverridesDocument | None = None,
    learned_facts: Sequence[Mapping[str, Any] | Any] = (),
    measured: MeasuredMediaEndpointSet
    | Mapping[tuple[str, str, str], Any]
    | None = None,
) -> ResolvedMediaEndpointLadder:
    """Resolve the ordered endpoint candidate ladder for ``(target_ref, operation)``.

    Precedence (strongest first):
    1. Operator pin (``surface`` / ``pin_surface=True`` in ``media_overrides.json``)
    2. Operator preference (``preferred_surface`` in ``media_overrides.json``)
    3. Learned deployment surface (``FACT_MEDIA_ENDPOINT_SURFACE``)
    4. Exact dated measurement (``MeasuredMediaEndpointSet``)
    5. Capability metadata match (``MediaCapabilityProfile.supported_generation_methods``)
    6. Provider descriptor declaration order (``surfaces_for``)

    Refused candidates (``FACT_MEDIA_ENDPOINT_REFUSED``) are marked
    ``refused=True`` and demoted behind all non-refused candidates unless
    explicitly pinned by the operator.
    """

    pool = effective_surfaces_for(surfaces, operation)
    if not pool:
        return ResolvedMediaEndpointLadder(
            model_ref=target_ref,
            operation=operation,
            candidates=(),
            winner=None,
            capability=capability_profile,
        )

    cleaned_ref = target_ref.strip()
    provider_key, sep, item_key = cleaned_ref.partition(_SLASH)
    if not sep:
        provider_key, item_key = "", cleaned_ref

    override = effective_media_override(overrides, cleaned_ref, operation)
    pinned_key = override.pinned_surface
    preferred_key = None if pinned_key is not None else override.preferred_surface

    proven_keys, refused_keys = _collect_learned_signals(
        learned_facts, provider_key, item_key, cleaned_ref, operation
    )
    measured_entry = _resolve_measured(measured, provider_key, item_key, operation)
    measured_key = None if measured_entry is None else measured_entry.surface_key

    ranked: list[tuple[tuple[int, int, int, int], MediaEndpointCandidate]] = []
    for decl_idx, surface in enumerate(pool):
        keys = _surface_keys(surface)
        is_pinned = pinned_key is not None and pinned_key in keys
        is_preferred = preferred_key is not None and preferred_key in keys
        is_refused = bool(keys & refused_keys) and not is_pinned

        proven_rank = len(proven_keys)
        is_proven = False
        for idx, proven_key in enumerate(proven_keys):
            if proven_key in keys:
                proven_rank = idx
                is_proven = True
                break

        is_measured = measured_key is not None and measured_key in keys
        cap_matched, cap_source, cap_detail = _capability_matches_surface(
            surface, capability_profile
        )

        if is_pinned:
            tier = 0
            sub_rank = 0
            source = SOURCE_OPERATOR
            detail = "pinned"
        elif is_preferred:
            tier = 1
            sub_rank = 0
            source = SOURCE_OPERATOR
            detail = "preferred_surface"
        elif is_proven:
            tier = 2
            sub_rank = proven_rank
            source = SOURCE_LEARNED
            detail = FACT_MEDIA_ENDPOINT_SURFACE
        elif is_measured:
            tier = 3
            sub_rank = 0
            source = SOURCE_MEASURED
            detail = (
                measured_entry.measured_at
                if measured_entry is not None and measured_entry.measured_at
                else "measured"
            )
        elif cap_matched:
            tier = 4
            sub_rank = 0
            source = cap_source
            detail = cap_detail
        else:
            tier = 5
            sub_rank = 0
            source = SOURCE_PROVIDER_DEFAULT
            detail = ""

        demoted = 1 if is_refused else 0
        if is_refused:
            detail = f"{detail};refused" if detail else FACT_MEDIA_ENDPOINT_REFUSED

        candidate = MediaEndpointCandidate(
            surface=surface,
            source=source,
            detail=detail,
            guessed=False,
            refused=is_refused,
        )
        ranked.append(((demoted, tier, sub_rank, decl_idx), candidate))

    ranked.sort(key=lambda item: item[0])
    candidates = tuple(candidate for _sort_key, candidate in ranked)
    return ResolvedMediaEndpointLadder(
        model_ref=target_ref,
        operation=operation,
        candidates=candidates,
        winner=candidates[0],
        capability=capability_profile,
    )


__all__ = [
    "EMPTY_MEASURED_MEDIA_ENDPOINTS",
    "FACT_MEDIA_ENDPOINT_REFUSED",
    "FACT_MEDIA_ENDPOINT_SURFACE",
    "MeasuredMediaEndpointEntry",
    "MeasuredMediaEndpointSet",
    "MediaEndpointCandidate",
    "ResolvedMediaEndpointLadder",
    "resolve_media_endpoint_ladder",
]
