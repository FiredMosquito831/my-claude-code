"""Serialise MCC's routes into Claude Desktop's ``inferenceModels`` array.

Claude Desktop's gateway mode fills its model picker from one of two places:
``modelDiscoveryEnabled`` makes it call ``GET /v1/models?limit=1000`` at
startup, or the configuration entry lists the models itself under
``inferenceModels``. Until 6.67.0 MCC wrote neither -- it set discovery on and
listed nothing -- so the picker's contents depended on a network call
completing before the user opened the menu. 6.67.0 turned discovery off and
listed the five ``mcc/*`` tier aliases; 7.3.0 lists the five ``claude-*``
names the user's own proven-working entry carries, from a declared table.

**The element shape is the user's working entry's, key for key**::

    {"name", "labelOverride", "supports1m", "prefer1m",
     "anthropicFamilyTier", "isFamilyDefault"}

**Why the names changed, and why this is now a constant.** Claude Desktop
1.52386.0.0 runs every ``name`` through a filter that accepts only a name
matching ``^(sonnet|opus|haiku|fable|mythos)(-[\\d.]+)?$`` or containing one of
``claude``/``sonnet``/``opus``/``haiku``/``fable``/``mythos``/``anthropic``,
minus a foreign-vendor denylist. ``mcc/best`` and its siblings satisfy none of
it: every entry MCC wrote was reported to the app's validation UI as *"is not
an Anthropic model and was removed from the list"*, and the list survived only
because the filter refuses to empty a list entirely. The rule is vendored at
``tests/fixtures/app_rules/claude-desktop-1.52386.0.0.json`` and every name
this module emits is run through it in CI.

The second change is that the array no longer depends on the live catalogue.
It used to be built by walking ``build_catalogue_models(...)``, which returns
nothing at all when ``settings.harness_tier_aliases`` is off or the provider
cache is cold -- so the picker of an app configured with discovery *off* could
still come up empty, which is the exact failure 6.67.0 set out to remove. The
five names are constants, so they are written as constants:
:data:`~my_claude_code.core.tier_refs.CLAUDE_DESKTOP_TIER_MODELS` is the one
declared table, in ``TIER_ORDER``. ``models`` is still accepted so the
``SIDECAR_SERIALISERS`` signature stays uniform, and is deliberately unused.

``supports1m`` and ``prefer1m`` are asserted rather than derived for the same
reason: they were read off ``context_length`` of whatever model the tier
happened to resolve to that minute, so a cold cache advertised every route as
a 200k model. The user's entry asserts both on all five and the app loads it.
The consequence of an over-claim is a menu label -- ``prefer1m`` does nothing
without ``supports1m``, and MCC still clamps context server-side.

``anthropicFamilyTier`` and ``isFamilyDefault`` are load-bearing rather than
decorative: the app pins one ``ANTHROPIC_DEFAULT_<TIER>_MODEL`` per tier from
the entry flagged ``isFamilyDefault``, and warns when two entries share a
tier. One entry per family, one default each.
"""

from collections.abc import Iterable
from typing import Any

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.core.tier_refs import (
    CLAUDE_DESKTOP_TIER_MODELS,
    TIER_ORDER,
)


def build_claude_desktop_models(models: Iterable[CatalogueModel]) -> list[Any]:
    """Return the ``inferenceModels`` array Claude Desktop's picker reads.

    A pure function of :data:`CLAUDE_DESKTOP_TIER_MODELS`: five entries, in
    ``TIER_ORDER``, whatever the catalogue currently holds.
    """

    del models  # The array is a constant; see the module docstring.
    return [
        {
            "name": entry.name,
            "labelOverride": entry.label,
            "supports1m": True,
            "prefer1m": True,
            "anthropicFamilyTier": entry.family_tier,
            "isFamilyDefault": True,
        }
        for tier in TIER_ORDER
        if (entry := CLAUDE_DESKTOP_TIER_MODELS.get(tier)) is not None
    ]
