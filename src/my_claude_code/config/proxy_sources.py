"""Proxy sources: ``~/.mcc/proxy_sources.json`` (7.89.0, PR-S3).

A **source** is something that *yields* proxy addresses -- ordinary
``socks5h://`` / ``http://`` URLs that go into the catalogue of
``proxy_chains.json`` and from there into chains, exactly like a typed or a
feed address. Nothing downstream knows a source exists: the chain, the checker,
the health ledgers and the request log see addresses.

This release builds one kind, ``local``: the listeners a scan of this computer
found (``application/proxy_sources.py``) -- Cloudflare WARP in proxy mode,
``ssh -D``, Tor, gluetun, wireproxy. The kinds the spec names next (``account``,
``gateway``, ``list``, ``runner``) are part of the vocabulary and of the
document's shape, so a later release adds a reader and a writer without
migrating anything; a source of a kind this release does not build is kept
verbatim and shown as such.

::

    {
      "version": 1,
      "sources": {
        "src_local": {"kind": "local", "name": "This computer", "enabled": true,
                      "added_at": "...Z", "scanned_at": "...Z",
                      "listeners": [
                        {"port": 9050, "protocol": "socks5", "auth": "none",
                         "answering": true, "proxy": "px_..."},
                        {"port": 1080, "protocol": "socks5", "auth": "userpass",
                         "answering": true, "proxy": "px_...", "secret": "sec_..."}]}
      },
      "secrets": {"sec_...": {"type": "userpass", "username": "...",
                              "password": "..."}}
    }

**Secrets live here and in the chain store's URL only** (the user's decision 6
of 2026-09-25: "option A"). Both files are written owner-only
(:mod:`~my_claude_code.config.owner_only`). Nothing here is ever rendered back:
:meth:`ProxySources.public_document` names a secret by
:func:`~my_claude_code.config.credentials.mask_key_label` of its username and
whether one is set, and the routes send only that.
"""

import json
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from loguru import logger

from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.owner_only import write_owner_only_json
from my_claude_code.config.paths import config_dir_path

PROXY_SOURCES_FILENAME = "proxy_sources.json"
DOCUMENT_VERSION = 1

KIND_LOCAL = "local"
#: Every kind the store knows the name of, in the spec's order (§5.1). Only
#: :data:`BUILT_KINDS` are read, written and offered by this release.
SOURCE_KINDS: tuple[str, ...] = (KIND_LOCAL, "account", "gateway", "list", "runner")
BUILT_KINDS: tuple[str, ...] = (KIND_LOCAL,)

PROTOCOL_SOCKS5 = "socks5"
PROTOCOL_HTTP = "http"
#: What a listener asked for: nothing, a username and password, or neither
#: method MCC can speak (a SOCKS5 server refusing both).
AUTH_NONE = "none"
AUTH_USERPASS = "userpass"
AUTH_UNSUPPORTED = "unsupported"
AUTH_KINDS: tuple[str, ...] = (AUTH_NONE, AUTH_USERPASS, AUTH_UNSUPPORTED)

SECRET_USERPASS = "userpass"
SECRET_TYPES: tuple[str, ...] = (SECRET_USERPASS,)

#: The one local source an install has: this computer.
LOCAL_SOURCE_ID = "src_local"
LOCAL_SOURCE_NAME = "This computer"
LOCAL_HOST = "127.0.0.1"


def proxy_sources_path() -> Path:
    """Beside ``proxy_chains.json``: configuration, not a log."""

    return config_dir_path() / PROXY_SOURCES_FILENAME


@dataclass(frozen=True, slots=True)
class SourceSecret:
    """A credential a source needs. Never rendered back to anybody."""

    type: str = SECRET_USERPASS
    username: str = ""
    password: str = ""

    @property
    def label(self) -> str:
        """The masked name a page may show: the username's ``first4…last4``."""

        return mask_key_label(self.username)

    def as_document(self) -> dict[str, Any]:
        return {"type": self.type, "username": self.username, "password": self.password}

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in SECRET_TYPES:
            return None
        return cls(
            type=kind,
            username=str(raw.get("username") or ""),
            password=str(raw.get("password") or ""),
        )


