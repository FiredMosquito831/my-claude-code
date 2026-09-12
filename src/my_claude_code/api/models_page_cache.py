"""The Models page, split into a half that changes rarely and one that does not.

`GET /admin/api/model-admin` is one 5.5 MB answer built from scratch on every
open. Measured on a copy of a real installation -- 12 providers, 1,170 models,
996 hide patterns -- it took 7.3 s even after the visibility matching was fixed,
and the remainder is the capability ladder: context length, output limit, prices,
vision and tool support resolved per model from the provider's own `/models`
answer, then models.dev, then the registry.

None of that moves unless the catalogue moves. What *does* move constantly is
the other quarter of the payload:

- ``reasoning_measured`` and ``image_estimate``, which come from the request log
  and change with every logged request;
- ``learned``, whose rows carry an age in seconds;
- ``catalogue_refresh``, which is two timestamps.

So the expensive half is computed with those four left out, stored under a key
made of its own inputs, and the four are merged back on every request. The merge
is not an approximation: it calls the same functions the one-shot build calls,
on the same data, so the assembled payload is the payload -- which is what
``test_the_merged_payload_equals_the_uncached_payload`` checks, field by field.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from my_claude_code.api.model_admin import (
    _image_estimate_row,
    attach_learned_facts,
    build_models_page_payload,
    learned_key,
    models_dev_cache_mark,
)
from my_claude_code.application.model_metadata import (
    ProviderModelInfo,
    canonical_model_info,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.model_refs import (
    ConfiguredChatModelRef,
    parse_model_name,
    parse_provider_type,
)
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.version import package_version

#: The entry name under ``<config dir>/cache/derived/``.
MODELS_PAGE_ENTRY = "models-page-capabilities"


def capability_half_key(
    model_infos: Sequence[ProviderModelInfo],
    configured: Sequence[ConfiguredChatModelRef],
    visibility: ModelVisibility,
    overrides: ModelParameterOverrides,
) -> str:
    """A digest of everything the cached half is built from.

    Every input of ``build_models_page_payload`` that is not merged back
    afterwards, plus the models.dev file and the running version -- the last so
    that an upgrade which changes how a capability is resolved never serves an
    answer computed by the release before it.

    The catalogue goes in whole rather than as a count: a provider that swaps a
    model's context length without changing how many models it publishes has
    changed this payload, and a key that could not see that would serve the old
    number forever.

    It goes in through :func:`canonical_model_info` rather than ``repr``.
    ``ProviderModelInfo`` carries two ``frozenset``s of strings, a set's
    ``repr`` lists its members in hash order, and CPython salts string hashing
    per process -- so a ``repr``-based key named a different catalogue on every
    start and this cache, whose entire purpose is to survive a restart, missed
    on every one of them.
    """

    digest = hashlib.sha256()
    digest.update(package_version().encode("utf-8"))
    digest.update(b"\x00models-dev\x00")
    digest.update(models_dev_cache_mark().encode("utf-8"))
    digest.update(b"\x00catalogue\x00")
    for info in model_infos:
        digest.update(canonical_model_info(info).encode("utf-8"))
        digest.update(b"\x00")
    digest.update(b"\x00configured\x00")
    for ref in configured:
        digest.update(f"{ref.model_ref}|{','.join(ref.sources)}".encode())
        digest.update(b"\x00")
    digest.update(b"\x00visibility\x00")
    digest.update(",".join(visibility.allow).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(",".join(visibility.deny).encode("utf-8"))
    digest.update(b"\x00overrides\x00")
    digest.update(
        json.dumps(overrides.as_document(), sort_keys=True, default=str).encode("utf-8")
    )
    return f"models-page-{digest.hexdigest()[:32]}"


def build_capability_half(
    model_infos: Sequence[ProviderModelInfo],
    configured: Sequence[ConfiguredChatModelRef],
    visibility: ModelVisibility,
    overrides: ModelParameterOverrides,
    *,
    dialect_lookup: Any,
    measured_days: int,
) -> dict[str, Any]:
    """The Models page with the four moving parts deliberately left out."""

    return build_models_page_payload(
        model_infos,
        configured,
        visibility,
        overrides,
        dialect_lookup=dialect_lookup,
        measured=None,
        measured_days=measured_days,
        learned=None,
        catalogue_refresh=None,
        image_estimates=None,
    )


def merge_moving_parts(
    payload: dict[str, Any],
    *,
    measured: Mapping[str, Mapping[str, Any]] | None,
    image_estimates: Mapping[str, Mapping[str, Any]] | None,
    learned: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    catalogue_refresh: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Put the four moving parts back, exactly where the one-shot build puts them.

    Mutates and returns ``payload``; callers pass a payload they own -- either
    one they just built or one freshly parsed out of the cache file.
    """

    payload["catalogue_refresh"] = dict(catalogue_refresh or {})
    for provider in payload.get("providers", []):
        provider_id = str(provider.get("provider_id", ""))
        provider["image_estimate"] = _image_estimate_row(image_estimates, provider_id)
        for model in provider.get("models", []):
            model_ref = str(model.get("model_ref", ""))
            row = None if measured is None else measured.get(model_ref)
            model["reasoning_measured"] = None if row is None else dict(row)
            # `attach_learned_facts` hangs each fact on the capability field it
            # narrows, which is a mutation of `capabilities` -- the same one the
            # one-shot build performs, on a capabilities dict that (like this
            # one) has not seen a fact yet.
            capabilities = model.get("capabilities")
            if isinstance(capabilities, dict):
                provider_key = parse_provider_type(model_ref)
                model_id = (
                    parse_model_name(model_ref) if "/" in model_ref else model_ref
                )
                facts = (
                    ()
                    if learned is None
                    else learned.get(learned_key(provider_key, model_id), ())
                )
                model["learned"] = attach_learned_facts(capabilities, facts)
    return payload
