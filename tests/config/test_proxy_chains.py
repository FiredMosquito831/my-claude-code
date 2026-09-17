"""The per-provider proxy chain store: ``~/.mcc/proxy_chains.json``.

Both halves matter here for the same reason they do for ``harness_tiers``:
what a malformed document does (nothing, loudly) and what a round trip
preserves. The difference is that one field of this store can carry a
password, so a third half matters too -- that the label derived from it never
does.
"""

import json
from pathlib import Path

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    DEFAULT_TRIGGER_KINDS,
    DIRECT,
    MAX_SWITCHES_DEFAULT,
    REFUSED_TRIGGER_KINDS,
    TLS_INTERCEPTED,
    TLS_STRICT,
    TLS_UNKNOWN,
    TRIGGER_KIND_ORDER,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyCheckRecord,
    clamp_max_switches,
    current_proxy_chains,
    is_valid_proxy_url,
    load_proxy_chains,
    normalise_policy,
    normalise_scope,
    normalise_trigger_kinds,
    reset_proxy_chains_cache,
    save_proxy_chains,
)


def _store_with_one_chain() -> tuple[ProxyChains, str]:
    store, proxy_id = ProxyChains().add_endpoint("socks5h://user:pass@203.0.113.7:1080")
    store = store.with_chain(
        "opencode",
        ProxyChain(
            enabled=True,
            policy="round_robin",
            entries=(
                ProxyChainEntry(proxy=proxy_id),
                ProxyChainEntry(proxy=DIRECT),
            ),
            on=("quota",),
        ),
    )
    return store, proxy_id


def test_an_absent_provider_inherits_its_env_proxy() -> None:
    """No chain is the third state, and it is the default for every provider.

    ``chain()`` returning ``None`` rather than an empty chain is what lets the
    caller distinguish "this provider uses ``<PROVIDER>_PROXY`` exactly as it
    always has" from "this provider has a chain that is currently switched
    off". Collapsing the two would make the upgrade story impossible to state.
    """

    store, _ = _store_with_one_chain()

    assert store.chain("opencode") is not None
    assert store.chain("anthropic") is None
    assert ProxyChains().chain("opencode") is None


def test_a_round_trip_preserves_the_chain(tmp_path: Path) -> None:
    store, proxy_id = _store_with_one_chain()
    path = tmp_path / "proxy_chains.json"

    save_proxy_chains(store, path)
    loaded = load_proxy_chains(path)

    chain = loaded.chain("opencode")
    assert chain is not None
    assert chain.enabled is True
    assert chain.policy == "round_robin"
    assert chain.on == ("quota",)
    assert [entry.proxy for entry in chain.entries] == [proxy_id, DIRECT]
    endpoint = loaded.endpoint(proxy_id)
    assert endpoint is not None
    assert endpoint.url == "socks5h://user:pass@203.0.113.7:1080"


def test_direct_is_a_legal_chain_entry() -> None:
    """ "Try my proxies, then my own address" must be expressible.

    Without it the feature would force an all-or-nothing choice: either every
    request goes through somebody else's machine or none does.
    """

    entry = ProxyChainEntry(proxy=DIRECT)

    assert entry.is_direct
    assert entry.as_document() == {"proxy": "", "paused": False}
    assert ProxyChain(entries=(entry,)).proxy_ids() == ()


def test_an_unknown_failure_kind_in_on_is_dropped_not_raised() -> None:
    """The ``parse_failure_kinds`` contract, for the same reason it has it.

    A stored document can predate a renamed kind, and a server that refuses to
    start because one word in a chain is no longer spelled that way would turn
    a cosmetic rename into an outage.
    """

    assert normalise_trigger_kinds(["quota", "not_a_kind", "timeout"]) == (
        "quota",
        "timeout",
    )
    assert normalise_trigger_kinds([]) == ()


def test_a_refused_kind_can_not_be_armed_by_hand_editing_the_file() -> None:
    """The two destructive kinds are refused by the store, not just by the page.

    Rotating on a 401 burns the whole chain inside one request and earns a
    lockout tier on every proxy it touched. A chip the page will not render is
    worth nothing if the file behind it accepts the word anyway.
    """

    for kind in REFUSED_TRIGGER_KINDS:
        assert normalise_trigger_kinds([kind, "quota"]) == ("quota",)


