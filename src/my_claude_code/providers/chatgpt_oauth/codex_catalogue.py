"""Read ChatGPT/Codex model catalogues -- rungs S1, S2a, and S2b for this provider.

Three vendor tiers feed existence and Option A capability metadata:

1. **S1 (``GATEWAY``):** Live authenticated ``GET {base_url}/codex/models?client_version=...``
   cached in memory and under ``config_dir_path()/cache/codex-models-live.json``
   with ``ETag`` revalidation and a 300-second TTL, strictly read-only on stored
   tokens (zero refresh, zero rotation, zero credential benching).
2. **S2a (``VENDOR_CLIENT``):** Codex CLI's own local response cache at
   ``$CODEX_HOME/models_cache.json`` (instant JSON parse, no 300 MB binary scan).
3. **S2b (``VENDOR_CLIENT``):** Installed Codex CLI or Codex Desktop bundled
   catalogue (``codex.exe`` / ``codex`` / ``models.json``), read **only** when
   both S1 and S2a decline, so retired or dropped models in a stale binary
   cannot ghost back in.

Option A capability extraction reads ``max_context_window`` / ``context_window``,
``input_modalities``, ``supported_reasoning_levels`` (clamping ``"ultra"`` to
``"max"``), ``description``, and raw ``published_row`` (preserving PR 4
``service_tiers`` / ``additional_speed_tiers`` / ``default_service_tier``),
while leaving pricing and ``max_output_tokens`` ``None`` for ``models.dev``
gap-filling.

Two listing states keep a model out of the listing: ``visibility: hide`` and
a passed ``upgrade.retirement_at``. Both are *listing* facts -- see
:class:`ListingVeto` for which of them a logged success may overturn, and for
why neither one ever touches a route.
"""

import hashlib
import json
import mmap
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
)
from my_claude_code.config.paths import config_dir_path

CODEX_CLIENT_VERSION_FLOOR = "0.172.0"
CODEX_MODELS_LIVE_TTL_SECONDS = 300.0
CODEX_LIVE_CACHE_FILENAME = "codex-models-live.json"

#: The document's opening bytes as Codex 0.151.0 stores them (pretty-printed,
#: CRLF) and as a compact build would. Both are searched; whichever appears
#: first wins.
CATALOGUE_MARKERS: tuple[bytes, ...] = (
    b'{\r\n  "models": [',
    b'{\n  "models": [',
    b'{"models":[',
)

#: How much of the binary past the marker is decoded before the JSON parser is
#: asked for the object. The 0.151.0 document is 425,306 bytes; this is an
#: order of magnitude of headroom and still a bounded read.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: The two ``visibility`` values Codex 0.151.0 actually publishes. ``list``
#: means the vendor offers the model in its own picker; ``hide`` means it does
#: not -- unreleased internals, and models on their way out. ``hide`` is a
#: filter on *listing* and on nothing else: a hidden model stays fully routable
#: when an operator names it, exactly like this project's own visibility globs
#: (the hide-only contract).
VISIBILITY_LISTED = "list"
VISIBILITY_HIDDEN = "hide"


