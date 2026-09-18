"""Two refusals a strict Responses host states in its own words.

Both are read the way every other matcher in this package is read: off the
**structured** complaint :func:`~my_claude_code.providers.recovery.complaint.upstream_complaint`
produces, with the echo keys already pruned. That pruning is the whole
difference between a net and a hazard here. A validation error echoes the
submitted request back under ``input``/``body``/``ctx``, and this request
*contains* the word ``tool_choice`` and a 68-character tool ``name`` -- so a
matcher that read the error as one flat string would fire on every 400 the
host ever sent, and would answer an unrelated rejection by silently dropping
the client's forced tool.

Neither matcher is a model-name branch or a provider branch. A host that has
never refused anything is unaffected; a host that refuses is retried once and
remembered, so it pays the 400 once rather than once per request.

**Tool-name length.** OpenCode Zen's Muse Spark answers a 68-character name
with ``param: "name"`` / "``name`` must be at most 64 characters, got 68", and
the 64-character alias with a 200 (measured 2026-09-17). ``opencode`` and
``opencode_go`` therefore *declare* 64 and never reach this rung. Every other
Responses host is covered by the net: the first such refusal costs one retry.

**tool_choice.** The same model answers any ``tool_choice`` other than
``auto`` with ``param: "tool_choice"`` / "only ``\"auto\"`` is supported for
``tool_choice``; ``\"none\"``, ``\"required\"``, and named function choices are
not currently supported". The Responses default *is* ``auto``, so omitting the
field sends the same instruction the host will accept.
"""

import re
from typing import Any

from my_claude_code.core.anthropic.openai_tool_names import (
    MIN_TOOL_NAME_MAX_LENGTH,
    OPENAI_TOOL_NAME_MAX_LENGTH,
)

from .complaint import is_bad_request, upstream_complaint

#: "at most 64 characters", "maximum length of 64", "64 characters or fewer".
#: The number is what the host stated; the phrasings are the ones a strict
#: JSON-schema validator and a hand-written check both produce.
_NAME_LENGTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"at most (\d{1,4}) characters"),
    re.compile(r"maximum length of (\d{1,4})"),
    re.compile(r"(\d{1,4}) characters or (?:fewer|less)"),
    re.compile(r"longer than (\d{1,4}) characters"),
    re.compile(r"max(?:imum)?[ _]?length[\"']?\s*[:=]\s*(\d{1,4})"),
)

#: The complaint has to be *about* a length. Without this a 400 that named the
#: ``name`` param for an unrelated reason -- an unknown function, a bad
#: character -- would be answered by aliasing, which fixes nothing and hides
#: the real refusal behind a second failure.
_LENGTH_WORDS = re.compile(r"\b(?:characters?|length|long|longer)\b")

#: The parameter the host says it is complaining about. ``param`` is a
#: complaint key, so the value reaches the complaint string.
_NAME_PARAM = re.compile(r"\bname\b")
_TOOL_CHOICE_PARAM = re.compile(r"\btool_choice\b")

#: "only `"auto"` is supported for `tool_choice`". Quotes and backticks are
#: whatever the host felt like; the two words in that order are the claim.
_AUTO_ONLY = re.compile(r"only\s+[`\"']*auto[`\"']*\s+is\s+supported")

#: The longer, list-shaped spelling of the same statement: the host enumerates
#: what it will *not* take. Matched separately so that a host wording it the
#: other way round is still understood.
_AUTO_ONLY_LIST = re.compile(
    r"[`\"']*auto[`\"']*[^.]{0,80}?\bnot\s+(?:currently\s+)?supported"
)


def rejected_tool_name_max_length(error: Exception) -> int | None:
    """The tool-name ceiling this 400 states, or ``None`` if it states none.

    ``None`` means "not my kind of rejection" and the caller must fail
    visibly. A complaint that names the ``name`` parameter *and* talks about
    length but whose number cannot be read falls back to
    :data:`OPENAI_TOOL_NAME_MAX_LENGTH`: 64 is the OpenAI-documented ceiling
    every strict host in the fleet has turned out to mean, and an alias built
    for it is accepted by anything more generous.

    A number below :data:`MIN_TOOL_NAME_MAX_LENGTH` is refused rather than
    approximated. An alias that short is no longer a name, and inventing one
    would trade a visible failure for a tool the model cannot identify.
    """

    if not is_bad_request(error):
        return None
    complaint = upstream_complaint(error)
    if not _NAME_PARAM.search(complaint) or not _LENGTH_WORDS.search(complaint):
        return None
    if _TOOL_CHOICE_PARAM.search(complaint):
        # The host named a different parameter. ``tool_choice`` carries a
        # ``name`` of its own, so a length complaint about it is not a
        # statement about the tool catalogue.
        return None
    for pattern in _NAME_LENGTH_PATTERNS:
        match = pattern.search(complaint)
        if match is None:
            continue
        stated = int(match.group(1))
        if stated < MIN_TOOL_NAME_MAX_LENGTH:
            return None
        return stated
    return OPENAI_TOOL_NAME_MAX_LENGTH


def is_tool_choice_auto_only(error: Exception) -> bool:
    """Whether this 400 says the host takes no ``tool_choice`` but ``auto``.

    Three things have to be true together, and the conjunction is what keeps
    the net off every other ``tool_choice`` rejection -- an unknown function
    name, a choice naming a tool that is not in ``tools``, a malformed object.
    Those are real faults in the request and must keep failing visibly.
    """

    if not is_bad_request(error):
        return False
    complaint = upstream_complaint(error)
    if not _TOOL_CHOICE_PARAM.search(complaint):
        return False
    return bool(_AUTO_ONLY.search(complaint) or _AUTO_ONLY_LIST.search(complaint))


def body_forces_tool_choice(body: dict[str, Any]) -> bool:
    """Whether this body carries a ``tool_choice`` other than ``auto``.

    The second half of the rung's proof. Dropping a field the request never
    sent cannot be what fixes a 400, so a body without one is not retried at
    all -- the same rule ``clone_body_without_reasoning_field`` applies by
    returning ``None`` when it removed nothing.
    """

    choice = body.get("tool_choice")
    if choice is None:
        return False
    return choice != "auto"


def clone_body_without_tool_choice(body: dict[str, Any]) -> dict[str, Any] | None:
    """A shallow clone with ``tool_choice`` gone, or ``None`` if it was absent.

    Shallow is enough and deliberate: ``tool_choice`` is a top-level key, the
    rest of the body is handed straight back to the sender unmodified, and a
    deep copy of a full conversation on every retry would be paid for nothing.
    """

    if not body_forces_tool_choice(body):
        return None
    cloned = dict(body)
    cloned.pop("tool_choice", None)
    return cloned