def test_the_trigger_order_lists_every_kind_exactly_once() -> None:
    assert len(TRIGGER_KIND_ORDER) == len(set(TRIGGER_KIND_ORDER))
    assert set(DEFAULT_TRIGGER_KINDS) <= set(TRIGGER_KIND_ORDER)
    assert set(REFUSED_TRIGGER_KINDS) <= set(TRIGGER_KIND_ORDER)
    assert not set(DEFAULT_TRIGGER_KINDS) & set(REFUSED_TRIGGER_KINDS)


def test_the_policy_vocabulary_is_the_credential_engine_s_own() -> None:
    """Four names and one alias -- not a fifth policy, and not a rename.

    Two rotation controls on the same dashboard meaning different things by
    the same word is the failure this pins against.
    """

    from my_claude_code.providers.runtime.config import CREDENTIAL_ROTATION_POLICIES

    for policy in CREDENTIAL_ROTATION_POLICIES - {"on_error"}:
        assert normalise_policy(policy) == policy
    assert normalise_policy("on_error") == "failover"
    assert normalise_policy("bogus") == "failover"
    assert normalise_policy(None) == "failover"


def test_the_scope_defaults_to_provider() -> None:
    """The quota this feature was built for is metered by address alone.

    Under ``provider`` a proxy benched for a quota trigger is benched for that
    provider across every key, because the exhausted thing is the IP and every
    key shares it. ``credential`` stays available for a provider that meters
    per (address, account).
    """

    assert ProxyChain().scope == "provider"
    assert normalise_scope("credential") == "credential"
    assert normalise_scope("nonsense") == "provider"


def test_the_switch_bound_is_clamped_into_the_user_s_range() -> None:
    assert clamp_max_switches(0) == 1
    assert clamp_max_switches(99) == 5
    assert clamp_max_switches("3") == 3
    assert clamp_max_switches(None) == MAX_SWITCHES_DEFAULT
    assert ProxyChain().max_switches == MAX_SWITCHES_DEFAULT


def test_a_malformed_document_means_no_chains_and_never_an_exception(
    tmp_path: Path,
) -> None:
    """Every provider falls back to its ``<PROVIDER>_PROXY``, which is today.

    The worst honest outcome of an unreadable store is the behaviour of every
    release before it, plus a log line. It must never stop the server.
    """

    for content in ("", "   ", "not json", "[]", '{"chains": 7}'):
        path = tmp_path / "proxy_chains.json"
        path.write_text(content, encoding="utf-8")
        assert load_proxy_chains(path).chains == {}

    assert load_proxy_chains(tmp_path / "absent.json").is_empty