@dataclass(frozen=True, slots=True)
class LocalListener:
    """One port on this computer that answered the scan as a proxy, or did not.

    ``proxy`` is the catalogue id of the address it yields (``""`` when it
    yields none: not a proxy, a method MCC cannot speak, or not answering).
    """

    port: int
    protocol: str = ""
    auth: str = AUTH_NONE
    answering: bool = True
    proxy: str = ""
    secret: str = ""
    #: What answered when it was not a proxy MCC can use, in a few words.
    note: str = ""

    @property
    def scheme(self) -> str:
        return "socks5h" if self.protocol == PROTOCOL_SOCKS5 else PROTOCOL_HTTP

    def url(self, secret: SourceSecret | None = None) -> str:
        """The address this listener yields, with its credential when it has one."""

        userinfo = ""
        if secret is not None and secret.username:
            userinfo = (
                f"{quote(secret.username, safe='')}:{quote(secret.password, safe='')}@"
            )
        return f"{self.scheme}://{userinfo}{LOCAL_HOST}:{self.port}"

    @property
    def usable(self) -> bool:
        """Whether this listener yields an address a chain could dial."""

        return (
            self.answering
            and self.protocol in {PROTOCOL_SOCKS5, PROTOCOL_HTTP}
            and self.auth in {AUTH_NONE, AUTH_USERPASS}
        )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "port": self.port,
            "protocol": self.protocol,
            "auth": self.auth,
            "answering": self.answering,
            "proxy": self.proxy,
        }
        if self.secret:
            document["secret"] = self.secret
        if self.note:
            document["note"] = self.note
        return document

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        raw_port = raw.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int | str):
            return None
        try:
            port = int(raw_port)
        except ValueError:
            return None
        if not 0 < port < 65536:
            return None
        protocol = str(raw.get("protocol") or "").strip().lower()
        auth = str(raw.get("auth") or AUTH_NONE).strip().lower()
        return cls(
            port=port,
            protocol=protocol if protocol in {PROTOCOL_SOCKS5, PROTOCOL_HTTP} else "",
            auth=auth if auth in AUTH_KINDS else AUTH_NONE,
            answering=bool(raw.get("answering", True)),
            proxy=str(raw.get("proxy") or "").strip(),
            secret=str(raw.get("secret") or "").strip(),
            note=str(raw.get("note") or "").strip()[:200],
        )


