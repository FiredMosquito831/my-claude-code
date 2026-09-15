"""Per-provider proxy chains: ``~/.mcc/proxy_chains.json``.

A provider's ``<PROVIDER>_PROXY`` setting is one string, read once in the
provider's constructor. This file is the store for the thing it cannot be: an
ordered list of egress addresses for one provider, with a rotation policy, the
failure classes that move it along, and a per-entry pause.

::

    {
      "version": 1,
      "proxies": {
        "px_7f3a1b2c": {"url": "socks5h://user:pass@203.0.113.7:1080",
                        "label": "", "added_at": "2026-09-15T...Z",
                        "source": "manual", "source_count": 1}
      },
      "chains": {
        "opencode": {"enabled": true, "policy": "round_robin",
                     "entries": [{"proxy": "px_7f3a1b2c", "paused": false},
                                 {"proxy": "", "paused": false}],
                     "on": ["quota", "rate_limit", "timeout"],
                     "scope": "provider", "max_switches": 2,
                     "oauth_acknowledged": false}
      }
    }

**Two tables, not one.** ``proxies`` is the endpoint catalogue, keyed by a
stable id rather than by URL so that editing a password does not orphan the
endpoint's health record (which the checker writes in a later release). Chains
reference endpoints by id, so the same address can serve several providers and
be re-labelled in one place.

**A provider absent from ``chains`` behaves exactly as it does today.** Its
``<PROVIDER>_PROXY`` (or a custom provider's registry ``proxy``) is used as the
single static proxy. Nothing migrates, the ``.env`` is never rewritten, and an
operator who never opens the Proxying page sees no change at all. When a chain
does exist, it is what the page shows and -- from the release that adds the
runtime seam -- what the provider uses.

A JSON document rather than settings keys, for the reason
``config/harness_tiers.py`` gives at 195 hypothetical fields: 56 providers x
(chain, policy, triggers, pauses, bound, scope) is well over two hundred
``ConfigFieldSpec`` entries, each with its own consumer-contract hop. That is
not a manifest, it is a second product. ``model_overrides.json``, ``rtk.json``
and ``custom_providers.json`` are the same precedent.

**Secrets.** A proxy URL may carry ``user:pass``. It is stored here in the
clear, exactly as ``custom_providers.json`` stores API keys, and it is never
rendered back to the browser: the admin route sends
:func:`my_claude_code.config.credentials.mask_proxy_label` and an entry the
operator did not edit travels back by id.

This module is deliberately storage only. It validates, normalises and
persists; it does not construct a provider, and nothing on the request path
imports it.
"""

import json
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.constants import (
    ROTATION_POLICY_ALIASES,
    ROTATION_POLICY_ORDER,
)
from my_claude_code.config.paths import proxy_chains_path

VERSION_KEY = "version"
PROXIES_KEY = "proxies"
CHAINS_KEY = "chains"
DOCUMENT_VERSION = 1

#: Schemes ``httpx[socks]`` and ``requests[socks]`` can actually dial. The same
#: four ``api/admin_custom_routes._normalize_proxy`` accepts for a custom
#: provider's static proxy, so a URL that is legal on one surface is legal on
#: the other.
PROXY_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "socks5", "socks5h"})

#: The empty proxy id, meaning "no proxy -- go out on this machine's own
#: address". A legal chain entry and the recommended last one: "try my proxies,
#: then fall back to my own IP". Without it the feature would force an
#: all-or-nothing choice.
DIRECT = ""
DIRECT_LABEL = "Direct (no proxy)"

#: 5 keys x 4 proxies is already 20 leaf providers for one entry in the
#: catalogue, each with its own client, recovery ladder and rate limiter. The
#: cap is refused at the API with a message rather than silently truncated.
PROXY_CHAIN_MAX_ENTRIES = 12

#: How many times one request may move to the next entry. The user's own range.
#: Every switch spends wall-clock inside a single attempt and the executor's
#: deadlines do not move, so 5 is a real ceiling rather than a formality.
MAX_SWITCHES_MIN = 1
MAX_SWITCHES_MAX = 5
MAX_SWITCHES_DEFAULT = 2

#: Declaration order of ``core.failures.FailureKind``. Mirrored rather than
#: imported -- ``config`` is a leaf package -- and pinned both ways against
#: ``FAILURE_KIND_NAMES`` below and against the enum itself in
#: ``tests/contracts/test_import_boundaries.py``.
TRIGGER_KIND_ORDER: tuple[str, ...] = (
    "invalid_request",
    "model_rejected",
    "context_length",
    "authentication",
    "permission",
    "quota",
    "rate_limit",
    "overloaded",
    "timeout",
    "upstream",
    "unavailable",
)

