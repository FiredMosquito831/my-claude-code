"""Which prompt-cache lifetime the operator asked for, and what a host gets (7.104.0).

Two questions, answered in two places on purpose:

* **What was asked** depends on the route, because the route decides the
  tier: ``route_prompt_cache_ask`` reads ``PROMPT_CACHE_TTL_<TIER>`` and then
  ``PROMPT_CACHE_TTL`` for the model name the client sent, with exactly the
  tier rules routing applies to ``REASONING_<TIER>`` (a ``mcc/<tier>`` alias,
  then a direct ``provider/model`` ref, which only the global setting covers,
  then the Claude-name keywords in routing's order). The door stamps the
  answer on the request before the plan is built.
* **What is sent** depends on the host, because the host decides the
  lifetimes: ``choose_prompt_cache_lifetime`` adapts the asked minutes to one
  provider's declared lifetimes (``config/prompt_cache_lifetimes.py``), less
  any the host has refused, per attempt.

``resolve_prompt_cache_ask`` is the precedence between them -- model row, then
provider row, then the tier, then the global setting, then the client. The two
override rows have no source yet (they arrive with the Models page controls)
and are passed as ``None``, which is "not set at this level".

Nothing set anywhere is ``None`` at every step, and ``None`` is what leaves a
body exactly as the client sent it.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from my_claude_code.config.constants import (
    PROMPT_CACHE_TTL_CLIENT,
    PROMPT_CACHE_TTL_INHERIT,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    CacheLifetime,
    PromptCacheLifetimeFamily,
    prompt_cache_ttl_minutes,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest, PromptCacheAsk
from my_claude_code.core.gateway_model_ids import decode_gateway_model_id
from my_claude_code.core.tier_refs import (
    TIER_PROMPT_CACHE_TTL_SETTINGS,
    parse_tier_ref,
)

#: The global setting's name, which is also the source a request records.
GLOBAL_PROMPT_CACHE_TTL_ENV = "PROMPT_CACHE_TTL"

#: The Claude-name keywords routing matches, in routing's order
#: (``application/routing._ROUTE_SETTINGS``), each with the lifetime setting
#: its route reads. A substring match, first hit wins, exactly as routing's --
#: pinned to that table by a contract test, so a keyword added there and not
#: here fails CI instead of sending one route's lifetime on another.
ROUTE_PROMPT_CACHE_TTL_SETTINGS: tuple[tuple[str, str], ...] = (
    ("mythos", "prompt_cache_ttl_mythos"),
    ("fable", "prompt_cache_ttl_fable"),
    ("opus", "prompt_cache_ttl_opus"),
    ("haiku", "prompt_cache_ttl_haiku"),
    ("sonnet", "prompt_cache_ttl_sonnet"),
)


def _env_name(settings_attr: str) -> str:
    return settings_attr.upper()


def resolve_prompt_cache_ask(
    *,
    model_row: str | None = None,
    provider_row: str | None = None,
    tier: tuple[str, str] | None = None,
    global_value: str | None = None,
) -> PromptCacheAsk | None:
    """The first level that decided, as an ask; ``None`` means "from client".

    Each level is a stored setting value -- whole minutes, ``client`` or
    ``inherit`` -- or ``None`` when the level has nothing to say. ``inherit``
    and ``None`` pass the question down; ``client`` stops it there with no
    ask, so a level can say "MCC sets nothing here" over a level below it.
    ``tier`` is ``(setting name, value)`` so the record can name the tier.
    """

    levels: list[tuple[str, str | None]] = [
        ("model override", model_row),
        ("provider override", provider_row),
    ]
    if tier is not None:
        levels.append(tier)
    levels.append((GLOBAL_PROMPT_CACHE_TTL_ENV, global_value))
    for source, value in levels:
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text or text == PROMPT_CACHE_TTL_INHERIT:
            continue
        if text == PROMPT_CACHE_TTL_CLIENT:
            return None
        minutes = prompt_cache_ttl_minutes(text)
        if minutes is None:
            # A value no validator produced: never a reason to change bytes.
            return None
        return PromptCacheAsk(minutes=minutes, source=source)
    return None


def _is_direct_provider_model(model_name: str) -> bool:
    """Whether routing reads this name as an explicit ``provider/model`` ref.

    The same two shapes ``ModelRouter._direct_provider_model`` accepts: a
    gateway model id that decodes to a supported provider, or
    ``<supported provider>/<non-empty model>``. Such a request is the global
    setting's, as its reasoning is ``REASONING_POLICY``'s.
    """

    supported = get_provider_registry().supported_ids()
    decoded = decode_gateway_model_id(model_name)
    if decoded is not None:
        return decoded.provider_id in supported
    provider_id, separator, provider_model = model_name.partition("/")
    return bool(separator) and provider_id in supported and bool(provider_model)


def route_tier_prompt_cache_setting(model_name: str) -> str | None:
    """The ``Settings`` attribute of the tier this model name routes through.

    ``None`` when no tier setting applies -- the vision tier, a direct ref, a
    name with no route keyword -- which leaves the global setting in charge.
    """

    name = str(model_name or "")
    tier = parse_tier_ref(name)
    if tier is not None:
        return TIER_PROMPT_CACHE_TTL_SETTINGS.get(tier)
    if _is_direct_provider_model(name):
        return None
    normalized = name.lower()
    return next(
        (
            attr
            for keyword, attr in ROUTE_PROMPT_CACHE_TTL_SETTINGS
            if keyword in normalized
        ),
        None,
    )


def _nothing_set(settings: Settings) -> bool:
    """No lifetime setting asks for minutes: the state of a default install."""

    attrs = ("prompt_cache_ttl", *TIER_PROMPT_CACHE_TTL_SETTINGS.values())
    return all(
        prompt_cache_ttl_minutes(str(getattr(settings, attr))) is None for attr in attrs
    )


def route_prompt_cache_ask(
    settings: Settings, model_name: str
) -> PromptCacheAsk | None:
    """What the operator asked for on this request's route; ``None`` = nothing.

    Read from the settings object the door holds, which is the one its lease
    built for this request, so a saved value applies to the next request with
    no restart. With no setting asking for minutes the answer is ``None``
    before the model name is even looked at.
    """

    if _nothing_set(settings):
        return None
    attr = route_tier_prompt_cache_setting(model_name)
    tier = None if attr is None else (_env_name(attr), str(getattr(settings, attr)))
    return resolve_prompt_cache_ask(
        tier=tier, global_value=str(settings.prompt_cache_ttl)
    )


def stamp_prompt_cache_ask(settings: Settings, request: MessagesRequest) -> None:
    """Stamp a door's request with the route's ask, before its plan is built.

    Writes nothing when nothing is asked, so a request with no lifetime
    setting leaves its door exactly as it arrived. The stamp is a private
    attribute: no dump carries it, and every routed copy of the request does.
    """

    ask = route_prompt_cache_ask(settings, request.model)
    if ask is not None:
        request.set_prompt_cache_ask(ask)


def adapt_prompt_cache_minutes(
    minutes: int, lifetimes: Iterable[CacheLifetime]
) -> CacheLifetime | None:
    """The host lifetime for ``minutes``: the shortest at least that long.

    Else the host's longest, because asking for more than a host keeps is
    answered with the most it keeps, never with a refusal (IV.2). ``None``
    only when the host accepts no lifetime at all.
    """

    ordered = sorted(lifetimes, key=lambda lifetime: lifetime.minutes)
    if not ordered:
        return None
    return next(
        (lifetime for lifetime in ordered if lifetime.minutes >= minutes),
        ordered[-1],
    )


@dataclass(frozen=True, slots=True)
class PromptCacheLifetimeChoice:
    """What one attempt sends a host, and why (the request records it)."""

    requested_minutes: int
    source: str
    family: PromptCacheLifetimeFamily
    #: Declared lifetimes this host refused for this model, learned from its
    #: own 400s. Never sent while the fact is fresh.
    refused: tuple[str, ...]
    #: The lifetime written, or ``None`` when the host has refused every one
    #: it was declared to take -- then nothing is written at all.
    sent: CacheLifetime | None

    @property
    def host_lifetimes(self) -> tuple[str, ...]:
        """The host's declared lifetimes, shortest first, in its own spelling."""
        return tuple(lifetime.wire for lifetime in self.family.lifetimes)

    def note(self) -> dict[str, object]:
        """The ``params.wire`` record of this choice."""
        record: dict[str, object] = {
            "requested": self.requested_minutes,
            "from": self.source,
            "sent": None if self.sent is None else self.sent.wire,
            "host_lifetimes": list(self.host_lifetimes),
            "declared_for": self.family.label,
        }
        if self.refused:
            record["refused_by_host"] = list(self.refused)
        return record


