"""The Models page's publication, retirement, cutoff and description rows (7.85.0).

Each walks its own ladder, provider official first (user decisions 2026-10-08
20:57-21:00 and 21:00):

- retirement: the vendor's own client catalogue -> the provider's list ->
  LiteLLM's map (only while LiteLLM pricing is on) -> models.dev's
  ``deprecated`` flag (a flag, never a day);
- publication: the provider's list -> models.dev's release date. OpenRouter's
  live list states no publication day; its ``created`` is its listing day and
  stays in its own row;
- knowledge cutoff, description: the provider's list -> OpenRouter's live list
  (7.84.0's rows) -> models.dev. The provider's statement of the same value
  takes the badge; since 7.86.0 a different one is shown, with OpenRouter's
  beside it (7.85.0 had them the other way round).

Display only, and LiteLLM's endpoint words fill the endpoints row only where
the provider's list names none.
"""

from typing import Any

import pytest

from my_claude_code.api.model_admin import (
    DEPRECATED_BY_MODELS_DEV,
    _model_entry,
    build_models_page_payload,
    capability_payload,
)
from my_claude_code.api.models_page_cache import capability_half_key
from my_claude_code.application.litellm_model_map import (
    LiteLLMCatalogue,
    LiteLLMModel,
    LiteLLMStatement,
)
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ModelListingEvidence,
    ModelListingProvenance,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_EXACT,
    LiveCatalogue,
    LiveModel,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.providers.runtime.models_dev import write_models_dev_cache

#: ``novita`` has a bucket; ``nous_portal`` reads models.dev's OpenRouter copy.
MODELS_DEV: dict[str, Any] = {
    "novita": {
        "id": "novita",
        "models": {
            "acme/bucketed": {
                "id": "acme/bucketed",
                "release_date": "2025-12-01",
                "knowledge": "2025-06",
                "description": "models.dev's words.",
            },
            "acme/retired": {
                "id": "acme/retired",
                "release_date": "2023-01-01",
                "status": "deprecated",
            },
        },
    },
    "openrouter": {
        "id": "openrouter",
        "models": {
            "acme/copied": {
                "id": "acme/copied",
                "release_date": "2025-11-04",
                "knowledge": "2025-03-31",
                "description": "models.dev's OpenRouter copy.",
            }
        },
    },
}


@pytest.fixture(autouse=True)
def _models_dev() -> None:
    write_models_dev_cache(MODELS_DEV)


def _live(**fields: Any) -> LiveCatalogue:
    answer = LiveModel(
        slugs=("acme/model",),
        match=LIVE_MATCH_EXACT,
        description=fields.get("description"),
        knowledge_cutoff=fields.get("knowledge_cutoff"),
        listed_at=fields.get("listed_at", "2026-02-11"),
    )
    return LiveCatalogue(
        mark="1:1", fetched_at=None, rows=1, lookup=lambda _p, _m: answer
    )


def _litellm(model: LiteLLMModel | None) -> LiteLLMCatalogue:
    return LiteLLMCatalogue(mark="7:7", lookup=lambda _p, _m: model)


def _said(value: Any) -> LiteLLMStatement[Any]:
    return LiteLLMStatement(value, "novita/acme/bucketed", "prefixed key")


def _record(model_id: str, **declared: Any) -> ProviderModelInfo:
    return ProviderModelInfo(
        model_id,
        declared=ProviderModelDeclaration(**declared) if declared else None,
    )


# ------------------------------------------------------------------ retirement


def test_retirement_the_vendor_s_client_comes_first() -> None:
    record = ProviderModelInfo(
        "gpt-5",
        listing=ModelListingEvidence(
            provenance=ModelListingProvenance.VENDOR_CLIENT,
            retirement_at="2026-12-01T00:00:00Z",
        ),
        declared=ProviderModelDeclaration(retires_at="2027-01-01"),
    )
    row = capability_payload("chatgpt_oauth", "gpt-5", record)["retires_at"]
    assert row["value"] == "2026-12-01T00:00:00Z"
    assert row["source_label"] == "the vendor's own client catalogue"


