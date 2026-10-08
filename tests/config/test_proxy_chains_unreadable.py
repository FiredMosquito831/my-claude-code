"""A chain file that cannot be read is not a file with no chains (7.78.8, C-5).

Until 7.78.8 a ``proxy_chains.json`` that was there but blank, not JSON or not
readable came back as "no chains", and every provider was built without its
chain -- from this computer's own address, Direct fallback off or not. Now:

* mid-run, the table read before it is kept, so every chain keeps routing;
* at a start that finds it broken, the masking record written beside it on
  every save names the providers whose chain has Direct fallback off, and
  those are refused while every other provider is built as before;
* the start-up read rides out a file held for a few hundred milliseconds;
* a document derived from the failed read is never saved over the file.
"""

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config import proxy_chains
from my_claude_code.config.proxy_chains import (
    MASKING_RECORD_FILENAME,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyChainsUnreadableError,
    ProxyEndpoint,
    current_proxy_chains,
    load_proxy_chains,
    masking_record_path,
    proxy_chains_problem,
    reset_proxy_chains_cache,
    save_proxy_chains,
    settle_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.base import MaskedRefusalPlan
from my_claude_code.providers.runtime.config import resolve_proxy_chain

MASKED = "nvidia_nim"
OPEN = "open_router"
PROXIES = {
    "px_one": ProxyEndpoint(url="http://198.51.100.9:8080"),
    "px_two": ProxyEndpoint(url="http://198.51.100.10:8080"),
}
LEGS = ("http://198.51.100.9:8080", "http://198.51.100.10:8080")
BROKEN = {
    "blank": b"",
    "whitespace": b"  \r\n ",
    "not_json": b"{ this is not json",
    "not_an_object": b"[]",
    "not_utf8": b"\xff\xfe\x00{",
}


@pytest.fixture
def path(tmp_path, monkeypatch) -> Iterator[Path]:
    chains_path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: chains_path)
    reset_proxy_chains_cache()
    yield chains_path
    reset_proxy_chains_cache()


def _table(*, masked_fallback: bool = False) -> ProxyChains:
    """Two chains: one with Direct fallback off (masked), one with it on."""

    entries = (ProxyChainEntry(proxy="px_one"), ProxyChainEntry(proxy="px_two"))
    return ProxyChains(
        proxies=PROXIES,
        chains={
            MASKED: ProxyChain(
                enabled=True, entries=entries, direct_fallback=masked_fallback
            ),
            OPEN: ProxyChain(enabled=True, entries=entries, direct_fallback=True),
        },
    )


def _legs(provider_id: str) -> tuple[str, ...] | None:
    _proxy, plan = resolve_proxy_chain(provider_id, "", Settings())
    if plan is None or isinstance(plan, MaskedRefusalPlan):
        return None
    return tuple(leg.url for leg in plan.legs)


def _record(path: Path) -> dict[str, Any]:
    return json.loads(masking_record_path(path).read_text(encoding="utf-8"))


# ----------------------------------------------------------- reading it


@pytest.mark.parametrize("content", sorted(BROKEN))
def test_a_broken_file_is_unreadable_not_empty(path: Path, content: str) -> None:
    path.write_bytes(BROKEN[content])

    table = current_proxy_chains()
    loaded = load_proxy_chains()

    assert table.unreadable
    assert table.chains == {}
    # What a writer is handed: still no chains (nothing to derive), but marked.
    assert loaded.chains == {}
    assert loaded.unreadable


def test_a_missing_file_is_still_no_chains(path: Path) -> None:
    table = current_proxy_chains()

    assert table.unreadable == ""
    assert table.is_empty
    assert proxy_chains_problem() == ""


def test_mid_run_the_table_read_before_is_kept(path: Path) -> None:
    save_proxy_chains(_table())
    assert _legs(MASKED) == LEGS

    path.write_text("{ this is not json", encoding="utf-8")

    assert current_proxy_chains().unreadable.startswith("cannot be parsed")
    # Every chain keeps routing exactly as it did, masked and unmasked alike.
    assert _legs(MASKED) == LEGS
    assert _legs(OPEN) == LEGS
    assert "proxy_chains.json" in proxy_chains_problem()


