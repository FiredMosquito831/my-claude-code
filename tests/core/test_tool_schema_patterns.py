"""The upstream ``\\p{...}`` translator (39b9c272), ported for the Responses surface.

Ported from ``free-claude-code`` ``tests/core/test_tool_schema_patterns.py``.
What moved: upstream's own schema walker is not ported -- MCC translates
through the one walker the 7.36.0 rung already prunes with (see
``tests/providers/test_responses_unicode_property_translation.py``) -- so the
pattern-level cases here call :func:`translate_pattern` directly and the walk
cases call the dialect's :func:`translate_schema_patterns`. What is new: the
tables are built once, under a lock, and a pattern with no ``\\p{`` never
builds them.
"""

import re
import threading
import unicodedata
from copy import deepcopy
from typing import Any

import pytest

from my_claude_code.core import tool_schema_patterns
from my_claude_code.core.tool_schema_patterns import (
    reset_unicode_property_ranges_for_tests,
    translate_pattern,
    unicode_property_ranges,
    unicode_property_ranges_ready,
    warm_unicode_property_ranges,
)
from my_claude_code.providers.openai_responses.tool_schema_dialect import (
    translate_schema_patterns,
)

ARTIFACT_PATTERN = r'^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}"\\./[\]]{1,200}$'


@pytest.mark.parametrize(
    "pattern", [r"^[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}]{1,200}$", ARTIFACT_PATTERN]
)
def test_translation_preserves_the_artifact_rules(pattern: str) -> None:
    translated = translate_pattern(pattern)
    assert r"\p{" not in translated
    regex = re.compile(translated)
    assert regex.fullmatch("demo")
    assert not regex.fullmatch("bad\u2028name")
    assert not regex.fullmatch("bad\U000e0001name")
    assert not regex.fullmatch("a" * 201)
    assert not regex.fullmatch("")


def test_the_artifact_lookahead_is_left_exactly_where_it_was() -> None:
    """The honest limit: translation does not make Artifact lookaround-free."""

    translated = translate_pattern(ARTIFACT_PATTERN)
    assert translated.startswith("^(?!__.*__$)")


@pytest.mark.parametrize(
    "pattern",
    [r"^[^\p{Cc}]{4294967296}$", "(" * 500 + r"[^\p{Cc}]" + ")" * 500],
    ids=["repeat-overflow", "group-recursion"],
)
def test_compiler_limits_leave_the_pattern_unchanged(pattern: str) -> None:
    assert translate_pattern(pattern) is pattern


@pytest.mark.parametrize("category", ["Cc", "Cf", "Zl", "Zp"])
def test_category_expansion_preserves_every_unicode_scalar(category: str) -> None:
    regex = re.compile(translate_pattern(r"[^\p{" + category + "}]+"))
    for point in range(0x110000):
        if 0xD800 <= point <= 0xDFFF:
            continue
        char = chr(point)
        assert bool(regex.fullmatch(char)) == (
            unicodedata.category(char) != category
        ), f"U+{point:04X}, category {category}, Unicode {unicodedata.unidata_version}"


@pytest.mark.parametrize(
    "name,accepted",
    [
        ("demo", True),
        ("é名字😀", True),
        ("__name", True),
        ("name__", True),
        ("-name-", True),
        ("a", True),
        ("a" * 200, True),
        ("", False),
        ("a" * 201, False),
        ("__name__", False),
        *[("x" + char + "y", False) for char in r'"\/.[]'],
        *[
            ("x" + char + "y", False)
            for char in [
                "\x00",
                "\x1f",
                "\x7f",
                "\x9f",
                "\u200d",
                "\u2028",
                "\u2029",
                "\U000110bd",
                "\U000e0001",
                "\U000e0020",
                "\U000e007f",
            ]
        ],
    ],
)
def test_artifact_restrictions_survive_translation(name: str, accepted: bool) -> None:
    assert bool(re.fullmatch(translate_pattern(ARTIFACT_PATTERN), name)) == accepted


@pytest.mark.parametrize(
    "pattern",
    [
        r"^[a-z]+$",
        r"[^\\p{Cc}]",
        r"\\p{Cc}",
        r"[\p{Cc}]",
        r"\p{Cc}",
        r"[^\P{Cc}]",
        r"[^\p{L}]",
        r"[^\p{cc}]",
        r"[^\p{Cc}\p{L}]",
        r"[^\p{Cc}]\P{Cf}",
        r"[^\p{Cc}]\p{Cf}",
        r"[^\p{Cc}\p{General_Category=Format}]",
        r"[^\p{Cc}\p{Cf]",
        r"[^\p{Cc}",
        r"[^\p{Cc}]" + "\\",
        r"([^\p{Cc}]",
        r"[^\p{Cc}-z]",
        r"[^a-\p{Cc}]",
        r"[^\p{Cc}--a]",
        r"[^\p{Cc}&&a]",
        r"[^\p{Cc}~~a]",
        r"[^\p{Cc}||a]",
        r"[^\p{Cc}[a]]",
        r"\[^\p{Cc}]",
    ],
)
def test_unsupported_or_literal_patterns_remain_unchanged(pattern: str) -> None:
    assert translate_pattern(pattern) is pattern
    schema = {"type": "string", "pattern": pattern}
    assert translate_schema_patterns(schema) is schema