def test_retirement_the_provider_s_list_then_litellm_then_models_dev() -> None:
    provider = capability_payload(
        "novita", "acme/retired", _record("acme/retired", retires_at="2026-10-09")
    )["retires_at"]
    assert (provider["value"], provider["source"], provider["tier"]) == (
        "2026-10-09",
        "provider",
        1,
    )
    over = capability_payload(
        "novita",
        "acme/retired",
        _record("acme/retired", retires_at="2026-10-09"),
        litellm=_litellm(LiteLLMModel(deprecation_date=_said("2026-06-17"))),
    )["retires_at"]
    assert (over["value"], over["source"]) == ("2026-10-09", "provider")
    lite = capability_payload(
        "novita",
        "acme/retired",
        _record("acme/retired"),
        litellm=_litellm(LiteLLMModel(deprecation_date=_said("2026-06-17"))),
    )["retires_at"]
    assert (lite["value"], lite["source"]) == ("2026-06-17", "litellm")
    assert lite["tier_label"] == "LiteLLM, prefixed key (novita/acme/bucketed)"
    flag = capability_payload("novita", "acme/retired", _record("acme/retired"))[
        "retires_at"
    ]
    assert flag["value"] == DEPRECATED_BY_MODELS_DEV
    assert flag["source"] == "models_dev"
    current = capability_payload("novita", "acme/bucketed", None)["retires_at"]
    assert current["value"] is None and current["source"] == "unknown"


# ----------------------------------------------------------------- publication


def test_publication_the_provider_s_day_then_models_dev_s_release() -> None:
    own = capability_payload(
        "novita", "acme/bucketed", _record("acme/bucketed", published_at="2026-08-26")
    )["published_at"]
    assert (own["value"], own["source"]) == ("2026-08-26", "provider")
    released = capability_payload("novita", "acme/bucketed", None)["published_at"]
    assert (released["value"], released["source"], released["tier"]) == (
        "2025-12-01",
        "models_dev",
        3,
    )


def test_openrouter_s_listing_day_is_never_a_publication_day() -> None:
    payload = capability_payload(
        "nous_portal", "acme/unlisted", None, live=_live(listed_at="2026-02-11")
    )
    assert payload["listed_on_openrouter"]["value"] == "2026-02-11"
    assert payload["published_at"]["value"] is None


# ------------------------------------------------- knowledge cutoff, description


def test_without_the_live_list_the_provider_then_models_dev() -> None:
    own = capability_payload(
        "novita", "acme/bucketed", _record("acme/bucketed", description="Own words.")
    )
    assert (own["description"]["value"], own["description"]["source"]) == (
        "Own words.",
        "provider",
    )
    assert own["knowledge_cutoff"]["value"] == "2025-06"
    copy = capability_payload("nous_portal", "acme/copied", None)
    assert copy["description"]["value"] == "models.dev's OpenRouter copy."
    assert copy["description"]["tier"] == 5
    assert copy["knowledge_cutoff"]["value"] == "2025-03-31"


def test_the_providers_own_words_answer_and_openrouters_stay_beside() -> None:
    """Provider first (7.86.0): the user's rule, turned round from 7.85.0.

    7.85.0 kept OpenRouter's live words shown where the provider's own list
    said something else, with the provider's beside them; the provider's own
    statement now answers, and OpenRouter's is the one beside it.
    """

    live = _live(description="OpenRouter's words.", knowledge_cutoff="2025-03-31")
    same = capability_payload(
        "nous_portal",
        "acme/copied",
        _record("acme/copied", description="OpenRouter's words."),
        live=live,
    )
    # Same words from the provider's own list: the badge is the provider's.
    assert same["description"]["value"] == "OpenRouter's words."
    assert same["description"]["source"] == "provider"
    assert "also_stated" not in same["description"]
    differs = capability_payload(
        "nous_portal",
        "acme/copied",
        _record(
            "acme/copied",
            description="Nous's own words.",
            knowledge_cutoff="2024-11",
        ),
        live=live,
    )
    row = differs["description"]
    assert (row["value"], row["source"], row["tier"]) == (
        "Nous's own words.",
        "provider",
        1,
    )
    assert row["also_stated"]["value"] == "OpenRouter's words."
    assert row["also_stated"]["source"] == "openrouter_live"
    assert row["also_stated"]["tier_label"] == "OpenRouter live, exact id"
    cutoff = differs["knowledge_cutoff"]
    assert (cutoff["value"], cutoff["source"]) == ("2024-11", "provider")
    assert cutoff["also_stated"]["value"] == "2025-03-31"
    # Where the provider says nothing, OpenRouter's live value stands and
    # models.dev never replaces it.
    silent = capability_payload(
        "nous_portal", "acme/copied", _record("acme/copied"), live=live
    )
    assert silent["knowledge_cutoff"]["value"] == "2025-03-31"
    assert silent["knowledge_cutoff"]["source"] == "openrouter_live"
    assert silent["description"]["source"] == "openrouter_live"