def test_readable_again_is_read_again(path: Path) -> None:
    save_proxy_chains(_table())
    current_proxy_chains()
    path.write_text("[]", encoding="utf-8")
    assert current_proxy_chains().unreadable

    save_proxy_chains(_table(masked_fallback=True))  # what a fix by hand would be

    table = current_proxy_chains()
    chain = table.chain(MASKED)
    assert table.unreadable == ""
    assert chain is not None
    assert chain.direct_fallback is True


def test_at_a_broken_start_the_masking_record_refuses_the_masked_providers(
    path: Path,
) -> None:
    save_proxy_chains(_table())
    assert _record(path)["masked"] == [MASKED]
    assert _record(path)["direct_only_when_unhealthy"] == [OPEN]
    path.write_text("{ this is not json", encoding="utf-8")
    reset_proxy_chains_cache()  # a new process: nothing read before

    _proxy, plan = resolve_proxy_chain(MASKED, "", Settings(), name="NVIDIA NIM")
    _proxy, open_plan = resolve_proxy_chain(OPEN, "", Settings(), name="OpenRouter")

    assert isinstance(plan, MaskedRefusalPlan)
    assert plan.reason.startswith("Not sent: proxy_chains.json cannot be parsed")
    assert "Direct fallback" in plan.reason
    assert "Proxying page" in plan.reason
    # 7.79.2: a chain with Direct fallback ON may go direct only once every
    # proxy is unhealthy, which nothing can judge from an unreadable file.
    assert isinstance(open_plan, MaskedRefusalPlan)
    assert open_plan.direct_fallback is True
    assert open_plan.reason.startswith("Not sent: proxy_chains.json cannot be parsed")
    assert "with Direct fallback on" in open_plan.reason
    assert "only once every proxy in the chain is unhealthy" in open_plan.reason
    # A provider the record does not name is built as before 7.78.8: no chain.
    assert resolve_proxy_chain("deepseek", "", Settings()) == ("", None)
    problem = proxy_chains_problem()
    assert MASKED in problem and OPEN in problem


def test_a_record_written_before_7_79_2_still_refuses_only_the_masked(
    path: Path,
) -> None:
    """A 7.78.8 record has no second list: those chains are built as it built them."""

    save_proxy_chains(_table())
    masking_record_path(path).write_text(
        json.dumps({"version": 1, "about": "7.78.8", "masked": [MASKED]}),
        encoding="utf-8",
    )
    path.write_text("{ this is not json", encoding="utf-8")
    reset_proxy_chains_cache()

    _proxy, plan = resolve_proxy_chain(MASKED, "", Settings())

    assert isinstance(plan, MaskedRefusalPlan)
    assert resolve_proxy_chain(OPEN, "", Settings()) == ("", None)


def test_at_a_start_that_cannot_open_the_file_the_record_decides_too(
    path: Path, monkeypatch
) -> None:
    save_proxy_chains(_table())
    reset_proxy_chains_cache()
    real = Path.read_text

    def held(target: Path, *args: Any, **kwargs: Any) -> str:
        if target == path:
            raise PermissionError(13, "held by another process", str(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", held)

    _proxy, plan = resolve_proxy_chain(MASKED, "", Settings())

    assert isinstance(plan, MaskedRefusalPlan)
    assert "could not be read" in plan.reason


def test_a_static_proxy_still_carries_a_masked_provider_at_a_broken_start(
    path: Path,
) -> None:
    save_proxy_chains(_table())
    path.write_text("", encoding="utf-8")
    reset_proxy_chains_cache()

    assert resolve_proxy_chain(MASKED, "http://203.0.113.50:3128", Settings()) == (
        "http://203.0.113.50:3128",
        None,
    )


# ------------------------------------------------------------ never rewritten


def test_a_document_derived_from_a_failed_read_is_never_saved(path: Path) -> None:
    path.write_bytes(BROKEN["not_json"])
    before = path.read_bytes()

    derived = load_proxy_chains().with_chain(
        MASKED, ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=""),))
    )
    with pytest.raises(ProxyChainsUnreadableError) as refused:
        save_proxy_chains(derived)

    assert path.read_bytes() == before
    assert isinstance(refused.value, OSError)
    assert "Not saved" in str(refused.value)
    assert "does not overwrite" in str(refused.value)


# ------------------------------------------------------------ the record


