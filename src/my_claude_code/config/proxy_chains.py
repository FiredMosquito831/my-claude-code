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
from itertools import islice
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.constants import (
    PROXY_MAX_SWITCHES_PER_REQUEST_DEFAULT,
    PROXY_MAX_SWITCHES_PER_REQUEST_MAX,
    PROXY_MAX_SWITCHES_PER_REQUEST_MIN,
    ROTATION_POLICY_ALIASES,
    ROTATION_POLICY_ORDER,
)
from my_claude_code.config.paths import proxy_chains_path
from my_claude_code.config.proxy_feed_legacy import convert_legacy_feed_ids
from my_claude_code.config.proxy_feeds import CustomFeed, mint_feed_id

VERSION_KEY = "version"
PROXIES_KEY = "proxies"
CHAINS_KEY = "chains"
#: Addresses a feed offered that no chain has taken. Absent from a document
#: written before ingestion shipped, which reads back as "none on offer" and
#: needs no migration.
CANDIDATES_KEY = "candidates"
#: The feeds this install has been told it may read, as the operator described
#: them. Absent means none, which is what a fresh install has: MCC ships no
#: feed of its own, so every entry here is one somebody typed.
#:
#: Releases up to 7.17.1 wrote this key as a list of *built-in feed ids*
#: (``["databay", "vpslab"]``). That shape is still read -- see
#: :mod:`~my_claude_code.config.proxy_feed_legacy` -- and converted to the
#: entries below, so an install that had feeds switched on keeps them.
FEEDS_KEY = "feeds"
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

#: The operator's own ceiling on chain length, and ``0`` -- the shipped
#: default -- means there is none.
#:
#: 7.13 shipped a hard 12 here, reasoning that 5 keys x 4 proxies is already 20
#: leaf providers each with its own client. That reasoning was about
#: *connections*, and connections are no longer what a chain entry costs: since
#: 7.19.0 a leg is built on its first use and closed again when it has been
#: idle longest (``PROXY_MAX_OPEN_LEGS``), so a three-hundred-entry chain is
#: three hundred strings and at most thirty-two clients. What is left is a
#: bound an operator may want for themselves, so it is a setting they set --
#: read from ``Settings.proxy_chain_max_entries`` at the API, refused with a
#: message, never silently truncated, and never applied when reading the store,
#: because a store this install already wrote is data rather than a request.
PROXY_CHAIN_MAX_ENTRIES_UNLIMITED = 0

#: How many times one request may move to the next entry. The user's own range.
#: Every switch spends wall-clock inside a single attempt and the executor's
#: deadlines do not move, so 5 is a real ceiling rather than a formality.
#: Mirrored from ``config.constants`` so the card's number and the install-wide
#: ``PROXY_MAX_SWITCHES_PER_REQUEST`` ceiling cannot drift apart.
MAX_SWITCHES_MIN = PROXY_MAX_SWITCHES_PER_REQUEST_MIN
MAX_SWITCHES_MAX = PROXY_MAX_SWITCHES_PER_REQUEST_MAX
MAX_SWITCHES_DEFAULT = PROXY_MAX_SWITCHES_PER_REQUEST_DEFAULT

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
#: An address that arrived from a named feed. It is a *candidate* and nothing
#: more: it sits in :attr:`ProxyChains.candidates` until an operator moves it
#: into a chain, and it cannot carry a credential before then.
SOURCE_FEED = "feed"

#: The most refused addresses the store keeps beyond the candidate list.
#:
#: A refusal is durable on purpose -- an address caught terminating TLS must
#: not come back as a fresh unknown row on the next fetch -- but a refused
#: address is not on offer either, so nothing else in the document references
#: it and :meth:`ProxyChains.pruned` would drop it. It is kept by name here
#: instead, newest first, with a ceiling so that a store cannot grow without
#: bound on a machine whose network is intercepting everything it dials. The
#: ceiling matches the reachability ladder's
#: ``MAX_TRACKED_ENDPOINTS`` for the same reason: five hundred is far more
#: than any real install refuses, and a document is not a log.
MAX_REFUSED_ENDPOINTS = 512

