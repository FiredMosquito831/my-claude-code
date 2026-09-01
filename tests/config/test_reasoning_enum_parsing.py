"""Reading a host's effort vocabulary out of the 400 it sent.

The Chinese case is not hypothetical: it is the verbatim body B-AI returned to
``reasoning_effort: "bogus_value"`` on 2026-09-01, and the reason this parser
exists. MCC already knew that host took ``max`` -- models.dev said so and the
host said so -- and still could not spell it.
"""

import pytest

from my_claude_code.config.reasoning_enum import (
    normalize_effort_words,
    parse_effort_enum,
)

# The real body, escaped so the file stays ASCII and the separators stay
# visible in a diff. Reads: "this model always thinks and does not support
# disabling thinking; please use low, high or max."
B_AI_400 = (
    "The request is invalid: "
    "\u8be5\u6a21\u578b\u59cb\u7ec8\u601d\u8003\uff0c"
    "\u4e0d\u652f\u6301\u5173\u95ed\u601d\u8003\uff1b"
    "\u8bf7\u4f7f\u7528 low\u3001high \u6216 max\u3002"
    ". Please check the request body, required fields, and request format."
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (B_AI_400, ("low", "high", "max")),
        (
            "Invalid value for 'reasoning_effort': expected one of "
            "'minimal', 'low', 'medium', 'high'",
            ("minimal", "low", "medium", "high"),
        ),
        ("reasoning_effort must be low, medium or high", ("low", "medium", "high")),
        (
            'Unsupported value: "bogus_value". Supported values are: none, low, high.',
            ("none", "low", "high"),
        ),
        ("reasoning_effort: use `low` | `high` | `max`", ("low", "high", "max")),
    ],
)
def test_parses_an_enum_a_host_named(message: str, expected: tuple[str, ...]) -> None:
    assert parse_effort_enum(message, sent="bogus_value") == expected


@pytest.mark.parametrize(
    "message",
    [
        "",
        "Something went wrong, please try again or contact support",
        "Expected a string, or null",
        "Please check the request body, required fields, and request format.",
        "Insufficient balance. Top up your account or switch plans.",
    ],
)
def test_refuses_to_read_a_vocabulary_out_of_prose(message: str) -> None:
    """A wire-shape claim invented from prose is worse than no claim at all."""
    assert parse_effort_enum(message, sent="bogus_value") == ()


def test_the_probes_own_invalid_value_is_never_read_back_as_a_rung() -> None:
    message = "bogus_value is not one of low, high, max"

    assert parse_effort_enum(message, sent="bogus_value") == ("low", "high", "max")


def test_normalize_accepts_the_cards_comma_list() -> None:
    assert normalize_effort_words(" Low , HIGH,max, low ") == ("low", "high", "max")


def test_normalize_accepts_a_json_list_and_rejects_junk() -> None:
    assert normalize_effort_words(["low", 7, "", "HIGH"]) == ("low", "high")
    assert normalize_effort_words(None) == ()
    assert normalize_effort_words(42) == ()


def test_normalize_caps_an_implausibly_long_list() -> None:
    words = normalize_effort_words(",".join(f"w{index}" for index in range(40)))

    assert len(words) == 8
