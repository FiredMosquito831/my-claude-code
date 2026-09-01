"""Turn a host's own effort vocabulary into the dialect a profile declares.

A static provider whose gateway takes ``max`` says so by writing an
``EffortValues`` table in :mod:`profiles`. A custom provider cannot write a
profile, so its vocabulary is probed at runtime, stored on the registry entry,
and arrives here as plain words. This module is the *only* new thing between
those words and the wire: it builds the same :class:`NamedEffortReasoning` a
static profile would have declared, and hands it to the profile through
``dataclasses.replace``.

Nothing downstream is aware of the difference. Gating still intersects the
model's vocabulary with the host's, ``adapt_reasoning_policy`` still records
the clamp it makes, and the encoder still owns the wire. The learned dialect
only changes what the host is *known to be able to spell* -- which is exactly
the fact the generic profile was missing, and the reason a request for ``max``
against a host that documents ``max`` was going out as ``high``.
"""

import dataclasses

from my_claude_code.config.reasoning_enum import (
    OFF_EFFORT_WORDS,
    normalize_effort_words,
)
from my_claude_code.core.reasoning import (
    EFFORT_BY_VALUE,
    ReasoningDialectOrigin,
    ReasoningEffort,
    nearest_effort,
)

from .profiles import GENERIC_OPENAI_PROFILE, OpenAIChatProfile
from .reasoning import EffortValues, NamedEffortReasoning


def learned_effort_values(words: tuple[str, ...]) -> EffortValues:
    """Map every FCC rung onto one of ``words``.

    Two cases, because a host either speaks FCC's words or it does not.

    When every word is an FCC rung the mapping is the ordinary one -- nearest
    rung at or below, the same rule :func:`nearest_effort` applies everywhere
    else -- so ``{low, high, max}`` sends ``low`` for ``medium`` and ``max``
    for ``max``.

    When the words are the host's own (``brief``, ``detailed``) there is no
    shared scale to be nearest on, so the six rungs are spread across the list
    in the order the host named them. That order is the only ranking on offer,
    and an enum in a 400 is written low-to-high in every message seen so far.
    """
    if not words:
        return ()
    rungs = tuple(ReasoningEffort)
    known = {word: EFFORT_BY_VALUE[word] for word in words if word in EFFORT_BY_VALUE}
    if len(known) == len(words):
        supported = frozenset(known.values())
        return tuple((rung, nearest_effort(rung, supported).value) for rung in rungs)
    count = len(words)
    return tuple(
        (rung, words[min(count - 1, index * count // len(rungs))])
        for index, rung in enumerate(rungs)
    )


def learned_named_effort_reasoning(
    words: tuple[str, ...],
) -> NamedEffortReasoning | None:
    """Build the encoder for a probed vocabulary, or ``None`` if unusable.

    An OFF word in the enum ("none", "off") is not a rung -- it is the host
    telling us how to spell OFF, which the generic profile deliberately never
    assumes. It becomes ``disabled_value`` and leaves the effort scale.
    """
    cleaned = normalize_effort_words(words)
    disabled = next((word for word in cleaned if word in OFF_EFFORT_WORDS), None)
    scale = tuple(word for word in cleaned if word not in OFF_EFFORT_WORDS)
    if not scale:
        return None
    return NamedEffortReasoning(
        learned_effort_values(scale),
        disabled_value=disabled,
        origin=ReasoningDialectOrigin.LEARNED,
    )


def profile_with_learned_dialect(
    profile: OpenAIChatProfile, words: tuple[str, ...]
) -> OpenAIChatProfile:
    """Return ``profile`` speaking ``words``, or ``profile`` unchanged.

    The declaration seam, and the whole of it: one ``dataclasses.replace`` of
    the ``reasoning`` field a static profile sets literally.
    """
    encoder = learned_named_effort_reasoning(words)
    if encoder is None:
        return profile
    return dataclasses.replace(profile, reasoning=encoder)


def generic_profile_for(words: tuple[str, ...]) -> OpenAIChatProfile:
    """Return the custom-provider profile, widened by ``words`` if any."""
    return profile_with_learned_dialect(GENERIC_OPENAI_PROFILE, words)
