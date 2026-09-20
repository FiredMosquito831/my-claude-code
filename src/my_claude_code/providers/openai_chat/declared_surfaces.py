"""Turn an operator's declared wire surfaces into the profile field for them.

The second half of the seam :mod:`learned_dialect` opened. A static provider
that fronts more than one API says so by writing ``response_surfaces`` in
:mod:`profiles` -- ``opencode`` and ``opencode_go`` are the two that do. A
custom provider cannot write a profile, so what its host serves is declared on
its registry entry by the person who configured it, arrives here as plain
words, and is handed to the generic profile through ``dataclasses.replace``.

Nothing downstream is aware of the difference. The resolver, the probe, the
learned ``response_surface`` fact and the Models page all read the profile
field, so a custom host that declares Responses is routed by exactly the code
that routes ``opencode`` there.

**A declaration of Chat Completions alone is not a declaration.** It resolves
to the empty tuple, which is what every profile but the two OpenCode ones
carries, and an empty tuple is the switch that keeps
``OpenAIChatProvider.stream_response`` on the single-surface path it has always
taken: no resolution, no label, no extra row in the request log. That is the
whole of "nothing changes for a provider that does not opt in", and it is one
line rather than a flag, because "one surface" and "no statement" really are
the same routing decision.
"""

import dataclasses
from collections.abc import Iterable

from my_claude_code.application.model_metadata import ResponseSurface

from .profiles import OpenAIChatProfile


def declared_surfaces(names: Iterable[str]) -> tuple[ResponseSurface, ...]:
    """The surfaces these words name, in the order MCC should try them.

    A word this build does not know is dropped rather than raised on: the
    registry already refuses an unknown surface at the form, so one reaching
    here came from a file written by a newer release, and the rest of that
    entry is still perfectly good.

    Chat Completions is kept first wherever it was declared, because it is the
    surface every OpenAI-compatible host is most likely to answer and trying
    the likely door first is what keeps the probe cheap.
    """

    wanted = tuple(names)
    return tuple(
        surface
        for surface in (
            ResponseSurface.CHAT_COMPLETIONS,
            ResponseSurface.RESPONSES,
            ResponseSurface.MESSAGES,
        )
        if surface.value in wanted
    )


def profile_with_declared_surfaces(
    profile: OpenAIChatProfile, names: Iterable[str]
) -> OpenAIChatProfile:
    """Return ``profile`` serving ``names``, or ``profile`` unchanged.

    Unchanged for the two cases that must stay byte-identical: an entry that
    declares nothing, and an entry that declares only Chat Completions.
    """

    # ``ResponseSurface.CHAT_COMPLETIONS`` rather than
    # ``response_surface.DEFAULT_SURFACE``, which is the same constant: reading
    # it from there would make this module and the resolver import each other,
    # and ``test_static_first_party_import_graph_is_acyclic`` counts a deferred
    # import as an edge like any other.
    surfaces = declared_surfaces(names)
    if not surfaces or surfaces == (ResponseSurface.CHAT_COMPLETIONS,):
        return profile
    return dataclasses.replace(profile, response_surfaces=surfaces)
