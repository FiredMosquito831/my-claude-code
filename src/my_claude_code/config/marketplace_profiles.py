"""Marketplace selection profiles: ``~/.mcc/marketplace_profiles.json`` (7.109.0).

A *profile* says how much MCC lets a marketplace charge for one model, in a
form that works across a $0.15 model and a $25 one. Its own store, like
``proxy_chains.json`` and for the same reason: a profile is a small object,
and ``model_overrides.json`` refuses objects by design -- its allow-list is a
body-injection boundary. An override row names a profile (the 15th override
key, ``marketplace_profile``); the profile itself lives here::

    {
      "version": 1,
      "profiles": {
        "opus-cap": {"kind": "pct_of_official", "percent": 10},
        "tight":    {"kind": "max_price", "max_input_usd": 0.5,
                     "max_output_usd": 2.5, "max_usd_per_request": 0.2},
        "follow":   {"kind": "cheapest_level_now", "cap_percent": 20,
                     "exclude_upstreams": ["kiro"]}
      }
    }

The kinds (``config/marketplaces.PROFILE_KINDS``, spec §6.2):

* ``inferhub_decides`` -- nothing is sent: the marketplace picks a seller
  regardless of price. Takes no other field.
* ``pct_of_official`` -- at most ``percent`` % of the model's own official
  price (InferHub's number on its list row, never typed here), on both sides.
* ``max_price`` -- at most ``max_input_usd`` / ``max_output_usd`` per 1M
  tokens; a side left out is not capped.
* ``cheapest_level_now`` -- the cheapest price level on the market right now,
  re-derived on every request; an optional cap (``cap_percent``, or
  ``cap_input_usd`` / ``cap_output_usd``) bounds how far it may follow.
* ``cheapest_level_min_accounts`` -- the cheapest level with at least
  ``min_accounts`` seller accounts at or below it (no default N), with the
  same optional cap.

Two modifiers on every kind but the first: ``exclude_upstreams`` (prefixes
never to be routed to) and ``max_usd_per_request`` (refuse a request whose
worst case, at the ceiling, is above that many dollars).

Nothing here invents a number (IV.1): every value is the operator's, and a
derived ceiling comes from the market data the snapshot read. Storage only:
it validates, normalises and persists, and builds no request.
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from loguru import logger

from my_claude_code.config.marketplaces import (
    KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS,
    KIND_CHEAPEST_LEVEL_NOW,
    KIND_MARKETPLACE_DECIDES,
    KIND_MAX_PRICE,
    KIND_PCT_OF_OFFICIAL,
    PROFILE_KINDS,
)
from my_claude_code.config.owner_only import write_owner_only_json
from my_claude_code.config.paths import marketplace_profiles_path

STORE_VERSION = 1
PROFILES_KEY = "profiles"
VERSION_KEY = "version"

#: A profile's name: what an override row stores and every refusal quotes.
PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: An upstream prefix as the marketplace spells it (``cc``, ``kiro``, ``cbcn``).
UPSTREAM_PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

FIELD_KIND = "kind"
FIELD_PERCENT = "percent"
FIELD_MAX_INPUT = "max_input_usd"
FIELD_MAX_OUTPUT = "max_output_usd"
FIELD_MIN_ACCOUNTS = "min_accounts"
FIELD_CAP_PERCENT = "cap_percent"
FIELD_CAP_INPUT = "cap_input_usd"
FIELD_CAP_OUTPUT = "cap_output_usd"
FIELD_EXCLUDE = "exclude_upstreams"
FIELD_MAX_PER_REQUEST = "max_usd_per_request"

#: The fields each kind takes, besides ``kind`` itself.
_CAP_FIELDS = frozenset({FIELD_CAP_PERCENT, FIELD_CAP_INPUT, FIELD_CAP_OUTPUT})
_MODIFIERS = frozenset({FIELD_EXCLUDE, FIELD_MAX_PER_REQUEST})
KIND_FIELDS: Mapping[str, frozenset[str]] = {
    KIND_MARKETPLACE_DECIDES: frozenset(),
    KIND_PCT_OF_OFFICIAL: frozenset({FIELD_PERCENT}) | _MODIFIERS,
    KIND_MAX_PRICE: frozenset({FIELD_MAX_INPUT, FIELD_MAX_OUTPUT}) | _MODIFIERS,
    KIND_CHEAPEST_LEVEL_NOW: _CAP_FIELDS | _MODIFIERS,
    KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS: frozenset({FIELD_MIN_ACCOUNTS})
    | _CAP_FIELDS
    | _MODIFIERS,
}

#: The words the profile editor shows beside a ceiling that follows the
#: market with no cap of the operator's own (the user's Q5).
FOLLOWS_THE_MARKET_NOTE = "follows the market — add a cap to be sure"
#: The warning the default carries wherever it is shown (the user's Q1).
MARKETPLACE_DECIDES_WARNING = (
    "No ceiling: InferHub picks a seller regardless of price, so a request "
    "can be charged up to the highest live ask (its platform cap is half the "
    "official price)."
)


@dataclass(frozen=True, slots=True)
class MarketplaceProfile:
    """One named profile, validated."""

    name: str
    kind: str
    percent: float | None = None
    max_input_usd: float | None = None
    max_output_usd: float | None = None
    min_accounts: int | None = None
    cap_percent: float | None = None
    cap_input_usd: float | None = None
    cap_output_usd: float | None = None
    exclude_upstreams: tuple[str, ...] = ()
    max_usd_per_request: float | None = None

    @property
    def has_cap(self) -> bool:
        """Whether a derived kind is bounded by a cap of the operator's own."""

        return (
            self.cap_percent is not None
            or self.cap_input_usd is not None
            or self.cap_output_usd is not None
        )

    def as_document(self) -> dict[str, Any]:
        """The on-disk shape: ``kind`` first, then only the fields set."""

        out: dict[str, Any] = {FIELD_KIND: self.kind}
        out.update(
            {
                name: value
                for name, value in (
                    (FIELD_PERCENT, self.percent),
                    (FIELD_MAX_INPUT, self.max_input_usd),
                    (FIELD_MAX_OUTPUT, self.max_output_usd),
                    (FIELD_MIN_ACCOUNTS, self.min_accounts),
                    (FIELD_CAP_PERCENT, self.cap_percent),
                    (FIELD_CAP_INPUT, self.cap_input_usd),
                    (FIELD_CAP_OUTPUT, self.cap_output_usd),
                )
                if value is not None
            }
        )
        if self.exclude_upstreams:
            out[FIELD_EXCLUDE] = list(self.exclude_upstreams)
        if self.max_usd_per_request is not None:
            out[FIELD_MAX_PER_REQUEST] = self.max_usd_per_request
        return out

    def summary(self) -> str:
        """One line in the operator's words, for the Models page and refusals."""

        if self.kind == KIND_MARKETPLACE_DECIDES:
            return "InferHub decides (no ceiling)"
        if self.kind == KIND_PCT_OF_OFFICIAL:
            text = f"at most {_number(self.percent)} % of official"
        elif self.kind == KIND_MAX_PRICE:
            text = (
                f"at most {_side(self.max_input_usd)} in / "
                f"{_side(self.max_output_usd)} out per Mtok"
            )
        elif self.kind == KIND_CHEAPEST_LEVEL_NOW:
            text = "the cheapest level now"
        else:
            text = f"the cheapest level with at least {self.min_accounts} accounts"
        if self.kind in (KIND_CHEAPEST_LEVEL_NOW, KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS):
            if self.cap_percent is not None:
                text += f", capped at {_number(self.cap_percent)} % of official"
            elif self.has_cap:
                text += (
                    f", capped at {_side(self.cap_input_usd)} in / "
                    f"{_side(self.cap_output_usd)} out"
                )
            else:
                text += f" ({FOLLOWS_THE_MARKET_NOTE})"
        if self.exclude_upstreams:
            text += f"; never {', '.join(self.exclude_upstreams)}"
        if self.max_usd_per_request is not None:
            text += f"; at most ${_number(self.max_usd_per_request)} per request"
        return text


