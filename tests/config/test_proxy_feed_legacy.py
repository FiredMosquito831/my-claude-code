"""Converting a pre-7.18.0 store's built-in feed ids into custom feeds.

This is the half of the release that can do real harm. Up to 7.17.1 MCC
shipped seven public proxy lists and an operator's selection was stored as a
list of *their ids*; 7.18.0 ships none, so a store naming ``"databay"`` names
something this install no longer has. Read naively, that operator's feeds
would silently switch themselves off -- and the addresses they had already
gathered would stop being refreshed by anything.

So the cases below are not decoration. They are the four shapes a real store
can be in at the moment of upgrade, plus the two that prove the write happens
exactly once:

* one built-in enabled, and several, and the seven;
* ids present that this install has never heard of;
* no ``feeds`` key at all, and an empty list;
* and, twice over, that a second start converts nothing and writes nothing.

The fixture in :func:`_legacy_store` is shaped like the store this was
developed against: feeds named by id, candidates already fetched, and a chain
with addresses promoted into it. Everything except the ``feeds`` key must come
out the other side byte-identical, because a migration that loses a working
configuration is worse than the feature is good.
"""

import json
from pathlib import Path

from my_claude_code.config.proxy_chains import (
    ProxyChains,
    load_proxy_chains,
    migrate_proxy_feeds,
    save_proxy_chains,
)
from my_claude_code.config.proxy_feed_legacy import (
    LEGACY_BUILT_IN_FEEDS,
    convert_legacy_feed_ids,
    is_legacy_feed_id,
)
from my_claude_code.config.proxy_feeds import CustomFeed, is_valid_feed_url


def _legacy_store(tmp_path: Path, feeds: object = "unset") -> Path:
    """A 7.17.1-shaped document: feeds by id, candidates, and a live chain."""

    document: dict[str, object] = {
        "version": 1,
        "proxies": {
            "px_inchain0": {
                "url": "socks5h://198.51.100.7:1080",
                "label": "198.51.100.7:1080",
                "added_at": "2026-09-15T10:00:00Z",
                "source": "feed",
                "source_count": 3,
                "sources": ["databay", "geonode", "vpslab"],
            },
            "px_offered0": {
                "url": "http://203.0.113.9:8080",
                "label": "203.0.113.9:8080",
                "added_at": "2026-09-15T10:00:00Z",
                "source": "feed",
                "source_count": 1,
                "sources": ["vpslab"],
            },
        },
        "chains": {
            "opencode": {
                "enabled": True,
                "policy": "round_robin",
                "entries": [
                    {"proxy": "px_inchain0", "paused": False},
                    {"proxy": "", "paused": False},
                ],
                "on": ["quota", "rate_limit", "timeout"],
                "scope": "provider",
                "max_switches": 2,
                "oauth_acknowledged": False,
            }
        },
        "candidates": ["px_offered0"],
    }
    if feeds != "unset":
        document["feeds"] = feeds
    path = tmp_path / "proxy_chains.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def _everything_but_the_feeds(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("feeds", None)
    return document


def test_one_enabled_built_in_becomes_one_custom_feed_still_switched_on(
    tmp_path: Path,
) -> None:
    """The commonest real store: one list ticked, and it keeps working.

    Enabled is the whole point. A conversion that produced a switched-off row
    would be a silent removal wearing a migration's clothes -- the operator
    would find their candidate list going stale with nothing on the page
    saying why.
    """

    path = _legacy_store(tmp_path, ["databay"])
    assert migrate_proxy_feeds(path) == ("databay",)

    store = load_proxy_chains(path)
    assert [feed.id for feed in store.feeds] == ["databay"]
    feed = store.feeds[0]
    assert feed.enabled is True
    assert feed.parser == "databay"
    assert feed.name == "Databay (TLS-strict)"
    # The URL is reproduced verbatim, filters and all: a converted feed must
    # fetch exactly what it fetched before.
    assert feed.url == LEGACY_BUILT_IN_FEEDS["databay"].url
    assert "ssl=strict" in feed.url
    assert store.enabled_feed_ids == ("databay",)


def test_a_plain_text_feed_keeps_the_assumptions_without_which_it_reads_nothing(
    tmp_path: Path,
) -> None:
    """The one conversion that can fail while looking like it worked.

    ``vpslab`` is a bare ``ip:port`` list. Such a file cannot say which scheme
    dials it, so the built-in carried ``assume_protocol`` -- and without it the
    ``lines`` reader yields zero addresses from a body that downloaded
    perfectly. That is the failure mode hardest to notice, so it is pinned.
    """

    path = _legacy_store(tmp_path, ["vpslab"])
    migrate_proxy_feeds(path)

    feed = load_proxy_chains(path).feeds[0]
    assert feed.parser == "lines"
    assert feed.assume_protocol == "http"
    assert feed.assume_https_ok is True
    assert feed.assume_anonymity == "elite"

    # And it really does parse: the assumption is carried all the way into the
    # ProxyFeed the ingest pass fetches with.
    found = feed.as_proxy_feed().parse("# a header\n203.0.113.5:8080\n")
    assert [endpoint.url for endpoint in found] == ["http://203.0.113.5:8080"]


