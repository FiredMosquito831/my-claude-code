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
    SUPER_EFFORT_WORDS,
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
    ladder = _ladder_with_super_rungs(words, known)
    if ladder is not None:
        return ladder
    count = len(words)
    return tuple(
        (rung, words[min(count - 1, index * count // len(rungs))])
        for index, rung in enumerate(rungs)
    )


def _ladder_with_super_rungs(
    words: tuple[str, ...], known: dict[str, ReasoningEffort]
) -> EffortValues | None:
    """MCC's own scale, plus rungs the host publishes *above* it.

    Codex CLI 0.154.0 spells ``minimal|low|medium|high|xhigh|max|ultra|
    persistent``: MCC's six rungs and two more on top. The positional spread
    below reads that as eight unknown words and scatters six rungs across them
    by index -- ``high`` lands on ``xhigh`` and ``xhigh`` on ``max``, which is
    a wrong answer for a vocabulary that contains MCC's own words verbatim.

    So: every rung the host names maps to **itself**, and ``max`` -- the
    client's word for "the most this model will do" -- becomes the host's own
    word for that.

    Which word, exactly, is decision Q7 of 2026-09-13: ``ultra`` when the
    vocabulary carries it, and ``persistent`` only when it is the vocabulary's
    **top** rung and there is no ``ultra``. ``ultra`` wins where both appear
    because it is unambiguously "more effort", while ``persistent`` reads as a
    mode rather than a rung -- and a client asking for the most effort has not
    asked to change modes. A host that names neither keeps ``max`` exactly as
    it always did.

    ``None`` when this is not that shape -- a host with its own words
    (``brief``, ``detailed``) has no shared scale and falls through to the
    spread, which is still the only ranking on offer there.
    """

    supers = [word for word in words if word in SUPER_EFFORT_WORDS]
    if not supers:
        return None
    if len(known) + len(supers) != len(words):
        # Some third kind of word is in the list, so this is not MCC's scale
        # with a top on it -- it is a vocabulary of the host's own that happens
        # to contain one familiar word. Do not pretend to rank it.
        return None
    supported = frozenset(known.values())
    if not supported:
        return None
    if "ultra" in supers:
        top = "ultra"
    elif supers[-1] == words[-1]:
        top = supers[-1]
    else:
        # A super-rung the host named but did not put on top is not the answer
        # to "the most this model will do", and MCC has no other use for it.
        return None
    return tuple(
        (
            rung,
            top
            if rung is ReasoningEffort.MAX
            else nearest_effort(rung, supported).value,
        )
        for rung in tuple(ReasoningEffort)
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