def _number(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _side(value: float | None) -> str:
    return "uncapped" if value is None else f"${_number(value)}"


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def profile_name_problem(name: object) -> str | None:
    """Why ``name`` cannot name a profile, or ``None`` if it can."""

    if not isinstance(name, str) or not PROFILE_NAME_PATTERN.match(name):
        return (
            "must be 1-64 characters of a-z, 0-9, '_' or '-', starting with a "
            "letter or digit"
        )
    return None


def parse_profile(name: str, document: object) -> tuple[MarketplaceProfile | None, str]:
    """Validate one profile; ``(profile, "")`` or ``(None, why)``."""

    problem = profile_name_problem(name)
    if problem is not None:
        return None, f"name {problem}"
    if not isinstance(document, Mapping):
        return None, "must be an object"
    raw: dict[str, Any] = {str(key): value for key, value in document.items()}
    kind = raw.get(FIELD_KIND)
    if kind not in PROFILE_KINDS:
        return None, f"kind must be one of {', '.join(PROFILE_KINDS)}"
    allowed = KIND_FIELDS[str(kind)]
    extra = sorted(str(key) for key in raw if key != FIELD_KIND and key not in allowed)
    if extra:
        return None, f"{kind} takes no {', '.join(extra)}"

    numbers: dict[str, float] = {}
    for key in (
        FIELD_PERCENT,
        FIELD_MAX_INPUT,
        FIELD_MAX_OUTPUT,
        FIELD_CAP_PERCENT,
        FIELD_CAP_INPUT,
        FIELD_CAP_OUTPUT,
        FIELD_MAX_PER_REQUEST,
    ):
        if key not in raw:
            continue
        number = _positive_number(raw[key])
        if number is None:
            return None, f"{key} must be a number greater than zero"
        if key in (FIELD_PERCENT, FIELD_CAP_PERCENT) and number > 100:
            return None, f"{key} must be at most 100"
        numbers[key] = number

    min_accounts: int | None = None
    if FIELD_MIN_ACCOUNTS in raw:
        value = raw[FIELD_MIN_ACCOUNTS]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None, f"{FIELD_MIN_ACCOUNTS} must be a whole number of at least 1"
        min_accounts = value

    excluded: list[str] = []
    if FIELD_EXCLUDE in raw:
        values = raw[FIELD_EXCLUDE]
        if not isinstance(values, list):
            return None, f"{FIELD_EXCLUDE} must be a list of upstream prefixes"
        for value in values:
            prefix = value.strip().lower() if isinstance(value, str) else ""
            if not UPSTREAM_PREFIX_PATTERN.match(prefix):
                return (
                    None,
                    f"{FIELD_EXCLUDE} entry {value!r} is not an upstream prefix",
                )
            if prefix not in excluded:
                excluded.append(prefix)
        if not excluded:
            return None, f"{FIELD_EXCLUDE} must name at least one upstream"

    if kind == KIND_PCT_OF_OFFICIAL and FIELD_PERCENT not in numbers:
        return None, f"{kind} needs {FIELD_PERCENT}"
    if kind == KIND_MAX_PRICE:
        if FIELD_MAX_INPUT not in numbers and FIELD_MAX_OUTPUT not in numbers:
            return None, f"{kind} needs {FIELD_MAX_INPUT} or {FIELD_MAX_OUTPUT}"
        if FIELD_MAX_PER_REQUEST in numbers and (
            FIELD_MAX_INPUT not in numbers or FIELD_MAX_OUTPUT not in numbers
        ):
            # An uncapped side makes the worst case unbounded, so a per-request
            # limit over it could never be checked honestly.
            return None, (
                f"{FIELD_MAX_PER_REQUEST} needs both {FIELD_MAX_INPUT} and "
                f"{FIELD_MAX_OUTPUT}"
            )
    if kind == KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS and min_accounts is None:
        return None, f"{kind} needs {FIELD_MIN_ACCOUNTS}"
    if FIELD_CAP_PERCENT in numbers and (
        FIELD_CAP_INPUT in numbers or FIELD_CAP_OUTPUT in numbers
    ):
        return None, (
            f"a cap is {FIELD_CAP_PERCENT} or {FIELD_CAP_INPUT} / "
            f"{FIELD_CAP_OUTPUT}, not both"
        )

    return (
        MarketplaceProfile(
            name=name,
            kind=str(kind),
            percent=numbers.get(FIELD_PERCENT),
            max_input_usd=numbers.get(FIELD_MAX_INPUT),
            max_output_usd=numbers.get(FIELD_MAX_OUTPUT),
            min_accounts=min_accounts,
            cap_percent=numbers.get(FIELD_CAP_PERCENT),
            cap_input_usd=numbers.get(FIELD_CAP_INPUT),
            cap_output_usd=numbers.get(FIELD_CAP_OUTPUT),
            exclude_upstreams=tuple(excluded),
            max_usd_per_request=numbers.get(FIELD_MAX_PER_REQUEST),
        ),
        "",
    )


@dataclass(frozen=True, slots=True)
class MarketplaceProfiles:
    """The whole store, validated. An unusable entry is kept out, and named."""

    profiles: Mapping[str, MarketplaceProfile] = field(default_factory=dict)
    #: ``name: why`` for every entry the file holds that could not be used.
    problems: Mapping[str, str] = field(default_factory=dict)

    def get(self, name: str) -> MarketplaceProfile | None:
        return self.profiles.get(name)

    @classmethod
    def from_document(cls, document: object) -> Self:
        if not isinstance(document, Mapping):
            logger.warning(
                "MARKETPLACE PROFILES: top-level JSON value is not an object; "
                "ignoring it"
            )
            return cls()
        section = document.get(PROFILES_KEY)
        if section is None:
            return cls()
        if not isinstance(section, Mapping):
            logger.warning("MARKETPLACE PROFILES: 'profiles' is not an object")
            return cls()
        profiles: dict[str, MarketplaceProfile] = {}
        problems: dict[str, str] = {}
        for raw_name, raw in section.items():
            name = str(raw_name)
            profile, problem = parse_profile(name, raw)
            if profile is None:
                problems[name] = problem
                logger.warning(
                    "MARKETPLACE PROFILES: '{}' {}; it is not used", name, problem
                )
                continue
            profiles[name] = profile
        return cls(profiles=profiles, problems=problems)

    def as_document(self) -> dict[str, Any]:
        return {
            VERSION_KEY: STORE_VERSION,
            PROFILES_KEY: {
                name: profile.as_document()
                for name, profile in sorted(self.profiles.items())
            },
        }


EMPTY_MARKETPLACE_PROFILES = MarketplaceProfiles()


def load_marketplace_profiles(path: Path | None = None) -> MarketplaceProfiles:
    """Read the store, treating every failure as "no profiles".

    A row naming a profile this read could not produce then fails closed on
    the request path (spec §6.3): an unreadable store never becomes "send
    no ceiling".
    """

    resolved = path if path is not None else marketplace_profiles_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_MARKETPLACE_PROFILES
    except OSError as exc:
        logger.warning("MARKETPLACE PROFILES: cannot read {}: {}", resolved, exc)
        return EMPTY_MARKETPLACE_PROFILES
    if not raw.strip():
        return EMPTY_MARKETPLACE_PROFILES
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("MARKETPLACE PROFILES: cannot parse {}: {}", resolved, exc)
        return EMPTY_MARKETPLACE_PROFILES
    return MarketplaceProfiles.from_document(document)


def save_marketplace_profiles(
    profiles: MarketplaceProfiles, path: Path | None = None
) -> None:
    """Write the store owner-only and atomically, and drop the cache.

    Owner-only for the ``proxy_chains.json`` reason (7.89.0): nothing secret
    is in it today, but it is configuration the operator typed, in the same
    folder, written the same way.
    """

    resolved = path if path is not None else marketplace_profiles_path()
    write_owner_only_json(resolved, profiles.as_document())
    reset_marketplace_profiles_cache()


_CACHE_SIGNATURE: tuple[str, int, int] | None = None
_CACHED: MarketplaceProfiles = EMPTY_MARKETPLACE_PROFILES


def reset_marketplace_profiles_cache() -> None:
    """Forget the cached store, so the next read goes back to disk."""

    global _CACHE_SIGNATURE, _CACHED
    _CACHE_SIGNATURE = None
    _CACHED = EMPTY_MARKETPLACE_PROFILES


def current_marketplace_profiles(path: Path | None = None) -> MarketplaceProfiles:
    """The store, re-read only when the file changed (read per attempt)."""

    global _CACHE_SIGNATURE, _CACHED
    resolved = path if path is not None else marketplace_profiles_path()
    try:
        stat = resolved.stat()
    except OSError:
        reset_marketplace_profiles_cache()
        return EMPTY_MARKETPLACE_PROFILES
    signature = (str(resolved), stat.st_mtime_ns, stat.st_size)
    if signature != _CACHE_SIGNATURE:
        _CACHED = load_marketplace_profiles(resolved)
        _CACHE_SIGNATURE = signature
    return _CACHED


__all__ = [
    "EMPTY_MARKETPLACE_PROFILES",
    "FOLLOWS_THE_MARKET_NOTE",
    "KIND_FIELDS",
    "MARKETPLACE_DECIDES_WARNING",
    "PROFILE_NAME_PATTERN",
    "MarketplaceProfile",
    "MarketplaceProfiles",
    "current_marketplace_profiles",
    "load_marketplace_profiles",
    "parse_profile",
    "profile_name_problem",
    "reset_marketplace_profiles_cache",
    "save_marketplace_profiles",
]
