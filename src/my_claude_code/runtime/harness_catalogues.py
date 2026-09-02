"""Keep every materialised harness catalogue in step with the ladder.

Replaces the single Codex publisher. Driven by the same trigger --
``ProviderManager.cache_model_infos`` and ``warm_referenced_model_cache`` both
call ``_publish_model_catalog`` -- but fanned out over the harness registry, so
adding a harness with a catalogue file needs no change here.

Two rules shape it.

**Only refresh what exists**, with one declared exception. A catalogue file is
created by that harness's own launcher, on the first ``mcc-<id>`` run, because
writing one for a CLI the user does not use would leave MCC's files behind for
a tool they never installed. The exception is a catalogue whose consumer has no
launcher at all: the Codex App reads ``~/.fcc/codex-model-catalog.json`` from a
persistent ``config.toml``, so nothing would ever create it. Those specs set
``created_at_startup`` and are the only ones :meth:`ensure_exists` may create.

A *merge* target is stricter still. Command Code reads only its own
``~/.commandcode/providers.json``, so MCC owns one key inside a document the
user wrote, and the file existing proves nothing about whether MCC was ever
invited into it. The test is therefore the presence of MCC's own key, never the
file: a ``provider.mcc`` block must never appear in someone's config because a
provider key rotated on a server they left running.

**One CLI's failure is one CLI's failure.** Each serialiser and each write is
isolated: a bug in one harness's mapping must not abort the others, and must
never take down the provider refresh that triggered it. The whole call is also
wrapped by ``_run_model_catalog_publication``'s swallow-and-log, which stays.

Because the records come from the resolution ladder rather than from
``/v1/models``, a capability change with no change to the model list re-emits
every catalogue -- which was not true of the old publisher and is the point of
the exercise.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from my_claude_code.application.catalogue_model import (
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.catalogues import serialise, serialise_sidecar
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.config.atomic_json import write_json_document_atomically_if_changed
from my_claude_code.config.harness_base_url import with_root_base_url, with_v1_base_url
from my_claude_code.config.harness_cline import (
    strip_mcc_keys,
    with_api_key,
    with_selected_model,
)
from my_claude_code.config.harness_config_merge import (
    merge_config_path,
    merge_owned_block,
    owned_block,
    owned_block_present,
    with_base_url,
)
from my_claude_code.config.harness_toml import (
    with_kimi_credentials,
    write_toml_document_atomically_if_changed,
)
from my_claude_code.config.harnesses import (
    CLINE_PROVIDER_ID,
    HarnessCatalogue,
    HarnessSpec,
    harness_specs,
)
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.proxy_auth import proxy_auth_token
from my_claude_code.config.server_urls import local_proxy_root_url
from my_claude_code.config.settings import Settings


class HarnessCatalogueFanoutPublisher:
    """Refresh every already-materialised harness catalogue from the ladder."""

    def __init__(self, catalogue_paths: Mapping[str, Path] | None = None) -> None:
        #: Per-harness path override, for tests. Production resolves through
        #: ``harness_catalogue_path`` so every generated file stays under
        #: ``~/.fcc``.
        self._catalogue_paths = dict(catalogue_paths or {})

    def ensure_exists(self, runtime: RequestRuntimePort) -> None:
        """Refresh at startup, creating only the declared server-owned files."""

        self._publish(runtime, create_missing=True)

    def publish(self, runtime: RequestRuntimePort) -> None:
        """Rewrite every materialised catalogue from the current ladder state."""

        self._publish(runtime, create_missing=False)

    def _publish(self, runtime: RequestRuntimePort, *, create_missing: bool) -> None:
        targets = [
            (spec, path)
            for spec in harness_specs()
            if (path := self._path_for(spec)) is not None
            and self._is_materialised(spec, path, create_missing=create_missing)
        ]
        if not targets:
            return

        settings = runtime.current_settings()
        models = build_catalogue_models(settings, runtime)
        if not models:
            # Preserve every last-known-good file rather than replacing them
            # with an empty picker during a provider outage.
            raise ValueError("Harness catalogues contain no routable models.")

        proxy_root_url = local_proxy_root_url(settings)
        for spec, path in targets:
            self._publish_one(spec, path, models, proxy_root_url, settings)

    def _is_materialised(
        self, spec: HarnessSpec, path: Path, *, create_missing: bool
    ) -> bool:
        """Return whether MCC has already written this harness's configuration.

        For a file MCC owns, its existence is the whole answer. For a merge
        target the file belongs to the *user*, so its existence proves
        nothing: a Command Code user who has never run ``mcc-commandcode``
        already has a ``providers.json``, and finding a ``provider.mcc`` block
        appear in it because an unrelated provider key rotated would be
        exactly the behaviour the never-write-for-an-unlaunched-harness rule
        exists to prevent.
        """

        catalogue = spec.catalogue
        if catalogue is None:
            return False
        if catalogue.merge is not None:
            return owned_block_present(path, catalogue.merge.owned_key_path)
        return path.exists() or (create_missing and catalogue.created_at_startup)

    def _publish_one(
        self,
        spec: HarnessSpec,
        path: Path,
        models: tuple[CatalogueModel, ...],
        proxy_root_url: str,
        settings: Settings,
    ) -> None:
        catalogue = spec.catalogue
        if catalogue is None:
            return
        try:
            document, defaulted = serialise(catalogue.format_id, models)
            if catalogue.merge is not None:
                merge_owned_block(
                    path=path,
                    owned_key_path=catalogue.merge.owned_key_path,
                    block=with_base_url(
                        owned_block(document, catalogue.merge.owned_key_path),
                        proxy_root_url,
                    ),
                    backup_suffix=catalogue.merge.backup_suffix,
                )
            elif catalogue.document_format == "toml":
                # Kimi Code's ``config.toml`` is the one generated document
                # that is not JSON, and the one whose provider block carries
                # the proxy token literally: its schema has no ``"$VAR"``
                # form and its environment overrides skip the ``anthropic``
                # provider type entirely. The file is MCC's own, under
                # ``~/.fcc`` beside the ``.env`` that already holds the same
                # value; nothing is written into a file the user owns.
                write_toml_document_atomically_if_changed(
                    path,
                    with_kimi_credentials(
                        document,
                        proxy_root_url=proxy_root_url,
                        api_key=proxy_auth_token(settings.anthropic_auth_token),
                    ),
                )
            else:
                if catalogue.base_url_sentinel is not None:
                    # Qwen Code, Crush and Droid substitute nothing of their
                    # own into a base-URL field, and the document is MCC's own
                    # file rather than one key inside the user's, so the real
                    # proxy URL is written wherever the sentinel sits. Which
                    # shape it takes is the harness's own: an Anthropic SDK
                    # appends ``/v1/messages`` to a root, an OpenAI one
                    # appends ``chat/completions`` to ``<root>/v1``.
                    resolve = (
                        with_v1_base_url
                        if catalogue.base_url_shape == "v1"
                        else with_root_base_url
                    )
                    document = resolve(
                        document, catalogue.base_url_sentinel, proxy_root_url
                    )
                if spec.id == CLINE_HARNESS_ID:
                    # Cline stores a literal key and carries the selected
                    # model's limits in the provider block. A background
                    # refresh cannot know which model the next session will
                    # ask for, so it preserves the one already on disk.
                    document = with_api_key(
                        document,
                        CLINE_PROVIDER_ID,
                        proxy_auth_token(settings.anthropic_auth_token),
                    )
                    document = with_selected_model(
                        document, CLINE_PROVIDER_ID, _selected_model_on_disk(path)
                    )
                    # Cline discards the whole file on any unrecognised root
                    # key, so MCC's bookkeeping never reaches disk.
                    document = strip_mcc_keys(document)
                write_json_document_atomically_if_changed(path, document)
                self._publish_sidecar(catalogue, models)
        except Exception as exc:
            logger.warning(
                "Harness catalogue publication failed: harness={} exc_type={}",
                spec.id,
                type(exc).__name__,
            )
            return
        if defaulted.model_count:
            logger.debug(
                "Harness catalogue published with CLI defaults: harness={} models={}",
                spec.id,
                defaulted.model_count,
            )

    def _publish_sidecar(
        self, catalogue: HarnessCatalogue, models: tuple[CatalogueModel, ...]
    ) -> None:
        """Write the second document, for the one harness that reads two."""

        if catalogue.sidecar_filename is None:
            return
        sidecar = serialise_sidecar(catalogue.format_id, models)
        if sidecar is None:
            return
        write_json_document_atomically_if_changed(
            harness_catalogue_path(catalogue.sidecar_filename), sidecar
        )

    def _path_for(self, spec: HarnessSpec) -> Path | None:
        catalogue = spec.catalogue
        if catalogue is None:
            return None
        override = self._catalogue_paths.get(spec.id)
        if override is not None:
            return override
        if catalogue.merge is not None:
            return merge_config_path(catalogue.merge, os.environ)
        if catalogue.filename is None:
            return None
        return harness_catalogue_path(catalogue.filename)


#: The harness whose generated document carries both a literal credential and
#: a per-session model selection. Named here rather than imported as a
#: launcher constant because ``runtime`` may not depend on ``cli``.
CLINE_HARNESS_ID = "cline_cli"


def _selected_model_on_disk(path: Path) -> str | None:
    """Return the model the last ``mcc-cline`` launch selected, if any.

    A background refresh must not silently move a running session onto a
    different model's limits, so the model already in the file wins over the
    serialiser's "first routable" default.
    """

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(document, Mapping):
        return None
    providers = document.get("providers")
    if not isinstance(providers, Mapping):
        return None
    entry = providers.get(CLINE_PROVIDER_ID)
    if not isinstance(entry, Mapping):
        return None
    settings = entry.get("settings")
    if not isinstance(settings, Mapping):
        return None
    model = settings.get("model")
    return model if isinstance(model, str) and model else None
