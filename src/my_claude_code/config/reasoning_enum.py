"""Parse a host's reasoning-effort enum out of its own rejection message.

Static providers had their effort vocabulary settled by an operator sending an
invalid value and reading the 400 back. A custom provider cannot ship a static
profile, so the same probe runs at runtime and its answer is stored on the
registry entry. This module owns only the *text -> words* half of that: no
HTTP, no provider imports, no Settings. The encoder that turns those words into
a wire dialect lives in ``providers/openai_chat/learned_dialect.py``, which is
the same seam a static profile declares through.

Two shapes are accepted, because hosts write the same sentence both ways::

    Invalid value ... expected one of 'minimal', 'low', 'medium', 'high'
    The request is invalid: ... <CJK prose> low, high or max.

The separators below are written as escapes rather than as the characters
themselves: a fullwidth comma and an ASCII one are indistinguishable in a
diff, and the linter is right to refuse them in source.

A candidate list wins only when at least two of its items are words a
reasoning-effort scale is actually spelled with. Without that floor the parser
happily reads "check the request body, required fields, and request format" as
a three-rung vocabulary, and a wire-shape claim invented out of prose is worse
than no claim at all.
"""

import re

MAX_EFFORT_ENUM_WORDS = 8
"""Refuse to believe a list longer than this is an effort scale."""

KNOWN_EFFORT_WORDS = frozenset(
    {
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "none",
        "off",
        "auto",
        "default",
        "disable",
        "disabled",
        "dynamic",
    }
)
"""Words that make a candidate list credible as an effort vocabulary."""

OFF_EFFORT_WORDS = frozenset({"none", "off", "disable", "disabled"})
"""Words that spell "do not reason" -- a dialect's OFF rung."""

# Quote characters a host may wrap an enum member in: ASCII, plus the curly
# and CJK forms that copy-pasted API docs carry.
_QUOTES = "`'\\\"\u201c\u201d"
_WORD = rf"[{_QUOTES}]?([A-Za-z][A-Za-z0-9_.-]{{0,31}})[{_QUOTES}]?"
# ASCII and fullwidth punctuation, plus the CJK enumeration comma and the
# conjunctions that join the last two members of a list.
_SEPARATOR = (
    r"\s*(?:,|\uff0c|\u3001|/|\||;|\uff1b|"
    r"\bor\b|\band\b|\u6216\u8005|\u6216|\u548c|\u4ee5\u53ca)\s*"
)
_LIST = re.compile(rf"{_WORD}(?:{_SEPARATOR}{_WORD})+")
_SPLIT = re.compile(_SEPARATOR)
_TOKEN = re.compile(r"[a-z][a-z0-9_.-]{0,31}\Z")
_STRIP = f" .{_QUOTES}\u3002\uff1a:"


def normalize_effort_words(words: object) -> tuple[str, ...]:
    """Return ``words`` as a clean, de-duplicated, order-preserving tuple.

    Accepts what the card's comma-list field posts and what the JSON file
    holds, so a hand-edited vocabulary is normalised exactly like a probed one.
    """
    if isinstance(words, str):
        candidates: list[object] = list(_SPLIT.split(words))
    elif isinstance(words, list | tuple):
        candidates = list(words)
    else:
        return ()
    cleaned: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        token = candidate.strip(_STRIP).lower()
        if _TOKEN.match(token) and token not in cleaned:
            cleaned.append(token)
    return tuple(cleaned[:MAX_EFFORT_ENUM_WORDS])


def parse_effort_enum(message: str, *, sent: str = "") -> tuple[str, ...]:
    """Return the effort words ``message`` names, or ``()`` if it names none.

    ``sent`` is the invalid value the probe used; a host that echoes it back
    inside its own list ("bogus_value is not one of low, high") must not have
    it read as a rung.
    """
    if not message:
        return ()
    best: tuple[str, ...] = ()
    best_score = 0
    for match in _LIST.finditer(message):
        items = _candidate_items(match.group(0), sent)
        if len(items) < 2:
            continue
        score = sum(1 for item in items if item in KNOWN_EFFORT_WORDS)
        if score >= 2 and score > best_score:
            best, best_score = items, score
    return best


def _candidate_items(raw: str, sent: str) -> tuple[str, ...]:
    items: list[str] = []
    lowered = sent.strip().lower()
    for chunk in _SPLIT.split(raw):
        token = chunk.strip(_STRIP).lower()
        if not _TOKEN.match(token):
            # One unparsable member disqualifies the whole run: a partial read
            # of a list is a different list.
            return ()
        if token == lowered or token in items:
            continue
        items.append(token)
    return tuple(items[:MAX_EFFORT_ENUM_WORDS])
