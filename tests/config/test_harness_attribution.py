"""Resolving the harness-id sentinel a shared serialiser has to write.

``build_opencode_catalogue`` is one function serving three harnesses, so the
harness id cannot be in the document it returns. These are the rules the
substitution obeys on the way to disk.
"""

from my_claude_code.config.harness_attribution import with_harness_id
from my_claude_code.config.harnesses import MCC_HARNESS_ID_SENTINEL
from my_claude_code.core.client_fingerprint import HARNESS_HEADER


def test_the_sentinel_is_replaced_wherever_it_sits() -> None:
    document = {
        "provider": {
            "mcc": {"options": {"headers": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}}}
        },
        "models": [
            {
                "generationConfig": {
                    "customHeaders": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}
                }
            }
        ],
    }

    resolved = with_harness_id(document, "opencode2")

    assert resolved["provider"]["mcc"]["options"]["headers"] == {
        HARNESS_HEADER: "opencode2"
    }
    assert resolved["models"][0]["generationConfig"]["customHeaders"] == {
        HARNESS_HEADER: "opencode2"
    }


def test_one_serialiser_answers_to_three_harness_ids() -> None:
    """The whole reason the sentinel exists, stated as a test."""

    document = {"headers": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}}

    for harness_id in ("opencode", "opencode2", "kilo"):
        assert with_harness_id(document, harness_id)["headers"] == {
            HARNESS_HEADER: harness_id
        }


def test_the_input_document_is_not_mutated() -> None:
    """The publisher serialises once and resolves per harness; sharing would leak."""

    document = {"headers": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}}

    with_harness_id(document, "kilo")

    assert document == {"headers": {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}}


def test_a_document_with_no_sentinel_passes_through_unchanged() -> None:
    document = {"provider": {"mcc": {"baseURL": "http://127.0.0.1:8082"}}}

    assert with_harness_id(document, "kimi_code") == document


def test_only_a_whole_value_is_replaced() -> None:
    """A sentinel embedded in prose is a bug to see, not a string to rewrite."""

    document = {"note": f"write {MCC_HARNESS_ID_SENTINEL} here"}

    assert with_harness_id(document, "crush") == document


def test_non_string_leaves_survive() -> None:
    document = {"limit": {"context": 131072}, "reasoning": True, "cost": None}

    assert with_harness_id(document, "crush") == document
