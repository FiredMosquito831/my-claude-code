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

import os
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from my_claude_code.application.catalogue_model import (
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.catalogues import serialise
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.config.atomic_json import write_json_document_atomically_if_changed
from my_claude_code.config.harness_config_merge import (
    merge_config_path,
    merge_owned_block,
    owned_block,
    owned_block_present,
    with_base_url,
)
from my_claude_code.config.harnesses import HarnessSpec, harness_specs
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.server_urls import local_proxy_root_url


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
            self._publish_one(spec, path, models, proxy_root_url)

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
            else:
                write_json_document_atomically_if_changed(path, document)
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