@dataclass(frozen=True, slots=True)
class CodexCatalogueEntry:
    """One model as Codex's own bundled catalogue states it -- no capabilities."""

    slug: str
    #: Verbatim ``visibility``; ``""`` when the entry published none.
    visibility: str = ""
    #: Verbatim ``upgrade.retirement_at``; ``None`` when none was published,
    #: which is not the same as "not retiring".
    retirement_at: str | None = None
    #: Verbatim ``upgrade.model``.
    replacement_model_id: str | None = None
    #: ``available_in_plans``; ``None`` when the entry published no list at
    #: all, so an unknown plan can never be read as an excluded plan.
    available_in_plans: frozenset[str] | None = None
    #: The entry exactly as Codex's catalogue publishes it (7.86.0) -- its
    #: ``context_window``, ``max_context_window`` and effort vocabulary
    #: included -- for the Models page's "Everything known" view only. Never
    #: read by this rung or the ladder (see the module docstring); outside
    #: equality, so an entry is the same entry it always was.
    raw: Mapping[str, Any] | None = field(
        default=None, compare=False, hash=False, repr=False
    )

    @property
    def listed(self) -> bool:
        return self.visibility == VISIBILITY_LISTED

    @property
    def hidden(self) -> bool:
        """Whether the vendor explicitly marked this entry ``hide``.

        Not the negation of :attr:`listed`: an entry that published no
        ``visibility`` at all, or one carrying a value this build has never
        seen, is *unknown*, and unknown is never read as hidden.
        """
        return self.visibility == VISIBILITY_HIDDEN

    def retired_at(self, now: datetime) -> bool:
        """Whether the vendor's own published retirement instant has passed."""
        moment = _parse_instant(self.retirement_at)
        return moment is not None and moment <= now

    def available_to_plan(self, plan_type: str) -> bool:
        """Whether this plan may use the model, when both halves are known.

        Unknown is never excluded: an entry that published no plan list, or a
        credential whose plan could not be read locally, keeps the model.
        """
        if not plan_type or self.available_in_plans is None:
            return True
        return plan_type in self.available_in_plans


@dataclass(frozen=True, slots=True)
class CodexCatalogue:
    """Codex's bundled catalogue, plus which install it was read from."""

    version: str
    source_path: str
    entries: tuple[CodexCatalogueEntry, ...]

    @property
    def label(self) -> str:
        return f"Codex CLI {self.version}" if self.version else "Codex CLI"


