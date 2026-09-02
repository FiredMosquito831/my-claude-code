"""Load the captured live catalogue payload back into ``CatalogueModel``s.

``catalogue_models_live_shaped.json`` is a verbatim capture of the ``models``
array of ``GET /admin/api/catalogue-models`` from a real running install --
270 variant records over 7 providers, 126 of them with no
``supported_parameters`` at all and a realistic spread of unknowns. It carries
no credentials and no URLs; it is model metadata only.

It exists because every serialiser fixture before it was one or two models
with hand-set capabilities, and the shape that actually broke a launch -- a
gateway that publishes no parameter list, no context window and no price --
had never been serialised inside a test. Values are deliberately *not*
refreshed to match whatever the ladder resolves today: the point of the
fixture is the shape of the unknowns, and a capture that keeps its unknowns
keeps exercising the paths a healthier one would skip.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.core.reasoning import EFFORT_BY_VALUE

LIVE_PAYLOAD_PATH = Path(__file__).with_name("catalogue_models_live_shaped.json")


def live_catalogue_models() -> tuple[CatalogueModel, ...]:
    """Return the captured records as neutral catalogue records."""

    payload = json.loads(LIVE_PAYLOAD_PATH.read_text(encoding="utf-8"))
    return tuple(_record(entry) for entry in payload["models"])


def _record(entry: dict[str, Any]) -> CatalogueModel:
    supported = entry.get("supported_parameters")
    defaults = entry.get("default_parameters")
    return CatalogueModel(
        gateway_id=entry["gateway_id"],
        provider_model_ref=entry["provider_model_ref"],
        display_name=entry["display_name"],
        force_no_thinking=bool(entry["force_no_thinking"]),
        context_length=entry.get("context_length"),
        max_output_tokens=entry.get("max_output_tokens"),
        supports_vision=entry.get("supports_vision"),
        supports_tool_calls=entry.get("supports_tool_calls"),
        reasoning=_reasoning(entry.get("reasoning")),
        input_price=entry.get("input_price"),
        output_price=entry.get("output_price"),
        cache_read_price=entry.get("cache_read_price"),
        cache_write_price=entry.get("cache_write_price"),
        supported_parameters=None if supported is None else frozenset(supported),
        default_parameters=(
            None
            if defaults is None
            else tuple((str(name), value) for name, value in defaults)
        ),
    )


def _reasoning(payload: dict[str, Any] | None) -> ModelReasoningCapability | None:
    if payload is None:
        return None
    efforts = payload.get("supported_efforts")
    return ModelReasoningCapability(
        can_reason=payload.get("can_reason"),
        supports_effort_control=payload.get("supports_effort_control"),
        supports_toggle_control=payload.get("supports_toggle_control"),
        supports_budget_control=payload.get("supports_budget_control"),
        supported_efforts=(
            None
            if efforts is None
            else frozenset(
                EFFORT_BY_VALUE[value] for value in efforts if value in EFFORT_BY_VALUE
            )
        ),
        mandatory=payload.get("mandatory"),
        default_enabled=payload.get("default_enabled"),
    )
