"""Convert a pre-7.18.0 store's built-in feed ids into custom feed entries.

**This module is a transitional table and nothing else.** Releases up to 7.17.1
shipped a catalogue of seven concrete public proxy lists and wrote an
operator's selection into ``~/.mcc/proxy_chains.json`` as a list of the
catalogue's ids::

    "feeds": ["databay", "vpslab"]

7.18.0 ships no catalogue: MCC ships readers, not sources, and a feed is
something the operator adds by name, URL and format. The table below is the
only thing in the product that still knows those seven URLs, and it exists for
exactly one purpose -- turning that stored id list into the custom feed entries
that mean the same thing -- so that an install which had feeds switched on
keeps them switched on, fetching the same URLs with the same readers.

**Nothing here offers a feed.** :data:`LEGACY_BUILT_IN_FEEDS` is never read by
the Proxying page, never reaches the admin payload and never appears in a
picker. A fresh install passes through this module without it producing
anything, because a fresh install has no built-in ids to convert.

**When this can be deleted.** After two or three releases, once no store in
circulation can still hold a built-in id -- **7.38.0** is the earliest sensible
point. It used to say 7.21.0, then 7.22.0, then 7.23.0, then 7.24.0, then
7.25.0, then 7.26.0, then 7.27.0, then 7.28.0, then 7.29.0, then 7.30.0, then
7.31.0, then 7.32.0, then 7.33.0, then 7.34.0, then 7.35.0, then 7.36.0, then
7.37.0; all
seventeen
releases went to
something else (the
proxy fetch pass, the 429 cooldown controls, the Responses surface learning what
a host refuses, putting every remaining proxy tunable on the dashboard,
per-model reasoning and output preferences, the desktop app waiting for a busy
server instead of replacing it, the server no longer holding its own event
loop while it works, OpenCode's free tier learning to read the tool
catalogue, key pools you can reorder and name, several OAuth accounts per
provider, then Pause, Resume and Refresh models letting go of the event
loop, then the last of the 7.24.0 audit's tunables reaching the
dashboard, then a hand-configured host being able to declare that it serves
Responses or Messages, then free OpenCode Zen models moving onto OpenCode's
own shared credential so the operator's key stops paying for them, then a
multi-surface gateway declaring which of its doors to knock on when nothing
has been published about a model, then a Responses host's refusal of one
JSON-Schema keyword being answered once and remembered, then the
stuck-request watchdog that writes down where a request that has gone quiet
is actually parked), and
deleting this in any of them would
have bundled an unrelated removal onto it. One concern per release,
so the note moves rather than the deletion happening quietly beside something
else. Deleting it is: remove this module,
remove the ``str`` branch in
:meth:`~my_claude_code.config.proxy_chains.ProxyChains.from_document`'s feed
loop, and remove :func:`~my_claude_code.config.proxy_chains.migrate_proxy_feeds`
from server startup. At that point
a store still naming built-in ids loses its feed switches, which is why this
waits for the ids to have aged out rather than going now.

**Idempotence.** The conversion happens in memory on every read, so a store is
never *seen* without its feeds even if the write below never runs. The write
runs once: after it, the document holds feed objects rather than id strings,
:attr:`ProxyChains.migrated_feed_ids` comes back empty on the next read, and
the one-time rewrite in ``proxy_chains`` returns without writing. A second
start is a no-op,
not a second set of entries.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from my_claude_code.config.proxy_feeds import CustomFeed

#: The seven feeds 7.17.1 shipped, as the custom entries that mean the same
#: thing. Name, URL, reader and -- for the one plain-text list -- what its own
#: filename said about its rows, without which it would parse to nothing.
#:
#: The URLs are reproduced verbatim from the deleted catalogue, filters and
#: all: a converted feed must fetch exactly what it fetched before, or the
#: migration would quietly change what the operator's install reads.
LEGACY_BUILT_IN_FEEDS: dict[str, CustomFeed] = {
    "proxyscrape": CustomFeed(
        id="",
        name="ProxyScrape",
        url=(
            "https://api.proxyscrape.com/v4/free-proxy-list/get"
            "?request=display_proxies&proxy_format=protocolipport"
            "&format=json&limit=300&protocol=http,socks5"
        ),
        parser="proxyscrape",
        observed=(
            "JSON with per-address metadata: protocol, anonymity, SSL "
            "support, uptime, timeout, country and ASN."
        ),
    ),
    "hproxy": CustomFeed(
        id="",
        name="HProxy",
        url="https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/live.json",
        parser="hproxy",
        observed=(
            "JSON from a GitHub repository: protocol, anonymity, country, "
            "latency and observed uptime per address."
        ),
    ),
    "databay": CustomFeed(
        id="",
        name="Databay (TLS-strict)",
        url=(
            "https://databay.com/api/v1/proxy-list?protocol=socks5&ssl=strict&limit=300"
        ),
        parser="databay",
        tls_strict=True,
        observed=(
            "JSON whose own ssl=strict filter selects for a tunnel that "
            "leaves the destination's certificate checkable."
        ),
    ),
    "proxifly": CustomFeed(
        id="",
        name="Proxifly",
        url=(
            "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
            "/proxies/protocols/socks5/data.json"
        ),
        parser="proxifly",
        observed=(
            "JSON from a CDN mirror: protocol, anonymity, an https flag and a "
            "country per address."
        ),
    ),
    "vpslab": CustomFeed(
        id="",
        name="VPSLab (HTTP, SSL, elite)",
        url=(
            "https://raw.githubusercontent.com/VPSLabCloud/"
            "VPSLab-Free-Proxy-List/main/http_ssl_elite.txt"
        ),
        parser="lines",
        # The three that matter most in this whole table. A bare ip:port list
        # parses to nothing without a protocol to read it as, so a conversion
        # that dropped these would leave the operator with a feed that fetches
        # fine and offers zero addresses -- the failure mode hardest to notice.
        assume_protocol="http",
        assume_https_ok=True,
        assume_anonymity="elite",
        observed=(
            "Plain ip:port lines behind a comment header naming the file's "
            "protocol, SSL and anonymity."
        ),
    ),
    "monosans": CustomFeed(
        id="",
        name="monosans",
        url="https://raw.githubusercontent.com/monosans/proxy-list/main/proxies.json",
        parser="monosans",
        observed=(
            "JSON from a GitHub repository: protocol, host, port, the "
            "response time in seconds, and an ASN and country per address."
        ),
    ),
    "geonode": CustomFeed(
        id="",
        name="Geonode",
        url=(
            "https://proxylist.geonode.com/api/proxy-list"
            "?limit=300&page=1&sort_by=lastChecked&sort_type=desc"
        ),
        parser="geonode",
        observed=(
            "JSON from a list API: protocols, anonymity level, country, ASN, "
            "latency, uptime percentage and a last-checked timestamp."
        ),
    ),
}


def is_legacy_feed_id(value: object) -> bool:
    """Whether ``value`` names one of the seven feeds 7.17.1 shipped."""

    return str(value or "").strip().lower() in LEGACY_BUILT_IN_FEEDS


def convert_legacy_feed_id(
    feed_id: str, *, at: str, taken: Sequence[str]
) -> CustomFeed:
    """One built-in id as the custom feed entry that means the same thing.

    Converted **enabled**. A stored id was the operator saying "read this one",
    and the whole point of the migration is that nothing they configured stops
    working; converting to a switched-off row would be a silent removal wearing
    a migration's clothes.
    """

    template = LEGACY_BUILT_IN_FEEDS[str(feed_id).strip().lower()]
    # The stored id is reused as the feed's id. It is unique by construction
    # (the source was a set of catalogue ids) and it makes the conversion
    # legible in the file itself: "databay" stays "databay".
    chosen = str(feed_id).strip().lower()
    while chosen in taken:  # pragma: no cover - ids came from a set
        chosen = f"{chosen}_1"
    return replace(template, id=chosen, enabled=True, added_at=at)


def convert_legacy_feed_ids(
    values: Sequence[object], *, taken: Sequence[str] = ()
) -> tuple[CustomFeed, ...]:
    """Every built-in id among ``values``, as custom feeds, in the given order.

    An id this table does not know is dropped rather than raised on -- the
    ``known_feed_ids`` contract it replaces, for the same reason: this is
    reached by a stored document that may name a feed retired before 7.18.0,
    and a store that refuses to load because one source went away is a worse
    outcome than a switch quietly going.
    """

    at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    converted: list[CustomFeed] = []
    seen = list(taken)
    for value in values:
        feed_id = str(value or "").strip().lower()
        if feed_id not in LEGACY_BUILT_IN_FEEDS or feed_id in seen:
            continue
        feed = convert_legacy_feed_id(feed_id, at=at, taken=seen)
        seen.append(feed.id)
        converted.append(feed)
    return tuple(converted)


__all__ = [
    "LEGACY_BUILT_IN_FEEDS",
    "convert_legacy_feed_id",
    "convert_legacy_feed_ids",
    "is_legacy_feed_id",
]
