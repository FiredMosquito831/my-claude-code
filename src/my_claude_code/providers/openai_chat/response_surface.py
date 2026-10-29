"""Which of a gateway's wire surfaces one model is actually served on.

OpenCode Zen is not one API. It is a front door onto four, and which one a
model lives behind is a property *of the model*, published by OpenCode itself:
each entry in its registry may carry a ``provider: {npm: "..."}`` override, and
that package name is what selects the endpoint. MCC has spoken Chat Completions
to all of them since the profile existed, which is why
``muse-spark-1.3-contributor-free`` answered 23 requests in five minutes on
2026-09-11 with a bare HTTP 500 and nothing else.

**Nothing here reads a model name.** That is the rule this module exists to
keep (WORKING-NOTES 66/78, and the contract test beside it): the surface is
metadata, and a table of model ids in a provider file would be wrong the day
the roster rotates -- which it does, four times in six weeks.

Four sources, strongest first, and the order is the ordinary one this project
already uses for output caps: what an **operator** wrote down, then what MCC
**measured**, then what the vendor **published**, then the default nobody
stands behind.

1. ``model_overrides.json`` -> :data:`ResponseSurfaceSource.OVERRIDE`.
2. The learned-facts store -> :data:`ResponseSurfaceSource.LEARNED`. Written
   only by a probe that actually got an answer, never by a guess.
3. A dated measurement shipped beside the profiles ->
   :data:`ResponseSurfaceSource.MEASURED` (7.114.0).
4. models.dev's cached copy of the vendor's registry ->
   :data:`ResponseSurfaceSource.REGISTRY`.
5. Chat Completions -> :data:`ResponseSurfaceSource.DEFAULT`.

A model whose real surface this provider cannot speak resolves to
:data:`ResponseSurface.UNSERVABLE` **with the reason attached**, and is still
listed. Hiding it was considered and rejected: a hiding rule that can produce a
false positive takes a working model away from the user, and the one thing
worse than a model that fails loudly is a model that is silently not there.

**Since 7.114.0 an answer may be a set.** A model can be known to answer on
more than one door, and which one to use then depends on the inbound client
family (``core/client_family.py``): a Claude Code request prefers Messages, an
OpenAI-format one prefers Responses, and everything with no request in flight
prefers nothing and keeps the discovery order. The set is ordered by
:data:`CLIENT_FAMILY_ORDER` and the first member is what every pre-7.114.0
caller reads, so a model with one known door resolves byte for byte what it
always did -- which, on this fleet, is every model but one.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from loguru import logger

from my_claude_code.application.model_metadata import (
    MeasuredSurfaceSet,
    ResolvedResponseSurface,
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config.marketplaces import upstream_prefix
from my_claude_code.config.model_overrides import (
    RESPONSE_SURFACE_OVERRIDE,
    current_model_overrides,
    model_ref_for,
)
from my_claude_code.core.client_family import ClientFamily, current_client_family
from my_claude_code.providers.recovery import (
    FACT_RESPONSE_SURFACE,
    SOURCE_PROBE,
    learned_fact_store,
)

#: The vendor's own mapping, read out of its shipped registry and confirmed
#: live where it could be. An entry with no override inherits the provider's
#: package (``@ai-sdk/openai-compatible``) and therefore Chat Completions,
#: which is why absence is not in this table -- absence is the default.
#:
#: * ``@ai-sdk/openai`` -> Responses. CONFIRMED 2026-09-11: the same model, the
#:   same key and the same identity headers answered 500 on
#:   ``/chat/completions`` and 200 on ``/responses`` minutes apart, and the real
#:   ``opencode-ai@1.18.30`` CLI was watched reaching it on ``/responses``.
#: * ``@ai-sdk/anthropic`` -> Messages. INFERRED from the package; not probed,
#:   because the only free model on that surface has been withdrawn and
#:   answers 401.
#: * ``@ai-sdk/google`` -> Google's own ``generateContent``, which is not an
#:   endpoint under this base URL at all.
NPM_SURFACES: Mapping[str, ResponseSurface] = {
    "@ai-sdk/openai": ResponseSurface.RESPONSES,
    "@ai-sdk/anthropic": ResponseSurface.MESSAGES,
    "@ai-sdk/google": ResponseSurface.UNSERVABLE,
    "@ai-sdk/openai-compatible": ResponseSurface.CHAT_COMPLETIONS,
}

#: Why a surface this provider does not speak is unservable, in the operator's
#: words. Never "unknown": each of these is a definite statement about a
#: definite published fact.
UNSERVABLE_REASONS: Mapping[ResponseSurface, str] = {
    ResponseSurface.MESSAGES: (
        "the host serves this model on its Messages API, which this provider "
        "does not speak"
    ),
    ResponseSurface.RESPONSES: (
        "the host serves this model on its Responses API, which this provider "
        "does not speak"
    ),
    ResponseSurface.CHAT_COMPLETIONS: (
        "the host serves this model on its Chat Completions API, which this "
        "provider does not speak"
    ),
    ResponseSurface.UNSERVABLE: (
        "the host serves this model through Google's own API, which is not an "
        "endpoint of this gateway"
    ),
}

DEFAULT_SURFACE = ResponseSurface.CHAT_COMPLETIONS

#: Which known door each inbound client family asks first (7.114.0).
#:
#: A **sort key, never a filter**: a door the family does not name is kept and
#: tried last, so a family that names fewer surfaces can never strand a
#: request on a floor already known to be worse, and a model whose only known
#: door is Messages keeps it for an OpenAI-format client too.
#:
#: ``ANTHROPIC`` names all three because a Claude Code request is natively the
#: Messages protocol and is translated to the other two. ``OPENAI`` names the
#: two OpenAI doors; Messages sorts last for it. ``UNKNOWN`` -- no request in
#: flight, or a dialect that says nothing about a surface -- prefers nothing,
#: so the discovery order (the rung order) stands exactly as it always did.
CLIENT_FAMILY_ORDER: Mapping[ClientFamily, tuple[ResponseSurface, ...]] = {
    ClientFamily.ANTHROPIC: (
        ResponseSurface.MESSAGES,
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
    ),
    ClientFamily.OPENAI: (
        ResponseSurface.RESPONSES,
        ResponseSurface.CHAT_COMPLETIONS,
    ),
    ClientFamily.UNKNOWN: (),
}


@dataclass(frozen=True, slots=True)
class ResolvedSurfaceLadder:
    """The doors one request may try for one model, in order.

    ``candidates`` is never empty: the resolver always has at least the
    profile's floor to answer with. ``primary`` is the first one, which is
    exactly the single answer every pre-7.114.0 caller reads.
    """

    candidates: tuple[ResolvedResponseSurface, ...]

    @property
    def primary(self) -> ResolvedResponseSurface:
        """The door to ask first -- and the whole answer for a one-door model."""

        return self.candidates[0]


def surface_from_npm(npm: str) -> ResponseSurface | None:
    """Map one ``provider.npm`` package to the endpoint it implies."""

    return NPM_SURFACES.get(npm.strip())


def registry_surface(
    registry_provider: str, model_id: str
) -> tuple[ResponseSurface, str] | None:
    """Return what the vendor's published registry says, or ``None``.

    ``None`` means the registry has no opinion -- either MCC has no cached copy
    of it, or this model carries no override -- and *not* that the model is
    unreachable. The caller falls through to the default, which is what every
    release before this one did for every model.
    """

    if not registry_provider or not model_id:
        return None
    # Deferred: ``providers.runtime`` builds every provider in the fleet from
    # its package ``__init__``, and this module is imported by one of them.
    from my_claude_code.providers.runtime.models_dev import (
        models_dev_provider_npm_overrides,
    )

    npm = models_dev_provider_npm_overrides(registry_provider).get(model_id)
    if not npm:
        return None
    surface = surface_from_npm(npm)
    if surface is None:
        logger.debug(
            "RESPONSE SURFACE: {} publishes package {!r} for {}, which names no "
            "endpoint this build knows; falling through",
            registry_provider,
            npm,
            model_id,
        )
        return None
    return surface, npm


def registry_silence(registry_provider: str) -> str:
    """Why the registry had nothing to say, in the operator's words.

    The two silences are not the same fact and a Models page that spelled them
    the same way would be hiding the one that matters. "No entry for this
    model" is the vendor having published a registry that simply does not
    override this model, and is permanent until the roster moves. "No cached
    copy yet" is *MCC* not having fetched one -- the state every fresh and
    every keyless install is in on its first runs, and any install whose
    models.dev fetch failed -- and it resolves itself on the next sweep.

    Reads only what is already on disk, like every other caller on a request
    path; a fetch here would turn a page render into a network wait.

    The bucket is resolved through :func:`models_dev_bucket_id`, the same
    lookup :func:`registry_surface` goes through, so the two silences describe
    the bucket that actually answered. Reading the raw id instead told every
    ``opencode_go`` row "no cached copy of the vendor's registry yet" -- a
    sentence about *this install* -- when the truth was "Go's registry has
    nothing for this model", a sentence about the *vendor*.
    """

    if not registry_provider:
        return ""
    from my_claude_code.providers.runtime.models_dev import (
        models_dev_bucket_id,
        read_models_dev_cache,
    )

    cache = read_models_dev_cache()
    if cache is None or models_dev_bucket_id(cache.index, registry_provider) is None:
        return "no cached copy of the vendor's registry yet"
    return "no registry entry for this model"


def learned_surfaces(provider_id: str, model_id: str) -> tuple[ResponseSurface, ...]:
    """Every door this deployment's own probes proved, in the order stored.

    7.114.0 stores a list -- the doors still worth trying after the ones that
    answered wrong were pruned -- but a row written by 7.113 or earlier is a
    scalar, and a scalar reads as a one-element tuple. A value this build
    cannot parse is skipped rather than obeyed: the row stays on the Models
    page, and the model falls through to the rungs below.
    """

    if not provider_id or not model_id:
        return ()
    surfaces: list[ResponseSurface] = []
    for fact in learned_fact_store().fresh_facts_for_provider(provider_id):
        if fact.model_id != model_id or fact.fact_kind != FACT_RESPONSE_SURFACE:
            continue
        stored = fact.value if isinstance(fact.value, (list, tuple)) else [fact.value]
        for value in stored:
            try:
                surface = ResponseSurface(str(value))
            except ValueError:
                continue
            if surface not in surfaces:
                surfaces.append(surface)
    return tuple(surfaces)


def learned_surface(provider_id: str, model_id: str) -> ResponseSurface | None:
    """Return the surface a probe of *this deployment* actually proved.

    Kept for the readers that want one answer; the resolver reads
    :func:`learned_surfaces`, because since 7.114.0 a learned fact may name
    every door that is still worth trying.
    """

    surfaces = learned_surfaces(provider_id, model_id)
    return surfaces[0] if surfaces else None


def override_surface(provider_id: str, model_id: str) -> ResponseSurface | None:
    """Return the surface an operator wrote into ``model_overrides.json``."""

    raw = current_model_overrides().non_body_override(
        RESPONSE_SURFACE_OVERRIDE, provider_id, model_ref_for(provider_id, model_id)
    )
    if raw is None:
        return None
    try:
        return ResponseSurface(raw)
    except ValueError:
        logger.warning(
            "MODEL OVERRIDES: {!r} is not a response surface this build knows "
            "({}); ignoring it for {}/{}",
            raw,
            ", ".join(member.value for member in ResponseSurface),
            provider_id,
            model_id,
        )
        return None


def resolve_surface_ladder(
    provider_id: str,
    model_id: str,
    *,
    client_family: ClientFamily | None = None,
    registry_provider: str = "",
    declared: tuple[ResponseSurface, ...] = (),
    no_information: ResponseSurface | None = None,
    by_upstream: tuple[tuple[str, ResponseSurface], ...] = (),
    measured: tuple[tuple[str, MeasuredSurfaceSet], ...] = (),
    optimistic: bool = False,
) -> ResolvedSurfaceLadder:
    """Resolve every door one model is *known* to answer on, in try order.

    Five rungs, strongest first, and the first non-empty one wins:
    the operator's override, a fact this deployment learned, a dated
    measurement shipped with the build, the vendor's published registry, the
    marketplace's per-upstream declaration -- and then the profile's floor,
    which answers only when every rung above it is empty. That is 7.113.x's
    control flow with one rung inserted where a measurement belongs, so a
    model with one known door produces the identical ``(surface, source,
    detail)`` triple it always produced.

    ``client_family`` orders the answer; it never filters it. ``None`` reads
    the request in flight (``core.client_family.current_client_family``), which
    is ``UNKNOWN`` outside one -- the Models page, an admin call, a capability
    probe -- and ``UNKNOWN`` prefers nothing, so those callers keep the order
    they had.

    ``optimistic`` is the profile's ``optimistic_first_try`` (7.114.0), and in
    this release only the **unservable** branch reads it: a model the vendor
    places on an API this build cannot speak is not refused on such a host but
    tried on the door its client prefers and then on the floor, one probe
    before the client's request ever reaches the guess. Every other profile
    keeps the hard refusal.

    ``declared`` is what the *profile* says this provider can speak. A
    resolved surface that is not in it becomes :data:`ResponseSurface.
    UNSERVABLE` carrying the reason, so the Models page can name it rather
    than the model quietly vanishing. An empty ``declared`` means the profile
    made no statement -- which is every profile but the OpenCode ones and
    InferHub -- and those keep the default with no unservable case at all,
    exactly as before.

    ``no_information`` is the profile's own answer for the case nobody else
    has one. ``None`` keeps :data:`DEFAULT_SURFACE` and the ``DEFAULT``
    source, so a single-surface provider resolves byte for byte what it always
    did.
    """

    speakable = declared or (DEFAULT_SURFACE,)
    family = current_client_family() if client_family is None else client_family

    forced = override_surface(provider_id, model_id)
    if forced is not None:
        return _one(_constrain(forced, ResponseSurfaceSource.OVERRIDE, "", speakable))

    proved = learned_surfaces(provider_id, model_id)
    if proved:
        return _ladder_of(
            proved,
            family,
            ResponseSurfaceSource.LEARNED,
            "proved by a probe",
            speakable,
        )

    measured_set = _measured_for(measured, model_id)
    if measured_set is not None:
        return _ladder_of(
            measured_set.surfaces,
            family,
            ResponseSurfaceSource.MEASURED,
            f"measured {measured_set.measured_on.isoformat()}",
            speakable,
        )

    published = registry_surface(registry_provider, model_id)
    if published is not None:
        surface, npm = published
        resolved = _constrain(surface, ResponseSurfaceSource.REGISTRY, npm, speakable)
        if resolved.surface is ResponseSurface.UNSERVABLE and optimistic:
            return _guessed_ladder(family, speakable, no_information)
        return _one(resolved)

    if by_upstream:
        # 7.109.0: a marketplace's declared door per upstream (data on the
        # profile, ``surface_by_upstream``). Empty for every other profile,
        # so this block is never entered for them.
        prefix = upstream_prefix(model_id)
        for name, surface in by_upstream:
            if prefix and name == prefix:
                return _one(
                    _constrain(
                        surface,
                        ResponseSurfaceSource.PROFILE,
                        f"the {prefix} upstream's own door, declared on the profile",
                        speakable,
                    )
                )

    if no_information is None:
        return _one(
            ResolvedResponseSurface(DEFAULT_SURFACE, ResponseSurfaceSource.DEFAULT)
        )
    return _one(
        _constrain(
            no_information,
            ResponseSurfaceSource.PROFILE,
            registry_silence(registry_provider),
            speakable,
        )
    )


def resolve_response_surface(
    provider_id: str,
    model_id: str,
    *,
    client_family: ClientFamily | None = None,
    registry_provider: str = "",
    declared: tuple[ResponseSurface, ...] = (),
    no_information: ResponseSurface | None = None,
    by_upstream: tuple[tuple[str, ResponseSurface], ...] = (),
    measured: tuple[tuple[str, MeasuredSurfaceSet], ...] = (),
    optimistic: bool = False,
) -> ResolvedResponseSurface:
    """One model's first door: :attr:`ResolvedSurfaceLadder.primary`.

    Every caller that wants a single answer -- the Models page, the tests that
    pin a resolution, the request log's label -- reads this, and gets exactly
    what 7.113.x answered unless the model has more than one known door *and*
    the caller named a client family that prefers another one.
    """

    return resolve_surface_ladder(
        provider_id,
        model_id,
        client_family=client_family,
        registry_provider=registry_provider,
        declared=declared,
        no_information=no_information,
        by_upstream=by_upstream,
        measured=measured,
        optimistic=optimistic,
    ).primary


def _measured_for(
    measured: tuple[tuple[str, MeasuredSurfaceSet], ...], model_id: str
) -> MeasuredSurfaceSet | None:
    """The shipped measurement for one model id, or ``None``."""

    for name, fact in measured:
        if name == model_id:
            return fact
    return None


def _one(resolved: ResolvedResponseSurface) -> ResolvedSurfaceLadder:
    """A ladder with exactly one door -- every model but one, on this fleet."""

    return ResolvedSurfaceLadder((resolved,))


def _ladder_of(
    surfaces: Iterable[ResponseSurface],
    family: ClientFamily,
    source: ResponseSurfaceSource,
    detail: str,
    speakable: tuple[ResponseSurface, ...],
) -> ResolvedSurfaceLadder:
    """Order a known set by the client family, and attach one source to all."""

    ordered = _ordered_surfaces(surfaces, family)
    return ResolvedSurfaceLadder(
        tuple(_constrain(surface, source, detail, speakable) for surface in ordered)
    )


def _ordered_surfaces(
    surfaces: Iterable[ResponseSurface], family: ClientFamily
) -> tuple[ResponseSurface, ...]:
    """The family's preference first, then discovery order; never a filter.

    A surface the family does not name -- Messages, for an OpenAI-format
    client -- is kept and sorted last, and duplicates are dropped in discovery
    order. A one-element set is returned untouched, which is why every
    single-door model is byte-identical to 7.113.x under every family.
    """

    discovery: list[ResponseSurface] = []
    for surface in surfaces:
        if surface not in discovery:
            discovery.append(surface)
    preference = CLIENT_FAMILY_ORDER.get(family, ())
    return tuple(
        sorted(
            discovery,
            key=lambda surface: (
                preference.index(surface) if surface in preference else len(preference),
                discovery.index(surface),
            ),
        )
    )


def _preferred_surface(
    family: ClientFamily, speakable: tuple[ResponseSurface, ...]
) -> ResponseSurface | None:
    """The door this family would ask first, or ``None`` when it has none."""

    for surface in CLIENT_FAMILY_ORDER.get(family, ()):
        if surface in speakable:
            return surface
    return None


def _guessed_ladder(
    family: ClientFamily,
    speakable: tuple[ResponseSurface, ...],
    no_information: ResponseSurface | None,
) -> ResolvedSurfaceLadder:
    """``[the family's preferred door] + [the floor]`` -- one guess, then home.

    7.114.0's unservable branch (and 7.115.0's unknown-surface branch will
    build the same shape). The vendor said this model needs an API this build
    cannot speak; that statement is honoured -- the named door is never
    dialled -- but "therefore the model is unusable" is no longer concluded
    from it, because nobody has asked the host whether it also answers on a
    door MCC does speak. The guess carries ``guessed=True`` so the walk probes
    it first; it is one ~16-token question, not the client's request.
    """

    floor = DEFAULT_SURFACE if no_information is None else no_information
    candidates: list[ResolvedResponseSurface] = []
    preferred = _preferred_surface(family, speakable)
    if preferred is not None and preferred is not floor:
        candidates.append(
            ResolvedResponseSurface(
                preferred,
                ResponseSurfaceSource.PROFILE,
                "guessed -- the vendor names a surface this build cannot speak",
                guessed=True,
            )
        )
    candidates.append(_constrain(floor, ResponseSurfaceSource.PROFILE, "", speakable))
    return ResolvedSurfaceLadder(tuple(candidates))


def _constrain(
    surface: ResponseSurface,
    source: ResponseSurfaceSource,
    detail: str,
    speakable: tuple[ResponseSurface, ...],
) -> ResolvedResponseSurface:
    """Fold "what it needs" and "what we speak" into one honest answer."""

    if surface in speakable:
        return ResolvedResponseSurface(surface, source, detail)
    reason = UNSERVABLE_REASONS.get(surface, "this provider cannot reach it")
    qualified = f"{reason} ({detail})" if detail else reason
    return ResolvedResponseSurface(ResponseSurface.UNSERVABLE, source, qualified)


def catalogue_surface(
    provider_id: str, model_id: str
) -> ResolvedResponseSurface | None:
    """The resolved surface for one catalogue row, for the Models page.

    ``None`` for every provider whose profile declares no surfaces -- 40 of the
    43 -- and for every custom entry that serves Chat Completions alone, so
    their rows are byte-identical to what they were. The lookup is a registry
    read, not a branch: the profile table is keyed by catalogue id exactly as
    the provider factory keys it, and a custom entry's declaration is read from
    the same registry the factory builds it from.

    The page reads the *first* door -- it has no request in flight, so its
    family is ``UNKNOWN`` and its order is the discovery order -- and it never
    reads the optimistic guess: a model the vendor places on an API this build
    cannot speak keeps saying so, with the vendor's reason, until a probe has
    actually answered. Its note gains one clause on a host that opted in
    (7.114.0), because the answer is no longer "unusable" but "not tried yet".
    The row self-corrects the moment the learned fact exists, which outranks
    the registry.
    """

    from .profiles import OPENAI_CHAT_PROFILES

    profile = OPENAI_CHAT_PROFILES.get(provider_id)
    if profile is None:
        declared = declared_surfaces_for(provider_id)
        if not declared:
            return None
        return resolve_response_surface(provider_id, model_id, declared=declared)
    if not profile.response_surfaces:
        return None
    resolved = resolve_response_surface(
        provider_id,
        model_id,
        registry_provider=profile.surface_registry_provider,
        declared=profile.response_surfaces,
        no_information=profile.no_information_surface,
        by_upstream=profile.surface_by_upstream,
        measured=profile.measured_surfaces,
    )
    if resolved.surface is ResponseSurface.UNSERVABLE and profile.optimistic_first_try:
        # The vendor's statement stays first and whole; MCC's conclusion from
        # it is what changed (7.114.0), so the note says which.
        return replace(
            resolved, detail=f"{resolved.detail}; MCC will probe it on first use"
        )
    return resolved


def declared_surfaces_for(provider_id: str) -> tuple[ResponseSurface, ...]:
    """What a custom entry says its host serves, or ``()`` for anything else.

    ``()`` for a static provider, for an unknown id, and -- the case that keeps
    every existing install unchanged -- for a custom entry that declares Chat
    Completions alone, because one surface is the same routing decision as no
    statement at all (see ``declared_surfaces.py``).

    Deferred import for the same reason the models.dev read above is deferred:
    this module is imported while the provider package is still being built.
    """

    if not provider_id:
        return ()
    from my_claude_code.config.provider_registry import get_provider_registry

    from .declared_surfaces import declared_surfaces

    entry = get_provider_registry().get(provider_id)
    if entry is None:
        return ()
    surfaces = declared_surfaces(entry.surfaces)
    return () if surfaces == (DEFAULT_SURFACE,) else surfaces


def alternative_surfaces(
    current: ResponseSurface, declared: tuple[ResponseSurface, ...]
) -> tuple[ResponseSurface, ...]:
    """The surfaces still worth trying after ``current`` failed, in order."""

    return tuple(
        surface
        for surface in declared
        if surface is not current and surface is not ResponseSurface.UNSERVABLE
    )


def remember_response_surfaces(
    provider_id: str,
    model_id: str,
    surfaces: Iterable[ResponseSurface],
    *,
    evidence: str = "",
) -> None:
    """Write down the doors still worth trying for one model.

    7.114.0 widens the stored value from one surface to a list of them, and
    nothing else: same fact kind, same ``source="probe"``, same seven-day TTL,
    same ``detail=""`` row identity -- so a write replaces the previous answer
    in place and a row written by an earlier release still reads (as a
    one-element set).

    The caller writes ``known - attempted``: the doors a failure has not
    disproved yet. An empty list is never written -- if the last door fails,
    the host's own failure is raised and nothing is claimed.
    """

    ordered: list[ResponseSurface] = []
    for surface in surfaces:
        if surface not in ordered:
            ordered.append(surface)
    if not ordered:
        return
    if not provider_id or not model_id:
        return
    learned_fact_store().record(
        provider_id,
        model_id,
        FACT_RESPONSE_SURFACE,
        [surface.value for surface in ordered],
        source=SOURCE_PROBE,
        evidence=evidence,
    )
    logger.warning(
        "RESPONSE SURFACE: {}/{} is served on {} -- learned from a probe; later "
        "requests go straight there",
        provider_id,
        model_id,
        ", ".join(surface.value for surface in ordered),
    )


def remember_response_surface(
    provider_id: str,
    model_id: str,
    surface: ResponseSurface,
    *,
    evidence: str = "",
) -> None:
    """Write down one surface a probe proved, so nobody pays the 500 twice."""

    remember_response_surfaces(provider_id, model_id, (surface,), evidence=evidence)
