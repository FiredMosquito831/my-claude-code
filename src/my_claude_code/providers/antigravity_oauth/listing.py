"""The ``fetchAvailableModels`` roster parser — the user's "detect models from the endpoint".

``POST {host}/v1internal:fetchAvailableModels`` (body ``{project}`` when known,
else ``{}``) answers ``{"models": {<id>: {…}}}``: ``models`` is an **object
keyed by model id**, and each value carries ``displayName``, ``modelId``,
``maxOutputTokens``, ``modelTier``, ``isInternal``, ``quotaInfo`` and
``recommendedUpgradeModel``.

Two rules decide what this module keeps:

* **Per-model output caps come from the roster.** There is no global constant:
  the published values are 65,536 / 65,535 for most models and 32,768 for
  ``gpt-oss-120b-medium``, and a measured 16,754-token completion disproves the
  ported 16,384 ceiling. A model with no published cap answers ``None`` and the
  capability ladder decides — never a number invented here.
* **``-disabled`` placeholders and old Gemini (< 3.5) are hidden from the
  listing and stay valid for request-time gating.** They are not routable
  through discovery, but a client that names one explicitly is not refused
  here.

``providers/model_listing.py`` is deliberately untouched: it speaks the
OpenAI/OpenRouter row dialects, and a bespoke RPC shape belongs in the
provider package (the ``providers/vertex/models.py`` precedent).
"""

import re
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ProviderModelInfo,
)
from my_claude_code.providers.runtime.served_models import resolve_served_models

from .constants import (
    DISABLED_MODEL_SUFFIX,
    FETCH_AVAILABLE_MODELS_PATH,
    LISTED_GEMINI_MAJOR,
    LISTED_GEMINI_MIN_MINOR,
    SEED_MODEL_IDS,
    SEED_VERIFIED_ON,
)

#: ``gemini-3.5-flash`` → (3, 5); ``gemini-3-flash`` → (3, None).
_GEMINI_VERSION = re.compile(r"^gemini-(\d+)(?:[.-](\d+))?", re.IGNORECASE)

HIDE_DISABLED = "disabled"
HIDE_OLD_GEMINI = "old-gemini"


@dataclass(frozen=True, slots=True)
class ModelRosterEntry:
    """One row of the roster, as the endpoint published it."""

    model_id: str
    display_name: str = ""
    max_output_tokens: int | None = None
    model_tier: str = ""
    is_internal: bool | None = None
    quota_info: dict[str, Any] = field(default_factory=dict)
    recommended_upgrade_model: str = ""
    #: ``""`` when the model is listable; a declared reason when it is hidden.
    hidden_reason: str = ""

    @property
    def listed(self) -> bool:
        """Whether this model appears in the catalogue."""

        return not self.hidden_reason


@dataclass(frozen=True, slots=True)
class RosterParse:
    """Every row that could be read, plus the ones that could not.

    A row that cannot be read is skipped and recorded, never raised: one
    unreadable row must not discard the whole roster (the partial-probe rule).
    """

    entries: tuple[ModelRosterEntry, ...] = ()
    skipped: tuple[str, ...] = ()

    def listed(self) -> tuple[ModelRosterEntry, ...]:
        return tuple(entry for entry in self.entries if entry.listed)

    def listed_ids(self) -> frozenset[str]:
        return frozenset(entry.model_id for entry in self.listed())

    def routable_ids(self) -> frozenset[str]:
        """Every id the host published, hidden ones included."""

        return frozenset(entry.model_id for entry in self.entries)

    def by_id(self) -> dict[str, ModelRosterEntry]:
        return {entry.model_id: entry for entry in self.entries}


def gemini_family(model_id: str) -> tuple[int, int] | None:
    """``(major, minor)`` for a Gemini id, or ``None`` for any other family."""

    match = _GEMINI_VERSION.match(model_id.strip())
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major, minor


def hides_old_gemini(model_id: str) -> bool:
    """Whether this id is an old Gemini family the listing must not show."""

    family = gemini_family(model_id)
    if family is None:
        return False
    major, minor = family
    if major < LISTED_GEMINI_MAJOR:
        return True
    return major == LISTED_GEMINI_MAJOR and minor < LISTED_GEMINI_MIN_MINOR


