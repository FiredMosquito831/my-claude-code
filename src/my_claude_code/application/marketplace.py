"""The price ceiling one marketplace attempt is sent, and every refusal of it (7.109.0).

``config/marketplaces.py`` declares how a marketplace takes a ceiling; this is
the *choice*: for one attempt on one model, which profile applies, what the
ceiling is, and whether the request may go at all. A pure function of the
provider, the model, the two override rows, the profile store and the market
snapshot -- read per attempt, like the prompt-cache lifetime, so a fallback
rung to another model on the same marketplace gets its own ceiling.

Three outcomes and no fourth:

* ``None`` -- nothing is sent. Every provider that declares no marketplace
  never reaches this module; for one that does, no row names a profile, or a
  row says ``null`` ("the marketplace decides"), or the profile is
  ``inferhub_decides``. The request is the request it has always been.
* a :class:`MarketplaceChoice` -- the headers to put on every try of this
  attempt, on whichever door it uses, and the note the request log keeps.
* :class:`~my_claude_code.application.errors.InvalidRequestError` -- a short
  400 in the door's own shape, before any upstream call: the profile is gone,
  it cannot compute a number for this model, it excludes the model's own
  upstream, or the snapshot already shows nothing under the ceiling. Never a
  silent switch and never "send it uncapped": sending without a ceiling is
  exactly the overcharge (the user's Q11).

Nothing here invents a number (IV.1): a ceiling is the operator's, or
InferHub's own official price times the operator's percentage, or a level the
market snapshot read.
"""