def choose_prompt_cache_lifetime(
    ask: PromptCacheAsk | None,
    family: PromptCacheLifetimeFamily | None,
    *,
    refused: Iterable[str] = (),
) -> PromptCacheLifetimeChoice | None:
    """Adapt one ask to one host; ``None`` when nothing is asked or declared.

    ``None`` is the common case and the one that matters: no ask (nothing
    set, or ``client``) or a host with no declared lifetimes leaves the body
    untouched. A host that refused every declared lifetime still gets a
    choice -- with nothing ``sent`` -- so the request can say why.
    """

    if ask is None or family is None:
        return None
    refused_set = frozenset(str(value) for value in refused)
    usable = [life for life in family.lifetimes if life.wire not in refused_set]
    return PromptCacheLifetimeChoice(
        requested_minutes=ask.minutes,
        source=ask.source,
        family=family,
        refused=tuple(
            life.wire for life in family.lifetimes if life.wire in refused_set
        ),
        sent=adapt_prompt_cache_minutes(ask.minutes, usable),
    )


__all__ = [
    "GLOBAL_PROMPT_CACHE_TTL_ENV",
    "ROUTE_PROMPT_CACHE_TTL_SETTINGS",
    "PromptCacheLifetimeChoice",
    "adapt_prompt_cache_minutes",
    "choose_prompt_cache_lifetime",
    "resolve_prompt_cache_ask",
    "route_prompt_cache_ask",
    "route_tier_prompt_cache_setting",
    "stamp_prompt_cache_ask",
]
