"""Read Codex CLI's own model catalogue off disk -- rung S2 for this provider.

The ChatGPT/Codex backend publishes no model-list endpoint. Its official
client does not call one either: Codex 0.151.0 contains zero occurrences of
``backend-api/models``, ``available_models``, ``model_slugs`` or
``/backend-api/me``, and instead embeds its complete catalogue as a
pretty-printed JSON document inside the executable (the loader's own assertion
strings are ``bundled models.json should parse: `` and ``bundled models.json
should include ``). That document is written by OpenAI, ships with OpenAI's
client, and updates when the user updates Codex.

This project already reads that binary: ``application/catalogues/codex.py`` was
written against its ``ModelInfo`` serde struct so MCC can *generate* a
catalogue Codex will accept. This module reads the *data* alongside the schema
MCC already trusts.

**Existence facts only.** Every entry also carries ``context_window``,
``max_context_window`` and a per-model effort vocabulary, and none of it is
read here. Numeric limits and capabilities keep coming off the resolution
ladder through the existing ``chatgpt_oauth -> openai`` models.dev alias; a
second source for the same number, arriving at a different tier, is a change
to the ladder and the ladder is not what is broken. What is broken is the id
list, so the id list -- plus the lifecycle facts that say when an id stops
being real -- is all this rung supplies.

The transplantable shape for any other OAuth/subscription provider is the
three steps below: locate the vendor's official client without executing it,
read the catalogue it ships, and publish only what that catalogue states.

Two of those states keep a model out of the listing: ``visibility: hide`` and
a passed ``upgrade.retirement_at``. Both are *listing* facts -- see
:class:`ListingVeto` for which of them a logged success may overturn, and for
why neither one ever touches a route.
"""

import json
import mmap
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
)

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


def load_codex_catalogue(
    *, binary_path: Path | None = None, version: str = ""
) -> CodexCatalogue | None:
    """Locate the installed Codex CLI and return its bundled catalogue.

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
    for entry in catalogue.entries:
        if entry.slug in vetoes:
            continue
        if not entry.available_to_plan(plan_type):
            continue
        detail = catalogue.label
        if entry.retirement_at:
            detail = f"{detail}; retires {entry.retirement_at}"
        evidence[entry.slug] = ModelListingEvidence(
            provenance=ModelListingProvenance.VENDOR_CLIENT,
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
    "MAX_DOCUMENT_BYTES",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_LISTED",
    "CodexCatalogue",
    "CodexCatalogueEntry",
    "ListingVeto",
    "catalogue_evidence",
    "catalogue_slugs",
    "clear_codex_catalogue_cache",
    "embedded_json_document",
    "listing_vetoes",
    "load_codex_catalogue",
    "parse_codex_catalogue",
    "retired_entries",
]