def _parse_instant(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------------ locating


def codex_home_dir() -> Path:
    """Return ``$CODEX_HOME`` when set, else ``~/.codex``."""
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def _npm_package_roots() -> list[Path]:
    """Directories that may hold a global ``node_modules`` tree.

    No subprocess: ``npm prefix -g`` would be authoritative and would also be
    an execution, and this module's whole contract is that it never runs
    anything.
    """
    roots: list[Path] = []
    prefix = os.environ.get("NPM_CONFIG_PREFIX", "").strip()
    if prefix:
        roots.extend((Path(prefix) / "node_modules", Path(prefix) / "lib"))
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules")
    home = Path.home()
    roots.extend(
        (
            home / "AppData" / "Roaming" / "npm" / "node_modules",
            home / ".npm-global" / "lib" / "node_modules",
            home / ".local" / "lib" / "node_modules",
            Path("/usr/local/lib/node_modules"),
            Path("/usr/lib/node_modules"),
            Path("/opt/homebrew/lib/node_modules"),
        )
    )
    return roots


def _codex_package_dirs() -> list[Path]:
    """Every ``@openai/codex`` package directory this machine appears to have."""
    found: list[Path] = []
    for root in _npm_package_roots():
        candidate = root / "@openai" / "codex"
        if candidate.is_dir():
            found.append(candidate)
    shim = shutil.which("codex")
    if shim:
        # ``codex`` on PATH is usually a shim next to ``node_modules``; walk up
        # from it rather than guessing a prefix.
        for parent in Path(shim).resolve().parents:
            candidate = parent / "node_modules" / "@openai" / "codex"
            if candidate.is_dir():
                found.append(candidate)
                break
    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def _package_version(package_dir: Path) -> str:
    for name in ("codex-package.json", "package.json"):
        try:
            payload = json.loads((package_dir / name).read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        version = payload.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return ""


def _catalogue_sources(package_dir: Path) -> list[Path]:
    """Files inside one Codex package that may carry the catalogue.

    A plain ``models.json`` is preferred where a build ships one: reading it
    costs a few hundred kilobytes instead of scanning a 300 MB executable.
    """
    sources = [
        path
        for path in (
            package_dir / "models.json",
            package_dir / "bin" / "models.json",
        )
        if path.is_file()
    ]
    binaries = sorted(
        path
        for pattern in ("codex-*/vendor/*/bin/codex", "codex-*/vendor/*/bin/codex.exe")
        for path in (package_dir / "node_modules" / "@openai").glob(pattern)
        if path.is_file()
    )
    sources.extend(binaries)
    sources.extend(
        path
        for path in (package_dir / "bin" / "codex.exe", package_dir / "bin" / "codex")
        if path.is_file()
    )
    return sources


def _desktop_binary_version(binary_path: Path) -> str:
    for parent in (binary_path.parent.parent, binary_path.parent):
        found = _package_version(parent)
        if found:
            return found
    return ""


def _codex_desktop_sources() -> list[tuple[Path, str]]:
    """Read-only Codex Desktop and user-local binary candidates (no execution)."""
    candidates: list[Path] = []
    localappdata = os.environ.get("LOCALAPPDATA", "").strip()
    if localappdata:
        base = Path(localappdata)
        for root, pattern in (
            (base / "OpenAI" / "Codex" / "bin", "*/codex.exe"),
            (
                base / "Packages",
                "OpenAI.Codex_*/LocalCache/Local/OpenAI/Codex/bin/*/codex.exe",
            ),
        ):
            try:
                candidates.extend(sorted(root.glob(pattern)))
            except OSError:
                continue
    candidates.extend(
        (
            Path("/Applications/Codex.app/Contents/Resources/models.json"),
            Path("/Applications/Codex.app/Contents/Resources/codex"),
            Path("/Applications/Codex.app/Contents/MacOS/codex"),
            Path.home() / ".local" / "bin" / "codex",
        )
    )
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            is_file = path.is_file()
        except OSError:
            continue
        if is_file:
            found.append((path, _desktop_binary_version(path)))
    return found


def resolve_codex_client_version() -> str:
    """Resolve ``client_version`` offline-first with zero external registry calls."""
    codex_home = codex_home_dir()
    for filename, key in (
        ("version.json", "latest_version"),
        ("models_cache.json", "client_version"),
    ):
        try:
            doc = json.loads((codex_home / filename).read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if isinstance(doc, Mapping):
            raw = doc.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    for package_dir in _codex_package_dirs():
        ver = _package_version(package_dir)
        if ver:
            return ver
    return CODEX_CLIENT_VERSION_FLOOR


# ----------------------------------------------------------------- extracting


def embedded_json_document(path: Path) -> dict[str, Any] | None:
    """Return the first JSON object at a catalogue marker inside ``path``.

    Memory-mapped rather than read: the 0.151.0 binary is 314 MB and the
    document sits near the end of it. Parsed with ``raw_decode`` from the
    marker rather than by counting braces, so a ``{`` inside a description
    string cannot end the object early.
    """
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size == 0:
                return None
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                start = -1
                for marker in CATALOGUE_MARKERS:
                    found = data.find(marker)
                    if found != -1 and (start == -1 or found < start):
                        start = found
                if start == -1:
                    return None
                chunk = bytes(data[start : start + MAX_DOCUMENT_BYTES])
    except (OSError, ValueError) as error:
        logger.debug("Codex catalogue read failed for {}: {}", path, error)
        return None
    text = chunk.decode("utf-8", errors="replace")
    try:
        document, _ = json.JSONDecoder().raw_decode(text)
    except ValueError as error:
        logger.debug("Codex catalogue at {} did not parse: {}", path, error)
        return None
    return document if isinstance(document, dict) else None


def _entry_from_payload(payload: Mapping[str, Any]) -> CodexCatalogueEntry | None:
    slug = payload.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        return None
    visibility = payload.get("visibility")
    upgrade = payload.get("upgrade")
    upgrade_map: Mapping[str, Any] = upgrade if isinstance(upgrade, Mapping) else {}
    retirement = upgrade_map.get("retirement_at")
    replacement = upgrade_map.get("model")
    plans = payload.get("available_in_plans")
    return CodexCatalogueEntry(
        slug=slug.strip(),
        visibility=visibility.strip() if isinstance(visibility, str) else "",
        retirement_at=(
            retirement.strip()
            if isinstance(retirement, str) and retirement.strip()
            else None
        ),
        replacement_model_id=(
            replacement.strip()
            if isinstance(replacement, str) and replacement.strip()
            else None
        ),
        available_in_plans=(
            frozenset(item for item in plans if isinstance(item, str))
            if isinstance(plans, list)
            else None
        ),
        raw=payload,
    )


def parse_codex_catalogue(
    document: Mapping[str, Any], *, version: str, source_path: str
) -> CodexCatalogue | None:
    """Turn Codex's ``{"models": [...]}`` document into existence facts.

    A document with no parseable entry declines outright rather than
    publishing a partial catalogue: half a vendor list is indistinguishable
    from a vendor that retired half its models.
    """
    models = document.get("models")
    if not isinstance(models, list):
        return None
    entries = tuple(
        entry
        for entry in (
            _entry_from_payload(item) for item in models if isinstance(item, Mapping)
        )
        if entry is not None
    )
    if not entries:
        return None
    return CodexCatalogue(version=version, source_path=source_path, entries=entries)


# --------------------------------------------------------------------- cache

#: Keyed by the file's identity, not just its path: a Codex upgrade replaces
#: the binary in place, and the 300 MB scan must run again when it does. The
#: scan itself happens inside discovery, never on the request path.
_CACHE: dict[tuple[str, int, int], CodexCatalogue | None] = {}


def clear_codex_catalogue_cache() -> None:
    """Forget every memoised scan (tests, and after a Codex upgrade in place)."""
    _CACHE.clear()


def load_codex_models_cache(*, cache_path: Path | None = None) -> CodexCatalogue | None:
    """Rung S2a -- read ``$CODEX_HOME/models_cache.json`` if present and valid."""
    path = cache_path or (codex_home_dir() / "models_cache.json")
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key in _CACHE:
        return _CACHE[key]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        _CACHE[key] = None
        return None
    if not isinstance(doc, Mapping):
        _CACHE[key] = None
        return None
    raw_version = doc.get("client_version")
    version = raw_version.strip() if isinstance(raw_version, str) else ""
    catalogue = parse_codex_catalogue(doc, version=version, source_path=str(path))
    _CACHE[key] = catalogue
    if catalogue is not None:
        logger.info(
            "Codex models cache read: version={} models={} source={}",
            catalogue.version or "unknown",
            len(catalogue.entries),
            path.name,
        )
    return catalogue


def load_codex_catalogue(
    *, binary_path: Path | None = None, version: str = ""
) -> CodexCatalogue | None:
    """Locate the installed Codex CLI/Desktop and return its bundled catalogue.

    ``None`` when Codex is not installed, when its packaging changed enough
    that the document cannot be found, or when what was found did not parse.
    All three are declines: this rung going quiet must cost its ids and
    nothing else.
    """
    candidates: list[tuple[Path, str]] = []
    if binary_path is not None:
        candidates.append((binary_path, version))
    else:
        for package_dir in _codex_package_dirs():
            package_version = _package_version(package_dir)
            candidates.extend(
                (source, package_version) for source in _catalogue_sources(package_dir)
            )
        candidates.extend(_codex_desktop_sources())
    for path, found_version in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key in _CACHE:
            cached = _CACHE[key]
            if cached is not None:
                return cached
            continue
        document: dict[str, Any] | None = None
        if path.suffix.lower() == ".json":
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    document = loaded
            except OSError, ValueError:
                document = None
        if document is None:
            document = embedded_json_document(path)
        catalogue = (
            None
            if document is None
            else parse_codex_catalogue(
                document, version=found_version, source_path=str(path)
            )
        )
        _CACHE[key] = catalogue
        if catalogue is not None:
            logger.info(
                "Codex catalogue read: version={} models={} source={}",
                catalogue.version or "unknown",
                len(catalogue.entries),
                path.name,
            )
            return catalogue
        logger.info(
            "Codex catalogue not readable from {} -- this rung declines", path.name
        )
    return None


def load_offline_codex_catalogue() -> CodexCatalogue | None:
    """Select S2a ``models_cache.json`` first; fall back to S2b only if absent."""
    return load_codex_models_cache() or load_codex_catalogue()


# ------------------------------------------------------------ S1 live cache


@dataclass(frozen=True, slots=True)
class CodexLiveSnapshot:
    """One cached S1 ``/codex/models`` response for a single account."""

    base_url: str
    account_key: str
    client_version: str
    fetched_at_mono: float
    fetched_at_epoch: float
    etag: str | None
    catalogue: CodexCatalogue
    raw_document: Mapping[str, Any] | None = field(
        default=None, compare=False, hash=False, repr=False
    )


_LIVE_CACHE_LOCK = threading.Lock()
_LIVE_CACHE: dict[tuple[str, str, str], CodexLiveSnapshot] = {}
_DISK_HYDRATED_STAMP: tuple[str, int, int] | None = None


def validate_codex_models_document(payload: object) -> dict[str, Any] | None:
    """Accept only a dict carrying a non-empty list of model objects with slugs."""
    if not isinstance(payload, Mapping):
        return None
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        return None
    has_slug = False
    for item in models:
        if not isinstance(item, Mapping):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and slug.strip():
            has_slug = True
            break
    if not has_slug:
        return None
    return {str(k): v for k, v in payload.items()}


def codex_live_account_key(account_id: str, access_token: str) -> str:
    """Stable non-secret cache key segment for an account."""
    acct = account_id.strip()
    if acct:
        return acct
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16]
    return f"anon:{digest}"


def is_live_snapshot_fresh(
    snapshot: CodexLiveSnapshot, *, now_mono: float | None = None
) -> bool:
    """Return True while ``snapshot`` is within ``CODEX_MODELS_LIVE_TTL_SECONDS``."""
    now = time.monotonic() if now_mono is None else now_mono
    return (now - snapshot.fetched_at_mono) < CODEX_MODELS_LIVE_TTL_SECONDS


def _live_cache_disk_path() -> Path:
    return config_dir_path() / "cache" / CODEX_LIVE_CACHE_FILENAME


def _hydrate_live_cache_locked() -> None:
    global _DISK_HYDRATED_STAMP
    disk_path = _live_cache_disk_path()
    try:
        stat = disk_path.stat()
    except OSError:
        return
    stamp = (str(disk_path), stat.st_size, stat.st_mtime_ns)
    if stamp == _DISK_HYDRATED_STAMP:
        return
    _DISK_HYDRATED_STAMP = stamp
    try:
        payload = json.loads(disk_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        return
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        return
    now_epoch = time.time()
    now_mono = time.monotonic()
    for item in entries.values():
        if not isinstance(item, Mapping):
            continue
        base_url = item.get("base_url")
        account_key = item.get("account_key")
        client_version = item.get("client_version")
        fetched_at_epoch = item.get("fetched_at_epoch")
        etag = item.get("etag")
        raw_doc = validate_codex_models_document(item.get("document"))
        if (
            not isinstance(base_url, str)
            or not isinstance(account_key, str)
            or not isinstance(client_version, str)
            or isinstance(fetched_at_epoch, bool)
            or not isinstance(fetched_at_epoch, int | float)
            or float(fetched_at_epoch) > now_epoch + 60.0
            or raw_doc is None
        ):
            continue
        norm_base = base_url.rstrip("/")
        key = (norm_base, account_key, client_version)
        if key in _LIVE_CACHE:
            continue
        catalogue = parse_codex_catalogue(
            raw_doc,
            version=client_version,
            source_path=f"{norm_base}/codex/models",
        )
        if catalogue is None:
            continue
        age = max(0.0, now_epoch - float(fetched_at_epoch))
        _LIVE_CACHE[key] = CodexLiveSnapshot(
            base_url=norm_base,
            account_key=account_key,
            client_version=client_version,
            fetched_at_mono=now_mono - age,
            fetched_at_epoch=float(fetched_at_epoch),
            etag=etag.strip() if isinstance(etag, str) and etag.strip() else None,
            catalogue=catalogue,
            raw_document=raw_doc,
        )


def _persist_live_cache_locked() -> None:
    global _DISK_HYDRATED_STAMP
    disk_path = _live_cache_disk_path()
    serialized_entries: dict[str, dict[str, Any]] = {}
    for key, snap in _LIVE_CACHE.items():
        if snap.raw_document is None:
            continue
        entry_id = hashlib.sha256(f"{key[0]}\0{key[1]}\0{key[2]}".encode()).hexdigest()[
            :24
        ]
        serialized_entries[entry_id] = {
            "base_url": snap.base_url,
            "account_key": snap.account_key,
            "client_version": snap.client_version,
            "fetched_at_epoch": snap.fetched_at_epoch,
            "etag": snap.etag,
            "document": dict(snap.raw_document),
        }
    payload = {"version": 1, "entries": serialized_entries}
    temporary: Path | None = None
    try:
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{disk_path.name}.",
            suffix=".tmp",
            dir=disk_path.parent,
            text=True,
        )
        temporary = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
            handle.flush()
        os.replace(temporary, disk_path)
        stat = disk_path.stat()
        _DISK_HYDRATED_STAMP = (str(disk_path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_cached_live_codex_snapshot(
    key: tuple[str, str, str],
) -> CodexLiveSnapshot | None:
    """Return any cached S1 snapshot (fresh or stale for ``If-None-Match``)."""
    with _LIVE_CACHE_LOCK:
        _hydrate_live_cache_locked()
        return _LIVE_CACHE.get(key)


def store_live_codex_snapshot(
    key: tuple[str, str, str],
    *,
    etag: str | None,
    document: Mapping[str, Any],
    catalogue: CodexCatalogue,
) -> CodexLiveSnapshot:
    """Persist a validated S1 live catalogue snapshot in memory and on disk."""
    norm_base, account_key, client_version = key
    clean_etag = etag.strip() if isinstance(etag, str) and etag.strip() else None
    snapshot = CodexLiveSnapshot(
        base_url=norm_base.rstrip("/"),
        account_key=account_key,
        client_version=client_version,
        fetched_at_mono=time.monotonic(),
        fetched_at_epoch=time.time(),
        etag=clean_etag,
        catalogue=catalogue,
        raw_document=dict(document),
    )
    with _LIVE_CACHE_LOCK:
        _hydrate_live_cache_locked()
        _LIVE_CACHE[key] = snapshot
        _persist_live_cache_locked()
    return snapshot


def touch_live_codex_snapshot(
    key: tuple[str, str, str], *, etag: str | None = None
) -> CodexLiveSnapshot | None:
    """Extend a cached snapshot's TTL after ``304 Not Modified``."""
    with _LIVE_CACHE_LOCK:
        _hydrate_live_cache_locked()
        existing = _LIVE_CACHE.get(key)
        if existing is None:
            return None
        clean_etag = (
            etag.strip() if isinstance(etag, str) and etag.strip() else existing.etag
        )
        refreshed = CodexLiveSnapshot(
            base_url=existing.base_url,
            account_key=existing.account_key,
            client_version=existing.client_version,
            fetched_at_mono=time.monotonic(),
            fetched_at_epoch=time.time(),
            etag=clean_etag,
            catalogue=existing.catalogue,
            raw_document=existing.raw_document,
        )
        _LIVE_CACHE[key] = refreshed
        _persist_live_cache_locked()
        return refreshed


def invalidate_live_codex_snapshot(key: tuple[str, str, str]) -> None:
    """Evict a cached S1 snapshot (e.g. on 401/403 auth rejection)."""
    with _LIVE_CACHE_LOCK:
        _hydrate_live_cache_locked()
        if key in _LIVE_CACHE:
            _LIVE_CACHE.pop(key, None)
            _persist_live_cache_locked()


def latest_live_codex_catalogue() -> CodexLiveSnapshot | None:
    """Return the newest cached S1 live snapshot (read-only, zero network calls)."""
    with _LIVE_CACHE_LOCK:
        _hydrate_live_cache_locked()
        if not _LIVE_CACHE:
            return None
        now_mono = time.monotonic()
        fresh = [
            snap
            for snap in _LIVE_CACHE.values()
            if is_live_snapshot_fresh(snap, now_mono=now_mono)
        ]
        pool = fresh if fresh else list(_LIVE_CACHE.values())
        return max(pool, key=lambda snap: snap.fetched_at_epoch)


def clear_codex_models_live_cache(*, remove_disk: bool = False) -> None:
    """Clear in-memory S1 live snapshots (and optionally remove the disk file)."""
    global _DISK_HYDRATED_STAMP
    with _LIVE_CACHE_LOCK:
        _LIVE_CACHE.clear()
        _DISK_HYDRATED_STAMP = None
        if remove_disk:
            with suppress(OSError):
                _live_cache_disk_path().unlink(missing_ok=True)


# -------------------------------------------------------------------- vetoes


@dataclass(frozen=True, slots=True)
class ListingVeto:
    """The vendor own reason for keeping one slug out of every listing.

    Two reasons, and they differ in exactly one way that matters -- whether a
    later observation is allowed to overturn them:

    ``hidden``
        ``visibility: hide``. The vendor is saying "do not show this", and no
        amount of local evidence contradicts that: a model this credential
        served successfully last week is still a model OpenAI does not offer.
        Not overturnable, ever.
    ``retired``
        the vendor published an ``upgrade.retirement_at`` that has passed.
        This one *is* overturnable, and must be: a catalogue that is stale or
        simply wrong must never hide a model the operator is actively and
        successfully using. A success logged **strictly after** the retirement
        instant overturns it; a success logged before it does not. "I used
        this last month" is not evidence that it works today, and a dated,
        forward-looking retirement is the better of the two facts.

    A veto withholds from *listings* only. It never rewrites a route, never
    removes a configured ref and never marks a model unsupported: a chain
    naming a vetoed id keeps resolving and keeps serving.
    """

    slug: str
    #: ``"hidden"`` or ``"retired"``; ``"hidden"`` wins when both apply.
    reason: str
    #: The vendor retirement instant, verbatim, when the reason is
    #: ``"retired"``. ``None`` for a veto that nothing can overturn.
    retirement_at: str | None = None

    def overturned_by(self, observed_at: str) -> bool:
        """Whether one observation is newer than the fact that vetoed the id.

        Both sides are parsed as instants rather than compared as text, and a
        value carrying no offset is read as UTC -- the request log stores UTC
        and the machine reading it need not be on UTC. A missing or
        unparseable timestamp overturns nothing.
        """
        retired = _parse_instant(self.retirement_at)
        if retired is None:
            return False
        observed = _parse_instant(observed_at)
        return observed is not None and observed > retired


def listing_vetoes(
    catalogue: CodexCatalogue, *, now: datetime | None = None
) -> dict[str, ListingVeto]:
    """Every slug the vendor catalogue says must not appear in a listing.

    The single place both rungs consult, so the vendor rung and the observed
    rung cannot disagree about what "listed" means. The plan gate is
    deliberately *not* here: ``available_in_plans`` is what the vendor
    believes this plan may use, and a logged success is direct proof about the
    same question -- so the proof wins outright rather than needing a date.
    """
    moment = now or datetime.now(UTC)
    vetoes: dict[str, ListingVeto] = {}
    for entry in catalogue.entries:
        if entry.hidden:
            vetoes[entry.slug] = ListingVeto(slug=entry.slug, reason="hidden")
        elif entry.retired_at(moment):
            vetoes[entry.slug] = ListingVeto(
                slug=entry.slug, reason="retired", retirement_at=entry.retirement_at
            )
    return vetoes


# ------------------------------------------------------------------ evidence


def catalogue_evidence(
    catalogue: CodexCatalogue,
    *,
    plan_type: str = "",
    now: datetime | None = None,
    provenance: ModelListingProvenance = ModelListingProvenance.VENDOR_CLIENT,
    detail_prefix: str | None = None,
) -> dict[str, ModelListingEvidence]:
    """Existence evidence for every entry this plan may still list.

    Three filters, all taken from the document rather than invented here:

    * an entry the vendor marked ``visibility: hide`` is dropped -- OpenAI
      saying "do not show this". It stays routable when an operator names it;
    * an entry whose own ``upgrade.retirement_at`` has passed is dropped --
      the vendor said when it stops being real, so it stops being offered on
      the vendor's schedule rather than on a maintainer's;
    * an entry whose ``available_in_plans`` excludes this credential's plan is
      dropped, and *only* when both halves are actually known.

    The first two are :func:`listing_vetoes`, shared with the observed rung so
    that the two rungs cannot disagree about what "listed" means.
    """
    moment = now or datetime.now(UTC)
    vetoes = listing_vetoes(catalogue, now=moment)
    evidence: dict[str, ModelListingEvidence] = {}
    default_prefix = (
        f"Live Codex API {catalogue.version}".strip()
        if provenance == ModelListingProvenance.GATEWAY
        else catalogue.label
    )
    base_detail = detail_prefix if detail_prefix is not None else default_prefix
    for entry in catalogue.entries:
        if entry.slug in vetoes:
            continue
        if not entry.available_to_plan(plan_type):
            continue
        detail = base_detail
        if entry.retirement_at:
            detail = f"{detail}; retires {entry.retirement_at}"
        evidence[entry.slug] = ModelListingEvidence(
            provenance=provenance,
            detail=detail,
            retirement_at=entry.retirement_at,
            replacement_model_id=entry.replacement_model_id,
            # ``False`` is unreachable by construction now: every entry
            # the vendor hid was vetoed above, so whatever survives either
            # published ``list`` or published something this build does not
            # recognise -- and unknown stays ``None`` rather than a guess.
            offered_by_default=True if entry.listed else None,
        )
    return evidence


def retired_entries(
    catalogue: CodexCatalogue, *, now: datetime | None = None
) -> tuple[CodexCatalogueEntry, ...]:
    """Entries the vendor has already retired, for the page's explanation."""
    moment = now or datetime.now(UTC)
    return tuple(entry for entry in catalogue.entries if entry.retired_at(moment))


def catalogue_slugs(entries: Iterable[CodexCatalogueEntry]) -> frozenset[str]:
    """Every slug in a catalogue, retired and hidden ones included."""
    return frozenset(entry.slug for entry in entries)


__all__ = [
    "CATALOGUE_MARKERS",
    "CODEX_CLIENT_VERSION_FLOOR",
    "CODEX_LIVE_CACHE_FILENAME",
    "CODEX_MODELS_LIVE_TTL_SECONDS",
    "MAX_DOCUMENT_BYTES",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_LISTED",
    "CodexCatalogue",
    "CodexCatalogueEntry",
    "CodexLiveSnapshot",
    "ListingVeto",
    "catalogue_evidence",
    "catalogue_slugs",
    "clear_codex_catalogue_cache",
    "clear_codex_models_live_cache",
    "codex_home_dir",
    "codex_live_account_key",
    "embedded_json_document",
    "get_cached_live_codex_snapshot",
    "invalidate_live_codex_snapshot",
    "is_live_snapshot_fresh",
    "latest_live_codex_catalogue",
    "listing_vetoes",
    "load_codex_catalogue",
    "load_codex_models_cache",
    "load_offline_codex_catalogue",
    "parse_codex_catalogue",
    "resolve_codex_client_version",
    "retired_entries",
    "store_live_codex_snapshot",
    "touch_live_codex_snapshot",
    "validate_codex_models_document",
]