#: Refused outright, with the reason on the chip. Rotating on a 401/403 burns
#: the whole chain inside one request *and* earns a lockout tier on every proxy
#: it touched, and a new address fixes neither: the credential is the problem.
#: This is the one choice that is actively destructive, which is why it is the
#: only one the operator is not offered.
REFUSED_TRIGGER_KINDS: tuple[str, ...] = ("authentication", "permission")

#: On for a chain the operator has not edited.
DEFAULT_TRIGGER_KINDS: tuple[str, ...] = ("quota", "rate_limit", "timeout")

#: Everything else is selectable and off. A new address is unlikely to fix any
#: of them, but choosing one costs the operator latency and nothing else, so
#: the page says so on the chip instead of taking the choice away.
SELECTABLE_TRIGGER_KINDS: tuple[str, ...] = tuple(
    kind for kind in TRIGGER_KIND_ORDER if kind not in REFUSED_TRIGGER_KINDS
)

DEFAULT_POLICY = "failover"
#: One shared pool per provider, used by every credential of that provider.
#: The provider's quota is metered by address alone on the case this feature
#: was built for, so a proxy exhausted on one key is exhausted for all of them.
#: ``"credential"`` stays selectable for a provider that meters per
#: (address, account); it is a setting the operator flips, not a redesign.
DEFAULT_SCOPE = "provider"
SCOPES: tuple[str, ...] = ("provider", "credential")

SOURCE_MANUAL = "manual"


def is_valid_proxy_url(url: str) -> bool:
    """Whether a string is a proxy URL this product will dial.

    A scheme from :data:`PROXY_URL_SCHEMES` and a host. A port is not required
    -- ``http://proxy.internal`` is a real thing -- but a bare ``host:port``
    with no scheme is refused rather than guessed at, because guessing between
    ``http`` and ``socks5`` for an operator is exactly the kind of silent
    default that produces a chain that cannot connect and says nothing.
    """

    candidate = url.strip()
    if not candidate:
        return False
    parsed = urlsplit(candidate)
    if parsed.scheme not in PROXY_URL_SCHEMES:
        return False
    try:
        host = parsed.hostname
    except ValueError:
        return False
    return bool(host)


def normalise_trigger_kinds(values: Iterable[object]) -> tuple[str, ...]:
    """Return the selectable kinds among ``values``, in declaration order.

    Unknown names are dropped rather than raised on -- the
    ``core.failures.parse_failure_kinds`` contract, for the same reason: this
    is reached by a stored document that may predate a renamed kind. A refused
    kind is dropped the same way, so a hand-edited file cannot arm the one
    trigger the page will not offer.
    """

    wanted = {str(value).strip().lower() for value in values}
    return tuple(kind for kind in SELECTABLE_TRIGGER_KINDS if kind in wanted)


def normalise_policy(value: object) -> str:
    """Return one of the four rotation policy names.

    ``on_error`` is the credential engine's accepted alias of ``failover`` and
    is accepted here for the same reason: this is the *same* vocabulary, not a
    second one that happens to overlap.
    """

    policy = str(value or "").strip().lower()
    policy = ROTATION_POLICY_ALIASES.get(policy, policy)
    return policy if policy in ROTATION_POLICY_ORDER else DEFAULT_POLICY


def normalise_scope(value: object) -> str:
    """Return ``provider`` or ``credential``."""

    scope = str(value or "").strip().lower()
    return scope if scope in SCOPES else DEFAULT_SCOPE


def clamp_max_switches(value: object) -> int:
    """Return a switch bound inside the 1-5 range, defaulting on nonsense."""

    if not isinstance(value, int | str):
        return MAX_SWITCHES_DEFAULT
    try:
        bound = int(value)
    except ValueError:
        return MAX_SWITCHES_DEFAULT
    return max(MAX_SWITCHES_MIN, min(MAX_SWITCHES_MAX, bound))


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    """One address in the catalogue.

    ``source_count`` is how many independent feeds listed this ``ip:port`` in
    the same window. Nothing sets it above ``1`` yet -- ingestion is manual --
    and it is carried from the first release precisely so the release that adds
    feeds needs no migration.
    """

    url: str
    label: str = ""
    added_at: str = ""
    source: str = SOURCE_MANUAL
    source_count: int = 1

    def as_document(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "label": self.label,
            "added_at": self.added_at,
            "source": self.source,
            "source_count": self.source_count,
        }

    @classmethod
    def from_document(cls, raw: object, where: str) -> Self | None:
        if not isinstance(raw, Mapping):
            logger.warning("PROXY CHAINS: '{}' is not an object; ignoring it", where)
            return None
        url = str(raw.get("url") or "").strip()
        if not is_valid_proxy_url(url):
            logger.warning(
                "PROXY CHAINS: '{}' is not a usable proxy URL; ignoring it", where
            )
            return None
        raw_count = raw.get("source_count", 1)
        try:
            source_count = (
                max(1, int(raw_count)) if isinstance(raw_count, int | str) else 1
            )
        except ValueError:
            source_count = 1
        return cls(
            url=url,
            label=str(raw.get("label") or "").strip(),
            added_at=str(raw.get("added_at") or "").strip(),
            source=str(raw.get("source") or SOURCE_MANUAL).strip() or SOURCE_MANUAL,
            source_count=source_count,
        )


