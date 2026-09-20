"""Dynamic custom provider registry with JSON persistence.

Custom providers are user-defined OpenAI-compatible providers added at runtime
(via the Admin UI/API). They live next to the static
:data:`~my_claude_code.config.provider_catalog.PROVIDER_CATALOG`: the catalog
stays import-time frozen while this registry answers "which providers exist
right now" for validation, routing, factory construction, and discovery.

This module is config-local: it must never import ``config.settings`` so the
registry can load before Settings are rebuilt.
"""

import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from my_claude_code.config.constants import TIER_NAMESPACE
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.provider_catalog import (
    PROVIDER_CATALOG,
    ProviderDescriptor,
)
from my_claude_code.config.reasoning_enum import normalize_effort_words

CUSTOM_PROVIDER_ID_PREFIX = "custom_"

#: Provider ids nothing may register. ``mcc`` is the namespace of the five tier
#: aliases (``mcc/best`` and friends, ``core/tier_refs``): a provider with that
#: id would make every alias ambiguous with a real ``provider/model`` ref, and
#: the router resolves the alias first, so the provider's models would simply
#: stop routing. Refused at creation rather than repaired afterwards, because
#: by then the operator has a provider whose models silently do not work.
RESERVED_PROVIDER_IDS: frozenset[str] = frozenset({TIER_NAMESPACE})
CUSTOM_PROVIDERS_FILENAME = "custom_providers.json"
DEFAULT_CUSTOM_CREDENTIAL_ROTATION = "failover"
CUSTOM_CREDENTIAL_ROTATION_POLICIES = frozenset(
    {"single", "round_robin", "least_used", "failover"}
)

#: The wire APIs a hand-configured host may declare that it serves, in the
#: order MCC tries them. The spellings are
#: :class:`~my_claude_code.application.model_metadata.ResponseSurface`'s own
#: values, written out here as strings because this module is config-local and
#: must not import ``application``; a contract test asserts the two lists stay
#: equal so a surface cannot be added on one side alone.
CUSTOM_PROVIDER_SURFACES: tuple[str, ...] = (
    "chat_completions",
    "responses",
    "messages",
)

#: What an entry that says nothing serves. Exactly what every custom provider
#: has spoken since 6.25.0, so an entry written by an older build -- or by an
#: operator who never opens the control -- keeps sending the same bytes to the
#: same endpoint.
DEFAULT_CUSTOM_PROVIDER_SURFACES: tuple[str, ...] = ("chat_completions",)

_UNSET: object = object()


@dataclass(frozen=True, slots=True)
class CustomProviderEntry:
    """One user-defined OpenAI-compatible provider."""

    provider_id: str
    display_name: str
    base_url: str
    api_keys: tuple[str, ...]
    credential_rotation: str = DEFAULT_CUSTOM_CREDENTIAL_ROTATION
    proxy: str | None = None
    enabled: bool = True
    added_at: str = ""
    # What this host was measured accepting in ``reasoning_effort``, in the
    # order it named them. A static provider writes this into its profile; a
    # custom one has it probed and stored here. ``None`` is "not measured" --
    # distinct from ``()``, which never occurs, and from
    # ``reasoning_field_ignored``, which is a measurement.
    reasoning_effort_enum: tuple[str, ...] | None = None
    # The host answered 200 to a deliberately invalid effort value, so it does
    # not read the field at all. Sending one costs nothing and means nothing.
    reasoning_field_ignored: bool = False
    # Free text for the card: "learned", "ignored", or why it is still unknown
    # ("401"). Never the key, never the response body.
    reasoning_probe_status: str = ""
    reasoning_probed_at: str = ""
    # Which wire APIs this host serves, in the order MCC should try them.
    # Defaults to Chat Completions alone -- what every custom provider has
    # spoken since 6.25.0 -- and an entry that keeps the default is routed
    # exactly as it was before this field existed: no surface resolution, no
    # extra row in its request log, the same body on the same endpoint.
    #
    # Declared rather than probed because only the operator knows what their
    # gateway fronts: a vLLM deployment serves one API, a corporate gateway may
    # serve three, and asking every custom host three questions at startup
    # would be a guess paid for on every install. What MCC *measures* is which
    # of the declared surfaces a given model answers on -- that is the 6.74.0
    # probe, and it can only choose between doors the operator said exist.
    surfaces: tuple[str, ...] = DEFAULT_CUSTOM_PROVIDER_SURFACES
    # ``(MODEL_*_PAUSED key, model ref)`` pairs that disabling this provider
    # paused. Recorded rather than recomputed so re-enabling lifts its own
    # pauses and leaves a hand-paused ref exactly where the operator put it.
    auto_paused_refs: tuple[tuple[str, str], ...] = ()