def _positive_int(value: Any) -> int | None:
    """A published cap, or ``None`` when the host did not state a usable one."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _entry_from_row(model_id: str, row: Any) -> ModelRosterEntry | None:
    if not isinstance(row, dict):
        return None
    display_name = row.get("displayName")
    max_output_tokens = _positive_int(row.get("maxOutputTokens"))
    tier = row.get("modelTier")
    internal = row.get("isInternal")
    quota = row.get("quotaInfo")
    upgrade = row.get("recommendedUpgradeModel")
    hidden = ""
    if model_id.endswith(DISABLED_MODEL_SUFFIX):
        hidden = HIDE_DISABLED
    elif hides_old_gemini(model_id):
        hidden = HIDE_OLD_GEMINI
    return ModelRosterEntry(
        model_id=model_id,
        display_name=display_name if isinstance(display_name, str) else "",
        max_output_tokens=max_output_tokens,
        model_tier=tier if isinstance(tier, str) else "",
        is_internal=internal if isinstance(internal, bool) else None,
        quota_info=dict(quota) if isinstance(quota, dict) else {},
        recommended_upgrade_model=upgrade if isinstance(upgrade, str) else "",
        hidden_reason=hidden,
    )


def parse_available_models(payload: Any) -> RosterParse:
    """Parse one ``fetchAvailableModels`` answer.

    ``models`` must be an object keyed by model id. A response with no ``models``
    object at all is an empty parse, never an error — a roster this build cannot
    read costs the operator ids, not a request.
    """

    document = payload if isinstance(payload, dict) else {}
    models = document.get("models")
    if not isinstance(models, dict):
        return RosterParse()
    entries: list[ModelRosterEntry] = []
    skipped: list[str] = []
    for key, row in models.items():
        model_id = str(key or "").strip()
        if not model_id:
            skipped.append(str(key))
            continue
        declared = row.get("modelId") if isinstance(row, dict) else None
        # The map key is the identity; a divergent ``modelId`` is still the same
        # row, so the key wins and the value is only a fallback.
        if not model_id and isinstance(declared, str):
            model_id = declared.strip()
        entry = _entry_from_row(model_id, row)
        if entry is None:
            skipped.append(model_id)
            continue
        entries.append(entry)
    return RosterParse(entries=tuple(entries), skipped=tuple(skipped))


def output_cap_for(parse: RosterParse, model_id: str) -> int | None:
    """The roster's published output ceiling for one model, or ``None``.

    ``None`` is the honest answer for an unknown model and for one the host
    published no cap for; the ladder supplies the figure in that case and the
    caller says so.
    """

    entry = parse.by_id().get(model_id)
    return None if entry is None else entry.max_output_tokens


def model_infos(parse: RosterParse) -> frozenset[ProviderModelInfo]:
    """The listable rows as catalogue records, carrying their published caps."""

    return frozenset(
        ProviderModelInfo(
            model_id=entry.model_id,
            max_output_tokens=entry.max_output_tokens,
        )
        for entry in parse.listed()
    )


def seed_evidence() -> dict[str, ModelListingEvidence]:
    """The S5 seed rung: the baked-in names, as a dated constant.

    Only consulted when the gateway answers nothing (a cold start with no
    credential path). It is never an existence source and never outranks the
    roster.
    """

    return {
        model_id: ModelListingEvidence(
            provenance=ModelListingProvenance.SEED,
            detail=f"baked-in name, recorded {SEED_VERIFIED_ON}",
        )
        for model_id in SEED_MODEL_IDS
    }


def resolve(
    gateway: Any = None,
) -> dict[str, ModelListingEvidence]:
    """The listing rungs, strongest first, with the seed as the floor.

    The S1 gateway rung is the roster; S3 (observed) and S4 (models.dev) are
    deliberately absent — models.dev is a capability source here, never an
    existence source.
    """

    return resolve_served_models(
        gateway=gateway,
        seed=seed_evidence,
        log_tag="antigravity",
    )


__all__ = [
    "FETCH_AVAILABLE_MODELS_PATH",
    "HIDE_DISABLED",
    "HIDE_OLD_GEMINI",
    "ModelRosterEntry",
    "RosterParse",
    "gemini_family",
    "hides_old_gemini",
    "model_infos",
    "output_cap_for",
    "parse_available_models",
    "resolve",
    "seed_evidence",
]
