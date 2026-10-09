"""PR-S1 (7.89.0): every address a chain can use has a ledger label of its own.

The reachability ladder, the interception refusals, the trigger benches, the
speed ledger, the exit memory and the request log all key an address by one
string. Two addresses that differ only in their credentials used to come out
as the same ``host:port`` and share every one of those books. These tests pin
the rule that tells them apart -- and that it renames nothing else.
"""

import hashlib
import json
from pathlib import Path

import pytest

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyEndpoint,
    load_proxy_chains,
    note_built_chain_labels,
    providers_with_stale_labels,
    reset_built_chain_labels,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health

GATEWAY_A = "socks5h://customer-alice-sessid-1:pw@gw.example:7777"
GATEWAY_B = "socks5h://customer-alice-sessid-2:pw@gw.example:7777"


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:4]


def _store(
    proxies: dict[str, ProxyEndpoint], chains: dict[str, list[str]] | None = None
) -> ProxyChains:
    return ProxyChains(
        proxies=proxies,
        chains={
            provider: ProxyChain(
                enabled=True,
                entries=tuple(ProxyChainEntry(proxy=proxy_id) for proxy_id in ids),
            )
            for provider, ids in (chains or {}).items()
        },
    )


@pytest.fixture(autouse=True)
def _fresh_ledgers():
    reset_proxy_health()
    reset_built_chain_labels()
    yield
    reset_proxy_health()
    reset_built_chain_labels()


def test_a_catalogue_without_a_collision_keeps_every_label_it_had() -> None:
    """The equality half: no collision, no rename -- byte for byte today's rule."""

    proxies = {
        "px_manual": ProxyEndpoint(url="socks5h://bob:pw@203.0.113.7:1080"),
        "px_named": ProxyEndpoint(url="http://198.51.100.9:8080", label="Tokyo"),
        "px_feed": ProxyEndpoint(
            url="socks5h://192.0.2.10:1080",
            label="192.0.2.10:1080",
            source="feed",
        ),
        "px_plain": ProxyEndpoint(url="https://proxy.internal"),
    }
    store = _store(proxies, {"nvidia_nim": ["px_manual", "px_named"]})

    for proxy_id, endpoint in proxies.items():
        assert store.ledger_label(proxy_id) == (
            endpoint.label or mask_proxy_label(endpoint.url)
        )


def test_two_sessions_of_one_gateway_get_distinct_credential_digests() -> None:
    store = _store(
        {"px_a": ProxyEndpoint(url=GATEWAY_A), "px_b": ProxyEndpoint(url=GATEWAY_B)},
        {"opencode": ["px_a", "px_b"]},
    )

    assert store.ledger_label("px_a") == (
        f"gw.example:7777#{_hex('customer-alice-sessid-1:pw')}"
    )
    assert store.ledger_label("px_b") == (
        f"gw.example:7777#{_hex('customer-alice-sessid-2:pw')}"
    )
    for proxy_id in ("px_a", "px_b"):
        label = store.ledger_label(proxy_id)
        assert "alice" not in label and "pw" not in label and "@" not in label


def test_the_labels_do_not_depend_on_the_order_the_addresses_were_added() -> None:
    forward = _store(
        {"px_a": ProxyEndpoint(url=GATEWAY_A), "px_b": ProxyEndpoint(url=GATEWAY_B)},
        {"opencode": ["px_a", "px_b"]},
    )
    backward = _store(
        {"px_b": ProxyEndpoint(url=GATEWAY_B), "px_a": ProxyEndpoint(url=GATEWAY_A)},
        {"opencode": ["px_b", "px_a"]},
    )

    assert dict(forward.ledger_labels()) == dict(backward.ledger_labels())


def test_the_same_credentials_under_another_scheme_are_still_told_apart() -> None:
    store = _store(
        {
            "px_socks": ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080"),
            "px_http": ProxyEndpoint(url="http://u:p@203.0.113.7:1080"),
        },
        {"opencode": ["px_socks", "px_http"]},
    )

    labels = {store.ledger_label("px_socks"), store.ledger_label("px_http")}
    assert len(labels) == 2
    assert all(label.startswith("203.0.113.7:1080#") for label in labels)


def test_a_feed_label_that_is_its_own_address_counts_as_no_name() -> None:
    """A feed writes ``ip:port`` into ``label``; that names nothing an operator
    chose, so it collides like an unlabelled address would."""

    store = _store(
        {
            "px_feed": ProxyEndpoint(
                url="socks5h://203.0.113.7:1080", label="203.0.113.7:1080"
            ),
            "px_typed": ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080"),
        },
        {"opencode": ["px_feed", "px_typed"]},
    )

    assert store.ledger_label("px_feed") == f"203.0.113.7:1080#{_hex('')}"
    assert store.ledger_label("px_typed") == f"203.0.113.7:1080#{_hex('u:p')}"


def test_a_name_the_operator_gave_is_never_renamed_and_never_reused() -> None:
    store = _store(
        {
            "px_named": ProxyEndpoint(
                url="socks5h://198.51.100.1:9050", label="203.0.113.7:1080"
            ),
            "px_typed": ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080"),
        },
        {"opencode": ["px_named", "px_typed"]},
    )

    assert store.ledger_label("px_named") == "203.0.113.7:1080"
    assert store.ledger_label("px_typed") == f"203.0.113.7:1080#{_hex('u:p')}"


