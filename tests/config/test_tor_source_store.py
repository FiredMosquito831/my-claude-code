"""A ``tor`` source in ``proxy_sources.json`` (7.90.0): shape, secrets, file.

The same owner-only writer and the same "never overwrite what could not be
read" rule as every source (7.89.0). What a Tor source adds: its ports, its
control port, and -- only when the user chose a control password -- a
``password`` secret. Tor's cookie is never stored at all.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from my_claude_code.application.proxy_sources import sources_document
from my_claude_code.config.proxy_chains import ProxyChains
from my_claude_code.config.proxy_sources import (
    SOURCE_KINDS,
    ProxySources,
    load_proxy_sources,
    save_proxy_sources,
)

PASSWORD = "control-pw-for-tor-5521"

DOCUMENT = {
    "version": 1,
    "sources": {
        "src_local": {
            "kind": "local",
            "name": "This computer",
            "enabled": True,
            "added_at": "2026-10-09T10:00:00Z",
            "scanned_at": "2026-10-09T10:00:00Z",
            "listeners": [
                {
                    "port": 40000,
                    "protocol": "socks5",
                    "auth": "none",
                    "answering": True,
                    "proxy": "px_cccc",
                }
            ],
        },
        "src_tor": {
            "kind": "tor",
            "name": "Tor",
            "enabled": True,
            "added_at": "2026-10-09T11:00:00Z",
            "control_port": 19260,
            "auth": "password",
            "socks_ports": [
                {"port": 19250, "proxy": "px_dddd"},
                {"port": 19251, "proxy": "px_eeee"},
            ],
            "secret": "sec_3333",
        },
        "src_tor_2": {
            "kind": "tor",
            "name": "Tor",
            "enabled": True,
            "added_at": "2026-10-09T11:05:00Z",
            "control_port": 19361,
            "auth": "cookie",
            "socks_ports": [{"port": 19360, "proxy": "px_ffff"}],
        },
    },
    "secrets": {"sec_3333": {"type": "password", "password": PASSWORD}},
}


def _raw(document: dict) -> bytes:
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def test_a_document_with_tor_sources_round_trips_byte_for_byte(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proxy_sources.json"
    path.write_bytes(_raw(DOCUMENT))

    table = load_proxy_sources(path)
    tor = table.source("src_tor")
    assert tor is not None and tor.tor is not None and tor.built
    assert tor.tor.ports == (19250, 19251)
    assert tor.tor.control_port == 19260
    assert tor.secret_ids() == ("sec_3333",)
    save_proxy_sources(table, path)

    assert path.read_bytes() == _raw(DOCUMENT)


def test_tor_is_not_one_of_the_kinds_the_sources_payload_lists() -> None:
    """The 7.89.0 ``kinds`` list is the base spec's five, unchanged, so an
    install with no tor source answers the Sources routes byte for byte.

    7.91.0 builds ``account``, ``gateway`` and ``list``, so those three now
    say so; ``tor`` is still not in the list and ``runner`` still unbuilt."""

    assert SOURCE_KINDS == ("local", "account", "gateway", "list", "runner")
    document = sources_document(ProxyChains(), ProxySources.from_document(DOCUMENT))
    assert document["kinds"] == [
        {"id": "local", "available": True},
        {"id": "account", "available": True},
        {"id": "gateway", "available": True},
        {"id": "list", "available": True},
        {"id": "runner", "available": False},
    ]


def test_a_tor_source_naming_no_usable_port_is_ignored(tmp_path: Path) -> None:
    broken = json.loads(json.dumps(DOCUMENT))
    broken["sources"]["src_tor_2"]["socks_ports"] = [{"port": "nope"}]
    broken["sources"]["src_tor"]["control_port"] = 70000

    table = ProxySources.from_document(broken)

    assert table.source("src_tor") is None
    assert table.source("src_tor_2") is None
    assert table.source("src_local") is not None


def test_a_socks_port_equal_to_the_control_port_is_dropped_on_read() -> None:
    odd = json.loads(json.dumps(DOCUMENT))
    odd["sources"]["src_tor"]["socks_ports"].append({"port": 19260, "proxy": "px_x"})

    table = ProxySources.from_document(odd)
    tor = table.source("src_tor")

    assert tor is not None and tor.tor is not None
    assert tor.tor.ports == (19250, 19251)


def test_the_password_is_dropped_with_the_source_that_named_it() -> None:
    table = ProxySources.from_document(DOCUMENT)

    without = table.without_source("src_tor")

    assert without.secrets == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_file_holding_a_tor_password_is_0600_on_posix(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        path = tmp_path / "proxy_sources.json"
        save_proxy_sources(ProxySources.from_document(DOCUMENT), path)
    finally:
        os.umask(old)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _acl(path: Path) -> list[str]:
    completed = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=True
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    lines[0] = lines[0][len(str(path)) :].strip()
    return [
        line for line in lines if ":" in line and not line.startswith("Successfully")
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_a_file_holding_a_tor_password_is_owner_only_on_windows(
    tmp_path: Path,
) -> None:
    """Same three principals as every source file: this user, SYSTEM and
    Administrators, full control, nothing inherited."""

    path = tmp_path / "proxy_sources.json"
    save_proxy_sources(ProxySources.from_document(DOCUMENT), path)

    entries = _acl(path)
    assert len(entries) == 3, entries
    assert all(entry.endswith(":(F)") for entry in entries), entries
    assert not any("(I)" in entry for entry in entries), entries
    principals = sorted(entry.rsplit(":", 1)[0].split("\\")[-1] for entry in entries)
    user = os.environ.get("USERNAME", "")
    assert principals == sorted(["Administrators", "SYSTEM", user]), entries
