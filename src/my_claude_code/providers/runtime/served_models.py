"""Which model ids a provider actually serves, for gateways with no ``/models``.

Most providers answer "what do you serve?" with an HTTP call. A subscription
gateway reached over OAuth often does not: ChatGPT/Codex has no model-list
endpoint at all (its official client embeds the catalogue in its own binary),
and a hand-written allowlist is the only thing left. That allowlist is exactly
the invented threshold this project bans: the one shipped for ``chatgpt_oauth``
named a model that has never existed, kept two OpenAI retired, and dropped one
OpenAI still lists.

So existence gets the same treatment capability metadata already has -- an
ordered chain of *evidence sources*, each of which either answers with real
ids or declines, and none of which is fatal when it raises:

===  =========================================================================
S1   the provider's own ``/models``, where it has one
S2   the catalogue shipped inside the backend's own official client, read off
     disk -- a document written by the vendor, updated when the user updates
     that client, needing no network and no credential
S3   ids this credential has already been served successfully, out of the
     request log -- free, offline, self-maintaining, and the only rung that is
     proof rather than a document
S4   a reference catalogue (models.dev) for the same vendor
S5   a literal seed list
===  =========================================================================

The published set is ``S1 | S2 | S3`` when any of the three answered, else S4,
else S5. The first three union rather than shadow each other because they
disagree in opposite directions: a catalogue can be stale about a model the
user is using right now, and the log can only know models the user has already
managed to select. Neither is allowed to delete the other's answer.

Nothing here reads or writes a capability, a limit or a price. A rung says
which ids exist and repeats what the source said about their lifecycle; every
number still comes off the resolution ladder exactly as before.
"""

from collections.abc import Callable, Mapping

from loguru import logger

from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
)

#: A rung: called with no arguments, answers with ids mapped to the evidence
#: behind them, or an empty mapping to decline. Raising is allowed and is
#: treated as declining.
type EvidenceRung = Callable[[], Mapping[str, ModelListingEvidence]]

#: Union rungs, strongest first. Order is the merge order: when two of them
#: name the same id, the earlier one's evidence is what the page shows.
UNION_PROVENANCE: tuple[ModelListingProvenance, ...] = (
    ModelListingProvenance.GATEWAY,
    ModelListingProvenance.VENDOR_CLIENT,
    ModelListingProvenance.OBSERVED,
)


def _call_rung(
    rung: EvidenceRung | None, *, label: str, log_tag: str
) -> dict[str, ModelListingEvidence]:
    """Run one rung; a failure is a decline, never an error for the caller.

    A model list that cannot be built is a picker that cannot be drawn, and
    every rung here is a best effort over something outside this process --
    a vendor's install layout, a SQLite file, a cache. One of them breaking
    must cost the ids it would have contributed and nothing else.
    """
    if rung is None:
        return {}
    try:
        answered = rung()
    except Exception as error:
        logger.warning(
            "{}model evidence rung {} declined: {}: {}",
            f"{log_tag}: " if log_tag else "",
            label,
            type(error).__name__,
            error,
        )
        return {}
    return {
        model_id: evidence
        for model_id, evidence in answered.items()
        if model_id.strip()
    }


def resolve_served_models(
    *,
    gateway: EvidenceRung | None = None,
    vendor_client: EvidenceRung | None = None,
    observed: EvidenceRung | None = None,
    reference: EvidenceRung | None = None,
    seed: EvidenceRung | None = None,
    log_tag: str = "",
) -> dict[str, ModelListingEvidence]:
    """Resolve one provider's served model ids from the evidence it has.

    Every argument is optional: a provider supplies the rungs it can actually
    source and leaves the rest out. Leaving a rung out is not the same as that
    rung answering nothing -- an absent rung is never logged as a decline.

    An empty answer is a decline, never "this provider serves no models". Only
    a non-empty answer counts, so a stale cache, an uninstalled client or an
    empty log can never collapse the catalogue to nothing.
    """
    merged: dict[str, ModelListingEvidence] = {}
    for rung, label in (
        (gateway, "gateway"),
        (vendor_client, "vendor-client"),
        (observed, "observed"),
    ):
        for model_id, evidence in _call_rung(
            rung, label=label, log_tag=log_tag
        ).items():
            # Strongest rung wins the row outright. Merging field by field
            # would let a weaker rung's silence read as a stronger rung's
            # answer, which is the distinction this whole module exists to
            # keep.
            merged.setdefault(model_id, evidence)
    if merged:
        return merged

    fallback = _call_rung(reference, label="reference", log_tag=log_tag)
    if fallback:
        return fallback
    return _call_rung(seed, label="seed", log_tag=log_tag)


__all__ = [
    "UNION_PROVENANCE",
    "EvidenceRung",
    "resolve_served_models",
]
