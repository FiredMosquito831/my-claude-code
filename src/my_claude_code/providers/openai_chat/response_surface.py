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
3. models.dev's cached copy of the vendor's registry ->
   :data:`ResponseSurfaceSource.REGISTRY`.
4. Chat Completions -> :data:`ResponseSurfaceSource.DEFAULT`.

A model whose real surface this provider cannot speak resolves to
:data:`ResponseSurface.UNSERVABLE` **with the reason attached**, and is still
listed. Hiding it was considered and rejected: a hiding rule that can produce a
false positive takes a working model away from the user, and the one thing
worse than a model that fails loudly is a model that is silently not there.
"""

from collections.abc import Mapping

from loguru import logger

from my_claude_code.application.model_metadata import (
    ResolvedResponseSurface,
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config.model_overrides import (
    RESPONSE_SURFACE_OVERRIDE,
    current_model_overrides,
    model_ref_for,
)
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


def learned_surface(provider_id: str, model_id: str) -> ResponseSurface | None:
    """Return the surface a probe of *this deployment* actually proved."""

    if not provider_id or not model_id:
        return None
    for fact in learned_fact_store().fresh_facts_for_provider(provider_id):
        if fact.model_id != model_id or fact.fact_kind != FACT_RESPONSE_SURFACE:
            continue
        try:
            return ResponseSurface(str(fact.value))
        except ValueError:
            return None
    return None


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


def resolve_response_surface(
    provider_id: str,
    model_id: str,
    *,
    registry_provider: str = "",
    declared: tuple[ResponseSurface, ...] = (),
) -> ResolvedResponseSurface:
    """Resolve one model's surface, and say where the answer came from.

    ``declared`` is what the *profile* says this provider can speak. A resolved
    surface that is not in it becomes :data:`ResponseSurface.UNSERVABLE`
    carrying the reason, so the Models page can name it rather than the model
    quietly vanishing. An empty ``declared`` means the profile made no
    statement -- which is every profile but the two OpenCode ones -- and those
    keep the default with no unservable case at all, exactly as before.
    """

    speakable = declared or (DEFAULT_SURFACE,)

    forced = override_surface(provider_id, model_id)
    if forced is not None:
        return _constrain(forced, ResponseSurfaceSource.OVERRIDE, "", speakable)

    proved = learned_surface(provider_id, model_id)
    if proved is not None:
        return _constrain(
            proved, ResponseSurfaceSource.LEARNED, "proved by a probe", speakable
        )

    published = registry_surface(registry_provider, model_id)
    if published is not None:
        surface, npm = published
        return _constrain(surface, ResponseSurfaceSource.REGISTRY, npm, speakable)

    return ResolvedResponseSurface(DEFAULT_SURFACE, ResponseSurfaceSource.DEFAULT)


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

    ``None`` for every provider whose profile declares no surfaces -- 39 of the
    41 -- so their rows are byte-identical to what they were. The lookup is a
    registry read, not a branch: the profile table is keyed by catalogue id
    exactly as the provider factory keys it.
    """

    from .profiles import OPENAI_CHAT_PROFILES

    profile = OPENAI_CHAT_PROFILES.get(provider_id)
    if profile is None or not profile.response_surfaces:
        return None
    return resolve_response_surface(
        provider_id,
        model_id,
        registry_provider=profile.surface_registry_provider,
        declared=profile.response_surfaces,
    )


def alternative_surfaces(
    current: ResponseSurface, declared: tuple[ResponseSurface, ...]
) -> tuple[ResponseSurface, ...]:
    """The surfaces still worth trying after ``current`` failed, in order."""

    return tuple(
        surface
        for surface in declared
        if surface is not current and surface is not ResponseSurface.UNSERVABLE
    )


def remember_response_surface(
    provider_id: str,
    model_id: str,
    surface: ResponseSurface,
    *,
    evidence: str = "",
) -> None:
    """Write down a surface a probe proved, so nobody pays the 500 twice."""

    if not provider_id or not model_id:
        return
    learned_fact_store().record(
        provider_id,
        model_id,
        FACT_RESPONSE_SURFACE,
        surface.value,
        source=SOURCE_PROBE,
        evidence=evidence,
    )
    logger.warning(
        "RESPONSE SURFACE: {}/{} is served on {} -- learned from a probe; later "
        "requests go straight there",
        provider_id,
        model_id,
        surface.value,
    )
