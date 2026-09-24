"""Portable Unicode exclusions for a tool parameter's ``pattern``.

Ported from upstream ``free-claude-code`` 39b9c272 (#1772, issue #1730).
Claude Code writes JavaScript regexes, and some of them spell a Unicode
general category as a property escape -- ``[^\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}]`` --
which OpenAI's tool-schema validator has refused. This module rewrites exactly
those four categories, as standalone atoms inside an ordinary *negated*
character class, into the explicit code-point ranges they stand for, so the
restriction survives instead of the whole ``pattern`` being dropped.

What it is and is not:

* **A bounded scanner, not a regex transpiler.** Anything outside that one
  shape -- a positive class, ``\\P{...}``, another category, a range touching
  the escape, a nested or set-operation class, an escape outside a class --
  returns the pattern **unchanged, by identity**, never half-translated.
* **Nothing else in the pattern moves.** Lookaround in particular is left
  exactly where it is. Claude Code's own ``Artifact`` pattern begins
  ``^(?!__.*__$)`` and still carries that lookahead after translation; this
  module does not make it acceptable to a validator that refuses lookaround,
  and the declared Responses dialect drops such a pattern after this has run.
* **Derived, not shipped.** The ranges come from this interpreter's own
  Unicode database, including supplementary characters, the first time a
  pattern actually needs them. That walk over all 0x110000 code points costs
  ~300 ms, which is why :func:`warm_unicode_property_ranges` exists: the
  server calls it on its startup worker thread so the first request that
  carries such a pattern never pays it on the event loop.

Where it runs is the caller's decision. In MCC it is the Responses surface
only (``providers/openai_responses/tool_schema_dialect.py``); Chat Completions
and native Anthropic bytes are untouched.
"""

import re
import threading
import unicodedata

#: The four general categories this module can expand, and nothing else.
UNICODE_PROPERTY_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

_ranges_lock = threading.Lock()
_ranges: dict[str, str] | None = None


def translate_pattern(pattern: str) -> str:
    """``pattern`` with its four supported ``\\p{...}`` atoms expanded.

    Returns ``pattern`` itself -- the same ``str`` object -- whenever there is
    nothing to translate or the pattern is outside the supported shape, so a
    caller can tell "changed" from "unchanged" by identity.
    """

    # A bounded scanner for four standalone atoms in ordinary negated classes,
    # not a general regex transpiler. Never partially translate unsupported input.
    if r"\p{" not in pattern:
        return pattern
    replacements: list[tuple[int, int, str]] = []
    in_class = False
    negated = False
    previous = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 == len(pattern):
                return pattern
            if pattern[index + 1] in "pP":
                end = pattern.find("}", index + 3)
                category = pattern[index + 3 : end]
                if (
                    not pattern.startswith(r"\p{", index)
                    or end == -1
                    or category not in UNICODE_PROPERTY_CATEGORIES
                    or not in_class
                    or not negated
                    or previous == "-"
                    or pattern[end + 1 : end + 2] == "-"
                ):
                    return pattern
                replacements.append((index, end + 1, category))
                previous = pattern[index : end + 1]
                index = end + 1
                continue
            previous = pattern[index : index + 2]
            index += 2
            continue
        if char == "[":
            if in_class:
                # Artifact's ordinary class includes a literal '[' before '\]'.
                # Other nested forms may be Unicode set syntax; leave them alone.
                if not pattern.startswith(r"[\]]", index):
                    return pattern
            else:
                in_class = True
                negated = pattern[index + 1 : index + 2] == "^"
        elif char == "]":
            if not in_class:
                return pattern
            in_class = False
        elif in_class and pattern[index : index + 2] in {"&&", "--", "~~", "||"}:
            return pattern
        previous = char
        index += 1
    if in_class or not replacements:
        return pattern
    ranges = unicode_property_ranges()
    parts = []
    offset = 0
    for start, end, category in replacements:
        parts.extend((pattern[offset:start], ranges[category]))
        offset = end
    parts.append(pattern[offset:])
    translated = "".join(parts)
    try:
        re.compile(translated)
    except re.error, OverflowError, RecursionError:
        return pattern
    return translated


def unicode_property_ranges() -> dict[str, str]:
    """The four categories as regex class bodies, built once per process.

    Built under a lock so the startup warm-up and a request that arrives
    before it has finished never walk the code points twice: the request
    waits for the rest of the walk instead of starting its own.
    """

    global _ranges
    built = _ranges
    if built is not None:
        return built
    with _ranges_lock:
        if _ranges is None:
            _ranges = _build_category_ranges()
        return _ranges


def unicode_property_ranges_ready() -> bool:
    """Whether the tables are already built (the warm-up has run)."""

    return _ranges is not None


def warm_unicode_property_ranges() -> None:
    """Build the tables now. Called on the server's startup worker thread."""

    unicode_property_ranges()


def _build_category_ranges() -> dict[str, str]:
    """Build the four finite tables, using Python's Unicode database."""

    spans_by_category: dict[str, list[tuple[int, int]]] = {
        key: [] for key in UNICODE_PROPERTY_CATEGORIES
    }
    for point in range(0x110000):
        category = unicodedata.category(chr(point))
        if category not in spans_by_category:
            continue
        spans = spans_by_category[category]
        if spans and spans[-1][1] == point - 1:
            spans[-1] = (spans[-1][0], point)
        else:
            spans.append((point, point))
    return {
        category: "".join(
            _codepoint(start)
            if start == end
            else f"{_codepoint(start)}-{_codepoint(end)}"
            for start, end in spans
        )
        for category, spans in spans_by_category.items()
    }


def _codepoint(point: int) -> str:
    # Actual astral scalars work in JS Unicode mode and Python. Surrogate ranges
    # would change the exclusion; \U and \u{...} escapes are dialect-specific.
    return f"\\u{point:04X}" if point <= 0xFFFF else chr(point)


def reset_unicode_property_ranges_for_tests() -> None:
    """Forget the built tables. Tests only."""

    global _ranges
    with _ranges_lock:
        _ranges = None