#: What the checker learned about the destination's certificate through this
#: address's tunnel. ``strict`` is an ordinary verified handshake. ``unknown``
#: is an address nothing has checked, or one that failed before TLS began.
TLS_STRICT = "strict"
TLS_UNKNOWN = "unknown"
#: The one value that refuses an address. The tunnel terminated TLS and
#: presented a certificate this machine's trust store rejects, which means
#: something between here and the provider is reading the plaintext. It is not
#: a reliability problem to be routed around; it is the class of proxy the
#: whole check exists to catch, and MCC will not carry a credential through it.
TLS_INTERCEPTED = "intercepted"

#: Providers whose credential is a person's *subscription* rather than a
#: revocable per-project key. Changing source address between requests is more
#: likely to be read as account sharing here than on a pay-as-you-go key, so
#: their chain stays inert -- in the page *and* in the runtime -- until the
#: operator says they understand that. Declared here rather than in the admin
#: route because the runtime has to honour the same rule and may not import an
#: API module to learn it.
OAUTH_PROVIDER_IDS: frozenset[str] = frozenset({"anthropic_oauth", "chatgpt_oauth"})


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
class ProxyCheckRecord:
    """What the checker last learned about one address.

    Written by the Test button and by the background checker, never by the
    request path: a measurement taken out of band, stored beside the address it
    describes so it survives a restart and a provider-generation replace.

    ``tls`` is the half of this record that is a security control rather than a
    convenience. :data:`TLS_INTERCEPTED` is durable and it is a refusal: an
    address carrying it cannot be put into a chain, and one already in a chain
    is held out of selection.
    """

    at: str = ""
    ok: bool = False
    latency_ms: int | None = None
    tls: str = TLS_UNKNOWN
    detail: str = ""
    #: What the operator's own exit-IP URL answered, when they named one. Never
    #: fetched by default and never from a URL MCC chose: it is an outbound
    #: request to a stranger, so it is the operator's call.
    exit_ip: str = ""
    #: How far the check that produced this record went -- ``"request"`` for
    #: the tunnel plus an HTTPS request, ``"tls"`` for the tunnel plus a
    #: verified handshake and nothing sent. Empty for a record written before
    #: 7.22.2, which the page reads as ``"request"`` because that is the only
    #: check those releases had. It says how an address was proven; it is not a
    #: second security verdict -- ``tls`` above is that, in both depths.
    depth: str = ""

    @property
    def intercepted(self) -> bool:
        return self.tls == TLS_INTERCEPTED

    def as_document(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "tls": self.tls,
            "detail": self.detail,
            "exit_ip": self.exit_ip,
            "depth": self.depth,
        }

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        raw_latency = raw.get("latency_ms")
        try:
            latency = (
                int(raw_latency) if isinstance(raw_latency, int | float | str) else None
            )
        except ValueError:
            latency = None
        tls = str(raw.get("tls") or TLS_UNKNOWN).strip().lower()
        return cls(
            at=str(raw.get("at") or "").strip(),
            ok=bool(raw.get("ok")),
            latency_ms=latency,
            tls=tls
            if tls in {TLS_STRICT, TLS_INTERCEPTED, TLS_UNKNOWN}
            else TLS_UNKNOWN,
            detail=str(raw.get("detail") or "").strip(),
            exit_ip=str(raw.get("exit_ip") or "").strip(),
            depth=str(raw.get("depth") or "").strip().lower(),
        )


@dataclass(frozen=True, slots=True)
class ProxyFeedFacts:
    """What the feeds said about one address, normalised.

    The §6.2 field list, minus the two that are the address itself. None of it
    is a measurement this product took: it is a summary of other people's
    claims, kept beside the address so the page can say where it came from and
    what was said about it, and so the operator can tell a four-feed agreement
    from a single scraper's guess. The checker's own verdict lives in
    :class:`ProxyCheckRecord` and is the only thing here that was measured from
    this machine.
    """

    protocol: str = ""
    country: str = ""
    anonymity: str = ""
    https_ok: bool = False
    latency_ms: int | None = None
    uptime_pct: float | None = None
    last_checked: str = ""
    asn: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "country": self.country,
            "anonymity": self.anonymity,
            "https_ok": self.https_ok,
            "latency_ms": self.latency_ms,
            "uptime_pct": self.uptime_pct,
            "last_checked": self.last_checked,
            "asn": self.asn,
        }

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        return cls(
            protocol=str(raw.get("protocol") or "").strip(),
            country=str(raw.get("country") or "").strip(),
            anonymity=str(raw.get("anonymity") or "").strip(),
            https_ok=bool(raw.get("https_ok")),
            latency_ms=_optional_int(raw.get("latency_ms")),
            uptime_pct=_optional_float(raw.get("uptime_pct")),
            last_checked=str(raw.get("last_checked") or "").strip(),
            asn=str(raw.get("asn") or "").strip(),
        )


