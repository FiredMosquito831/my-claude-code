"""The fan-out publisher refreshes what exists and creates nothing else."""

import json
import tomllib
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.application.ports import RequestRuntimeLease, RequestRuntimePort
from my_claude_code.config.harnesses import MCC_HARNESS_ID_SENTINEL, harness_specs
from my_claude_code.config.proxy_auth import PROXY_NO_AUTH_SENTINEL
from my_claude_code.config.settings import Settings
from my_claude_code.core.client_fingerprint import HARNESS_HEADER
from my_claude_code.core.model_ids import ResolutionTier
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

    # Nothing in this module exercises the tiered rungs; the fan-out is
    # about which files get written, not about what the ladder resolved.
    _vision: ClassVar[dict[str, bool]] = {}
    _tool_calls: ClassVar[dict[str, bool]] = {}
    _prices: ClassVar[dict[str, dict[str, float]]] = {}

    def model_context_length_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[int | None, ResolutionTier | None]:
        return self._context_lengths.get(f"{provider_id}/{model_id}"), None

    def model_vision_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        return self._vision.get(f"{provider_id}/{model_id}"), None

    def model_tool_call_tiered(
        self, provider_id: str, model_id: str
    ) -> tuple[bool | None, ResolutionTier | None]:
        return self._tool_calls.get(f"{provider_id}/{model_id}"), None

    def model_prices_tiered(
        self, provider_id: str, model_id: str
    ) -> dict[str, tuple[float | None, ResolutionTier | None]]:
        rates = self._prices.get(f"{provider_id}/{model_id}", {})
        return {
            name: (rates.get(name), None)
            for name in (
                "input_price",
                "output_price",
                "cache_read_price",
                "cache_write_price",
            )
        }

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


def test_the_fan_out_creates_a_missing_document(tmp_path: Path) -> None:
    """A publish writes the file even when nothing has launched that harness.

    It used to skip it, and that is the whole of the defect this test pins. The
    launcher was the only thing that could create the file; its fetch was given
    the 1.5 s health-check budget for a route measuring 1.8-4.0 s; so the file
    was never created and this publisher never refreshed a file that did not
    exist. Every ``mcc-opencode`` after the first failed identically, forever.
    """

    codex_path = tmp_path / "codex-model-catalog.json"

    _publisher(codex_path).publish(_runtime())

    slugs = [
        entry["slug"]
        for entry in json.loads(codex_path.read_text(encoding="utf-8"))["models"]
    ]
    assert slugs == ["nvidia_nim/configured", "open_router/discovered"]


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


def test_every_catalogue_format_is_materialised_at_startup(tmp_path: Path) -> None:
    """Every MCC-owned document exists after startup, whatever its format.

    JSON, TOML and the two-document harness alike: the launcher's job is to read
    one of these, so a format that startup skips is a coding agent that pays a
    cold-start fetch it should never have needed. The merge target is the one
    exclusion, and it has its own test -- that file belongs to the user.
    """

    paths = {
        spec.id: tmp_path / f"{spec.id}{Path(spec.catalogue.filename).suffix}"
        for spec in harness_specs()
        if spec.catalogue is not None
        and spec.catalogue.filename is not None
        and spec.catalogue.merge is None
    }
    assert len(paths) == 11

    HarnessCatalogueFanoutPublisher(paths).ensure_exists(_runtime())

    missing = sorted(harness_id for harness_id, p in paths.items() if not p.exists())
    assert missing == []


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
        ("kimi-code-config.toml",),
        ("qwen-code-settings.json",),
        ("crush/crush.json",),
        ("cline/data/settings/providers.json",),
        ("aider-model-metadata.json",),
        ("droid-settings.json",),
        ("gemini-cli-settings.json",),
        # Aider's second document, resolved during the write pass rather than
        # the target sweep above. It is written now that the first one is
        # always created; it used to be skipped for a harness never launched.
        ("aider-model-settings.yml",),
    ]


def test_aiders_second_document_is_published_beside_its_first(
    tmp_path: Path,
) -> None:
    """Aider reads two files, so a refresh has to rewrite both.

    The settings document says what each model *accepts* -- whether
    ``--reasoning-effort`` is honoured, whether ``temperature`` may be sent --
    and those are ladder facts like any other. Leaving it behind on a refresh
    would let the two documents disagree about the same model.
    """

    metadata_path = tmp_path / "aider-model-metadata.json"
    sidecar_path = tmp_path / "aider-model-settings.yml"
    publisher = HarnessCatalogueFanoutPublisher({"aider": metadata_path})
    metadata_path.write_text("{}", encoding="utf-8")

    # Every harness's document is materialised now, so the resolver has to
    # answer per filename rather than handing every caller one path.
    with patch(
        "my_claude_code.runtime.harness_catalogues.harness_catalogue_path",
        side_effect=lambda name: tmp_path / Path(name).name,
    ):
        publisher.publish(_runtime())

    assert json.loads(metadata_path.read_text(encoding="utf-8"))
    # A list, not a mapping: Aider constructs one ``ModelSettings`` per entry.
    assert isinstance(json.loads(sidecar_path.read_text(encoding="utf-8")), list)


# --------------------------------------------------------------- merge targets


def _merge_publisher(path: Path) -> HarnessCatalogueFanoutPublisher:
    return HarnessCatalogueFanoutPublisher({"commandcode_cli": path})


def test_a_users_own_config_is_not_written_into_until_mcc_is_invited(
    tmp_path: Path,
) -> None:
    """The file existing proves nothing: only MCC's own key does.

    A Command Code user who has never run ``mcc-commandcode`` already has a
    ``providers.json``. Finding a ``provider.mcc`` block appear in it because a
    provider key rotated on a server they left running would be exactly the
    behaviour the never-write-for-an-unlaunched-harness rule exists to stop.
    """

    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"provider": {"ollama": {"baseURL": "http://x/v1"}}}),
        encoding="utf-8",
    )
    before = path.read_bytes()

    _merge_publisher(path).publish(_runtime())

    assert path.read_bytes() == before