@pytest.mark.parametrize(
    "pattern,excluded,accepted",
    [
        (r"[^\\\p{Cc}]", "\\\x00", "p{}C"),
        (r"[^\p{Cc}\-]", "-\x00", "az"),
        (r"[^\]\p{Cc}]", "]\x00", "["),
        (r"[^\p{Cc}[\]]", "[]\x00", "az"),
        (r"[^\p{Zl}][^\p{Zp}]", "\u2028x", "ab"),
    ],
)
def test_class_escaping_is_preserved(
    pattern: str, excluded: str, accepted: str
) -> None:
    translated = translate_pattern(pattern)
    assert translated != pattern
    regex = re.compile(translated)
    if pattern == r"[^\p{Zl}][^\p{Zp}]":
        assert regex.fullmatch(accepted)
        assert not regex.fullmatch(excluded)
    else:
        assert all(not regex.fullmatch(char) for char in excluded)
        assert all(regex.fullmatch(char) for char in accepted)


# --------------------------------------------------------------------------
# The walk: MCC's shared walker, not a fourth copy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "container",
    ["$defs", "definitions", "properties", "patternProperties", "dependentSchemas"],
)
def test_schema_maps_visit_values_and_preserve_names(container: str) -> None:
    child = {"type": "string", "pattern": r"[^\p{Cc}]"}
    schema = {container: {"pattern": child, r"\p{Cc}": True, "required": ["name"]}}
    original = deepcopy(schema)
    converted = translate_schema_patterns(schema)
    assert converted[container]["pattern"]["pattern"] != child["pattern"]
    assert converted[container][r"\p{Cc}"] is True
    assert converted[container]["required"] == ["name"]
    assert schema == original


@pytest.mark.parametrize(
    "keyword",
    [
        "additionalProperties",
        "additionalItems",
        "unevaluatedProperties",
        "unevaluatedItems",
        "items",
        "contains",
        "propertyNames",
        "if",
        "then",
        "else",
        "not",
    ],
)
def test_single_schema_keywords_are_translated(keyword: str) -> None:
    schema = {keyword: {"pattern": r"[^\p{Cf}]"}}
    converted = translate_schema_patterns(schema)
    assert converted[keyword]["pattern"] != schema[keyword]["pattern"]


@pytest.mark.parametrize("keyword", ["dependencies", "contentSchema"])
def test_the_walk_keeps_the_rungs_vocabulary(keyword: str) -> None:
    """Upstream also descends these two; MCC's one walker (7.36.0) does not.

    Widening the shared vocabulary would change what the 7.38.0 dialect and
    the rung *drop*, which is not this change's business -- so the walker
    translates exactly where it prunes, and no further.
    """

    schema = {keyword: {"name": {"pattern": r"[^\p{Cf}]"}}}
    assert translate_schema_patterns(schema) is schema


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf", "prefixItems", "items"])
def test_schema_arrays_preserve_boolean_and_unchanged_children(keyword: str) -> None:
    child = {"type": "string", "pattern": r"[a-z]"}
    schema = {keyword: [child, False, {"pattern": r"[^\p{Cf}]"}]}
    converted = translate_schema_patterns(schema)
    assert converted[keyword][0] is child
    assert converted[keyword][1] is False
    assert converted[keyword][2]["pattern"] != r"[^\p{Cf}]"


def test_instance_data_is_untouched_and_translation_is_idempotent() -> None:
    literal = {"pattern": r"[^\p{Cc}]"}
    schema = {
        "type": "object",
        "properties": {"name": literal},
        "default": literal,
        "const": literal,
        "enum": [literal],
        "examples": [literal],
        "x-custom": literal,
        "description": r"\p{Cc}",
    }
    before = deepcopy(schema)
    converted = translate_schema_patterns(schema)
    for key in ("default", "const", "enum", "examples", "x-custom", "description"):
        assert converted[key] is schema[key]
    assert schema == before
    assert translate_schema_patterns(converted) is converted


@pytest.mark.parametrize(
    "schema", [True, False, {}, {"pattern": 123}, {"type": "string"}]
)
def test_non_patterns_remain_unchanged(schema: Any) -> None:
    assert translate_schema_patterns(schema) is schema


# --------------------------------------------------------------------------
# The 302 ms walk: once, off the request path
# --------------------------------------------------------------------------


@pytest.fixture
def cold_tables():
    reset_unicode_property_ranges_for_tests()
    yield
    reset_unicode_property_ranges_for_tests()


def test_a_pattern_without_a_property_escape_never_builds_the_tables(
    cold_tables: None,
) -> None:
    catalogue = {
        "properties": {
            "a": {"pattern": r"^[^\n\r]*$"},
            "b": {"pattern": r"^(?![\s\S])"},
            "c": {"pattern": r"[\\p{Cc}]"},
        }
    }
    assert translate_schema_patterns(catalogue) is catalogue
    assert not unicode_property_ranges_ready()


def test_the_warm_up_builds_the_tables_that_translation_then_reuses(
    cold_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = tool_schema_patterns._build_category_ranges
    calls: list[int] = []

    def _counting() -> dict[str, str]:
        calls.append(1)
        return built()

    monkeypatch.setattr(tool_schema_patterns, "_build_category_ranges", _counting)
    warm_unicode_property_ranges()
    assert unicode_property_ranges_ready()
    translate_pattern(ARTIFACT_PATTERN)
    translate_pattern(r"[^\p{Zl}]")
    assert calls == [1]


def test_concurrent_first_callers_build_the_tables_once(
    cold_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that arrives mid-warm-up waits; it never walks a second time."""

    release = threading.Event()
    calls: list[int] = []
    built = tool_schema_patterns._build_category_ranges

    def _slow() -> dict[str, str]:
        calls.append(1)
        release.wait(5)
        return built()

    monkeypatch.setattr(tool_schema_patterns, "_build_category_ranges", _slow)
    results: list[dict[str, str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(unicode_property_ranges()))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(10)
    assert calls == [1]
    assert len(results) == 4
    assert all(result is results[0] for result in results)