def _optional_int(value: object) -> int | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _optional_float(value: object) -> float | None:
    if not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ProxyHealthState:
    """One address's reachability bench, made durable.

    The ledger in ``core/proxy_rotation.py`` is process-lifetime state on a
    *monotonic* clock, and before 7.19.0 a restart therefore made every dead
    address in every chain look healthy again -- which mattered little while a
    bench expiring re-admitted an address anyway, and matters a great deal now
    that only a passing check does. This is that ledger's record written down.

    ``until`` is a **wall-clock** epoch second rather than the ledger's
    monotonic deadline, because a monotonic number means nothing to the process
    that reads it back. ``failures`` is the index into the ladder and is the
    load-bearing half: it is what makes a proxy that has failed three times
    come back on the hour tier rather than the minute one.
    """

    failures: int = 0
    #: Epoch seconds, from :func:`time.time`. ``0.0`` means "no deadline"; an
    #: address with ``failures > 0`` and an elapsed deadline is *due for a
    #: re-probe*, which is emphatically not the same as usable.
    until: float = 0.0
    reason: str = ""
    #: When this was written, ISO-8601 Z, for the page.
    at: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "until": self.until,
            "reason": self.reason,
            "at": self.at,
        }

    @classmethod
    def from_document(cls, raw: object) -> ProxyHealthState | None:
        if not isinstance(raw, Mapping):
            return None
        raw_failures = raw.get("failures")
        try:
            failures = (
                max(0, int(raw_failures)) if isinstance(raw_failures, int | str) else 0
            )
        except ValueError:
            failures = 0
        if failures <= 0:
            # A healthy address is the absence of a record, not a record saying
            # zero. Reading one back as ``None`` keeps the two spellings from
            # meaning different things anywhere downstream.
            return None
        return cls(
            failures=failures,
            until=_optional_float(raw.get("until")) or 0.0,
            reason=str(raw.get("reason") or "").strip(),
            at=str(raw.get("at") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    """One address in the catalogue.

    ``source_count`` is how many independent feeds listed this ``ip:port`` in
    the same window, and ``sources`` names them. It is the one quality signal
    that costs nothing: an address four feeds agree on is a materially better
    bet than one from a single scraper, and knowing *which* four is what lets
    an operator see where an address in front of their credential came from.
    """

    url: str
    label: str = ""
    added_at: str = ""
    source: str = SOURCE_MANUAL
    source_count: int = 1
    #: The feed ids that listed this address in the pass that added it, in
    #: catalogue order. Empty for an address the operator typed.
    sources: tuple[str, ...] = ()
    #: What those feeds published about it. ``None`` for a typed address.
    feed: ProxyFeedFacts | None = None
    #: The checker's last verdict, or ``None`` for an address nothing has
    #: checked. ``None`` and "checked and failed" are different states and the
    #: page says which it is looking at.
    last_check: ProxyCheckRecord | None = None
    #: The provider id :attr:`last_check` was measured against, or ``""`` for a
    #: record from a release that did not say. A check is a question about one
    #: destination -- "does this tunnel reach *that* host with its certificate
    #: intact" -- so a verdict is only honest about the provider it was asked
    #: for. The page prints it ("working for Anthropic") rather than letting a
    #: pass against one host read as a pass against all of them.
    checked_for: str = ""
    #: The reachability bench this address was carrying when it was last
    #: written. ``None`` for an address that has never failed.
    health: ProxyHealthState | None = None

    @property
    def refused(self) -> bool:
        """Whether this address may not carry traffic at all."""

        return self.last_check is not None and self.last_check.intercepted

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "url": self.url,
            "label": self.label,
            "added_at": self.added_at,
            "source": self.source,
            "source_count": self.source_count,
        }
        if self.sources:
            document["sources"] = list(self.sources)
        if self.feed is not None:
            document["feed"] = self.feed.as_document()
        if self.last_check is not None:
            document["last_check"] = self.last_check.as_document()
        if self.checked_for:
            document["checked_for"] = self.checked_for
        if self.health is not None:
            document["health"] = self.health.as_document()
        return document

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
        raw_sources = raw.get("sources")
        sources = (
            tuple(str(name).strip() for name in raw_sources if str(name).strip())
            if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, str)
            else ()
        )
        return cls(
            url=url,
            label=str(raw.get("label") or "").strip(),
            added_at=str(raw.get("added_at") or "").strip(),
            source=str(raw.get("source") or SOURCE_MANUAL).strip() or SOURCE_MANUAL,
            source_count=max(source_count, len(sources)),
            sources=sources,
            feed=ProxyFeedFacts.from_document(raw.get("feed")),
            last_check=ProxyCheckRecord.from_document(raw.get("last_check")),
            checked_for=str(raw.get("checked_for") or "").strip().lower(),
            health=ProxyHealthState.from_document(raw.get("health")),
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
    #: Whether a request that has run out of healthy addresses goes out on this
    #: machine's own address instead of failing. TRUE, including for a chain
    #: written before this key existed: a document without it reads back as
    #: ``True``, because the alternative -- a provider that stops answering the
    #: moment its free proxies die -- is not what an operator who added proxies
    #: to *reach* a provider asked for. Turn it off on a provider that must
    #: never see this machine's address.
    direct_fallback: bool = True
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
            "direct_fallback": self.direct_fallback,
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
            entries=tuple(entries),
            on=on,
            scope=normalise_scope(raw.get("scope")),
            max_switches=clamp_max_switches(raw.get("max_switches")),
            # ``"direct_fallback" not in raw`` rather than ``raw.get(..., True)``
            # alone, so that an explicit ``false`` an operator saved is honoured
            # and a document written before 7.19.0 reads back as on.
            direct_fallback=(
                True
                if raw.get("direct_fallback") is None
                else bool(raw.get("direct_fallback"))
            ),
            oauth_acknowledged=bool(raw.get("oauth_acknowledged")),
        )


