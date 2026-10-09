"""Every list row is kept, verbatim, on the record it became (7.86.0).

The Models page's "Everything known" view shows each source's own row beside
the value the ladder used -- every field the row publishes, the ones no typed
field keeps included (a tokenizer, a moderation flag, per-request limits, Kilo's
and Novita's extra keys). Each listing parser keeps its row as compact JSON
text on ``ProviderModelInfo.published_row``.

What these hold: every parser's shape keeps its whole row; an alias keeps the
row it shares; an SDK object row keeps its extra keys; a row that cannot be
written as JSON costs only its copy; and the copy is invisible to everything
that existed before it -- equality, hashing, the canonical encoding, the stored
document -- and survives every rewrite of the record on its way to the cache.
"""

import json
from dataclasses import replace
from typing import Any

from openai.types import Model

from my_claude_code.application.model_metadata import (
    ProviderModelInfo,
    canonical_model_info,
    model_info_document,
)
from my_claude_code.providers.anthropic.models import extract_anthropic_model_infos
from my_claude_code.providers.commandcode.models import extract_commandcode_model_infos
from my_claude_code.providers.model_listing import (
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
    extract_tool_capable_model_infos,
    openrouter_row_model_info,
    published_row_text,
    record_with_declared,
)
from my_claude_code.providers.runtime.model_cache import ProviderModelCache
from my_claude_code.providers.runtime.models_dev import enrich_model_infos

#: An OpenRouter-dialect row with the fields no typed field keeps (spec §11.1).
OPENROUTER_ROW: dict[str, Any] = {
    "id": "acme/model-one",
    "canonical_slug": "acme/model-one-20260901",
    "hugging_face_id": "acme/Model-One",
    "name": "Acme: Model One",
    "architecture": {
        "modality": "text+image->text",
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "tokenizer": "Other",
        "instruct_type": None,
    },
    "top_provider": {
        "context_length": 131072,
        "max_completion_tokens": 32768,
        "is_moderated": False,
    },
    "per_request_limits": None,
    "pricing": {"prompt": "0.0000003", "completion": "0.0000012", "image": "0"},
    "supported_parameters": ["reasoning", "tools"],
    "links": {"details": "/api/v1/models/acme/model-one/endpoints"},
}
#: Kilo's extras beside the dialect.
KILO_ROW: dict[str, Any] = {
    "id": "kilo-auto/efficient",
    "isFree": True,
    "mayTrainOnYourPrompts": False,
    "preferredIndex": 3,
    "supported_parameters": ["tools"],
}
#: Nous Portal: one row, listed again under each of its aliases.
NOUS_ROW: dict[str, Any] = {
    "id": "acme/aliased",
    "aliases": ["acme/aliased-latest"],
    "description": "Listed twice.",
}
#: Novita's billing extras, read by nothing typed.
NOVITA_ROW: dict[str, Any] = {
    "id": "acme/novita-model",
    "status": 1,
    "tags": ["chat"],
    "is_tiered_billing": True,
    "tiered_billing_configs": [{"max_tokens": 32000, "input": 1}],
    "owned_by": "acme",
}
COMMANDCODE_ROW: dict[str, Any] = {
    "id": "claude-sonnet-4-6",
    "name": "Claude Sonnet 4.6",
    "context_length": 200000,
    "supported_endpoints": ["/messages"],
    "created": 1791464961,
}
ANTHROPIC_ROW: dict[str, Any] = {
    "id": "claude-opus-5",
    "display_name": "Claude Opus 5",
    "created_at": "2026-08-01T00:00:00Z",
    "type": "model",
}


def _row(info: ProviderModelInfo) -> Any:
    assert info.published_row is not None, info.model_id
    return json.loads(info.published_row)


def _by_id(infos: frozenset[ProviderModelInfo]) -> dict[str, ProviderModelInfo]:
    return {info.model_id: info for info in infos}


def test_the_openrouter_dialect_keeps_its_whole_row() -> None:
    infos = extract_openrouter_tool_model_infos(
        {"data": [OPENROUTER_ROW]}, provider_name="open_router"
    )
    (info,) = infos
    assert _row(info) == OPENROUTER_ROW
    tool_capable = extract_tool_capable_model_infos(
        {"data": [OPENROUTER_ROW, KILO_ROW]}, provider_name="kilo"
    )
    assert {_row(i)["id"]: _row(i) for i in tool_capable} == {
        OPENROUTER_ROW["id"]: OPENROUTER_ROW,
        KILO_ROW["id"]: KILO_ROW,
    }