def test_no_record_is_written_while_no_chain_is_switched_on(path: Path) -> None:
    table = _table(masked_fallback=True)
    save_proxy_chains(
        ProxyChains(
            proxies=table.proxies,
            chains={
                provider_id: replace(chain, enabled=False)
                for provider_id, chain in table.chains.items()
            },
        )
    )

    assert not masking_record_path(path).exists()
    assert masking_record_path(path).name == MASKING_RECORD_FILENAME


def test_chains_with_direct_fallback_on_are_recorded_beside_the_masked(
    path: Path,
) -> None:
    """7.79.2: their own list, so a 7.78.8 reader of ``masked`` is unchanged."""

    save_proxy_chains(_table(masked_fallback=True))

    record = _record(path)
    assert record["masked"] == []
    assert record["direct_only_when_unhealthy"] == sorted([MASKED, OPEN])


def test_a_chain_of_only_direct_entries_is_not_recorded(path: Path) -> None:
    """The operator wrote Direct: it goes out from here because they said so."""

    save_proxy_chains(
        ProxyChains(
            proxies=PROXIES,
            chains={
                OPEN: ProxyChain(
                    enabled=True,
                    entries=(ProxyChainEntry(proxy=""),),
                    direct_fallback=True,
                )
            },
        )
    )

    assert not masking_record_path(path).exists()


def test_the_record_follows_the_chains_and_is_not_rewritten_when_unchanged(
    path: Path,
) -> None:
    save_proxy_chains(_table())
    record = masking_record_path(path)
    first = record.read_bytes()
    stamp = record.stat().st_mtime_ns

    save_proxy_chains(_table())  # same list
    assert record.read_bytes() == first
    assert record.stat().st_mtime_ns == stamp

    save_proxy_chains(_table(masked_fallback=True))
    assert _record(path)["masked"] == []


def test_the_record_holds_provider_ids_and_nothing_secret(path: Path) -> None:
    secret = ProxyChains(
        proxies={
            "px_one": ProxyEndpoint(url="socks5h://alice:hunter2@203.0.113.7:1080")
        },
        chains={
            MASKED: ProxyChain(
                enabled=True,
                entries=(ProxyChainEntry(proxy="px_one"),),
                direct_fallback=False,
            )
        },
    )
    save_proxy_chains(secret)

    text = masking_record_path(path).read_text(encoding="utf-8")
    assert "hunter2" not in text and "203.0.113.7" not in text
    assert json.loads(text)["masked"] == [MASKED]


# ------------------------------------------------------------ start-up


def test_settle_rides_out_a_file_held_at_boot(path: Path, monkeypatch, caplog) -> None:
    save_proxy_chains(_table())
    reset_proxy_chains_cache()
    real = Path.read_text
    calls = {"n": 0}

    def held_twice(target: Path, *args: Any, **kwargs: Any) -> str:
        if target == path and calls["n"] < 2:
            calls["n"] += 1
            raise PermissionError(13, "scanner", str(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", held_twice)

    with caplog.at_level(logging.ERROR):
        table = settle_proxy_chains()

    assert table.unreadable == ""
    assert table.chain(MASKED) is not None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_settle_gives_up_within_its_bound_and_says_so(path: Path, caplog) -> None:
    save_proxy_chains(_table())
    path.write_text("{ this is not json", encoding="utf-8")
    reset_proxy_chains_cache()

    started = time.monotonic()
    with caplog.at_level(logging.ERROR):
        table = settle_proxy_chains(within=0.2)
    elapsed = time.monotonic() - started

    assert table.unreadable
    assert table.masked_provider_ids() == (MASKED,)
    assert elapsed < 2.0
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "proxy_chains.json" in message
        and MASKED in message
        and "does not rewrite the file" in message
        for message in errors
    ), errors


def test_settle_writes_the_record_for_an_install_that_never_saved_since(
    path: Path,
) -> None:
    """An upgrade: chains on disk from before 7.78.8, no record yet."""

    path.write_text(json.dumps(_table().as_document()), encoding="utf-8")
    assert not masking_record_path(path).exists()

    settle_proxy_chains()

    assert _record(path)["masked"] == [MASKED]


def test_a_healthy_save_writes_the_chain_file_exactly_as_before(path: Path) -> None:
    """The chain file's bytes are ``as_document()`` and nothing else."""

    from my_claude_code.config.atomic_json import json_document_bytes

    table = _table()
    save_proxy_chains(table)

    assert path.read_bytes() == json_document_bytes(table.as_document())
    assert load_proxy_chains() == table
