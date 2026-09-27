"""Build the ordered candidates for a media request from its rail's settings.

The Vision adapter's chain (``application/routing.py`` ``_vision_adapter_chain``)
is the pattern: primary, then fallbacks, de-duplicated, an unknown provider
skipped with a warning rather than taking the whole rail down. Media refs are
never added to the chat catalogue (``configured_chat_model_refs``), so nothing
here is advertised to a coding agent.
"""

from collections.abc import Callable, Mapping

from loguru import logger

from my_claude_code.application.errors import ApplicationError
from my_claude_code.application.routing import ModelRouter, ResolvedModel
from my_claude_code.config.model_refs import (
    parse_model_name,
    parse_model_ref_list,
    parse_provider_type,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.failures import FailureKind

from .request import (
    RAIL_SETTINGS,
    MediaAttempt,
    MediaPlan,
    MediaRail,
    MediaRequest,
)


class MediaRailNotConfigured(ApplicationError):
    """The rail this endpoint routes on has no model configured.

    Never answered by borrowing a chat model: an image request sent to a text
    model is billed and fails, and a silent re-route is the one thing a rail
    exists to prevent.
    """

    kind = FailureKind.INVALID_REQUEST
    status_code = 404


def _setting(settings: Settings, attr: str) -> str:
    value = getattr(settings, attr, None)
    return value if isinstance(value, str) else ""


def _known_provider(provider_id: str) -> bool:
    return provider_id in get_provider_registry().all_descriptors()


def _resolved(original_model: str, model_ref: str) -> ResolvedModel:
    return ResolvedModel(
        original_model=original_model,
        provider_id=parse_provider_type(model_ref),
        provider_model=parse_model_name(model_ref),
        provider_model_ref=model_ref,
        reasoning_preference=ReasoningPreference.INHERIT,
    )


def rail_refs(settings: Settings, rail: MediaRail) -> tuple[str, ...]:
    """The rail's configured refs in order: primary, then its fallbacks."""

    names = RAIL_SETTINGS[rail]
    refs: list[str] = []
    primary = _setting(settings, names.model_attr).strip()
    if primary:
        refs.append(primary)
    refs.extend(parse_model_ref_list(_setting(settings, names.fallbacks_attr)))
    seen: set[str] = set()
    ordered: list[str] = []
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        ordered.append(ref)
    return tuple(ordered)


def configured_media_model_refs(settings: Settings) -> tuple[str, ...]:
    """Every ref on every media rail -- the media twin of the chat function.

    Kept apart from ``config.model_refs.configured_chat_model_refs`` on
    purpose: that list feeds ``/v1/models`` and every harness catalogue, and an
    image model offered to a coding agent as a chat model is a request that
    can only fail.
    """

    refs: list[str] = []
    for rail in MediaRail:
        refs.extend(ref for ref in rail_refs(settings, rail) if ref not in refs)
    return tuple(refs)


class MediaRouter:
    """Turn a :class:`MediaRequest` into the plan the media executor walks."""

    def __init__(
        self,
        settings: Settings,
        *,
        probe_candidates: Callable[[], Mapping[str, ResolvedModel]] | None = None,
    ) -> None:
        self._settings = settings
        self._probe_candidates = probe_candidates

    def plan(self, request: MediaRequest) -> MediaPlan:
        names = RAIL_SETTINGS[request.rail]
        requested = request.model.strip()
        refs: tuple[str, ...]
        if (
            requested
            and "/" in requested
            and _known_provider(parse_provider_type(requested))
        ):
            # A ``provider/model`` ref pins one candidate, the same rule the
            # chat router applies to a direct ref.
            refs = (requested,)
        else:
            refs = rail_refs(self._settings, request.rail)
        attempts: list[MediaAttempt] = []
        for ref in refs:
            provider_id = parse_provider_type(ref)
            if not _known_provider(provider_id):
                logger.warning(
                    "MEDIA ROUTE SKIPPED: '{}' names unknown provider '{}'",
                    ref,
                    provider_id,
                )
                continue
            attempts.append(
                MediaAttempt(request=request, resolved=_resolved(request.model, ref))
            )
        if not attempts:
            raise MediaRailNotConfigured(
                f"No model is configured for the {names.label} rail. Set "
                f"{names.model_env} (and optionally {names.fallbacks_env}) on "
                "Model Config; a media request is never sent to a chat model."
            )
        paused = frozenset(
            parse_model_ref_list(_setting(self._settings, names.paused_attr))
        )
        return MediaPlan(
            attempts=tuple(attempts),
            paused_refs=paused,
            paused_env_var=names.paused_env,
            probe_candidates=(
                {} if self._probe_candidates is None else dict(self._probe_candidates())
            ),
        )


def chat_probe_candidates(settings: Settings) -> Mapping[str, ResolvedModel]:
    """The chat executor's probe candidates, from the chat router itself.

    Read through the router rather than re-derived so the two executors ask
    the same model the same question -- the parity contract depends on it.
    """

    return ModelRouter(settings)._probe_candidates()