def test_several_enabled_built_ins_all_convert_in_the_stored_order(
    tmp_path: Path,
) -> None:
    """Seven ticked is the development-server state, and it must land whole."""

    every_id = list(LEGACY_BUILT_IN_FEEDS)
    path = _legacy_store(tmp_path, every_id)
    assert list(migrate_proxy_feeds(path)) == every_id

    store = load_proxy_chains(path)
    assert [feed.id for feed in store.feeds] == every_id
    assert all(feed.enabled for feed in store.feeds)
    assert all(feed.readable for feed in store.feeds)
    assert store.enabled_feed_ids == tuple(every_id)


def test_ids_this_install_has_never_heard_of_are_dropped_rather_than_raised_on(
    tmp_path: Path,
) -> None:
    """A feed retired before 7.18.0 leaves a switch behind, not a crash.

    The same contract ``known_feed_ids`` held: a store that refuses to load
    because one source went away is a worse outcome than a switch quietly
    going.
    """

    path = _legacy_store(tmp_path, ["a-feed-that-retired", "databay"])
    assert migrate_proxy_feeds(path) == ("databay",)

    store = load_proxy_chains(path)
    assert [feed.id for feed in store.feeds] == ["databay"]


def test_a_store_with_no_feeds_key_converts_nothing_and_is_not_rewritten(
    tmp_path: Path,
) -> None:
    """A fresh install, and an install that never ticked anything.

    Nothing to convert means nothing to write. The file is left exactly as it
    was, which is what keeps a start cheap on the overwhelming majority of
    installs.
    """

    path = _legacy_store(tmp_path)
    before = path.read_bytes()
    assert migrate_proxy_feeds(path) == ()
    assert path.read_bytes() == before
    assert load_proxy_chains(path).feeds == ()


def test_an_empty_feed_list_converts_nothing_and_is_not_rewritten(
    tmp_path: Path,
) -> None:
    """ "Ids present but none enabled" in the only shape 7.17.1 could store it.

    The stored list *was* the enabled set, so "none enabled" is an empty list.
    It reads back as no feeds, which is exactly what it meant.
    """

    path = _legacy_store(tmp_path, [])
    before = path.read_bytes()
    assert migrate_proxy_feeds(path) == ()
    assert path.read_bytes() == before
    assert load_proxy_chains(path).feeds == ()


def test_the_migration_is_idempotent_and_a_second_start_writes_nothing(
    tmp_path: Path,
) -> None:
    """The requirement that keeps this safe to run on every boot.

    Not "converts the same thing twice" -- writes *nothing* the second time,
    and leaves one entry per feed rather than two. The write is what makes it
    stop: afterwards the document holds objects, so the read converts nothing
    and there is nothing to report.
    """

    path = _legacy_store(tmp_path, ["databay", "vpslab"])
    assert migrate_proxy_feeds(path) == ("databay", "vpslab")
    after_first = path.read_bytes()

    assert migrate_proxy_feeds(path) == ()
    assert path.read_bytes() == after_first
    assert migrate_proxy_feeds(path) == ()

    store = load_proxy_chains(path)
    assert [feed.id for feed in store.feeds] == ["databay", "vpslab"]
    assert store.migrated_feed_ids == ()


def test_saving_a_converted_store_by_any_other_route_also_completes_it(
    tmp_path: Path,
) -> None:
    """An operator who edits a feed before the migration runs loses nothing.

    The conversion happens on every read, so any write of the store -- a chain
    edit, a feed edit, an ingest pass -- persists it. The startup migration is
    how it happens without waiting for one, not the only way it can.
    """

    path = _legacy_store(tmp_path, ["databay"])
    save_proxy_chains(load_proxy_chains(path), path)

    assert migrate_proxy_feeds(path) == ()
    assert load_proxy_chains(path).enabled_feed_ids == ("databay",)