def test_an_invited_merge_target_is_refreshed_and_keeps_every_other_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "provider": {
                    "ollama": {"baseURL": "http://x/v1"},
                    "mcc": {"models": {"stale/model": {}}},
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )

    _merge_publisher(path).publish(_runtime({"nvidia_nim/configured": 300_000}))

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["theme"] == "dark"
    assert document["provider"]["ollama"] == {"baseURL": "http://x/v1"}
    models = document["provider"]["mcc"]["models"]
    assert sorted(models) == ["nvidia_nim/configured", "open_router/discovered"]
    assert models["nvidia_nim/configured"]["contextWindow"] == 300_000
    # Written by the server, so the URL is this install's own proxy root and
    # the token is still only a reference the launcher expands.
    assert document["provider"]["mcc"]["baseURL"].endswith("/v1")
    assert document["provider"]["mcc"]["apiKey"] == "$MCC_COMMANDCODE_API_KEY"


def test_a_merge_target_is_never_created_at_startup(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"

    _merge_publisher(path).ensure_exists(_runtime())

    assert not path.exists()


# ------------------------------------------------------------------ TOML target


def test_a_toml_catalogue_is_written_as_toml_with_its_credentials_resolved(
    tmp_path: Path,
) -> None:
    """The one generated document that is not JSON, and the one that holds a key.

    The server writes it as well as the launcher, so this is where the
    substitution has to hold: a sentinel that survived to disk would be a
    config that parses and cannot authenticate.
    """

    path = tmp_path / "kimi-code-config.toml"
    path.write_text("providers = {}\n", encoding="utf-8")
    publisher = HarnessCatalogueFanoutPublisher({"kimi_code": path})

    publisher.publish(_runtime({"nvidia_nim/configured": 300_000}))

    document = tomllib.loads(path.read_text(encoding="utf-8"))
    assert document["providers"]["mcc"]["type"] == "anthropic"
    assert not document["providers"]["mcc"]["base_url"].endswith("/v1")
    assert document["providers"]["mcc"]["base_url"].startswith("http")
    assert "base-url.mcc.invalid" not in path.read_text(encoding="utf-8")
    assert document["providers"]["mcc"]["api_key"] == PROXY_NO_AUTH_SENTINEL
    models = document["models"]
    assert sorted(models) == [
        "mcc/nvidia_nim/configured",
        "mcc/open_router/discovered",
    ]
    assert models["mcc/nvidia_nim/configured"]["max_context_size"] == 300_000


def test_a_toml_catalogue_is_created_as_toml_when_it_is_missing(
    tmp_path: Path,
) -> None:
    """Kimi's document is the one that is not JSON, so creation is tested twice.

    A creation path that only ever ran for the JSON writer would leave exactly
    one harness paying a cold-start fetch, and it would be the harness whose
    document carries a literal credential.
    """

    path = tmp_path / "kimi-code-config.toml"

    HarnessCatalogueFanoutPublisher({"kimi_code": path}).ensure_exists(_runtime())

    document = tomllib.loads(path.read_text(encoding="utf-8"))
    assert document["providers"]["mcc"]["type"] == "anthropic"


# -------------------------------------------------------- harness attribution


def test_every_published_document_resolves_the_harness_id_sentinel(
    tmp_path: Path,
) -> None:
    """No ``{{...}}`` reaches disk, and each file names its own harness.

    The three OpenCode-family harnesses are the case this exists for: they
    share two serialisers between them, so the id in the file can only come
    from the ``HarnessSpec`` this publisher is holding.
    """

    paths = {
        spec.id: tmp_path / f"{spec.id}{Path(spec.catalogue.filename).suffix}"
        for spec in harness_specs()
        if spec.catalogue is not None
        and spec.catalogue.filename is not None
        and spec.catalogue.merge is None
    }

    HarnessCatalogueFanoutPublisher(paths).publish(_runtime())

    for harness_id, path in paths.items():
        text = path.read_text(encoding="utf-8")
        assert MCC_HARNESS_ID_SENTINEL not in text, harness_id
        assert "{{" not in text, harness_id
        if HARNESS_HEADER in text:
            document = (
                tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
            )
            assert _harness_header_values(document) == {harness_id}, harness_id

    # The three that share a serialiser each got their own id, not one id three
    # times, which is the failure the sentinel exists to make impossible.
    family = {
        harness_id: _harness_header_values(
            json.loads(paths[harness_id].read_text(encoding="utf-8"))
        )
        for harness_id in ("opencode", "opencode2", "kilo")
    }
    assert family == {
        "opencode": {"opencode"},
        "opencode2": {"opencode2"},
        "kilo": {"kilo"},
    }


def test_a_merged_block_carries_the_resolved_harness_id(tmp_path: Path) -> None:
    """The merge path resolves it too -- the substitution is before the branch."""

    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps({"provider": {"mcc": {"models": {"stale/model": {}}}}}),
        encoding="utf-8",
    )

    _merge_publisher(path).publish(_runtime())

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["provider"]["mcc"]["headers"] == {HARNESS_HEADER: "commandcode_cli"}


def _harness_header_values(node: object) -> set[str]:
    """Return every value bound to the attribution header, at any depth."""

    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() == HARNESS_HEADER:
                found.add(str(value))
            found |= _harness_header_values(value)
    elif isinstance(node, list):
        for item in node:
            found |= _harness_header_values(item)
    return found