def test_where_the_live_list_is_silent_the_provider_then_models_dev_fill() -> None:
    silent = _live()
    payload = capability_payload(
        "novita",
        "acme/bucketed",
        _record("acme/bucketed", knowledge_cutoff="2025-01"),
        live=silent,
    )
    assert payload["knowledge_cutoff"]["value"] == "2025-01"
    assert payload["knowledge_cutoff"]["source"] == "provider"
    assert payload["description"]["value"] == "models.dev's words."


# ------------------------------------------------------------------- endpoints


def test_litellm_s_endpoints_fill_only_where_the_provider_names_none() -> None:
    lite = _litellm(LiteLLMModel(endpoints=_said(("/v1/chat/completions",))))
    filled = capability_payload("novita", "acme/bucketed", None, litellm=lite)
    row = filled["declared_endpoints"]
    assert (row["value"], row["source"]) == (["/v1/chat/completions"], "litellm")
    kept = capability_payload(
        "novita",
        "acme/bucketed",
        _record("acme/bucketed", endpoints=("chat/completions",)),
        litellm=lite,
    )
    assert kept["declared_endpoints"]["value"] == ["chat/completions"]
    assert kept["declared_endpoints"]["source"] == "provider"


def test_without_litellm_nothing_but_the_four_rows_is_added() -> None:
    record = _record("acme/bucketed", description="Own words.")
    plain = capability_payload("novita", "acme/bucketed", record)
    empty = capability_payload(
        "novita", "acme/bucketed", record, litellm=_litellm(None)
    )
    assert plain == empty


# ---------------------------------------------------------------- page + kinds


def test_the_page_row_reads_the_map_for_its_kind_too() -> None:
    hears = DeclaredModalities(inputs=("audio", "text"), outputs=("text",))
    lite = _litellm(LiteLLMModel(modalities=_said(hears)))
    entry = _model_entry(
        "unknownprov/acme/model",
        None,
        visibility=ModelVisibility.from_raw("", ""),
        overrides=ModelParameterOverrides(),
        configured_refs=frozenset(),
        kind_modalities=lambda _p, _m: (None, None),
        kind_words=lambda _p, _m: (None, None),
        litellm=lite,
    )
    assert entry["kind"]["source"] == "litellm"
    assert entry["kind"]["kinds"] == ["chat", "asr"]
    assert entry["capabilities"]["retires_at"]["source"] == "unknown"


def test_the_page_builds_with_and_without_the_map() -> None:
    infos = (ProviderModelInfo("novita/acme/bucketed"),)
    args = (infos, (), ModelVisibility.from_raw("", ""), ModelParameterOverrides())
    without = build_models_page_payload(*args)
    with_map = build_models_page_payload(*args, litellm=_litellm(None))
    assert without == with_map


def test_the_page_cache_key_takes_the_map_s_mark_only_while_it_is_on() -> None:
    infos = (ProviderModelInfo("novita/acme/bucketed"),)
    args = (infos, (), ModelVisibility.from_raw("", ""), ModelParameterOverrides(), {})
    off = capability_half_key(*args, live_mark=None)
    assert capability_half_key(*args, live_mark=None, litellm_mark=None) == off
    on = capability_half_key(*args, live_mark=None, litellm_mark="7:7")
    assert on != off
    assert capability_half_key(*args, live_mark=None, litellm_mark="7:7") == on
    assert capability_half_key(*args, live_mark=None, litellm_mark="8:7") != on