def _slug(display_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")


def _slugify(display_name: str) -> str:
    return _slug(display_name) or "provider"


def custom_provider_id(display_name: str) -> str:
    """Return the id this display name claims, or ``""`` if it slugs to nothing.

    The admin API needs the id *before* calling :meth:`ProviderRegistry.add`, to
    reject a duplicate name and a name with no usable characters. Deriving it
    here keeps one implementation of the rule: a second copy in the API layer
    drifted from this one and produced ids the registry never allocated.
    """

    slug = _slug(display_name)
    return f"{CUSTOM_PROVIDER_ID_PREFIX}{slug}" if slug else ""


def _effort_enum_from_payload(value: object) -> tuple[str, ...] | None:
    """Return a stored vocabulary, or ``None`` for "never measured".

    An empty list on disk reads back as ``None`` rather than ``()``: no probe
    produces an empty vocabulary, so an empty one is a hand-edit meaning
    "forget what you learned".
    """
    if value is None:
        return None
    words = normalize_effort_words(value)
    return words or None


def _surfaces_from_payload(value: object) -> tuple[str, ...]:
    """Read back a declared surface list, falling back to today's default.

    Anything unrecognised is dropped rather than refused: a file written by a
    newer build may name a surface this one cannot speak, and the entry's keys,
    credentials and routes are all still good. An empty result -- a hand-edited
    ``[]``, or a list of nothing but unknown words -- reads back as the default
    rather than as "serves nothing", because a provider that serves no surface
    is not a configuration anyone means to write.
    """

    if not isinstance(value, list):
        return DEFAULT_CUSTOM_PROVIDER_SURFACES
    named = tuple(
        surface
        for surface in CUSTOM_PROVIDER_SURFACES
        if any(item == surface for item in value)
    )
    return named or DEFAULT_CUSTOM_PROVIDER_SURFACES


def normalize_custom_surfaces(value: object) -> tuple[str, ...]:
    """Validate a surface list an operator submitted, in canonical order.

    Raises rather than repairing, because this one reaches the registry from a
    form: a word the build does not know is a mistake worth showing, where the
    same word on disk is a file from a newer release.
    """

    if value is None:
        return DEFAULT_CUSTOM_PROVIDER_SURFACES
    if not isinstance(value, (list, tuple)):
        raise ValueError("Custom provider surfaces must be a list")
    unknown = sorted(
        str(item) for item in value if item not in CUSTOM_PROVIDER_SURFACES
    )
    if unknown:
        raise ValueError(
            f"Unknown wire surface(s): {unknown}. "
            f"Valid: {list(CUSTOM_PROVIDER_SURFACES)}"
        )
    named = tuple(
        surface for surface in CUSTOM_PROVIDER_SURFACES if surface in tuple(value)
    )
    if not named:
        raise ValueError(
            "Custom provider must serve at least one wire surface; "
            f"valid: {list(CUSTOM_PROVIDER_SURFACES)}"
        )
    return named


def _paused_pair(item: object) -> tuple[str, str] | None:
    if not isinstance(item, list) or len(item) != 2:
        return None
    first, second = item
    if isinstance(first, str) and first and isinstance(second, str) and second:
        return (first, second)
    return None


def _paused_pairs_from_payload(value: object) -> tuple[tuple[str, str], ...]:
    """Read back the pauses a disable added, ignoring anything malformed."""
    if not isinstance(value, list):
        return ()
    pairs = (_paused_pair(item) for item in value)
    return tuple(pair for pair in pairs if pair is not None)


def _text_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ProviderRegistry:
    """Thread-safe registry of static + custom providers with persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._custom: dict[str, CustomProviderEntry] = {}
        self._on_change: list[Callable[[], None]] = []
        self._loaded = False

    # ------------------------------------------------------------------ path

    def _storage_path(self) -> Path:
        if self._path is not None:
            return self._path
        return config_dir_path() / CUSTOM_PROVIDERS_FILENAME

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._storage_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "Custom provider registry load failed: path={} reason={}",
                path,
                exc,
            )
            return
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, list):
            return
        for item in providers:
            entry = self._entry_from_payload(item)
            if entry is not None and entry.provider_id not in self._custom:
                self._custom[entry.provider_id] = entry

    @staticmethod
    def _entry_from_payload(item: object) -> CustomProviderEntry | None:
        if not isinstance(item, dict):
            return None
        provider_id = item.get("provider_id")
        if isinstance(provider_id, str) and provider_id in RESERVED_PROVIDER_IDS:
            # A hand-edited file is the only way one of these reaches here.
            logger.warning(
                "CUSTOM PROVIDER: '{}' is a reserved id; ignoring the entry",
                provider_id,
            )
            return None
        display_name = item.get("display_name")
        base_url = item.get("base_url")
        api_keys = item.get("api_keys")
        if not (
            isinstance(provider_id, str)
            and provider_id.startswith(CUSTOM_PROVIDER_ID_PREFIX)
            and isinstance(display_name, str)
            and isinstance(base_url, str)
            and isinstance(api_keys, list)
        ):
            return None
        keys = tuple(key for key in api_keys if isinstance(key, str) and key.strip())
        rotation = item.get("credential_rotation")
        proxy = item.get("proxy")
        added_at = item.get("added_at")
        return CustomProviderEntry(
            provider_id=provider_id,
            display_name=display_name,
            base_url=base_url,
            api_keys=keys,
            credential_rotation=(
                rotation
                if isinstance(rotation, str)
                and rotation in CUSTOM_CREDENTIAL_ROTATION_POLICIES
                else DEFAULT_CUSTOM_CREDENTIAL_ROTATION
            ),
            proxy=proxy if isinstance(proxy, str) and proxy.strip() else None,
            enabled=bool(item.get("enabled", True)),
            added_at=added_at if isinstance(added_at, str) else "",
            reasoning_effort_enum=_effort_enum_from_payload(
                item.get("reasoning_effort_enum")
            ),
            reasoning_field_ignored=bool(item.get("reasoning_field_ignored", False)),
            reasoning_probe_status=_text_or_empty(item.get("reasoning_probe_status")),
            reasoning_probed_at=_text_or_empty(item.get("reasoning_probed_at")),
            surfaces=_surfaces_from_payload(item.get("surfaces")),
            auto_paused_refs=_paused_pairs_from_payload(item.get("auto_paused_refs")),
        )

    # ------------------------------------------------------------- persistence

    def _persist_locked(self) -> None:
        path = self._storage_path()
        payload = {
            "providers": [
                {
                    "provider_id": entry.provider_id,
                    "display_name": entry.display_name,
                    "base_url": entry.base_url,
                    "api_keys": list(entry.api_keys),
                    "credential_rotation": entry.credential_rotation,
                    "proxy": entry.proxy,
                    "enabled": entry.enabled,
                    "added_at": entry.added_at,
                    "reasoning_effort_enum": (
                        None
                        if entry.reasoning_effort_enum is None
                        else list(entry.reasoning_effort_enum)
                    ),
                    "reasoning_field_ignored": entry.reasoning_field_ignored,
                    "reasoning_probe_status": entry.reasoning_probe_status,
                    "reasoning_probed_at": entry.reasoning_probed_at,
                    # Always written, including the default, so the file says
                    # what this install serves rather than leaving a reader to
                    # infer it. An older build ignores the key entirely.
                    "surfaces": list(entry.surfaces),
                    "auto_paused_refs": [
                        [paused_key, model_ref]
                        for paused_key, model_ref in entry.auto_paused_refs
                    ],
                }
                for entry in self._custom.values()
            ]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    # ------------------------------------------------------------------ reads

    def list_custom(self) -> tuple[CustomProviderEntry, ...]:
        """Return all custom providers in insertion order (enabled or not)."""
        with self._lock:
            self._ensure_loaded()
            return tuple(self._custom.values())

    def get(self, provider_id: str) -> CustomProviderEntry | None:
        """Return one custom provider entry by id, if present."""
        with self._lock:
            self._ensure_loaded()
            return self._custom.get(provider_id)

    def all_descriptors(self) -> Mapping[str, ProviderDescriptor]:
        """Return static catalog descriptors plus enabled custom providers."""
        with self._lock:
            self._ensure_loaded()
            descriptors: dict[str, ProviderDescriptor] = dict(PROVIDER_CATALOG)
            for entry in self._custom.values():
                if entry.enabled:
                    descriptors[entry.provider_id] = self.descriptor_for(entry)
            return descriptors

    def supported_ids(self) -> tuple[str, ...]:
        """Return static catalog order followed by enabled custom ids."""
        return tuple(self.all_descriptors())

    def configurable_ids(self) -> tuple[str, ...]:
        """Ids a ``MODEL*`` route setting may name, disabled customs included.

        Deliberately not :meth:`supported_ids`. Runtime asks "can I build this
        provider right now", and a disabled entry must answer no -- that is
        what disabling is. Settings validation asks a different question:
        "is this a provider this install knows about", and for a custom entry
        the operator switched off for the afternoon the answer is yes. Fusing
        the two is what made ``Settings()`` raise ``ValidationError`` on every
        ``MODEL*`` naming a disabled custom provider, taking the whole process
        down rather than the one route.
        """
        with self._lock:
            self._ensure_loaded()
            return tuple(PROVIDER_CATALOG) + tuple(
                provider_id
                for provider_id in self._custom
                if provider_id not in PROVIDER_CATALOG
            )

    @staticmethod
    def descriptor_for(entry: CustomProviderEntry) -> ProviderDescriptor:
        """Build the dynamic descriptor for one custom provider entry."""
        return ProviderDescriptor(
            provider_id=entry.provider_id,
            display_name=entry.display_name,
            static_credential=entry.api_keys[0] if entry.api_keys else None,
            default_base_url=entry.base_url,
            dynamic=True,
            reasoning_effort_enum=entry.reasoning_effort_enum,
            response_surfaces=entry.surfaces,
        )

    # ---------------------------------------------------------------- mutations

    def add(
        self,
        display_name: str,
        base_url: str,
        api_keys: tuple[str, ...] | list[str],
        credential_rotation: str = DEFAULT_CUSTOM_CREDENTIAL_ROTATION,
        proxy: str | None = None,
        enabled: bool = True,
        surfaces: tuple[str, ...] | list[str] | None = None,
    ) -> CustomProviderEntry:
        """Register a new custom provider; the id is slugged from the name."""
        name = display_name.strip()
        if not name:
            raise ValueError("Custom provider display_name must not be empty")
        if _slug(name) in RESERVED_PROVIDER_IDS:
            raise ValueError(
                f"'{_slug(name)}' is reserved for MCC's own coding-agent tier "
                f"aliases (mcc/cyber, mcc/best, mcc/good, mcc/medium, "
                f"mcc/cheap, mcc/vision). Choose another name."
            )
        url = base_url.strip()
        if not url:
            raise ValueError("Custom provider base_url must not be empty")
        keys = tuple(key for key in (k.strip() for k in api_keys) if key)
        if not keys:
            raise ValueError("Custom provider requires at least one API key")
        if credential_rotation not in CUSTOM_CREDENTIAL_ROTATION_POLICIES:
            raise ValueError(
                f"Unknown credential_rotation: {credential_rotation!r}. "
                f"Valid: {sorted(CUSTOM_CREDENTIAL_ROTATION_POLICIES)}"
            )
        declared = normalize_custom_surfaces(surfaces)
        with self._lock:
            self._ensure_loaded()
            provider_id = self._unique_provider_id_locked(name)
            entry = CustomProviderEntry(
                provider_id=provider_id,
                display_name=name,
                base_url=url,
                api_keys=keys,
                credential_rotation=credential_rotation,
                proxy=proxy.strip()
                if isinstance(proxy, str) and proxy.strip()
                else None,
                enabled=enabled,
                added_at=_utc_now_iso(),
                surfaces=declared,
            )
            self._custom[provider_id] = entry
            self._persist_locked()
        self._notify_change()
        return entry

    def _unique_provider_id_locked(self, display_name: str) -> str:
        base = f"{CUSTOM_PROVIDER_ID_PREFIX}{_slugify(display_name)}"
        taken = set(PROVIDER_CATALOG) | set(self._custom)
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    def update(
        self,
        provider_id: str,
        *,
        display_name: str | None = None,
        base_url: str | None = None,
        api_keys: tuple[str, ...] | list[str] | None = None,
        credential_rotation: str | None = None,
        proxy: str | None | object = _UNSET,
        enabled: bool | None = None,
        reasoning_effort_enum: tuple[str, ...] | list[str] | None | object = _UNSET,
        reasoning_field_ignored: bool | None = None,
        reasoning_probe_status: str | None = None,
        reasoning_probed_at: str | None = None,
        surfaces: tuple[str, ...] | list[str] | None = None,
        auto_paused_refs: tuple[tuple[str, str], ...] | None = None,
    ) -> CustomProviderEntry:
        """Update fields of one custom provider and return the new entry."""
        with self._lock:
            self._ensure_loaded()
            current = self._custom.get(provider_id)
            if current is None:
                raise KeyError(f"Unknown custom provider: {provider_id!r}")
            if credential_rotation is not None and (
                credential_rotation not in CUSTOM_CREDENTIAL_ROTATION_POLICIES
            ):
                raise ValueError(
                    f"Unknown credential_rotation: {credential_rotation!r}. "
                    f"Valid: {sorted(CUSTOM_CREDENTIAL_ROTATION_POLICIES)}"
                )
            updated = CustomProviderEntry(
                provider_id=current.provider_id,
                display_name=(
                    display_name.strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else current.display_name
                ),
                base_url=(
                    base_url.strip()
                    if isinstance(base_url, str) and base_url.strip()
                    else current.base_url
                ),
                api_keys=(
                    tuple(key for key in (k.strip() for k in api_keys) if key)
                    if api_keys is not None
                    else current.api_keys
                ),
                credential_rotation=(
                    credential_rotation
                    if credential_rotation is not None
                    else current.credential_rotation
                ),
                proxy=(
                    current.proxy
                    if proxy is _UNSET
                    else (
                        proxy.strip()
                        if isinstance(proxy, str) and proxy.strip()
                        else None
                    )
                ),
                enabled=current.enabled if enabled is None else bool(enabled),
                added_at=current.added_at,
                reasoning_effort_enum=(
                    current.reasoning_effort_enum
                    if reasoning_effort_enum is _UNSET
                    else _effort_enum_from_payload(reasoning_effort_enum)
                ),
                reasoning_field_ignored=(
                    current.reasoning_field_ignored
                    if reasoning_field_ignored is None
                    else bool(reasoning_field_ignored)
                ),
                reasoning_probe_status=(
                    current.reasoning_probe_status
                    if reasoning_probe_status is None
                    else reasoning_probe_status
                ),
                reasoning_probed_at=(
                    current.reasoning_probed_at
                    if reasoning_probed_at is None
                    else reasoning_probed_at
                ),
                surfaces=(
                    current.surfaces
                    if surfaces is None
                    else normalize_custom_surfaces(surfaces)
                ),
                auto_paused_refs=(
                    current.auto_paused_refs
                    if auto_paused_refs is None
                    else tuple(auto_paused_refs)
                ),
            )
            self._custom[provider_id] = updated
            self._persist_locked()
        self._notify_change()
        return updated

    def remove(self, provider_id: str) -> CustomProviderEntry:
        """Remove one custom provider and return the removed entry."""
        with self._lock:
            self._ensure_loaded()
            entry = self._custom.pop(provider_id, None)
            if entry is None:
                raise KeyError(f"Unknown custom provider: {provider_id!r}")
            self._persist_locked()
        self._notify_change()
        return entry

    # ------------------------------------------------------------------ hooks

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked after every registry mutation."""
        with self._lock:
            self._on_change.append(callback)

    def _notify_change(self) -> None:
        with self._lock:
            callbacks = tuple(self._on_change)
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning("Custom provider on_change hook failed: {}", exc)

    # ------------------------------------------------------------------ test

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._custom.clear()
            self._on_change.clear()
            self._loaded = False


_registry_lock = threading.Lock()
_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    """Return the process-wide provider registry (lazy, loads once)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ProviderRegistry()
    return _registry


def reset_provider_registry() -> None:
    """Drop the process-wide registry singleton (test isolation)."""
    global _registry
    with _registry_lock:
        _registry = None