EMPTY_CHAIN = ProxyChain()


@dataclass(frozen=True, slots=True)
class ProxyChains:
    """Every endpoint this install knows and every chain it has been given."""

    proxies: Mapping[str, ProxyEndpoint] = field(default_factory=dict)
    chains: Mapping[str, ProxyChain] = field(default_factory=dict)
    #: Addresses a feed offered and nobody has chosen yet, best first.
    #:
    #: **A candidate is not a chain member.** It is in the catalogue so it can
    #: be shown, ranked and tested; it is in no provider's ``entries``, so no
    #: credential goes through it and nothing in the runtime can select it.
    #: Moving one into a chain is an operator pressing a button on a row, and
    #: it goes through the same checker and the same ``TLS intercepted``
    #: refusal a typed address does.
    candidates: tuple[str, ...] = ()
    #: The feeds this install has been told to read, in the order they were
    #: added. Empty on a fresh install and empty until an operator adds one:
    #: MCC ships none, so this list existing at all is a decision somebody
    #: made, and it is what decides whether this product ever contacts a third
    #: party.
    feeds: tuple[CustomFeed, ...] = ()
    #: Built-in feed ids that were converted into :attr:`feeds` while reading
    #: this document. **Never persisted** -- it is how
    #: :func:`~my_claude_code.config.proxy_feed_legacy.migrate_proxy_feeds`
    #: knows there is a one-time write to do, and it is empty on every read of
    #: a document that has already been through it. That emptiness is what
    #: makes the migration idempotent.
    migrated_feed_ids: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.chains and not self.proxies and not self.feeds

    @property
    def enabled_feed_ids(self) -> tuple[str, ...]:
        """The ids of the feeds a fetch would actually read."""

        return tuple(feed.id for feed in self.feeds if feed.enabled and feed.readable)

    def feed(self, feed_id: str) -> CustomFeed | None:
        return next((feed for feed in self.feeds if feed.id == feed_id), None)

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

    def with_feeds(self, feeds: Iterable[CustomFeed]) -> ProxyChains:
        """Return a copy whose feed list is exactly ``feeds``.

        The one write path for adding, editing, enabling and removing a feed:
        the page sends the list it is showing and this replaces it, the way a
        chain ``PUT`` replaces the chain. Removing a feed is leaving it out,
        and it deliberately **does not touch the candidates** that feed
        supplied -- an address on offer is an independent fact with its own
        ``source_count`` and its own health record, and deleting the row that
        named it would throw away work the operator may be halfway through.

        Ids are minted here for entries that arrive without one, which is what
        makes "add" and "edit" the same request.
        """

        kept: list[CustomFeed] = []
        taken: set[str] = set()
        for feed in feeds:
            feed_id = feed.id.strip() or mint_feed_id(taken)
            while feed_id in taken:
                feed_id = mint_feed_id(taken)
            taken.add(feed_id)
            kept.append(replace(feed, id=feed_id))
        return replace(self, feeds=tuple(kept))

    def with_candidates(
        self, offered: Sequence[tuple[str, ProxyEndpoint]]
    ) -> ProxyChains:
        """Return a copy whose candidate list is exactly ``offered``.

        A replacement rather than a merge: a pass over the feeds is a fresh
        answer to "what is on offer right now", and an address that has dropped
        off every feed since the last pass should leave the list rather than
        linger as a row nobody can account for. An address already in a chain
        is untouched by this -- chains and candidates are different tables and
        a promoted address has stopped being on offer.

        **A refused address is kept whether or not it is still offered.** It is
        the one row here that is a security control rather than a convenience:
        dropping it would mean the next fetch offering the same machine as a
        fresh unknown, testing it again, and -- on the pass where it happened
        not to intercept -- putting it in front of a credential. The caller
        decides how many addresses are offered;
        :data:`MAX_REFUSED_ENDPOINTS` is the only ceiling this method imposes,
        and it is on the refusals rather than on the offer.
        """

        chained = {
            proxy_id for chain in self.chains.values() for proxy_id in chain.proxy_ids()
        }
        proxies = {
            proxy_id: endpoint
            for proxy_id, endpoint in self.proxies.items()
            if proxy_id in chained
        }
        for proxy_id in self.refused_ids()[:MAX_REFUSED_ENDPOINTS]:
            if proxy_id not in proxies:
                proxies[proxy_id] = self.proxies[proxy_id]
        candidates: list[str] = []
        for proxy_id, endpoint in offered:
            if proxy_id in proxies:
                # Already in a chain, or standing refused. Either way it is not
                # on offer: the copy the operator chose keeps its own label and
                # health, and a refused one keeps the verdict that refused it.
                continue
            previous = self.proxies.get(proxy_id)
            if previous is not None and previous.last_check is not None:
                # Carry the checker's verdict across the pass. A candidate
                # already found to be terminating TLS must come back refused
                # rather than as a fresh unknown row: the refusal is the one
                # piece of state here that is a security control.
                endpoint = replace(endpoint, last_check=previous.last_check)
            proxies[proxy_id] = endpoint
            candidates.append(proxy_id)
        return replace(self, proxies=proxies, candidates=tuple(candidates))

    def without_candidate(self, proxy_id: str) -> ProxyChains:
        """Return a copy with one address no longer merely on offer.

        Called when an operator moves a candidate into a chain: the address
        stays in the catalogue, keeping its provenance and its health record,
        and stops being listed as something nobody has chosen.
        """

        if proxy_id not in self.candidates:
            return self
        return replace(
            self,
            candidates=tuple(item for item in self.candidates if item != proxy_id),
        )

    def with_check(
        self, proxy_id: str, record: ProxyCheckRecord, *, checked_for: str = ""
    ) -> ProxyChains:
        """Return a copy carrying one address's latest check.

        A no-op for an id the catalogue no longer holds: the checker runs out
        of band and may finish after the operator removed the address it was
        measuring, and inventing an endpoint from a stale result would put a
        row back on a page somebody just cleared.

        ``checked_for`` records which provider's host the verdict is about.
        Empty leaves whatever was there, so a caller that does not know cannot
        erase what an earlier one did.
        """

        endpoint = self.proxies.get(proxy_id)
        if endpoint is None:
            return self
        proxies = dict(self.proxies)
        proxies[proxy_id] = replace(
            endpoint,
            last_check=record,
            checked_for=checked_for.strip().lower() or endpoint.checked_for,
        )
        return replace(self, proxies=proxies)

    def with_health(
        self, proxy_id: str, health: ProxyHealthState | None
    ) -> ProxyChains:
        """Return a copy carrying one address's reachability bench.

        A no-op for an id the catalogue no longer holds, for the reason
        :meth:`with_check` gives: the writer runs out of band and must not put
        back a row the operator just removed.
        """

        endpoint = self.proxies.get(proxy_id)
        if endpoint is None:
            return self
        if endpoint.health == health:
            return self
        proxies = dict(self.proxies)
        proxies[proxy_id] = replace(endpoint, health=health)
        return replace(self, proxies=proxies)

    def refused_ids(self) -> tuple[str, ...]:
        """Every address the checker found terminating TLS, in store order."""

        return tuple(
            proxy_id for proxy_id, endpoint in self.proxies.items() if endpoint.refused
        )

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

        **A refusal is not a health record and is not dropped with one.** An
        address the checker caught terminating TLS is kept, up to
        :data:`MAX_REFUSED_ENDPOINTS` of them, even when no chain and no offer
        names it: forgetting it is what lets the next fetch offer the same
        machine as an unknown, and the one control between a credential and a
        hostile proxy must not be cleared by a tidy-up.
        """

        referenced: set[str] = set(self.candidates)
        referenced.update(self.refused_ids()[:MAX_REFUSED_ENDPOINTS])
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
            CANDIDATES_KEY: list(self.candidates),
            # Always objects, never the pre-7.18.0 id strings. Writing this
            # document is what completes the migration: the next read finds
            # entries rather than ids and converts nothing.
            FEEDS_KEY: [feed.as_document() for feed in self.feeds],
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

        raw_candidates = document.get(CANDIDATES_KEY)
        candidates = (
            tuple(
                proxy_id
                for proxy_id in (str(value).strip() for value in raw_candidates)
                if proxy_id in proxies
            )
            if isinstance(raw_candidates, Sequence)
            and not isinstance(raw_candidates, str)
            else ()
        )

        # Prune here rather than through ``pruned()``: the classmethod has to
        # return ``Self``, and a copy made by ``dataclasses.replace`` is only
        # ever the base class. The rule is the same one ``pruned()`` applies,
        # including its exception: a refused address is kept even when nothing
        # else in the document names it, because forgetting the verdict on a
        # tunnel caught reading the traffic is the one loss here that is a
        # security regression rather than a tidy-up.
        referenced = set(candidates)
        referenced.update(
            islice(
                (
                    proxy_id
                    for proxy_id, endpoint in proxies.items()
                    if endpoint.refused
                ),
                MAX_REFUSED_ENDPOINTS,
            )
        )
        referenced.update(
            proxy_id for chain in chains.values() for proxy_id in chain.proxy_ids()
        )
        feeds, migrated = _read_feeds(document.get(FEEDS_KEY))
        return cls(
            proxies={
                proxy_id: endpoint
                for proxy_id, endpoint in proxies.items()
                if proxy_id in referenced
            },
            chains=chains,
            candidates=candidates,
            feeds=feeds,
            migrated_feed_ids=migrated,
        )


EMPTY_PROXY_CHAINS = ProxyChains()


def _read_feeds(raw: object) -> tuple[tuple[CustomFeed, ...], tuple[str, ...]]:
    """Read the ``feeds`` key in either shape, and say what was converted.

    **Two shapes, one list.** 7.18.0 onwards writes objects. Up to 7.17.1 this
    key was a list of built-in feed ids, and those strings are converted here
    into the entries that mean the same thing, using the transitional table in
    :mod:`~my_claude_code.config.proxy_feed_legacy`.

    The conversion happens on **every** read of such a document, in memory, so
    an install's feeds keep working from the moment it starts even if the
    one-time rewrite has not run or could not run. The ids it converted are
    reported back so that rewrite knows it has something to do -- and, once it
    has run, this function sees objects, converts nothing, and reports nothing.

    A mixed list is read as a mixture: entries and ids each go down their own
    branch. That cannot arise from any release, but it is what a half-finished
    hand edit looks like and there is no reason to lose either half of it.
    """

    if isinstance(raw, str) or not isinstance(raw, Sequence):
        if raw is not None:
            logger.warning("PROXY CHAINS: '{}' is not a list", FEEDS_KEY)
        return (), ()

    feeds: list[CustomFeed] = []
    legacy: list[str] = []
    taken: list[str] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, str):
            legacy.append(entry)
            continue
        feed = CustomFeed.from_document(entry, f"{FEEDS_KEY}[{index}]")
        if feed is None:
            continue
        feed_id = feed.id or mint_feed_id(taken)
        while feed_id in taken:
            feed_id = mint_feed_id(taken)
        taken.append(feed_id)
        feeds.append(replace(feed, id=feed_id))

    converted = convert_legacy_feed_ids(legacy, taken=taken)
    return tuple(feeds) + converted, tuple(feed.id for feed in converted)


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


def migrate_proxy_feeds(path: Path | None = None) -> tuple[str, ...]:
    """Write a pre-7.18.0 feed list back in the new shape, once.

    Called on server startup. The conversion itself happens on **every** read
    (:func:`_read_feeds`), so an install's feeds keep working from the moment
    it starts whether or not this ever runs; what this adds is making it
    durable, which is also what makes it stop happening.

    **Idempotent.** It writes only when the read actually converted something.
    After the write the document holds feed objects rather than id strings, so
    the next start converts nothing, reports nothing and writes nothing -- a
    second start is a no-op, not a second set of entries.

    Never raises. A migration that cannot write leaves an install whose feeds
    still work and whose store is rewritten at the next start that can; that is
    a far better outcome than a server refusing to boot over a read-only file.
    """

    try:
        store = load_proxy_chains(path)
        if not store.migrated_feed_ids:
            return ()
        save_proxy_chains(store, path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("PROXY FEEDS: could not rewrite the feed list: {}", exc)
        return ()

    logger.info(
        "PROXY FEEDS: converted {} built-in feed(s) to custom entries, still "
        "switched on: {}. MCC no longer ships any feed of its own; these are "
        "now yours to edit or remove on the Proxying page.",
        len(store.migrated_feed_ids),
        ", ".join(store.migrated_feed_ids),
    )
    return store.migrated_feed_ids


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
    "CANDIDATES_KEY",
    "CHAINS_KEY",
    "DEFAULT_POLICY",
    "DEFAULT_SCOPE",
    "DEFAULT_TRIGGER_KINDS",
    "DIRECT",
    "DIRECT_LABEL",
    "DOCUMENT_VERSION",
    "EMPTY_CHAIN",
    "EMPTY_PROXY_CHAINS",
    "FEEDS_KEY",
    "MAX_REFUSED_ENDPOINTS",
    "MAX_SWITCHES_DEFAULT",
    "MAX_SWITCHES_MAX",
    "MAX_SWITCHES_MIN",
    "OAUTH_PROVIDER_IDS",
    "PROXIES_KEY",
    "PROXY_CHAIN_MAX_ENTRIES_UNLIMITED",
    "PROXY_URL_SCHEMES",
    "REFUSED_TRIGGER_KINDS",
    "SCOPES",
    "SELECTABLE_TRIGGER_KINDS",
    "SOURCE_FEED",
    "SOURCE_MANUAL",
    "TLS_INTERCEPTED",
    "TLS_STRICT",
    "TLS_UNKNOWN",
    "TRIGGER_KIND_ORDER",
    "VERSION_KEY",
    "CustomFeed",
    "ProxyChain",
    "ProxyChainEntry",
    "ProxyChains",
    "ProxyCheckRecord",
    "ProxyEndpoint",
    "ProxyFeedFacts",
    "ProxyHealthState",
    "clamp_max_switches",
    "current_proxy_chains",
    "is_valid_proxy_url",
    "load_proxy_chains",
    "migrate_proxy_feeds",
    "normalise_policy",
    "normalise_scope",
    "normalise_trigger_kinds",
    "reset_proxy_chains_cache",
    "save_proxy_chains",
]
