"""Allow/deny glob matching over provider-prefixed model refs."""

from my_claude_code.core.model_visibility import ModelVisibility, parse_model_patterns

# Real refs from a live configuration, so the patterns are exercised against
# the shapes that actually occur: a nested vendor path, a ``:free`` suffix, and
# a gateway that flattens the vendor into the model name.
INKLING = "nvidia_nim/thinkingmachines/inkling"
HY3_FREE = "nous_portal/tencent/hy3:free"
MINIMAX = "commandcode/minimax/minimax-m3-free"
ALL_REFS = (INKLING, HY3_FREE, MINIMAX)


def _visible(allow: str, deny: str = "") -> tuple[str, ...]:
    return tuple(ModelVisibility.from_raw(allow, deny).visible(ALL_REFS))


def test_empty_allow_lists_everything():
    visibility = ModelVisibility.from_raw("", "")

    assert _visible("") == ALL_REFS
    assert visibility.hides_anything is False


def test_a_non_empty_allow_hides_everything_it_does_not_name():
    assert _visible(INKLING) == (INKLING,)


def test_deny_is_applied_after_allow_and_wins():
    assert _visible("*", "*:free") == (INKLING, MINIMAX)
    assert _visible(HY3_FREE, HY3_FREE) == ()


def test_deny_alone_hides_without_an_allow_list():
    assert _visible("", "commandcode/*") == (INKLING, HY3_FREE)


def test_free_suffix_glob():
    assert _visible("*:free") == (HY3_FREE,)


def test_provider_prefix_glob():
    assert _visible("nous_portal/*") == (HY3_FREE,)


def test_substring_glob():
    assert _visible("*inkling*") == (INKLING,)


def test_exact_ref_is_just_a_pattern_with_no_wildcards():
    """The mechanism a future "tick this model" UI writes into.

    An explicit pick is an exact-match pattern, so picking models and writing
    globs stay one mechanism rather than two that can disagree.
    """

    assert _visible(f"{INKLING},{MINIMAX}") == (INKLING, MINIMAX)


def test_matching_is_case_insensitive_on_both_sides():
    visibility = ModelVisibility.from_raw("NVIDIA_NIM/ThinkingMachines/INKLING", "")

    assert visibility.is_visible(INKLING) is True
    assert visibility.is_visible(INKLING.upper()) is True


def test_blank_and_whitespace_only_entries_are_ignored_not_rejected():
    assert parse_model_patterns("  ,, \t ,  ") == ()
    assert parse_model_patterns(None) == ()
    # A list typed with a trailing comma and stray spaces still means one
    # pattern, and an empty entry must not silently match everything.
    assert parse_model_patterns(" nvidia_nim/* , ,") == ("nvidia_nim/*",)
    assert _visible("  ,, ,  ") == ALL_REFS


def test_duplicate_patterns_collapse():
    assert parse_model_patterns("*:free,*:FREE,*:free") == ("*:free",)


def test_surrounding_whitespace_on_a_ref_does_not_defeat_a_pattern():
    visibility = ModelVisibility.from_raw(INKLING, "")

    assert visibility.is_visible(f"  {INKLING}  ") is True


def test_a_slash_pattern_matches_the_same_way_on_every_platform():
    """`fnmatch` normcases paths on Windows; `fnmatchcase` must not.

    Refs are built out of forward slashes, so a matcher that rewrote them as
    backslashes would make the same setting behave differently per OS.
    """

    visibility = ModelVisibility.from_raw("nvidia_nim/*/inkling", "")

    assert visibility.is_visible(INKLING) is True
    assert visibility.is_visible("nvidia_nim\\thinkingmachines\\inkling") is False


def test_hides_anything_reports_whether_any_pattern_is_configured():
    assert ModelVisibility.from_raw("*", "").hides_anything is True
    assert ModelVisibility.from_raw("", "*:free").hides_anything is True
    assert ModelVisibility.from_raw(" , ", "").hides_anything is False
