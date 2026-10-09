"""PR-S3 (7.89.0): ``proxy_sources.json`` -- its shape, its secrets, its file.

The store holds a source's login, so the three things pinned here are the
security half of the feature: the file is owner-only (and so, since the same
release, is ``proxy_chains.json``, whose URLs may carry ``user:pass``), a
secret never leaves it except masked, and a file that cannot be read is never
overwritten.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from my_claude_code.application.proxy_sources import sources_document
from my_claude_code.config.proxy_chains import (
    ProxyChains,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_sources import (
    LocalListener,
    ProxySource,
    ProxySources,
    ProxySourcesUnreadableError,
    SourceSecret,
    load_proxy_sources,
    save_proxy_sources,
)

USERNAME = "tor-isolation-user-7"
PASSWORD = "hunter2-correct-horse"

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
                    "port": 1080,
                    "protocol": "socks5",
                    "auth": "userpass",
                    "answering": True,
                    "proxy": "px_aaaa",
                    "secret": "sec_1111",
                },
                {
                    "port": 9050,
                    "protocol": "socks5",
                    "auth": "none",
                    "answering": True,
                    "proxy": "px_bbbb",
                },
            ],
        },
        # A kind a later release builds: kept verbatim, never edited here.
        "src_nord": {
            "kind": "account",
            "name": "NordVPN",
            "enabled": True,
            "scheme": "socks5h",
            "port": 1080,
            "hosts": ["amsterdam.nl.socks.nordhold.net"],
            "secret": "sec_2222",
        },
    },
    "secrets": {
        "sec_1111": {"type": "userpass", "username": USERNAME, "password": PASSWORD},
        "sec_2222": {"type": "userpass", "username": "nord-svc", "password": "pw"},
    },
}


def _raw(document: dict) -> bytes:
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def test_a_document_round_trips_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "proxy_sources.json"
    path.write_bytes(_raw(DOCUMENT))

    table = load_proxy_sources(path)
    assert table.source("src_local") is not None
    nord = table.source("src_nord")
    assert nord is not None
    assert not nord.built
    save_proxy_sources(table, path)

    assert path.read_bytes() == _raw(DOCUMENT)


def test_the_public_document_never_carries_a_secret() -> None:
    table = ProxySources.from_document(DOCUMENT)

    text = json.dumps(sources_document(ProxyChains(), table))

    for secret in (USERNAME, PASSWORD, "nord-svc", '"pw"', "hunter2"):
        assert secret not in text
    local = sources_document(ProxyChains(), table)["sources"][0]
    first = local["listeners"][0]
    assert first["secret_set"] is True
    assert first["secret_label"] == "tor-…er-7"
    nord = sources_document(ProxyChains(), table)["sources"][1]
    assert nord == {
        "id": "src_nord",
        "kind": "account",
        "built": False,
        "name": "NordVPN",
        "enabled": True,
        "added_at": "",
        "scanned_at": "",
        "secret_set": True,
        "offers": 0,
    }


def test_a_secret_no_source_names_is_dropped() -> None:
    table = ProxySources.from_document(DOCUMENT)
    source = table.source("src_local")
    assert source is not None

    cleared = table.with_source(
        ProxySource(
            id=source.id,
            kind=source.kind,
            name=source.name,
            listeners=(LocalListener(port=9050, protocol="socks5"),),
        )
    )

    assert "sec_1111" not in cleared.secrets
    assert "sec_2222" in cleared.secrets


def test_a_file_that_cannot_be_read_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "proxy_sources.json"
    path.write_text("{ not json", encoding="utf-8")

    table = load_proxy_sources(path)
    assert table.unreadable
    with pytest.raises(ProxySourcesUnreadableError):
        save_proxy_sources(table.with_secret("sec_x", SourceSecret(username="u")), path)

    assert path.read_text(encoding="utf-8") == "{ not json"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.parametrize("which", ["sources", "chains"])
def test_both_secret_files_are_0600_on_posix(tmp_path: Path, which: str) -> None:
    old = os.umask(0o022)
    try:
        path = tmp_path / f"proxy_{which}.json"
        if which == "sources":
            save_proxy_sources(ProxySources.from_document(DOCUMENT), path)
        else:
            save_proxy_chains(ProxyChains(), path)
    finally:
        os.umask(old)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _acl(path: Path) -> list[str]:
    completed = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=True
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    # The first line carries the path before the first entry.
    lines[0] = lines[0][len(str(path)) :].strip()
    return [
        line for line in lines if ":" in line and not line.startswith("Successfully")
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
@pytest.mark.spawns_process
@pytest.mark.local_serial
@pytest.mark.parametrize("which", ["sources", "chains"])
def test_both_secret_files_are_owner_only_on_windows(
    tmp_path: Path, which: str
) -> None:
    """Read back with ``icacls``: this user, SYSTEM and Administrators, full
    control, nothing inherited -- whatever the folder grants (a pytest
    folder on another drive grants Users read)."""

    path = tmp_path / f"proxy_{which}.json"
    if which == "sources":
        save_proxy_sources(ProxySources.from_document(DOCUMENT), path)
    else:
        save_proxy_chains(ProxyChains(), path)

    entries = _acl(path)
    assert len(entries) == 3, entries
    assert all(entry.endswith(":(F)") for entry in entries), entries
    assert not any("(I)" in entry for entry in entries), entries
    principals = sorted(entry.rsplit(":", 1)[0].split("\\")[-1] for entry in entries)
    user = os.environ.get("USERNAME", "")
    assert principals == sorted(["Administrators", "SYSTEM", user]), entries


def test_a_chain_store_saved_owner_only_keeps_its_bytes(tmp_path: Path) -> None:
    """The owner-only writer writes exactly what the atomic writer wrote."""

    from my_claude_code.config.atomic_json import json_document_bytes

    path = tmp_path / "proxy_chains.json"
    save_proxy_chains(ProxyChains(), path)

    assert path.read_bytes() == json_document_bytes(ProxyChains().as_document())
    assert load_proxy_chains(path) == ProxyChains()