def test_the_generic_reader_keeps_rows_and_an_alias_keeps_the_row_it_shares() -> None:
    infos = _by_id(
        extract_openai_model_infos(
            {"data": [NOUS_ROW]}, provider_name="nous_portal", aliases_field="aliases"
        )
    )
    assert _row(infos["acme/aliased"]) == NOUS_ROW
    assert _row(infos["acme/aliased-latest"]) == NOUS_ROW
    (novita,) = extract_openai_model_infos(
        {"data": [NOVITA_ROW]}, provider_name="novita"
    )
    assert _row(novita) == NOVITA_ROW


def test_command_code_and_anthropic_keep_their_rows() -> None:
    (code,) = extract_commandcode_model_infos(
        {"data": [COMMANDCODE_ROW]}, provider_name="COMMANDCODE"
    )
    assert _row(code) == COMMANDCODE_ROW
    (anthropic,) = extract_anthropic_model_infos(
        {"data": [ANTHROPIC_ROW]}, provider_name="ANTHROPIC"
    )
    assert _row(anthropic) == ANTHROPIC_ROW


def test_an_sdk_object_row_keeps_the_keys_it_was_given() -> None:
    """The ``openai`` client hands rows over as ``Model`` objects with extras."""

    row = Model.model_validate(
        {
            "id": "nvidia/model",
            "object": "model",
            "created": 735790403,
            "owned_by": "nvidia",
            "root": "nvidia/model",
        }
    )
    (info,) = extract_openai_model_infos({"data": [row]}, provider_name="nvidia_nim")
    assert _row(info) == {
        "id": "nvidia/model",
        "object": "model",
        "created": 735790403,
        "owned_by": "nvidia",
        "root": "nvidia/model",
    }


def test_a_row_that_cannot_be_written_costs_only_its_copy() -> None:
    odd: dict[Any, Any] = {"id": "acme/odd", ("not", "a", "key"): 1}
    (info,) = extract_openai_model_infos({"data": [odd]}, provider_name="custom_x")
    assert info.published_row is None
    assert info.model_id == "acme/odd"
    assert published_row_text(object()) is None
    assert published_row_text(["a", "list"]) is None


def test_the_text_is_compact_and_verbatim() -> None:
    row = {"id": "a/b", "description": "naïve — “quoted”", "z": 1, "a": [1, 2]}
    text = published_row_text(row)
    assert text == '{"id":"a/b","description":"naïve — “quoted”","z":1,"a":[1,2]}'


def test_the_copy_is_invisible_to_everything_that_existed_before_it() -> None:
    """Equality, hashing, the canonical text and the stored document."""

    (with_row,) = extract_openrouter_tool_model_infos(
        {"data": [OPENROUTER_ROW]}, provider_name="open_router"
    )
    without = replace(with_row, published_row=None)
    assert with_row.published_row is not None
    assert with_row == without
    assert hash(with_row) == hash(without)
    assert len({with_row, without}) == 1
    assert canonical_model_info(with_row) == canonical_model_info(without)
    assert "published_row" not in canonical_model_info(with_row)
    assert model_info_document(with_row) == model_info_document(without)
    assert "published_row" not in model_info_document(with_row)
    assert "published_row" not in repr(with_row)


def test_the_copy_survives_every_rewrite_on_the_way_to_the_cache() -> None:
    (info,) = extract_openrouter_tool_model_infos(
        {"data": [OPENROUTER_ROW]}, provider_name="open_router"
    )
    assert record_with_declared(info).published_row == info.published_row
    bare = ProviderModelInfo("acme/model-one", published_row=info.published_row)
    (enriched,) = enrich_model_infos(
        (bare,),
        {
            "acme": {
                "models": {
                    "acme/model-one": {"limit": {"context": 1000}, "cost": {"input": 1}}
                }
            }
        },
    )
    assert enriched.context_length == 1000
    assert enriched.published_row == info.published_row
    cache = ProviderModelCache(("open_router",))
    cache.cache_model_infos("open_router", (info,))
    (prefixed,) = cache.cached_prefixed_model_infos()
    assert prefixed.model_id == "open_router/acme/model-one"
    assert prefixed.published_row == info.published_row


def test_openrouters_live_list_keeps_its_rows_in_its_own_file() -> None:
    """The live rung's records carry no copy: its rows are its stored file."""

    info = openrouter_row_model_info(OPENROUTER_ROW)
    assert info is not None
    assert info.published_row is None