@dataclass(frozen=True, slots=True)
class ProxyChainEntry:
    """One rung of a chain: an endpoint id, or ``""`` for direct."""

    proxy: str = DIRECT
    paused: bool = False

    @property
    def is_direct(self) -> bool:
        return not self.proxy

    def as_document(self) -> dict[str, Any]:
        return {"proxy": self.proxy, "paused": self.paused}


@dataclass(frozen=True, slots=True)
class ProxyChain:
    """What one provider says about its egress.

    ``enabled`` is a separate switch from "has entries" on purpose: an
    operator who is debugging wants to turn the whole chain off for one
    request without losing the order they spent time on, which is the same
    reason a paused route entry is kept rather than deleted.
    """

    enabled: bool = False
    policy: str = DEFAULT_POLICY
    entries: tuple[ProxyChainEntry, ...] = ()
    on: tuple[str, ...] = DEFAULT_TRIGGER_KINDS
    scope: str = DEFAULT_SCOPE
    max_switches: int = MAX_SWITCHES_DEFAULT
    #: Set by the operator on a subscription-login provider. The rail is
    #: inert until it is, because changing source address between requests on
    #: a personal subscription is the operator's risk to take knowingly.
    oauth_acknowledged: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether this entry would change nothing about the provider."""

        return not self.entries and not self.enabled and not self.oauth_acknowledged

    def proxy_ids(self) -> tuple[str, ...]:
        return tuple(entry.proxy for entry in self.entries if entry.proxy)

    def as_document(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "policy": self.policy,
            "entries": [entry.as_document() for entry in self.entries],
            "on": list(self.on),
            "scope": self.scope,
            "max_switches": self.max_switches,
            "oauth_acknowledged": self.oauth_acknowledged,
        }

    @classmethod
    def from_document(cls, raw: object, where: str) -> Self | None:
        if not isinstance(raw, Mapping):
            logger.warning("PROXY CHAINS: '{}' is not an object; ignoring it", where)
            return None
        entries: list[ProxyChainEntry] = []
        raw_entries = raw.get("entries")
        if isinstance(raw_entries, Sequence) and not isinstance(raw_entries, str):
            for index, raw_entry in enumerate(raw_entries):
                if not isinstance(raw_entry, Mapping):
                    logger.warning(
                        "PROXY CHAINS: '{}.entries[{}]' is not an object; ignoring it",
                        where,
                        index,
                    )
                    continue
                entries.append(
                    ProxyChainEntry(
                        proxy=str(raw_entry.get("proxy") or "").strip(),
                        paused=bool(raw_entry.get("paused")),
                    )
                )
        elif raw_entries is not None:
            logger.warning("PROXY CHAINS: '{}.entries' is not a list", where)
        raw_on = raw.get("on")
        on = (
            normalise_trigger_kinds(raw_on)
            if isinstance(raw_on, Sequence) and not isinstance(raw_on, str)
            else DEFAULT_TRIGGER_KINDS
        )
        return cls(
            enabled=bool(raw.get("enabled")),
            policy=normalise_policy(raw.get("policy")),
            entries=tuple(entries[:PROXY_CHAIN_MAX_ENTRIES]),
            on=on,
            scope=normalise_scope(raw.get("scope")),
            max_switches=clamp_max_switches(raw.get("max_switches")),
            oauth_acknowledged=bool(raw.get("oauth_acknowledged")),
        )


EMPTY_CHAIN = ProxyChain()


@dataclass(frozen=True, slots=True)
class ProxyChains:
    """Every endpoint this install knows and every chain it has been given."""

    proxies: Mapping[str, ProxyEndpoint] = field(default_factory=dict)
    chains: Mapping[str, ProxyChain] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.chains and not self.proxies

    def chain(self, provider_id: str | None) -> ProxyChain | None:
        """Return one provider's chain, or ``None`` for "behaves as today"."""

        if not provider_id:
            return None
        return self.chains.get(provider_id.strip().lower())

    def endpoint(self, proxy_id: str) -> ProxyEndpoint | None:
        return self.proxies.get(proxy_id)

    def add_endpoint(self, url: str) -> tuple[ProxyChains, str]:
        """Return a copy carrying ``url``, and the id it was filed under.

        An address already in the catalogue is reused rather than duplicated:
        two chains naming the same machine must share one health record, or a
        dead proxy would have to be discovered twice.
        """

        cleaned = url.strip()
        for proxy_id, endpoint in self.proxies.items():
            if endpoint.url == cleaned:
                return self, proxy_id
        proxy_id = _mint_proxy_id(self.proxies)
        proxies = dict(self.proxies)
        proxies[proxy_id] = ProxyEndpoint(
            url=cleaned,
            added_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        return replace(self, proxies=proxies), proxy_id

    def with_chain(self, provider_id: str, chain: ProxyChain | None) -> ProxyChains:
        """Return a copy with one provider's chain replaced, or removed.

        Removal is how "this provider goes back to its ``<PROVIDER>_PROXY``"
        is expressed, and it is a different state from a chain that exists and
        is switched off.
        """

        chains = dict(self.chains)
        if chain is None:
            chains.pop(provider_id, None)
        else:
            chains[provider_id] = chain
        return replace(self, chains=chains).pruned()

    def pruned(self) -> ProxyChains:
        """Drop endpoints no chain references any more.

        The catalogue exists to be shared between chains, not to accumulate:
        an address removed from the last chain that named it has no health
        record worth keeping and no way back onto the page.
        """

        referenced: set[str] = set()
        for chain in self.chains.values():
            referenced.update(chain.proxy_ids())
        if referenced == set(self.proxies):
            return self
        return replace(
            self,
            proxies={
                proxy_id: endpoint
                for proxy_id, endpoint in self.proxies.items()
                if proxy_id in referenced
            },
        )

    def as_document(self) -> dict[str, Any]:
        return {
            VERSION_KEY: DOCUMENT_VERSION,
            PROXIES_KEY: {
                proxy_id: endpoint.as_document()
                for proxy_id, endpoint in self.proxies.items()
            },
            CHAINS_KEY: {
                provider_id: chain.as_document()
                for provider_id, chain in self.chains.items()
            },
        }

    @classmethod
    def from_document(cls, document: object) -> Self:
        """Build a table from parsed JSON, ignoring what it cannot use."""

        if not isinstance(document, Mapping):
            logger.warning(
                "PROXY CHAINS: top-level JSON value is not an object; ignoring it"
            )
            return cls()
        proxies: dict[str, ProxyEndpoint] = {}
        raw_proxies = document.get(PROXIES_KEY)
        if isinstance(raw_proxies, Mapping):
            for raw_id, raw_entry in raw_proxies.items():
                proxy_id = str(raw_id).strip()
                if not proxy_id:
                    continue
                endpoint = ProxyEndpoint.from_document(
                    raw_entry, f"{PROXIES_KEY}.{proxy_id}"
                )
                if endpoint is not None:
                    proxies[proxy_id] = endpoint
        elif raw_proxies is not None:
            logger.warning("PROXY CHAINS: '{}' is not an object", PROXIES_KEY)

        chains: dict[str, ProxyChain] = {}
        raw_chains = document.get(CHAINS_KEY)
        if isinstance(raw_chains, Mapping):
            for raw_id, raw_entry in raw_chains.items():
                provider_id = str(raw_id).strip().lower()
                if not provider_id:
                    continue
                chain = ProxyChain.from_document(
                    raw_entry, f"{CHAINS_KEY}.{provider_id}"
                )
                if chain is None:
                    continue
                # An entry naming an endpoint the catalogue lost is dropped
                # rather than kept as a dangling id: a rung that resolves to
                # nothing would render as a blank row and route nowhere.
                kept = tuple(
                    entry
                    for entry in chain.entries
                    if entry.is_direct or entry.proxy in proxies
                )
                chains[provider_id] = replace(chain, entries=kept)
        elif raw_chains is not None:
            logger.warning("PROXY CHAINS: '{}' is not an object", CHAINS_KEY)

        # Prune here rather than through ``pruned()``: the classmethod has to
        # return ``Self``, and a copy made by ``dataclasses.replace`` is only
        # ever the base class.
        referenced = {
            proxy_id for chain in chains.values() for proxy_id in chain.proxy_ids()
        }
        return cls(
            proxies={
                proxy_id: endpoint
                for proxy_id, endpoint in proxies.items()
                if proxy_id in referenced
            },
            chains=chains,
        )


EMPTY_PROXY_CHAINS = ProxyChains()


def _mint_proxy_id(existing: Mapping[str, ProxyEndpoint]) -> str:
    """Return an id no endpoint in this store already holds."""

    while True:
        proxy_id = f"px_{secrets.token_hex(4)}"
        if proxy_id not in existing:
            return proxy_id


def load_proxy_chains(path: Path | None = None) -> ProxyChains:
    """Read the store, treating every failure as "no chains".

    A malformed file must never stop the proxy from starting: the worst honest
    outcome is that every provider falls back to its ``<PROVIDER>_PROXY``,
    which is the behaviour of every release before this one, and a log line
    says so.
    """

    resolved_path = path if path is not None else proxy_chains_path()
    try:
        raw = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_PROXY_CHAINS
    except OSError as exc:
        logger.warning("PROXY CHAINS: cannot read {}: {}", resolved_path, exc)
        return EMPTY_PROXY_CHAINS

    if not raw.strip():
        return EMPTY_PROXY_CHAINS

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("PROXY CHAINS: cannot parse {}: {}", resolved_path, exc)
        return EMPTY_PROXY_CHAINS

    return ProxyChains.from_document(document)


def save_proxy_chains(chains: ProxyChains, path: Path | None = None) -> None:
    """Write the store atomically and drop the cache."""

    resolved_path = path if path is not None else proxy_chains_path()
    write_json_document_atomically(resolved_path, chains.as_document())
    reset_proxy_chains_cache()


# (path, mtime_ns, size) -> parsed table, keyed on the file's own stat so a
# dashboard edit is picked up without a restart. The same cache shape
# ``harness_tiers`` uses, and for the same reason: the admin route writes the
# file and nothing else has to be told.
_CACHE_SIGNATURE: tuple[str, int, int] | None = None
_CACHED_CHAINS: ProxyChains = EMPTY_PROXY_CHAINS


def reset_proxy_chains_cache() -> None:
    """Forget the cached table, so the next read goes back to disk."""

    global _CACHE_SIGNATURE, _CACHED_CHAINS
    _CACHE_SIGNATURE = None
    _CACHED_CHAINS = EMPTY_PROXY_CHAINS


def current_proxy_chains(path: Path | None = None) -> ProxyChains:
    """Return the table, re-reading only when the file has changed."""

    global _CACHE_SIGNATURE, _CACHED_CHAINS
    resolved_path = path if path is not None else proxy_chains_path()
    try:
        stat = resolved_path.stat()
    except OSError:
        reset_proxy_chains_cache()
        return EMPTY_PROXY_CHAINS

    signature = (str(resolved_path), stat.st_mtime_ns, stat.st_size)
    if signature != _CACHE_SIGNATURE:
        _CACHED_CHAINS = load_proxy_chains(resolved_path)
        _CACHE_SIGNATURE = signature
    return _CACHED_CHAINS


__all__ = [
    "CHAINS_KEY",
    "DEFAULT_POLICY",
    "DEFAULT_SCOPE",
    "DEFAULT_TRIGGER_KINDS",
    "DIRECT",
    "DIRECT_LABEL",
    "DOCUMENT_VERSION",
    "EMPTY_CHAIN",
    "EMPTY_PROXY_CHAINS",
    "MAX_SWITCHES_DEFAULT",
    "MAX_SWITCHES_MAX",
    "MAX_SWITCHES_MIN",
    "PROXIES_KEY",
    "PROXY_CHAIN_MAX_ENTRIES",
    "PROXY_URL_SCHEMES",
    "REFUSED_TRIGGER_KINDS",
    "SCOPES",
    "SELECTABLE_TRIGGER_KINDS",
    "SOURCE_MANUAL",
    "TRIGGER_KIND_ORDER",
    "VERSION_KEY",
    "ProxyChain",
    "ProxyChainEntry",
    "ProxyChains",
    "ProxyEndpoint",
    "clamp_max_switches",
    "current_proxy_chains",
    "is_valid_proxy_url",
    "load_proxy_chains",
    "normalise_policy",
    "normalise_scope",
    "normalise_trigger_kinds",
    "reset_proxy_chains_cache",
    "save_proxy_chains",
]
