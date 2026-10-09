"""Each provider list row is stored beside the catalogue, never in it (7.86.0).

``provider-catalogue.json`` is exactly what 7.85.0 wrote; the rows each record
keeps (``published_row``) go to ``provider-rows.json`` under the same scope
key, scrubbed of anything credential-shaped, compared before every write like
the catalogue, and kept for a record a failed sweep left without one. After a
restart the "Everything known" view reads them back through the runtime.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.settings import Settings
from my_claude_code.core.derived_cache import DerivedCache
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.runtime import provider_manager
from my_claude_code.runtime.catalogue_store import (
    PROVIDER_ROWS_ENTRY,
    catalogue_document,
    read_stored_catalogue,
    read_stored_provider_rows,
    store_catalogue,
    store_provider_rows,
)
from my_claude_code.runtime.provider_manager import ProviderRuntimeManager

ROW: dict[str, Any] = {
    "id": "acme/model",
    "tokenizer": "Other",
    "pricing": {"prompt": "0.000001"},
    # A custom host may put anything in its list; a key-shaped value is never
    # written to disk.
    "note": "sk-" + "Z" * 48,
    "api_key": "whatever-it-is",
}


def _record(model_id: str, row: dict[str, Any] | None) -> ProviderModelInfo:
    return ProviderModelInfo(
        model_id,
        context_length=1000,
        published_row=None if row is None else json.dumps(row),
    )


def _catalogues(
    row: dict[str, Any] | None = ROW,
) -> dict[str, tuple[ProviderModelInfo, ...]]:
    return {
        "custom_acme": (_record("acme/model", row), _record("acme/plain", None)),
        "nvidia_nim": (_record("nv/model", {"id": "nv/model", "owned_by": "nv"}),),
    }


def test_the_rows_go_beside_the_catalogue_and_the_catalogue_is_unchanged(
    tmp_path: Path,
) -> None:
    cache = DerivedCache(tmp_path)
    catalogues = _catalogues()
    assert store_catalogue(catalogues, "scope", computed_at=1.0, cache=cache)
    assert store_provider_rows(catalogues, "scope", computed_at=1.0, cache=cache)
    stored = json.loads((tmp_path / "provider-catalogue.json").read_text("utf-8"))
    assert stored["payload"] == catalogue_document(
        {
            provider: tuple(
                ProviderModelInfo(i.model_id, context_length=1000) for i in infos
            )
            for provider, infos in catalogues.items()
        }
    )
    assert "published_row" not in (tmp_path / "provider-catalogue.json").read_text(
        "utf-8"
    )
    rows, written_at = read_stored_provider_rows("scope", cache=cache) or ({}, 0.0)
    assert written_at == 1.0
    assert set(rows) == {"custom_acme", "nvidia_nim"}
    assert set(rows["custom_acme"]) == {"acme/model"}
    kept = rows["custom_acme"]["acme/model"]
    assert kept["tokenizer"] == "Other"
    assert kept["pricing"] == {"prompt": "0.000001"}
    assert kept["note"] == "<redacted>"
    assert kept["api_key"] == "<redacted>"
    restored = read_stored_catalogue("scope", cache=cache)
    assert restored is not None
    assert restored[0] == catalogues


def test_an_unchanged_sweep_rewrites_nothing(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path)
    assert store_provider_rows(_catalogues(), "scope", computed_at=1.0, cache=cache)
    assert not store_provider_rows(_catalogues(), "scope", computed_at=2.0, cache=cache)
    assert (read_stored_provider_rows("scope", cache=cache) or ({}, 0.0))[1] == 1.0


def test_a_record_without_a_row_keeps_the_one_stored_before(tmp_path: Path) -> None:
    """A sweep that could not reach a provider never drops its stored rows."""

    cache = DerivedCache(tmp_path)
    store_provider_rows(_catalogues(), "scope", computed_at=1.0, cache=cache)
    restored = _catalogues(row=None)
    store_provider_rows(restored, "scope", computed_at=2.0, cache=cache)
    rows, _ = read_stored_provider_rows("scope", cache=cache) or ({}, 0.0)
    assert rows["custom_acme"]["acme/model"]["tokenizer"] == "Other"


def test_another_scope_is_ignored(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path)
    store_provider_rows(_catalogues(), "scope", computed_at=1.0, cache=cache)
    assert read_stored_provider_rows("other-scope", cache=cache) is None
    store_provider_rows(
        _catalogues(row=None), "other-scope", computed_at=2.0, cache=cache
    )
    rows, _ = read_stored_provider_rows("other-scope", cache=cache) or ({}, 0.0)
    # Nothing carried over from a document written for another scope.
    assert "custom_acme" not in rows


def test_a_damaged_document_reads_as_nothing(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path)
    cache.write(
        PROVIDER_ROWS_ENTRY,
        key="scope",
        payload={
            "providers": [{"provider_id": 7}, "junk", {"provider_id": "p", "rows": []}]
        },
        computed_at=1.0,
    )
    assert read_stored_provider_rows("scope", cache=cache) == ({}, 1.0)


def test_the_runtime_stores_both_and_serves_the_stored_row(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    manager = ProviderRuntimeManager(Settings.model_validate({}))
    # The harness's own scope: exactly these two providers' catalogues.
    manager._model_cache = ProviderModelCache(("custom_acme", "nvidia_nim"))
    for provider_id, infos in _catalogues().items():
        manager._model_cache.cache_model_infos(provider_id, infos)
    manager._store_catalogue(5.0)
    derived = tmp_path / "cache" / "derived"
    assert (derived / "provider-catalogue.json").exists()
    assert (derived / "provider-rows.json").exists()
    (row,) = manager.model_published_rows("nvidia_nim", "nv/model", ())
    assert row.source == "provider_list"
    assert row.key == "nv/model"
    assert row.row == {"id": "nv/model", "owned_by": "nv"}
    assert row.match == "stored beside the catalogue"
    assert row.as_of is not None and row.as_of.startswith("1970-01-01T00:00:05")
    # Nothing is stored for a model that has no row, and no OpenRouter or
    # LiteLLM rows are asked for without their files.
    assert manager.model_published_rows("custom_acme", "acme/plain", ("x/y",)) == ()


def test_a_sweep_with_the_same_rows_skips_the_scrub(monkeypatch, tmp_path) -> None:
    """The rows document is rebuilt only when some row (or the scope) changed."""

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    manager = ProviderRuntimeManager(Settings.model_validate({}))
    manager._model_cache = ProviderModelCache(("custom_acme", "nvidia_nim"))
    for provider_id, infos in _catalogues().items():
        manager._model_cache.cache_model_infos(provider_id, infos)
    calls: list[float] = []
    real = provider_manager.store_provider_rows

    def counting(*args: Any, **kwargs: Any) -> bool:
        calls.append(kwargs["computed_at"])
        return real(*args, **kwargs)

    monkeypatch.setattr(provider_manager, "store_provider_rows", counting)
    manager._store_catalogue(1.0)
    manager._store_catalogue(2.0)
    assert calls == [1.0]
    changed = {"id": "nv/model", "owned_by": "someone else"}
    manager._model_cache.cache_model_infos(
        "nvidia_nim", (_record("nv/model", changed),)
    )
    manager._store_catalogue(3.0)
    assert calls == [1.0, 3.0]
    (row,) = manager.model_published_rows("nvidia_nim", "nv/model", ())
    assert row.row == changed