import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfile,
    current_marketplace_profiles,
)
from my_claude_code.config.marketplaces import (
    KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS,
    KIND_CHEAPEST_LEVEL_NOW,
    KIND_MARKETPLACE_DECIDES,
    KIND_MAX_PRICE,
    KIND_PCT_OF_OFFICIAL,
    MarketplaceDialect,
    snapshot_key,
    upstream_prefix,
)
from my_claude_code.config.model_overrides import (
    StatedMarketplaceProfile,
    current_model_overrides,
    model_ref_for,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.core.derived_cache import DerivedCache

#: The longest refusal MCC answers with: one sentence, never an echo (7.102.3).
MAX_REFUSAL_BYTES = 300
#: The longest model id a refusal repeats.
_MAX_MODEL_CHARS = 72
#: A price is compared with this much slack, so a ceiling typed as the very
#: price the market shows is never refused for a float's last digit.
_PRICE_EPSILON = 1e-9

FROM_MODEL_OVERRIDE = "model_override"
FROM_PROVIDER_OVERRIDE = "provider_override"

#: Where derived documents live; the same two segments the catalogue uses.
_CACHE_DIRNAME = "cache"
_CACHE_SUBDIR = "derived"


# ------------------------------------------------------------- the snapshot


@dataclass(frozen=True, slots=True)
class MarketModel:
    """What the snapshot knows about one model's market."""

    #: InferHub's official price, input and output, USD per 1M tokens.
    official: tuple[float, float] | None = None
    #: ``(price, accounts)`` per level, cheapest first, from the keyed list.
    levels_in: tuple[tuple[float, int], ...] = ()
    levels_out: tuple[tuple[float, int], ...] = ()
    #: The cheapest asks, input and output (the list row's or the public
    #: market's), when known.
    min_ask: tuple[float | None, float | None] = (None, None)

    def lowest(self, side: int) -> float | None:
        """The cheapest known price on one side (0 input, 1 output)."""

        levels = self.levels_in if side == 0 else self.levels_out
        if levels:
            return levels[0][0]
        return self.min_ask[side]

    def level_with_depth(self, side: int, accounts: int) -> float | None:
        """The cheapest price with at least ``accounts`` sellers at or below it."""

        levels = self.levels_in if side == 0 else self.levels_out
        total = 0
        for price, count in levels:
            total += count
            if total >= accounts:
                return price
        return None


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """The stored snapshot, parsed. Built by the refresher, read per attempt."""

    fetched_at: str | None = None
    models: Mapping[str, MarketModel] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def model(self, model_id: str) -> MarketModel | None:
        return self.models.get(model_id)

    def stamp(self) -> str:
        """When it was read, as a refusal says it (``2026-10-24 01:07Z``)."""

        if not self.fetched_at:
            return "no snapshot yet"
        try:
            when = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return self.fetched_at[:20]
        return when.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 2:
        return None
    first, second = _price(value[0]), _price(value[1])
    if first is None or second is None:
        return None
    return first, second


def _levels(value: Any) -> tuple[tuple[float, int], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    out: list[tuple[float, int]] = []
    for entry in value:
        if not isinstance(entry, Sequence) or isinstance(entry, str) or len(entry) != 2:
            continue
        price = _price(entry[0])
        count = entry[1]
        if price is None or isinstance(count, bool) or not isinstance(count, int):
            continue
        if count > 0:
            out.append((price, count))
    return tuple(sorted(out))


def market_model_from_document(entry: Any) -> MarketModel | None:
    """One ``models`` entry of a stored snapshot; ``None`` if it is not one."""

    if not isinstance(entry, Mapping):
        return None
    minimum = entry.get("min")
    if isinstance(minimum, Sequence) and not isinstance(minimum, str) and len(minimum) == 2:
        min_ask = (_price(minimum[0]), _price(minimum[1]))
    else:
        min_ask = (None, None)
    return MarketModel(
        official=_pair(entry.get("official")),
        levels_in=_levels(entry.get("levels_in")),
        levels_out=_levels(entry.get("levels_out")),
        min_ask=min_ask,
    )


def market_snapshot_from_payload(payload: Any) -> MarketSnapshot:
    """A stored snapshot payload, parsed; an unusable one is an empty snapshot."""

    if not isinstance(payload, Mapping):
        return MarketSnapshot()
    models: dict[str, MarketModel] = {}
    raw_models = payload.get("models")
    if isinstance(raw_models, Mapping):
        for model_id, entry in raw_models.items():
            parsed = market_model_from_document(entry)
            if parsed is not None:
                models[str(model_id)] = parsed
    fetched_at = payload.get("fetched_at")
    return MarketSnapshot(
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        models=models,
        payload=payload,
    )


def market_snapshot_cache() -> DerivedCache:
    """The store the snapshot is written to and read from."""

    return DerivedCache(config_dir_path() / _CACHE_DIRNAME / _CACHE_SUBDIR)


_snapshot_lock = threading.Lock()
#: ``entry -> (path, mtime_ns, size, snapshot)``: parsed once per file version.
_snapshots: dict[str, tuple[str, int, int, MarketSnapshot]] = {}


def reset_market_snapshot_cache() -> None:
    """Forget every parsed snapshot (tests; a config-dir change)."""

    with _snapshot_lock:
        _snapshots.clear()


def current_market_snapshot(
    dialect: MarketplaceDialect, cache: DerivedCache | None = None
) -> MarketSnapshot | None:
    """The stored snapshot for one marketplace, or ``None`` when there is none.

    Read per attempt, parsed only when the file changed -- one ``stat`` on the
    request path, the cost the override file already pays.
    """

    store = cache if cache is not None else market_snapshot_cache()
    try:
        path: Path = store.path_for(dialect.snapshot_entry)
        stat = path.stat()
    except OSError, ValueError:
        return None
    signature = (str(path), stat.st_mtime_ns, stat.st_size)
    with _snapshot_lock:
        cached = _snapshots.get(dialect.snapshot_entry)
    if cached is not None and cached[:3] == signature:
        return cached[3]
    entry = store.read(dialect.snapshot_entry)
    if entry is None or not entry.matches(snapshot_key(dialect)):
        return None
    snapshot = market_snapshot_from_payload(entry.payload)
    with _snapshot_lock:
        _snapshots[dialect.snapshot_entry] = (*signature, snapshot)
    return snapshot


# ---------------------------------------------------------------- the choice


@dataclass(frozen=True, slots=True)
class MarketplaceChoice:
    """The ceiling one attempt sends, and where it came from."""

    dialect_id: str
    profile: str
    #: Which override row named the profile.
    origin: str
    kind: str
    #: Input and output ceilings, USD per 1M tokens; ``None`` = not capped.
    ceiling_in: float | None
    ceiling_out: float | None
    excluded: tuple[str, ...]
    #: The request headers, in the order they go out.
    headers: Mapping[str, str]
    #: How the numbers were reached, in the operator's words.
    derived_from: str
    max_usd_per_request: float | None = None

    def wire_note(self) -> dict[str, Any]:
        """What the request log keeps beside the body (``params.wire.marketplace``)."""

        return {
            "marketplace": {
                "profile": self.profile,
                "from": self.origin,
                "kind": self.kind,
                "sent": dict(self.headers),
                "derived_from": self.derived_from,
            }
        }

    def ceiling_text(self) -> str:
        return f"{_usd(self.ceiling_in)} / {_usd(self.ceiling_out)} per Mtok"


def price_text(value: float | None) -> str:
    """A price as a header carries it: plain decimal, no exponent, no trailing 0."""

    if value is None:
        return ""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _usd(value: float | None) -> str:
    return "uncapped" if value is None else f"${price_text(value)}"


def _short_model(model_id: str) -> str:
    text = model_id.strip()
    return text if len(text) <= _MAX_MODEL_CHARS else text[: _MAX_MODEL_CHARS - 3] + "..."


def bounded_refusal(text: str) -> str:
    """One refusal sentence, never longer than :data:`MAX_REFUSAL_BYTES`."""

    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_REFUSAL_BYTES:
        return text
    return encoded[: MAX_REFUSAL_BYTES - 3].decode("utf-8", errors="ignore") + "..."


def _refuse(text: str) -> InvalidRequestError:
    return InvalidRequestError(bounded_refusal(text))


def stated_profile(
    provider_id: str, model_id: str
) -> tuple[StatedMarketplaceProfile, str] | None:
    """The row that decides for this model, and which row it is, or ``None``.

    Model row over provider row; absent at both levels is ``None``.
    """

    model_row, provider_row = current_model_overrides().marketplace_profile_rows(
        provider_id, model_ref_for(provider_id, model_id)
    )
    if model_row is not None:
        return model_row, FROM_MODEL_OVERRIDE
    if provider_row is not None:
        return provider_row, FROM_PROVIDER_OVERRIDE
    return None


def _row_words(origin: str) -> str:
    return "this model's row" if origin == FROM_MODEL_OVERRIDE else "the provider row"


def marketplace_choice(
    dialect: MarketplaceDialect | None, provider_id: str, model_id: str
) -> MarketplaceChoice | None:
    """The ceiling for one attempt, ``None`` to send nothing, or a 400.

    ``dialect`` is the provider profile's declaration; ``None`` -- every
    provider but a marketplace -- returns ``None`` before anything is read.
    """

    if dialect is None:
        return None
    stated = stated_profile(provider_id, model_id)
    if stated is None:
        return None
    row, origin = stated
    if row.name is None:
        return None
    profile = current_marketplace_profiles().get(row.name)
    if profile is None:
        raise _refuse(
            f"{dialect.label} profile '{row.name}' not found (set on "
            f"{_row_words(origin)} for {provider_id}/{_short_model(model_id)}). "
            "MCC sends no request without the ceiling it names: add the "
            "profile back or set the row to another one."
        )
    if profile.kind == KIND_MARKETPLACE_DECIDES:
        return None
    return _choice_for_profile(dialect, profile, origin, model_id)


def _choice_for_profile(
    dialect: MarketplaceDialect,
    profile: MarketplaceProfile,
    origin: str,
    model_id: str,
) -> MarketplaceChoice:
    snapshot = current_market_snapshot(dialect)
    market = snapshot.model(model_id) if snapshot is not None else None
    stamp = snapshot.stamp() if snapshot is not None else "no snapshot yet"
    model = _short_model(model_id)
    prefix = upstream_prefix(model_id)
    if prefix and prefix in profile.exclude_upstreams:
        raise _refuse(
            f"Profile '{profile.name}' excludes the upstream '{prefix}', which is "
            f"this model's own ({model}); {dialect.label} would answer 404. Pick "
            "another model or profile."
        )

    def official(why: str) -> tuple[float, float]:
        if market is None or market.official is None:
            raise _refuse(
                f"No official {dialect.label} price is known for {model}, so "
                f"profile '{profile.name}' ({why}) cannot compute a ceiling "
                f"(snapshot {stamp}). MCC does not send it uncapped: pick a "
                "dollar ceiling for this model."
            )
        return market.official

    ceiling_in: float | None
    ceiling_out: float | None
    if profile.kind == KIND_PCT_OF_OFFICIAL:
        percent = profile.percent or 0.0
        base = official(f"{price_text(percent)} % of official")
        ceiling_in = base[0] * percent / 100
        ceiling_out = base[1] * percent / 100
        derived = (
            f"{price_text(percent)} % of official ${price_text(base[0])} / "
            f"${price_text(base[1])}"
        )
    elif profile.kind == KIND_MAX_PRICE:
        ceiling_in, ceiling_out = profile.max_input_usd, profile.max_output_usd
        derived = "your dollar ceiling"
    else:
        if market is None:
            raise _refuse(
                f"No {dialect.label} price levels are known for {model} "
                f"(snapshot {stamp}), so profile '{profile.name}' cannot follow "
                "the market. MCC does not send it uncapped."
            )
        if profile.kind == KIND_CHEAPEST_LEVEL_NOW:
            ceiling_in, ceiling_out = market.lowest(0), market.lowest(1)
            derived = f"market snapshot {stamp} lowest level"
        else:
            depth = profile.min_accounts or 1
            ceiling_in = market.level_with_depth(0, depth)
            ceiling_out = market.level_with_depth(1, depth)
            derived = f"market snapshot {stamp} cheapest level with >= {depth} accounts"
        if ceiling_in is None or ceiling_out is None:
            raise _refuse(
                f"The {dialect.label} market snapshot ({stamp}) has no level for "
                f"{model} that profile '{profile.name}' accepts, so it cannot "
                "compute a ceiling. MCC does not send it uncapped."
            )
        derived += f" ${price_text(ceiling_in)} / ${price_text(ceiling_out)}"
        cap_in, cap_out = profile.cap_input_usd, profile.cap_output_usd
        if profile.cap_percent is not None:
            base = official(f"a cap of {price_text(profile.cap_percent)} % of official")
            cap_in = base[0] * profile.cap_percent / 100
            cap_out = base[1] * profile.cap_percent / 100
        capped_in = ceiling_in if cap_in is None else min(ceiling_in, cap_in)
        capped_out = ceiling_out if cap_out is None else min(ceiling_out, cap_out)
        if (capped_in, capped_out) != (ceiling_in, ceiling_out):
            derived += f", capped at {_usd(cap_in)} / {_usd(cap_out)}"
        ceiling_in, ceiling_out = capped_in, capped_out

    headers: dict[str, str] = {}
    if ceiling_in is not None:
        headers[dialect.ceiling_headers[0]] = price_text(ceiling_in)
    if ceiling_out is not None:
        headers[dialect.ceiling_headers[1]] = price_text(ceiling_out)
    if profile.exclude_upstreams and dialect.exclude_header is not None:
        headers[dialect.exclude_header] = ",".join(profile.exclude_upstreams)
    choice = MarketplaceChoice(
        dialect_id=dialect.dialect_id,
        profile=profile.name,
        origin=origin,
        kind=profile.kind,
        ceiling_in=ceiling_in,
        ceiling_out=ceiling_out,
        excluded=profile.exclude_upstreams,
        headers=headers,
        derived_from=derived,
        max_usd_per_request=profile.max_usd_per_request,
    )
    if market is not None:
        _refuse_when_nothing_fits(dialect, choice, market, model, stamp)
    return choice


def _refuse_when_nothing_fits(
    dialect: MarketplaceDialect,
    choice: MarketplaceChoice,
    market: MarketModel,
    model: str,
    stamp: str,
) -> None:
    """The free pre-check: the snapshot's cheapest level is above the ceiling."""

    lowest_in, lowest_out = market.lowest(0), market.lowest(1)
    over = (
        choice.ceiling_in is not None
        and lowest_in is not None
        and lowest_in > choice.ceiling_in + _PRICE_EPSILON
    ) or (
        choice.ceiling_out is not None
        and lowest_out is not None
        and lowest_out > choice.ceiling_out + _PRICE_EPSILON
    )
    if not over:
        return
    raise _refuse(
        f"No {dialect.label} offer is under your ceiling for {model} (profile "
        f"'{choice.profile}': {choice.ceiling_text()}; cheapest now "
        f"{_usd(lowest_in)} / {_usd(lowest_out)}, snapshot {stamp}). Raise the "
        "ceiling or pick another profile."
    )


def check_request_cost(
    choice: MarketplaceChoice | None,
    *,
    model_id: str,
    input_tokens: int,
    max_tokens: int | None,
) -> None:
    """Refuse before sending when the worst case is over the per-request limit.

    The worst case is every input token (cache reads included -- their rate is
    third-party knowledge only) at the input ceiling plus the whole output
    allowance at the output ceiling. A profile with a per-request limit always
    has both ceilings (the store refuses one without), and an allowance MCC
    does not know is an unbounded worst case, refused for the same reason.
    """

    if choice is None or choice.max_usd_per_request is None:
        return
    if choice.ceiling_in is None or choice.ceiling_out is None or max_tokens is None:
        raise _refuse(
            f"Profile '{choice.profile}' limits a request to "
            f"${price_text(choice.max_usd_per_request)}, and this request's worst "
            f"case for {_short_model(model_id)} is not bounded. MCC does not send it."
        )
    worst = (
        max(0, input_tokens) * choice.ceiling_in + max(0, max_tokens) * choice.ceiling_out
    ) / 1_000_000
    if worst <= choice.max_usd_per_request + _PRICE_EPSILON:
        return
    raise _refuse(
        f"Profile '{choice.profile}' limits a request to "
        f"${price_text(choice.max_usd_per_request)}; this one could cost up to "
        f"${price_text(worst)} on {_short_model(model_id)} ({input_tokens} tokens in, "
        f"{max_tokens} out at {choice.ceiling_text()}). Lower max_tokens or raise "
        "the limit."
    )


def no_offer_message(
    dialect: MarketplaceDialect,
    *,
    model_id: str,
    choice: MarketplaceChoice | None,
    minimums: tuple[float | None, float | None],
) -> str:
    """The marketplace's own "nothing under your ceiling", in one short sentence.

    ``choice`` is ``None`` when MCC sent no ceiling: the refusal then came
    from a limit on the operator's marketplace account itself.
    """

    whose = (
        f"your ceiling (profile '{choice.profile}': {choice.ceiling_text()})"
        if choice is not None
        else f"the limit set on your {dialect.label} account"
    )
    return bounded_refusal(
        f"{dialect.label} has no offer under {whose} for "
        f"{_short_model(model_id)}; it says the cheapest now is "
        f"{_usd(minimums[0])} / {_usd(minimums[1])} per Mtok. MCC does not "
        "re-bid: raise the ceiling or pick another profile."
    )


__all__ = [
    "FROM_MODEL_OVERRIDE",
    "FROM_PROVIDER_OVERRIDE",
    "MAX_REFUSAL_BYTES",
    "MarketModel",
    "MarketSnapshot",
    "MarketplaceChoice",
    "bounded_refusal",
    "check_request_cost",
    "current_market_snapshot",
    "market_model_from_document",
    "market_snapshot_cache",
    "market_snapshot_from_payload",
    "marketplace_choice",
    "no_offer_message",
    "price_text",
    "reset_market_snapshot_cache",
    "stated_profile",
]
