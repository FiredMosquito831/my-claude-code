"""The fast path through the visibility lists answers what `fnmatch` answers.

A real installation reaches a thousand patterns, and the ticks write exact refs
rather than globs: on the configuration this was measured against, **995 of 996
deny patterns are exact and one is a glob**. Matching every ref against every
pattern with `fnmatchcase` cost 5.88 s per Models page load.

The optimisation is that a pattern containing none of `*?[` matches exactly one
string -- itself -- so it is a set lookup, and that the *first* matching pattern
in declaration order can be found without walking the list. Neither is allowed
to change an answer, so every test here is `fnmatch` as the oracle.
"""

from fnmatch import fnmatchcase

import pytest

from my_claude_code.api.model_admin import ALLOW_LIST_SENTINEL, hiding_pattern
from my_claude_code.core.model_visibility import (
    ModelVisibility,
    is_glob,
    parse_model_patterns,
)

#: Shapes that between them exercise every branch: bare refs, globs of each
#: kind, a character class, a pattern that is a prefix of another, and the
#: `provider/*` form the bulk actions write.
PATTERNS = (
    "open_router/routed",
    "open_router/routed-extra",
    "open_router/*",
    "*:free",
    "groq/llama?",
    "groq/llama[0-9]",
    "nous_portal/tencent/hy3:free",
    "custom_b_ai/glm-5.3-flash",
)
REFS = (
    "open_router/routed",
    "open_router/routed-extra",
    "open_router/other",
    "groq/llama1",
    "groq/llamaX",
    "groq/gemma",
    "nous_portal/tencent/hy3:free",
    "anything:free",
    "custom_b_ai/glm-5.3-flash",
    "CUSTOM_B_AI/GLM-5.3-FLASH",
    "  open_router/routed  ",
    "",
)


def _reference_visible(allow, deny, ref: str) -> bool:
    """What the pre-optimisation implementation did, spelled out."""

    candidate = ref.strip().casefold()
    if allow and not any(fnmatchcase(candidate, pattern) for pattern in allow):
        return False
    return not any(fnmatchcase(candidate, pattern) for pattern in deny)


def _reference_first_deny(deny, ref: str, ignoring: str) -> str:
    candidate = ref.strip().casefold()
    for pattern in deny:
        if pattern != ignoring and fnmatchcase(candidate, pattern):
            return pattern
    return ""


def test_is_glob_names_exactly_the_fnmatch_metacharacters() -> None:
    assert is_glob("a*b")
    assert is_glob("a?b")
    assert is_glob("a[0-9]b")
    assert not is_glob("plain/ref")
    # A closing bracket with no opening one is literal to fnmatch, and so here.
    assert not is_glob("weird]ref")
    assert fnmatchcase("weird]ref", "weird]ref")


@pytest.mark.parametrize("ref", REFS)
def test_is_visible_agrees_with_the_fnmatch_reference(ref: str) -> None:
    for allow in ((), ("open_router/*",), ("*:free", "groq/llama?")):
        visibility = ModelVisibility(allow=allow, deny=PATTERNS)
        assert visibility.is_visible(ref) == _reference_visible(allow, PATTERNS, ref), (
            allow,
            ref,
        )


@pytest.mark.parametrize("ref", REFS)
def test_first_deny_match_agrees_with_the_reference_in_order(ref: str) -> None:
    visibility = ModelVisibility(deny=PATTERNS)
    candidate = ref.strip().casefold()

    for ignoring in ("", candidate, "open_router/*"):
        assert visibility.first_deny_match(
            candidate, ignoring=ignoring
        ) == _reference_first_deny(PATTERNS, ref, ignoring), (ref, ignoring)


def test_the_first_pattern_wins_even_when_a_later_one_also_matches() -> None:
    """Which pattern is reported is the answer, not an implementation detail."""

    glob_first = ModelVisibility(deny=("open_router/*", "open_router/routed"))
    exact_first = ModelVisibility(deny=("open_router/routed", "open_router/*"))

    assert glob_first.first_deny_match("open_router/routed") == "open_router/*"
    assert exact_first.first_deny_match("open_router/routed") == "open_router/routed"


def test_skipping_a_rows_own_pattern_falls_through_to_the_next_match() -> None:
    visibility = ModelVisibility(deny=("open_router/routed", "open_router/*"))

    assert (
        visibility.first_deny_match("open_router/routed", ignoring="open_router/routed")
        == "open_router/*"
    )


def test_a_glob_after_the_exact_match_never_wins() -> None:
    """The walk stops at the exact pattern's position, and that is the answer."""

    visibility = ModelVisibility(deny=("open_router/routed", "*routed*"))

    assert visibility.first_deny_match("open_router/routed") == "open_router/routed"


def test_hiding_pattern_still_names_the_glob_and_the_allow_list() -> None:
    by_glob = ModelVisibility(deny=("open_router/*",))
    by_own = ModelVisibility(deny=("open_router/routed",))
    by_allow = ModelVisibility(allow=("groq/*",))

    assert hiding_pattern(by_glob, "open_router/routed") == "open_router/*"
    assert hiding_pattern(by_own, "open_router/routed") == ""
    assert hiding_pattern(by_allow, "open_router/routed") == ALLOW_LIST_SENTINEL
    assert hiding_pattern(by_glob, "groq/llama") == ""


def test_a_thousand_exact_patterns_answer_the_same_as_fnmatch() -> None:
    """The shape that made this slow: many exact refs and one glob."""

    deny = (*(f"gw/model-{index:04d}" for index in range(1000)), "*:free")
    visibility = ModelVisibility(deny=deny)

    for ref in ("gw/model-0000", "gw/model-0999", "gw/model-1000", "other:free"):
        assert visibility.is_visible(ref) == _reference_visible((), deny, ref), ref
        assert visibility.first_deny_match(ref) == _reference_first_deny(deny, ref, "")


def test_the_derived_lookups_are_not_part_of_the_value() -> None:
    """Equality, hashing and repr stay the pair of tuples they always were."""

    left = ModelVisibility(deny=("a/b", "c/*"))
    right = ModelVisibility.from_raw(None, "a/b, c/*")

    assert left == right
    assert hash(left) == hash(right)
    assert "_deny_index" not in repr(left)
    assert left.deny == ("a/b", "c/*")


def test_parsing_still_folds_and_deduplicates() -> None:
    assert parse_model_patterns(" A/B , a/b ,, C/* ") == ("a/b", "c/*")
    assert ModelVisibility.from_raw(None, "A/B").is_visible("a/b") is False