def test_an_offer_steps_around_a_chained_address_and_never_renames_it() -> None:
    """A feed pass must never rename an address a running chain dials."""

    chained = ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080")
    alone = _store({"px_chain": chained}, {"opencode": ["px_chain"]})
    with_offer = _store(
        {
            "px_chain": chained,
            "px_offer": ProxyEndpoint(
                url="socks5h://203.0.113.7:1080", label="203.0.113.7:1080"
            ),
        },
        {"opencode": ["px_chain"]},
    )

    assert alone.ledger_label("px_chain") == "203.0.113.7:1080"
    assert with_offer.ledger_label("px_chain") == "203.0.113.7:1080"
    assert with_offer.ledger_label("px_offer") == f"203.0.113.7:1080#{_hex('')}"


def test_the_label_a_fetch_charges_is_the_label_the_offer_is_stored_under() -> None:
    chained = ProxyEndpoint(url="socks5h://u:p@203.0.113.7:1080")
    before = _store({"px_chain": chained}, {"opencode": ["px_chain"]})
    url = "socks5h://203.0.113.7:1080"
    offered = ProxyEndpoint(url=url, label=mask_proxy_label(url), source="feed")

    predicted = before.offer_ledger_label("px_new", url)
    after = before.with_candidates([("px_new", offered)])

    assert predicted == after.ledger_label("px_new")
    assert predicted != after.ledger_label("px_chain")
    # An address with nothing to collide with keeps its plain ``ip:port``.
    assert before.offer_ledger_label("px_other", "http://192.0.2.1:3128") == (
        "192.0.2.1:3128"
    )


def test_a_bench_on_one_session_does_not_bench_the_other(monkeypatch) -> None:
    """The bug S1 fixes, through the frozen ledger's public API only."""

    from my_claude_code.providers.runtime.config import resolve_proxy_chain

    store = _store(
        {"px_a": ProxyEndpoint(url=GATEWAY_A), "px_b": ProxyEndpoint(url=GATEWAY_B)},
        {"opencode": ["px_a", "px_b"]},
    )
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.config.current_proxy_chains", lambda: store
    )

    _, plan = resolve_proxy_chain("opencode", "", Settings())
    assert plan is not None
    first, second = (leg.label for leg in plan.legs)
    assert first != second
    assert {first, second} == {store.ledger_label("px_a"), store.ledger_label("px_b")}

    PROXY_REACHABILITY.note_failure(first, "rig: refused")
    assert PROXY_REACHABILITY.is_unhealthy(first)
    assert not PROXY_REACHABILITY.is_unhealthy(second)


def test_a_provider_built_under_a_label_another_write_renamed_is_stale() -> None:
    """Adding a colliding address to a SECOND chain renames the first chain's
    address; the republish has to rebuild that first provider too."""

    first = _store({"px_a": ProxyEndpoint(url=GATEWAY_A)}, {"opencode": ["px_a"]})
    note_built_chain_labels("opencode", first.chain_ledger_labels("opencode"))
    assert providers_with_stale_labels(first) == frozenset()

    both = _store(
        {"px_a": ProxyEndpoint(url=GATEWAY_A), "px_b": ProxyEndpoint(url=GATEWAY_B)},
        {"opencode": ["px_a"], "nvidia_nim": ["px_b"]},
    )
    assert both.ledger_label("px_a") != first.ledger_label("px_a")
    assert providers_with_stale_labels(both) == frozenset({"opencode"})


def test_a_chain_naming_the_same_address_twice_reports_the_shared_label() -> None:
    store = _store(
        {"px_a": ProxyEndpoint(url=GATEWAY_A), "px_b": ProxyEndpoint(url=GATEWAY_B)},
        {"opencode": ["px_a", "px_b", "px_a"]},
    )

    assert store.duplicate_labels("opencode") == ((store.ledger_label("px_a"), (1, 3)),)


def test_direct_entries_never_count_as_a_shared_label() -> None:
    store = ProxyChains(
        proxies={"px_a": ProxyEndpoint(url=GATEWAY_A)},
        chains={
            "opencode": ProxyChain(
                enabled=True,
                entries=(
                    ProxyChainEntry(proxy="px_a"),
                    ProxyChainEntry(proxy=""),
                    ProxyChainEntry(proxy=""),
                ),
            )
        },
    )

    assert store.duplicate_labels("opencode") == ()


def test_a_stored_document_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """S1 adds nothing to the file: labels are derived, never written."""

    path = tmp_path / "proxy_chains.json"
    document = {
        "version": 1,
        "proxies": {
            "px_a": {
                "url": GATEWAY_A,
                "label": "",
                "added_at": "2026-10-01T00:00:00Z",
                "source": "manual",
                "source_count": 1,
            },
            "px_b": {
                "url": GATEWAY_B,
                "label": "",
                "added_at": "2026-10-01T00:00:00Z",
                "source": "manual",
                "source_count": 1,
            },
        },
        "chains": {
            "opencode": {
                "enabled": True,
                "policy": "failover",
                "entries": [
                    {"proxy": "px_a", "paused": False},
                    {"proxy": "px_b", "paused": False},
                ],
                "on": ["quota", "rate_limit", "timeout"],
                "scope": "provider",
                "max_switches": 2,
                "direct_fallback": True,
                "oauth_acknowledged": False,
            }
        },
        "candidates": [],
        "feeds": [],
    }
    raw = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    reset_proxy_chains_cache()

    store = load_proxy_chains(path)
    assert store.ledger_label("px_a") != store.ledger_label("px_b")
    save_proxy_chains(store, path)

    assert path.read_bytes() == raw
