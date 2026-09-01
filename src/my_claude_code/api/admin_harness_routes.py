"""Coding-agent harness routes: what is installed, and what MCC tells it.

Two loopback-only routes.

``GET /admin/api/harnesses`` backs the Coding agents dashboard page: one card
per registered harness with an installed / not-installed probe, the command to
copy, the protocol it will speak, the catalogue MCC generates for it and the
RTK toggle. It is deliberately a probe and nothing more -- MCC never installs a
third-party CLI, so a "not installed" card offers the vendor's own install line
and stops there.

``GET /admin/api/catalogue-models`` is the capability-bearing model list.
``GET /v1/models`` cannot serve this purpose: it is an Anthropic-compatible
protocol payload consumed by Claude Code itself and its ``ModelResponse``
carries an id, a display name and no capability fields whatsoever. Extending it
would risk client-side breakage for a purely internal need. So the launchers --
which run in their own processes and have no ``RequestRuntimePort`` -- fetch
this route instead, and get both the neutral records and each harness's
already-serialised document, so a launcher never has to reimplement a mapping
the server already owns.
"""

import asyncio
import json
import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from my_claude_code.api.admin_routes import require_loopback_admin
from my_claude_code.api.dependencies import get_services
from my_claude_code.api.model_admin import capability_payload
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.catalogue_model import (
    CatalogueFieldProvenance,
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.catalogues import model_entries, serialise
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.atomic_json import json_document_bytes
from my_claude_code.config.harness_config_merge import merge_config_path
from my_claude_code.config.harnesses import (
    PROTOCOL_LABELS,
    HarnessSpec,
    harness_command_lines,
    harness_specs,
)
from my_claude_code.config.paths import harness_catalogue_path
from my_claude_code.config.rtk import load_rtk_state

router = APIRouter()


@router.get("/admin/api/harnesses")
async def list_harnesses(request: Request):
    """Return every registered coding-agent harness and its local state."""

    require_loopback_admin(request)
    return await asyncio.to_thread(_harness_payload)


@router.get("/admin/api/catalogue-models")
async def list_catalogue_models(
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Return the ladder's capabilities per model, plus each CLI's document."""

    require_loopback_admin(request)
    return await asyncio.to_thread(_catalogue_models_payload, services)


# ------------------------------------------------------------------ harnesses


def _harness_payload() -> dict[str, Any]:
    rtk = load_rtk_state()
    return {
        "harnesses": [
            _harness_entry(spec, rtk.enabled(spec.id)) for spec in harness_specs()
        ]
    }


def _harness_entry(spec: HarnessSpec, rtk_enabled: bool) -> dict[str, Any]:
    binary_path = shutil.which(spec.binary)
    return {
        "id": spec.id,
        "display_name": spec.display_name,
        "binary": spec.binary,
        "installed": binary_path is not None,
        "binary_path": binary_path,
        "install_hint": spec.install_hint,
        "command": spec.command,
        "commands": [entry.command for entry in spec.commands],
        # Every command line a user can type for this agent, generated from
        # the registry so the page cannot list fewer than the shims that were
        # installed. The dashboard renders one copyable row per entry.
        "command_lines": [
            {"command": line.command, "help": line.help_text, "kind": line.kind}
            for line in harness_command_lines(spec)
        ],
        "protocol": str(spec.protocol),
        "protocol_label": PROTOCOL_LABELS[spec.protocol],
        "summary": spec.summary,
        "rtk_agent": spec.rtk_agent,
        "rtk_enabled": rtk_enabled,
        "catalogue": _catalogue_entry(spec),
    }


def _catalogue_entry(spec: HarnessSpec) -> dict[str, Any] | None:
    catalogue = spec.catalogue
    if catalogue is None:
        return None
    if catalogue.delivery == "process_local":
        return {
            "format": catalogue.format_id,
            "config_env_var": None,
            "merged_key": None,
            "delivery": "process_local",
            "path": None,
            "exists": True,
            "updated_at": None,
            "model_count": None,
            "defaulted_model_count": None,
        }

    merge = catalogue.merge
    if merge is not None:
        path = merge_config_path(merge, os.environ)
    else:
        # ``delivery`` narrows this to a file catalogue, which always names one.
        path = harness_catalogue_path(catalogue.filename or "")
    entry: dict[str, Any] = {
        "format": catalogue.format_id,
        # The CLI's own variable naming an extra config file. Its presence is
        # what lets MCC own a document instead of editing the user's.
        "config_env_var": catalogue.config_env_var,
        # Set instead when the CLI reads only its own document: the one key
        # MCC writes into a file it does not own.
        "merged_key": merge.owned_key_label if merge is not None else None,
        "delivery": catalogue.delivery,
        "path": str(path),
        "exists": False,
        "updated_at": None,
        "model_count": None,
        "defaulted_model_count": None,
    }
    document = _read_catalogue(path)
    if document is None:
        return entry
    if merge is not None:
        # The file is the user's, so its existence says nothing about whether
        # MCC has ever been launched for this harness. Only MCC's own key does.
        block = _merged_block(document, merge.owned_key_path)
        if block is None:
            return entry
        document = {**document, "_mcc_defaulted": block.get("_mcc_defaulted")}
    entry["exists"] = True
    entry["updated_at"] = _mtime_iso(path)
    entry["model_count"] = len(model_entries(catalogue.format_id, document))
    defaulted = document.get("_mcc_defaulted")
    entry["defaulted_model_count"] = (
        len(defaulted) if isinstance(defaulted, dict) else 0
    )
    return entry


def _merged_block(
    document: Mapping[str, Any], owned_key_path: tuple[str, ...]
) -> Mapping[str, Any] | None:
    node: Any = document
    for key in owned_key_path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node if isinstance(node, Mapping) else None


def _read_catalogue(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _mtime_iso(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------- catalogue models


def _catalogue_models_payload(services: ApiServices) -> dict[str, Any]:
    runtime = services.requests
    models = build_catalogue_models(
        runtime.current_settings(), runtime, provenance=capability_provenance
    )
    catalogues: dict[str, Any] = {}
    for spec in harness_specs():
        catalogue = spec.catalogue
        if catalogue is None:
            continue
        document, defaulted = serialise(catalogue.format_id, models)
        catalogues[spec.id] = {
            "format": catalogue.format_id,
            "filename": catalogue.filename,
            "document": document,
            "defaulted": defaulted.as_document(),
            "model_count": len(model_entries(catalogue.format_id, document)),
            "byte_length": len(json_document_bytes(document)),
        }
    return {
        "models": [_model_payload(model) for model in models],
        "catalogues": catalogues,
    }


def capability_provenance(
    provider_id: str, model_id: str, info: ProviderModelInfo | None
) -> Mapping[str, CatalogueFieldProvenance]:
    """Return the per-field ladder provenance the Models page already computes.

    Reusing the admin capability inspector rather than re-deriving the tiers is
    the point: a number in a generated catalogue and the same number on the
    Models page must never be able to disagree about where it came from.
    """

    payload = capability_payload(provider_id, model_id, info)
    provenance: dict[str, CatalogueFieldProvenance] = {}
    for name, value in payload.items():
        if name == "reasoning" and isinstance(value, Mapping):
            for reasoning_name, reasoning_value in value.items():
                if isinstance(reasoning_value, Mapping):
                    provenance[f"reasoning.{reasoning_name}"] = _provenance(
                        reasoning_value
                    )
            continue
        if isinstance(value, Mapping) and "source" in value:
            provenance[name] = _provenance(value)
    return provenance


def _provenance(field: Mapping[str, Any]) -> CatalogueFieldProvenance:
    return CatalogueFieldProvenance(
        source=str(field.get("source")),
        source_label=str(field.get("source_label")),
        tier=field.get("tier"),
        tier_label=field.get("tier_label"),
        approximate=bool(field.get("approximate")),
    )


def _model_payload(model: CatalogueModel) -> dict[str, Any]:
    return {
        "gateway_id": model.gateway_id,
        "provider_model_ref": model.provider_model_ref,
        "provider_id": model.provider_id,
        "display_name": model.display_name,
        "force_no_thinking": model.force_no_thinking,
        "context_length": model.context_length,
        "max_output_tokens": model.max_output_tokens,
        "supports_vision": model.supports_vision,
        "supports_tool_calls": model.supports_tool_calls,
        "input_price": model.input_price,
        "output_price": model.output_price,
        "supported_parameters": (
            None
            if model.supported_parameters is None
            else sorted(model.supported_parameters)
        ),
        "default_parameters": (
            None
            if model.default_parameters is None
            else [list(pair) for pair in model.default_parameters]
        ),
        "reasoning": _reasoning_payload(model),
        "provenance": {
            name: {
                "source": entry.source,
                "source_label": entry.source_label,
                "tier": entry.tier,
                "tier_label": entry.tier_label,
                "approximate": entry.approximate,
            }
            for name, entry in model.field_provenance.items()
        },
    }


def _reasoning_payload(model: CatalogueModel) -> dict[str, Any] | None:
    reasoning = model.reasoning
    if reasoning is None:
        return None
    return {
        "can_reason": reasoning.can_reason,
        "supports_effort_control": reasoning.supports_effort_control,
        "supports_toggle_control": reasoning.supports_toggle_control,
        "supports_budget_control": reasoning.supports_budget_control,
        "supported_efforts": (
            None
            if reasoning.supported_efforts is None
            else sorted(effort.value for effort in reasoning.supported_efforts)
        ),
        "mandatory": reasoning.mandatory,
        "default_enabled": reasoning.default_enabled,
    }
