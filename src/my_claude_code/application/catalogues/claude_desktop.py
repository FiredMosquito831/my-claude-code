"""Serialise MCC's routes into Claude Desktop's ``inferenceModels`` array.

Claude Desktop's gateway mode fills its model picker from one of two places:
``modelDiscoveryEnabled`` makes it call ``GET /v1/models?limit=1000`` at
startup, or the configuration entry lists the models itself under
``inferenceModels``. Until 6.67.0 MCC wrote neither -- it set discovery on and
listed nothing -- so the picker's contents depended on a network call
completing before the user opened the menu, and on
``settings.harness_tier_aliases`` being on, since that is what puts the five
``mcc/*`` refs in the models payload at all
(``api/model_catalog.py``). The working configuration the user built by hand,
read from a backup on 2026-09-10, does the opposite: discovery **off** and the
five models named explicitly. This module produces that array.

**The element shape is the user's working entry's, key for key**::

    {"name", "labelOverride", "supports1m", "prefer1m",
     "anthropicFamilyTier", "isFamilyDefault"}

**Every value is MCC's own answer, and none of them is invented.** ``name`` is
the wire id MCC actually routes -- ``mcc/best`` and its four siblings, from
``core/tier_refs`` -- rather than a ``claude-…`` display name MCC does not
advertise. ``anthropicFamilyTier`` and ``isFamilyDefault`` come from
:data:`~my_claude_code.core.tier_refs.TIER_FAMILY_TIERS`, the same table
``GET /v1/models`` answers with, so the picker and the models payload cannot
disagree. ``supports1m`` is *derived* from the ladder's own context length for
the route rather than asserted: a route whose model publishes no context length
says ``false``, because "nobody said" is not "a million".

``labelOverride`` is the tier's label. The route's primary ref is deliberately
not appended to it the way the ``/v1/models`` ``display_name`` does: that
payload is read by a machine, and this string is a menu item.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.core.tier_refs import (
    TIER_FAMILY_TIERS,
    TIER_LABELS,
    parse_tier_ref,
)

#: The context length at or above which a route is offered as a 1M-context
#: model. Claude Desktop's own switch is a boolean, so the threshold has to
#: live somewhere; it lives here, next to the only key that reads it.
ONE_MILLION_CONTEXT = 1_000_000


def build_claude_desktop_models(models: Iterable[CatalogueModel]) -> list[Any]:
    """Return the ``inferenceModels`` array for MCC's five tier routes.

    Only the tier aliases. A gateway that listed every routable
    ``provider/model`` here would hand the user a picker of a hundred entries
    whose names mean nothing to Claude Desktop's Claude-family filtering, and
    the tiers are what MCC's own documentation tells a reader to select.
    """

    entries: list[Any] = []
    seen: set[str] = set()
    for model in models:
        tier = parse_tier_ref(model.provider_model_ref)
        if tier is None or model.provider_model_ref in seen:
            continue
        seen.add(model.provider_model_ref)
        family_tier, family_default = TIER_FAMILY_TIERS[tier]
        supports_1m = (
            model.context_length is not None
            and model.context_length >= ONE_MILLION_CONTEXT
        )
        entries.append(
            {
                "name": model.provider_model_ref,
                "labelOverride": TIER_LABELS[tier],
                "supports1m": supports_1m,
                "prefer1m": supports_1m,
                "anthropicFamilyTier": family_tier,
                "isFamilyDefault": family_default,
            }
        )
    return entries