def test_nothing_but_the_feed_list_is_touched(tmp_path: Path) -> None:
    """Candidates, endpoints and a live chain come out byte-identical.

    The migration is about one key. Sixty addresses already fetched and twelve
    already promoted into a chain are the operator's work, and this is the
    assertion that says the release did not spend any of it.
    """

    path = _legacy_store(tmp_path, ["databay", "vpslab"])
    before = _everything_but_the_feeds(path)

    migrate_proxy_feeds(path)

    assert _everything_but_the_feeds(path) == before
    store = load_proxy_chains(path)
    assert store.candidates == ("px_offered0",)
    assert set(store.proxies) == {"px_inchain0", "px_offered0"}
    chain = store.chains["opencode"]
    assert chain.enabled is True
    assert [entry.proxy for entry in chain.entries] == ["px_inchain0", ""]
    # The provenance on an address outlives the feed that supplied it: these
    # names are still what the page renders beside "3 feeds agreed".
    assert store.proxies["px_inchain0"].sources == ("databay", "geonode", "vpslab")


def test_a_converted_feed_can_then_be_edited_and_removed_like_any_other(
    tmp_path: Path,
) -> None:
    """After the migration there is no such thing as a built-in feed.

    The point of converting rather than special-casing: the operator gets rows
    they own. Removing one keeps the addresses it supplied, which is the rule
    for every feed and is what stops "I do not want this list any more" from
    also meaning "throw away the work I did choosing from it".
    """

    path = _legacy_store(tmp_path, ["databay", "vpslab"])
    migrate_proxy_feeds(path)
    store = load_proxy_chains(path)

    kept = [feed for feed in store.feeds if feed.id != "databay"]
    save_proxy_chains(store.with_feeds(kept), path)

    after = load_proxy_chains(path)
    assert [feed.id for feed in after.feeds] == ["vpslab"]
    assert after.candidates == ("px_offered0",)
    assert set(after.proxies) == {"px_inchain0", "px_offered0"}


def test_a_document_mixing_ids_and_entries_keeps_both_halves(
    tmp_path: Path,
) -> None:
    """What a half-finished hand edit looks like. Neither half is lost.

    No release can produce this, but a person can, and there is no reason for
    the reader to choose one branch and discard the other.
    """

    path = _legacy_store(
        tmp_path,
        [
            {
                "id": "fd_mine",
                "name": "My mirror",
                "url": "https://lists.example.com/socks5.json",
                "parser": "proxifly",
                "enabled": True,
            },
            "databay",
        ],
    )
    assert migrate_proxy_feeds(path) == ("databay",)

    store = load_proxy_chains(path)
    assert [feed.id for feed in store.feeds] == ["fd_mine", "databay"]
    assert store.enabled_feed_ids == ("fd_mine", "databay")


def test_every_entry_in_the_transitional_table_is_one_this_install_can_read(
    tmp_path: Path,
) -> None:
    """The table is only useful if what it produces actually works.

    Seven rows, each with an https URL and a reader this install still ships.
    A row naming a parser that had been dropped would convert a working feed
    into an unreadable one, which is the migration failing quietly.
    """

    assert len(LEGACY_BUILT_IN_FEEDS) == 7
    for feed_id, feed in LEGACY_BUILT_IN_FEEDS.items():
        assert is_legacy_feed_id(feed_id)
        assert is_valid_feed_url(feed.url), feed_id
        assert feed.readable, feed_id
        assert feed.name
        assert feed.observed


def test_converting_takes_ids_already_in_use_into_account() -> None:
    """A converted id never collides with a feed the operator already has."""

    converted = convert_legacy_feed_ids(["databay"], taken=["databay"])
    assert converted == ()

    converted = convert_legacy_feed_ids(["databay", "databay"])
    assert [feed.id for feed in converted] == ["databay"]


def test_the_in_memory_conversion_happens_even_if_the_write_never_does(
    tmp_path: Path,
) -> None:
    """A read-only store still behaves correctly; it just stays unconverted.

    This is why the conversion lives in the reader rather than only in the
    migration: an install that cannot rewrite its store keeps every feed
    working, and the rewrite happens at the next start that can.
    """

    path = _legacy_store(tmp_path, ["databay"])
    store = load_proxy_chains(path)

    assert store.enabled_feed_ids == ("databay",)
    assert store.migrated_feed_ids == ("databay",)
    # Nothing was written by the read itself.
    assert json.loads(path.read_text(encoding="utf-8"))["feeds"] == ["databay"]


def test_the_converted_ids_are_never_written_back_into_the_document() -> None:
    """``migrated_feed_ids`` is a fact about one read, not stored state.

    If it survived a round trip the migration would look undone on every
    start, and the "write once" property would quietly become "write every
    time".
    """

    feed = CustomFeed(
        id="databay",
        name="Databay",
        url="https://databay.example/list",
        parser="databay",
        enabled=True,
    )
    store = ProxyChains(feeds=(feed,), migrated_feed_ids=("databay",))
    document = store.as_document()

    assert "migrated_feed_ids" not in document
    assert ProxyChains.from_document(document).migrated_feed_ids == ()