def test_an_entry_naming_a_lost_endpoint_is_dropped_not_kept_dangling(
    tmp_path: Path,
) -> None:
    """A rung that resolves to nothing would render blank and route nowhere."""

    path = tmp_path / "proxy_chains.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "proxies": {},
                "chains": {
                    "opencode": {
                        "enabled": True,
                        "entries": [{"proxy": "px_gone"}, {"proxy": ""}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    chain = load_proxy_chains(path).chain("opencode")
    assert chain is not None
    assert [entry.proxy for entry in chain.entries] == [DIRECT]


def test_the_same_address_is_filed_once_so_two_chains_share_its_record() -> None:
    """Two chains naming the same machine must share one health record.

    Otherwise a dead proxy would have to be discovered separately for every
    provider that happened to list it.
    """

    store, first = ProxyChains().add_endpoint("http://198.51.100.9:8080")
    store, second = store.add_endpoint("http://198.51.100.9:8080")

    assert first == second
    assert len(store.proxies) == 1


def test_an_endpoint_no_chain_names_is_pruned() -> None:
    """The catalogue is shared storage, not an accumulator."""

    store, proxy_id = _store_with_one_chain()
    assert store.endpoint(proxy_id) is not None

    emptied = store.with_chain("opencode", ProxyChain())
    assert emptied.proxies == {}


def test_reading_a_chain_never_truncates_what_the_operator_saved(
    tmp_path: Path,
) -> None:
    """Every entry in the document comes back, however many there are.

    Replaces ``test_a_chain_is_capped_at_the_documented_entry_limit``, which
    asserted the opposite: up to 7.18 this reader silently dropped everything
    past the twelfth entry. That cap existed because a leg cost a client, a
    rate limiter and a recovery ladder at construction time -- 7.19.0 builds
    legs on first use, so it does not, and a ceiling is now the operator's own
    ``PROXY_CHAIN_MAX_ENTRIES`` enforced at the API with a message. A store
    this install already wrote is data, and silently losing part of it on read
    was never the right answer to a bound on a request.
    """

    path = tmp_path / "proxy_chains.json"
    path.write_text(
        json.dumps(
            {
                "chains": {
                    "opencode": {
                        "entries": [{"proxy": ""} for _ in range(40)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    chain = load_proxy_chains(path).chain("opencode")
    assert chain is not None
    assert len(chain.entries) == 40


def test_a_proxy_url_needs_a_scheme_this_product_can_dial() -> None:
    """A bare host:port is refused rather than guessed at.

    Guessing between ``http`` and ``socks5`` for an operator is exactly the
    silent default that produces a chain which cannot connect and says nothing
    about why.
    """

    assert is_valid_proxy_url("socks5h://user:pass@203.0.113.7:1080")
    assert is_valid_proxy_url("http://proxy.internal")
    assert not is_valid_proxy_url("203.0.113.7:1080")
    assert not is_valid_proxy_url("ftp://203.0.113.7:21")
    assert not is_valid_proxy_url("")
    assert not is_valid_proxy_url("socks5://")


def test_the_label_for_a_proxy_never_contains_its_password() -> None:
    """The only form of a proxy URL that may leave this process.

    A proxy password in an HTTP response or the request log would be a worse
    leak than the thing a proxy chain is trying to avoid.
    """

    label = mask_proxy_label("socks5h://alice:hunter2@203.0.113.7:1080")

    assert label == "203.0.113.7:1080"
    assert "hunter2" not in label
    assert "alice" not in label
    assert mask_proxy_label("http://proxy.internal") == "proxy.internal"
    assert mask_proxy_label("") == ""
    assert mask_proxy_label("not a url at all") == ""


def test_the_cache_follows_the_file_rather_than_the_process(tmp_path: Path) -> None:
    """A dashboard edit is picked up without a restart.

    Keyed on the file's own stat, which is what lets the admin route write the
    document and tell nothing else.
    """

    path = tmp_path / "proxy_chains.json"
    reset_proxy_chains_cache()
    assert current_proxy_chains(path).is_empty

    store, _ = _store_with_one_chain()
    save_proxy_chains(store, path)

    assert current_proxy_chains(path).chain("opencode") is not None
    reset_proxy_chains_cache()


# ------------------------------------------------- the checker's own verdict


def test_a_check_verdict_round_trips_through_the_document(tmp_path: Path) -> None:
    """The verdict is durable, because the refusal it can carry has to be.

    The two ledgers the runtime reads are process-lifetime state. If the
    document did not keep the verdict, a restart would quietly re-admit every
    address a previous check found terminating TLS.
    """

    store, proxy_id = _store_with_one_chain()
    store = store.with_check(
        proxy_id,
        ProxyCheckRecord(
            at="2026-09-15T12:00:00Z",
            ok=True,
            latency_ms=412,
            tls=TLS_STRICT,
            exit_ip="203.0.113.7",
        ),
    )
    path = tmp_path / "proxy_chains.json"
    save_proxy_chains(store, path)

    endpoint = load_proxy_chains(path).endpoint(proxy_id)

    assert endpoint is not None
    assert endpoint.last_check is not None
    assert endpoint.last_check.tls == TLS_STRICT
    assert endpoint.last_check.latency_ms == 412
    assert endpoint.last_check.exit_ip == "203.0.113.7"
    assert endpoint.refused is False


def test_an_intercepted_verdict_marks_the_endpoint_refused(tmp_path: Path) -> None:
    """One field, read by three surfaces, and all three must agree.

    The store says it, the API refuses a write naming it, and the pool holds
    it out of selection. This is the field the other two read.
    """

    store, proxy_id = _store_with_one_chain()
    store = store.with_check(
        proxy_id,
        ProxyCheckRecord(at="2026-09-15T12:00:00Z", ok=False, tls=TLS_INTERCEPTED),
    )
    path = tmp_path / "proxy_chains.json"
    save_proxy_chains(store, path)

    reloaded = load_proxy_chains(path)

    endpoint = reloaded.endpoint(proxy_id)
    assert endpoint is not None
    assert endpoint.refused is True
    assert reloaded.refused_ids() == (proxy_id,)


def test_an_endpoint_with_no_check_says_so_rather_than_guessing() -> None:
    """``None`` is not "failed". Nothing has looked, and the page says that."""

    store, proxy_id = _store_with_one_chain()

    endpoint = store.endpoint(proxy_id)
    assert endpoint is not None
    assert endpoint.last_check is None
    assert endpoint.refused is False
    # And the document carries no key at all rather than a default-shaped one
    # that would read as a measurement.
    assert "last_check" not in endpoint.as_document()


def test_a_nonsense_tls_value_in_the_document_reads_as_unknown() -> None:
    """A hand-edited file must not be able to invent a verdict.

    Unknown is the safe direction: it means "nothing was measured", which
    neither refuses a working address nor admits a refused one -- the refusal
    is only ever written by a check.
    """

    record = ProxyCheckRecord.from_document(
        {"tls": "definitely-fine", "ok": True, "latency_ms": "not a number"}
    )

    assert record is not None
    assert record.tls == TLS_UNKNOWN
    assert record.latency_ms is None


def test_a_check_for_an_address_the_store_lost_is_dropped() -> None:
    """The checker is out of band and may finish after a removal.

    Inventing an endpoint from a stale result would put a row back on a page
    somebody had just cleared.
    """

    store, _ = _store_with_one_chain()

    assert store.with_check("px_gone", ProxyCheckRecord(ok=True)) == store


def test_a_refetch_keeps_what_was_added_and_what_was_refused() -> None:
    """The third reading of "persistence", verified rather than implemented.

    An operator who fetches the feeds again must not lose the work of the last
    pass: an address they added is in a chain and must not come back as
    something nobody has chosen, and an address the checker refused must come
    back refused rather than as a fresh unknown row -- that verdict is the one
    piece of state on this page that is a security control.
    """

    from my_claude_code.config.proxy_chains import ProxyEndpoint

    offered = [
        ("px_chosen01", ProxyEndpoint(url="socks5h://203.0.113.7:1080", source="feed")),
        ("px_bad00002", ProxyEndpoint(url="http://203.0.113.8:8080", source="feed")),
        ("px_plain003", ProxyEndpoint(url="http://203.0.113.9:8080", source="feed")),
    ]
    store = ProxyChains().with_candidates(offered)
    store = store.with_check(
        "px_bad00002",
        ProxyCheckRecord(at="2026-09-16T00:00:00Z", ok=False, tls=TLS_INTERCEPTED),
    )
    # One of them is promoted into a chain, the way a bulk add promotes them.
    store = store.without_candidate("px_chosen01").with_chain(
        "nvidia_nim",
        ProxyChain(entries=(ProxyChainEntry(proxy="px_chosen01"),)),
    )

    # The feeds answer with all three again, as they will.
    refetched = store.with_candidates(offered)

    # The chosen one stays chosen rather than being re-offered.
    assert "px_chosen01" not in refetched.candidates
    chain = refetched.chain("nvidia_nim")
    assert chain is not None and chain.proxy_ids() == ("px_chosen01",)
    # And the refusal survives the pass.
    refused = refetched.endpoint("px_bad00002")
    assert refused is not None and refused.refused is True
    assert "px_bad00002" in refetched.candidates
