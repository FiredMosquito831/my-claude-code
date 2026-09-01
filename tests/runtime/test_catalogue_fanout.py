"""The fan-out publisher refreshes what exists and creates nothing else."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.application.ports import RequestRuntimeLease, RequestRuntimePort
from my_claude_code.config.harnesses import harness_specs
from my_claude_code.config.settings import Settings
from my_claude_code.runtime.harness_catalogues import HarnessCatalogueFanoutPublisher


class FakeRuntime(RequestRuntimePort):
    def __init__(
        self,
        *,
        settings: Settings,
        cached_infos: tuple[ProviderModelInfo, ...] = (),
        context_lengths: dict[str, int] | None = None,
        reasoning: dict[str, ModelReasoningCapability] | None = None,
    ) -> None:
        self._settings = settings
        self._cached_infos = cached_infos
        self._context_lengths = context_lengths or {}
        self._reasoning = reasoning or {}

    async def acquire(self) -> RequestRuntimeLease:
        raise AssertionError("Catalogue publication must not acquire a lease.")

    def current_settings(self) -> Settings:
        return self._settings

    def cached_model_supports_thinking(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        return None

    def cached_model_supports_vision(
        self, provider_id: str, model_id: str
    ) -> bool | None:
        return None

    def model_reasoning_capability(
        self, provider_id: str, model_id: str
    ) -> ModelReasoningCapability | None:
        return self._reasoning.get(f"{provider_id}/{model_id}")

    def model_context_length(self, provider_id: str, model_id: str) -> int | None:
        return self._context_lengths.get(f"{provider_id}/{model_id}")

    def model_output_limit(self, provider_id: str, model_id: str) -> int | None:
        return None

    def cached_prefixed_model_infos(self) -> tuple[ProviderModelInfo, ...]:
        return self._cached_infos


def _runtime(context_lengths: dict[str, int] | None = None) -> FakeRuntime:
    settings = Settings().model_copy(update={"model": "nvidia_nim/configured"})
    return FakeRuntime(
        settings=settings,
        cached_infos=(ProviderModelInfo("open_router/discovered"),),
        context_lengths=context_lengths,
    )


def _publisher(codex_path: Path) -> HarnessCatalogueFanoutPublisher:
    return HarnessCatalogueFanoutPublisher({"codex": codex_path})


def test_no_catalogue_is_created_for_a_harness_never_launched(tmp_path: Path) -> None:
    codex_path = tmp_path / "codex-model-catalog.json"

    _publisher(codex_path).publish(_runtime())

    assert not codex_path.exists()


def test_an_existing_catalogue_is_refreshed_in_place(tmp_path: Path) -> None:
    codex_path = tmp_path / "codex-model-catalog.json"
    codex_path.write_text("{}\n", encoding="utf-8")

    _publisher(codex_path).publish(_runtime())

    slugs = [
        entry["slug"]
        for entry in json.loads(codex_path.read_text(encoding="utf-8"))["models"]
    ]
    assert slugs == ["nvidia_nim/configured", "open_router/discovered"]


def test_capability_change_alone_re_emits_every_catalogue(tmp_path: Path) -> None:
    """The old publisher could not do this, and it is the point of the layer.

    The model list is identical across both publications; only the resolved
    context length changed. A catalogue built from ``/v1/models`` would have
    written byte-identical output and left Codex's picker showing the stale
    window forever.
    """

    codex_path = tmp_path / "codex-model-catalog.json"
    codex_path.write_text("{}\n", encoding="utf-8")
    publisher = _publisher(codex_path)

    publisher.publish(_runtime(context_lengths={"open_router/discovered": 32768}))
    first = json.loads(codex_path.read_text(encoding="utf-8"))

    publisher.publish(_runtime(context_lengths={"open_router/discovered": 262144}))
    second = json.loads(codex_path.read_text(encoding="utf-8"))

    def window(document: dict[str, Any], slug: str) -> int:
        return next(
            entry["context_window"]
            for entry in document["models"]
            if entry["slug"] == slug
        )

    assert [entry["slug"] for entry in first["models"]] == [
        entry["slug"] for entry in second["models"]
    ]
    assert window(first, "open_router/discovered") == 32768
    assert window(second, "open_router/discovered") == 262144


def test_an_unchanged_catalogue_is_not_rewritten(tmp_path: Path) -> None:
    codex_path = tmp_path / "codex-model-catalog.json"
    codex_path.write_text("{}\n", encoding="utf-8")
    publisher = _publisher(codex_path)

    publisher.publish(_runtime())
    first = codex_path.stat().st_mtime_ns
    publisher.publish(_runtime())

    assert codex_path.stat().st_mtime_ns == first


def test_one_serialiser_raising_does_not_abort_the_others_or_the_refresh(
    tmp_path: Path,
) -> None:
    codex_path = tmp_path / "codex-model-catalog.json"
    codex_path.write_text("last known good\n", encoding="utf-8")

    with patch(
        "my_claude_code.runtime.harness_catalogues.serialise",
        side_effect=RuntimeError("serialiser bug"),
    ):
        _publisher(codex_path).publish(_runtime())

    assert codex_path.read_text(encoding="utf-8") == "last known good\n"


def test_an_empty_projection_preserves_every_last_known_good_file(
    tmp_path: Path,
) -> None:
    codex_path = tmp_path / "codex-model-catalog.json"
    codex_path.write_text("last known good\n", encoding="utf-8")
    empty = FakeRuntime(
        settings=Settings().model_copy(
            update={"model": "nvidia_nim/configured", "model_visibility_deny": "*"}
        )
    )

    with pytest.raises(ValueError, match="no routable models"):
        _publisher(codex_path).publish(empty)

    assert codex_path.read_text(encoding="utf-8") == "last known good\n"


def test_startup_creates_only_the_declared_server_owned_catalogue(
    tmp_path: Path,
) -> None:
    """Exactly one catalogue may be created before anything launches.

    The Codex App reads ``~/.fcc/codex-model-catalog.json`` from a persistent
    ``config.toml`` and has no launcher of its own, so nothing else would ever
    create it. Every other catalogue is the launcher's to create.
    """

    codex_path = tmp_path / "codex-model-catalog.json"

    _publisher(codex_path).ensure_exists(_runtime())

    assert codex_path.exists()
    assert [
        spec.id
        for spec in harness_specs()
        if spec.catalogue is not None and spec.catalogue.created_at_startup
    ] == ["codex"]


def test_a_startup_refresh_does_not_overwrite_an_unchanged_catalogue(
    tmp_path: Path,
) -> None:
    codex_path = tmp_path / "codex-model-catalog.json"
    publisher = _publisher(codex_path)

    publisher.ensure_exists(_runtime())
    first = codex_path.stat().st_mtime_ns
    publisher.ensure_exists(_runtime())

    assert codex_path.stat().st_mtime_ns == first


def test_the_default_path_resolves_under_the_fcc_config_directory(
    tmp_path: Path,
) -> None:
    publisher = HarnessCatalogueFanoutPublisher()

    with patch(
        "my_claude_code.runtime.harness_catalogues.harness_catalogue_path",
        return_value=tmp_path / "defaulted.json",
    ) as resolved:
        publisher.publish(_runtime())

    # One resolution per harness that materialises a file, and every one of
    # them through the same helper, so no generated document can land outside
    # ~/.fcc.
    assert [call.args for call in resolved.call_args_list] == [
        ("codex-model-catalog.json",),
        ("opencode-config.json",),
        ("opencode2-config.json",),
        ("kilo-config.json",),
    ]