@dataclass(frozen=True, slots=True)
class ProxySource:
    """One source. ``raw`` keeps a kind this release does not build, verbatim."""

    id: str
    kind: str
    name: str = ""
    enabled: bool = True
    added_at: str = ""
    scanned_at: str = ""
    listeners: tuple[LocalListener, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def built(self) -> bool:
        return self.kind in BUILT_KINDS

    def secret_ids(self) -> tuple[str, ...]:
        if not self.built:
            value = self.raw.get("secret")
            return (str(value),) if isinstance(value, str) and value else ()
        return tuple(listener.secret for listener in self.listeners if listener.secret)

    def listener(self, port: int) -> LocalListener | None:
        return next((item for item in self.listeners if item.port == port), None)

    def as_document(self) -> dict[str, Any]:
        if not self.built:
            return dict(self.raw)
        return {
            "kind": self.kind,
            "name": self.name,
            "enabled": self.enabled,
            "added_at": self.added_at,
            "scanned_at": self.scanned_at,
            "listeners": [listener.as_document() for listener in self.listeners],
        }

    @classmethod
    def from_document(cls, source_id: str, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            logger.warning(
                "PROXY SOURCES: '{}' is not an object; ignoring it", source_id
            )
            return None
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in SOURCE_KINDS:
            logger.warning(
                "PROXY SOURCES: '{}' has a kind MCC does not know; ignoring it",
                source_id,
            )
            return None
        listeners: list[LocalListener] = []
        raw_listeners = raw.get("listeners")
        if (
            kind in BUILT_KINDS
            and isinstance(raw_listeners, Sequence)
            and not isinstance(raw_listeners, str)
        ):
            seen: set[int] = set()
            for item in raw_listeners:
                listener = LocalListener.from_document(item)
                if listener is not None and listener.port not in seen:
                    seen.add(listener.port)
                    listeners.append(listener)
        return cls(
            id=source_id,
            kind=kind,
            name=str(raw.get("name") or "").strip(),
            enabled=raw.get("enabled") is not False,
            added_at=str(raw.get("added_at") or "").strip(),
            scanned_at=str(raw.get("scanned_at") or "").strip(),
            listeners=tuple(listeners),
            # A kind this release does not build is kept verbatim, so the
            # release that builds it reads what was written.
            raw={}
            if kind in BUILT_KINDS
            else {str(key): value for key, value in raw.items()},
        )


@dataclass(frozen=True, slots=True)
class ProxySources:
    """Every source and every secret this install holds."""

    sources: Mapping[str, ProxySource] = field(default_factory=dict)
    secrets: Mapping[str, SourceSecret] = field(default_factory=dict)
    #: Why the file could not be read, when this table is NOT what it holds.
    #: Never persisted; a save of a table derived from it is refused, so a
    #: file nobody could read is never replaced by one built from nothing.
    unreadable: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.sources and not self.secrets

    def source(self, source_id: str) -> ProxySource | None:
        return self.sources.get(source_id)

    def secret(self, secret_id: str) -> SourceSecret | None:
        return self.secrets.get(secret_id) if secret_id else None

    def with_source(self, source: ProxySource) -> ProxySources:
        sources = dict(self.sources)
        sources[source.id] = source
        return replace(self, sources=sources).without_orphan_secrets()

    def without_source(self, source_id: str) -> ProxySources:
        sources = {
            key: value for key, value in self.sources.items() if key != source_id
        }
        return replace(self, sources=sources).without_orphan_secrets()

    def with_secret(self, secret_id: str, secret: SourceSecret) -> ProxySources:
        stored = dict(self.secrets)
        stored[secret_id] = secret
        return replace(self, secrets=stored)

    def without_orphan_secrets(self) -> ProxySources:
        """Drop secrets no source names any more: a credential is not kept for nothing."""

        named = {
            secret_id
            for source in self.sources.values()
            for secret_id in source.secret_ids()
        }
        kept = {key: value for key, value in self.secrets.items() if key in named}
        if len(kept) == len(self.secrets):
            return self
        return replace(self, secrets=kept)

    def as_document(self) -> dict[str, Any]:
        return {
            "version": DOCUMENT_VERSION,
            "sources": {
                source_id: source.as_document()
                for source_id, source in self.sources.items()
            },
            "secrets": {
                secret_id: secret.as_document()
                for secret_id, secret in self.secrets.items()
            },
        }

    @classmethod
    def from_document(cls, document: object) -> Self:
        if not isinstance(document, Mapping):
            logger.warning("PROXY SOURCES: top-level JSON value is not an object")
            return cls()
        sources: dict[str, ProxySource] = {}
        raw_sources = document.get("sources")
        if isinstance(raw_sources, Mapping):
            for raw_id, raw in raw_sources.items():
                source_id = str(raw_id).strip()
                if not source_id:
                    continue
                source = ProxySource.from_document(source_id, raw)
                if source is not None:
                    sources[source_id] = source
        stored: dict[str, SourceSecret] = {}
        raw_secrets = document.get("secrets")
        if isinstance(raw_secrets, Mapping):
            for raw_id, raw in raw_secrets.items():
                secret = SourceSecret.from_document(raw)
                if secret is not None and str(raw_id).strip():
                    stored[str(raw_id).strip()] = secret
        return cls(sources=sources, secrets=stored)


EMPTY_PROXY_SOURCES = ProxySources()

#: Every write of ``proxy_sources.json``, from its re-read to its save.
PROXY_SOURCES_WRITE_LOCK = threading.RLock()


def mint_secret_id(existing: Mapping[str, SourceSecret]) -> str:
    while True:
        secret_id = f"sec_{secrets.token_hex(4)}"
        if secret_id not in existing:
            return secret_id


def load_proxy_sources(path: Path | None = None) -> ProxySources:
    """Read the store, never raising. A missing file is no sources at all."""

    resolved = path if path is not None else proxy_sources_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_PROXY_SOURCES
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "PROXY SOURCES: cannot read {}: {}", resolved.name, type(exc).__name__
        )
        return ProxySources(unreadable=f"could not be read ({type(exc).__name__})")
    if not raw.strip():
        return ProxySources(unreadable="is empty")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("PROXY SOURCES: {} is not valid JSON", resolved.name)
        return ProxySources(unreadable="is not valid JSON")
    if not isinstance(document, Mapping):
        return ProxySources(unreadable="is not a JSON object")
    return ProxySources.from_document(document)


# (signature, table): the file's own stat and the table read under it, so
# the Proxying page -- which asks on every repaint, and every 1.5 s while a
# fetch runs -- reads the file again only when it changed.
_CACHE: tuple[tuple[str, int, int] | None, ProxySources] = (None, EMPTY_PROXY_SOURCES)


def current_proxy_sources(path: Path | None = None) -> ProxySources:
    """The store for a reader, re-read only when the file changed."""

    global _CACHE
    resolved = path if path is not None else proxy_sources_path()
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        _CACHE = (None, EMPTY_PROXY_SOURCES)
        return EMPTY_PROXY_SOURCES
    except OSError:
        return load_proxy_sources(resolved)
    signature = (str(resolved), stat.st_mtime_ns, stat.st_size)
    if _CACHE[0] == signature:
        return _CACHE[1]
    table = load_proxy_sources(resolved)
    if not table.unreadable:
        _CACHE = (signature, table)
    return table


def reset_proxy_sources_cache() -> None:
    global _CACHE
    _CACHE = (None, EMPTY_PROXY_SOURCES)


class ProxySourcesUnreadableError(OSError):
    """A save refused because the table was derived from a failed read."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"Not saved: {path.name} {reason}. MCC does not overwrite a source "
            "file it cannot read. Fix or remove it, then try again."
        )


def save_proxy_sources(table: ProxySources, path: Path | None = None) -> None:
    """Write the store owner-only, atomically. Off the event loop."""

    resolved = path if path is not None else proxy_sources_path()
    if table.unreadable:
        raise ProxySourcesUnreadableError(resolved, table.unreadable)
    with PROXY_SOURCES_WRITE_LOCK:
        write_owner_only_json(resolved, table.as_document())
        reset_proxy_sources_cache()


__all__ = [
    "AUTH_KINDS",
    "AUTH_NONE",
    "AUTH_UNSUPPORTED",
    "AUTH_USERPASS",
    "BUILT_KINDS",
    "DOCUMENT_VERSION",
    "EMPTY_PROXY_SOURCES",
    "KIND_LOCAL",
    "LOCAL_HOST",
    "LOCAL_SOURCE_ID",
    "LOCAL_SOURCE_NAME",
    "PROTOCOL_HTTP",
    "PROTOCOL_SOCKS5",
    "PROXY_SOURCES_FILENAME",
    "PROXY_SOURCES_WRITE_LOCK",
    "SECRET_TYPES",
    "SECRET_USERPASS",
    "SOURCE_KINDS",
    "LocalListener",
    "ProxySource",
    "ProxySources",
    "ProxySourcesUnreadableError",
    "SourceSecret",
    "current_proxy_sources",
    "load_proxy_sources",
    "mint_secret_id",
    "proxy_sources_path",
    "reset_proxy_sources_cache",
    "save_proxy_sources",
]
