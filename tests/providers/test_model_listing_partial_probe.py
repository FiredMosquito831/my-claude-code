"""A partial probe is a SUCCESS (8.18.0).

The user's instruction, verbatim: *"when probing capabilities accept partial
probes too, don't drop all probe results if one or more models fail to probe"*.

`providers/model_listing.py` had 21 `raise` sites inside its per-row loop, and
every one of them is a statement about ONE row -- yet every one discarded the
other four hundred. On a fresh install with nothing cached that meant the
provider contributed **zero** models and no capabilities at all, while a warm
install kept the previous sweep's records and looked fine. That is the most
likely explanation for "it doesn't properly detect model capabilities" on a new
machine.

The rule this holds:

* a row that cannot be read is **skipped and recorded**, never raised;
* the provider is **not** marked failed when at least one row parsed;
* a provider whose list is **entirely** unreadable is still a failure -- that is
  not a partial probe;
* with no unreadable rows the answer is byte-identical to before, which is what
  the operator's own catalogue measures (0 rows skipped, 0 providers failing).
"""

from typing import Any

import pytest

from my_claude_code.providers.anthropic.models import parse_anthropic_model_list
from my_claude_code.providers.commandcode.models import parse_commandcode_model_list
from my_claude_code.providers.model_listing import (
    ModelListResponseError,
    parse_openai_model_list,
)


def _good(index: int) -> dict[str, Any]:
    return {
        "id": f"model-{index}",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    }


def test_one_bad_row_among_four_hundred_keeps_the_four_hundred() -> None:
    rows: list[Any] = [_good(index) for index in range(400)]
    # The one row that cannot be read: no id at all.
    rows.insert(200, {"input_modalities": ["text"]})

    parsed = parse_openai_model_list({"data": rows}, provider_name="BIG")

    assert len(parsed.infos) == 400
    assert len(parsed.skipped) == 1
    skipped = parsed.skipped[0]
    assert skipped.index == 200
    assert skipped.model_id is None
    assert "include id" in skipped.reason
    assert not parsed.complete


def test_a_malformed_required_field_costs_only_its_row() -> None:
    rows = [
        {"id": "keep-a", "kind": "chat"},
        {"id": "drop-me"},
        {"id": "keep-b", "kind": "chat"},
    ]
    parsed = parse_openai_model_list(
        {"data": rows},
        provider_name="P",
        required_path_values=((("kind",), ("chat",)),),
    )
    # `infos` is a frozenset, so the order is the set's, not the list's.
    assert sorted(info.model_id for info in parsed.infos) == ["keep-a", "keep-b"]
    assert [(row.index, row.model_id) for row in parsed.skipped] == [(1, "drop-me")]
    assert "kind" in parsed.skipped[0].reason


def test_a_bad_alias_costs_the_row_that_carried_it() -> None:
    rows = [
        {"id": "keep", "aliases": ["keep-latest"]},
        {"id": "drop", "aliases": [7]},
    ]
    parsed = parse_openai_model_list(
        {"data": rows}, provider_name="A", aliases_field="aliases"
    )
    assert sorted(info.model_id for info in parsed.infos) == ["keep", "keep-latest"]
    assert [row.model_id for row in parsed.skipped] == ["drop"]


def test_a_list_that_is_entirely_unreadable_is_still_a_failure() -> None:
    """That is not a partial probe: nobody could read any of it."""

    rows = [{"input_modalities": ["text"]}, {"input_modalities": ["text"]}]
    with pytest.raises(ModelListResponseError) as raised:
        parse_openai_model_list({"data": rows}, provider_name="DEAD")
    assert "none of 2" in raised.value.message


def test_a_clean_list_records_nothing() -> None:
    """The equality half: no unreadable row means the answer is what it always was."""

    rows = [_good(index) for index in range(5)]
    parsed = parse_openai_model_list({"data": rows}, provider_name="CLEAN")
    assert parsed.skipped == ()
    assert parsed.complete
    assert sorted(info.model_id for info in parsed.infos) == [
        f"model-{index}" for index in range(5)
    ]


def test_the_payload_itself_being_wrong_still_raises() -> None:
    """A list-level check is not a row-level one, and still fails the sweep."""

    with pytest.raises(ModelListResponseError):
        parse_openai_model_list({"not_data": []}, provider_name="X")
    with pytest.raises(ModelListResponseError):
        parse_openai_model_list({"data": []}, provider_name="X")


def test_an_empty_list_of_rows_is_not_a_partial_probe_either() -> None:
    """Every row filtered out is a list that stated no models, as before."""

    with pytest.raises(ModelListResponseError):
        parse_openai_model_list(
            {"data": [{"id": "x", "kind": "other"}]},
            provider_name="F",
            required_path_values=((("kind",), ("chat",)),),
        )


def test_anthropic_keeps_every_row_it_can_read() -> None:
    payload = {
        "data": [
            {"type": "model", "id": "claude-a"},
            {"type": "model"},
            {"type": "model", "id": "claude-b"},
        ]
    }
    parsed = parse_anthropic_model_list(payload, provider_name="A")
    # `infos` is a frozenset, so the order is the set's, not the list's.
    assert sorted(info.model_id for info in parsed.infos) == ["claude-a", "claude-b"]
    assert [row.index for row in parsed.skipped] == [1]


def test_command_code_keeps_every_row_it_can_read() -> None:
    payload = {
        "data": [
            {"id": "cc-a", "context_length": 200000},
            {"id": "cc-bad", "context_length": -1},
            {"id": "cc-c", "context_length": 400000},
        ]
    }
    parsed = parse_commandcode_model_list(payload, provider_name="CC")
    # `infos` is a frozenset, so the order is the set's, not the list's.
    assert sorted(info.model_id for info in parsed.infos) == ["cc-a", "cc-c"]
    assert [row.model_id for row in parsed.skipped] == ["cc-bad"]


def test_the_one_value_form_is_unchanged_for_a_clean_list() -> None:
    """`extract_*` still returns exactly the frozenset it always did."""

    from my_claude_code.providers.model_listing import extract_openai_model_infos

    rows = [_good(index) for index in range(3)]
    assert extract_openai_model_infos({"data": rows}, provider_name="V") == (
        parse_openai_model_list({"data": rows}, provider_name="V").infos
    )
