"""Freeze ``WEBSEARCH_CATALOG`` insertion order and field sanity."""

from pathlib import Path

from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import (
    SUPPORTED_WEBSEARCH_PROVIDER_IDS,
    WEBSEARCH_CATALOG,
)
from my_claude_code.websearch.adapters import ADAPTER_CLASSES

_EXPECTED_WEBSEARCH_ORDER: tuple[str, ...] = (
    "ddgs",
    "ollama",
    "exa",
    "tavily",
    "brave",
    "searxng",
    "jina",
    "serper",
    "firecrawl",
    "linkup",
    "perplexity",
    "parallel",
    "searchapi",
    "serpapi",
    # 7.113.0: library documentation, appended so Auto's order is unchanged
    # (and Auto never picks it: ``auto_select=False``).
    "context7",
)


def test_websearch_catalog_key_order_matches_canonical_plan() -> None:
    """ddgs first (keyless fallback); keyed providers follow per coordination spec."""

    assert tuple(WEBSEARCH_CATALOG.keys()) == _EXPECTED_WEBSEARCH_ORDER
    assert SUPPORTED_WEBSEARCH_PROVIDER_IDS == _EXPECTED_WEBSEARCH_ORDER


def test_websearch_catalog_descriptors_are_internally_consistent() -> None:
    problems: list[str] = []
    for provider_id, desc in WEBSEARCH_CATALOG.items():
        if desc.provider_id != provider_id:
            problems.append(f"{provider_id}: provider_id mismatch {desc.provider_id!r}")
        if not desc.display_name.strip():
            problems.append(f"{provider_id}: display_name is empty")
        if desc.requires_key:
            if desc.credential_env is None:
                problems.append(
                    f"{provider_id}: requires_key but credential_env is None"
                )
            if desc.settings_attr is None:
                problems.append(
                    f"{provider_id}: requires_key but settings_attr is None"
                )
        else:
            if desc.credential_env is not None:
                problems.append(f"{provider_id}: keyless but credential_env set")
            if desc.settings_attr is not None:
                problems.append(f"{provider_id}: keyless but settings_attr set")
        if desc.credential_env is not None and not desc.credential_env.endswith("_KEY"):
            problems.append(
                f"{provider_id}: credential_env {desc.credential_env} shape"
            )
        if (
            desc.base_url_attr is None
            and desc.default_base_url is None
            and desc.requires_key
        ):
            problems.append(f"{provider_id}: keyed provider without a default base URL")

    assert problems == []


def test_websearch_catalog_settings_attrs_match_settings_fields() -> None:
    """Every settings_attr exists on Settings with the matching env alias."""

    problems: list[str] = []
    for provider_id, desc in WEBSEARCH_CATALOG.items():
        for attr, expected_env in (
            (desc.settings_attr, desc.credential_env),
            (desc.base_url_attr, "SEARXNG_BASE_URL"),
        ):
            if attr is None:
                continue
            field = Settings.model_fields.get(attr)
            if field is None:
                problems.append(f"{provider_id}: Settings.{attr} missing")
                continue
            if expected_env is not None and str(field.validation_alias) != expected_env:
                problems.append(
                    f"{provider_id}: Settings.{attr} alias "
                    f"{field.validation_alias!r} != {expected_env!r}"
                )

    assert problems == []


def test_websearch_catalog_matches_adapter_registry() -> None:
    """ADAPTER_CLASSES mirrors the catalog ids, order, provider ids, and flags."""

    assert tuple(ADAPTER_CLASSES.keys()) == _EXPECTED_WEBSEARCH_ORDER
    problems: list[str] = []
    for provider_id, adapter_cls in ADAPTER_CLASSES.items():
        desc = WEBSEARCH_CATALOG[provider_id]
        if provider_id != adapter_cls.PROVIDER_ID:
            problems.append(f"{provider_id}: PROVIDER_ID {adapter_cls.PROVIDER_ID!r}")
        if desc.supports_domains != adapter_cls.SUPPORTS_DOMAINS:
            problems.append(
                f"{provider_id}: SUPPORTS_DOMAINS={adapter_cls.SUPPORTS_DOMAINS} "
                f"but catalog supports_domains={desc.supports_domains}"
            )

    assert problems == []


# Exact advanced option sets per coordination spec (docs/AGENT_SPEC_WEBSEARCH_ADV.md).
_EXPECTED_ADVANCED_ENVS: dict[str, tuple[str, ...]] = {
    "ddgs": ("DDGS_BACKEND", "DDGS_REGION", "DDGS_TIMELIMIT", "DDGS_SAFESEARCH"),
    "ollama": (),
    "exa": (
        "EXA_SEARCH_TYPE",
        "EXA_CONTENTS",
        "EXA_CATEGORY",
        "EXA_MAX_AGE_HOURS",
        "EXA_START_PUBLISHED_DATE",
        "EXA_END_PUBLISHED_DATE",
        "EXA_USER_LOCATION",
    ),
    "tavily": (
        "TAVILY_CHUNKS_PER_SOURCE",
        "TAVILY_COUNTRY",
        "TAVILY_START_DATE",
        "TAVILY_END_DATE",
        "TAVILY_SEARCH_DEPTH",
        "TAVILY_TOPIC",
        "TAVILY_TIME_RANGE",
        "TAVILY_INCLUDE_ANSWER",
        "TAVILY_INCLUDE_RAW_CONTENT",
    ),
    "brave": (
        "BRAVE_SAFESEARCH",
        "BRAVE_SEARCH_MODE",
        "BRAVE_EXTRA_SNIPPETS",
        "BRAVE_FRESHNESS",
        "BRAVE_COUNTRY",
        "BRAVE_SEARCH_LANG",
        "BRAVE_LLM_MAX_TOKENS",
    ),
    "searxng": (
        "SEARXNG_SAFESEARCH",
        "SEARXNG_ENGINES",
        "SEARXNG_CATEGORIES",
        "SEARXNG_TIME_RANGE",
        "SEARXNG_LANGUAGE",
    ),
    "jina": ("JINA_MAX_TOKENS", "JINA_SITE", "JINA_GL"),
    "serper": ("SERPER_GL", "SERPER_HL", "SERPER_TBS", "SERPER_RICH_BLOCKS"),
    "firecrawl": (
        "FIRECRAWL_COUNTRY",
        "FIRECRAWL_CATEGORIES",
        "FIRECRAWL_SOURCES",
        "FIRECRAWL_SCRAPE_FORMAT",
        "FIRECRAWL_TBS",
        "FIRECRAWL_LOCATION",
    ),
    "linkup": (
        "LINKUP_FROM_DATE",
        "LINKUP_TO_DATE",
        "LINKUP_DEPTH",
        "LINKUP_OUTPUT_TYPE",
    ),
    "perplexity": (
        "PERPLEXITY_SEARCH_RECENCY",
        "PERPLEXITY_CONTEXT_SIZE",
        "PERPLEXITY_MAX_TOKENS_PER_PAGE",
    ),
    "parallel": (
        "PARALLEL_LOCATION",
        "PARALLEL_MODE",
        "PARALLEL_EXCERPT_CHARS",
        "PARALLEL_TOTAL_CHARS",
    ),
    "searchapi": (
        "SEARCHAPI_SAFE",
        "SEARCHAPI_ENGINE",
        "SEARCHAPI_TIME_PERIOD",
        "SEARCHAPI_GL",
        "SEARCHAPI_HL",
    ),
    "serpapi": (
        "SERPAPI_SAFE",
        "SERPAPI_ENGINE",
        "SERPAPI_TBS",
        "SERPAPI_GL",
        "SERPAPI_HL",
    ),
    # Its API takes a query and a library and nothing else MCC would set.
    "context7": (),
}


def test_only_context7_is_kept_out_of_auto() -> None:
    """Auto picks a web search; a documentation provider is chosen by name."""

    assert [
        provider_id
        for provider_id, desc in WEBSEARCH_CATALOG.items()
        if not desc.auto_select
    ] == ["context7"]

_FIELD_TYPES = frozenset({"select", "text", "number", "boolean"})


def test_websearch_catalog_advanced_option_envs_match_spec() -> None:
    """The exact per-provider option env table from the coordination spec."""

    actual = {
        provider_id: tuple(spec.env for spec in desc.advanced_options)
        for provider_id, desc in WEBSEARCH_CATALOG.items()
    }
    assert actual == _EXPECTED_ADVANCED_ENVS


def test_websearch_catalog_option_specs_are_internally_consistent() -> None:
    problems: list[str] = []
    seen_envs: set[str] = set()
    for provider_id, desc in WEBSEARCH_CATALOG.items():
        prefix = f"{provider_id.upper()}_"
        for spec in desc.advanced_options:
            where = f"{provider_id}:{spec.env}"
            if not spec.env.startswith(prefix):
                problems.append(f"{where}: env does not start with {prefix!r}")
            if spec.env in seen_envs:
                problems.append(f"{where}: duplicate env across catalog")
            seen_envs.add(spec.env)
            if spec.field_type not in _FIELD_TYPES:
                problems.append(f"{where}: field_type {spec.field_type!r}")
            if not spec.label.strip():
                problems.append(f"{where}: label is empty")
            if spec.field_type == "select":
                if not spec.options:
                    problems.append(f"{where}: select without options")
                elif spec.options[0][0] != spec.default:
                    problems.append(
                        f"{where}: first select option {spec.options[0][0]!r} "
                        f"!= default {spec.default!r}"
                    )
                values = [value for value, _label in spec.options]
                if len(set(values)) != len(values):
                    problems.append(f"{where}: duplicate select values")
            elif spec.options:
                problems.append(f"{where}: non-select with options")
            if spec.field_type != "boolean" and spec.default != "":
                problems.append(f"{where}: unexpected default {spec.default!r}")
    assert problems == []


def test_every_catalog_option_is_documented_in_env_example() -> None:
    """`.env.example` is what the README points at for the full option list.

    Options were added to the catalog without landing here, so the documented
    list silently fell behind what the admin UI actually offers.
    """

    env_example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    missing = [
        f"{descriptor.provider_id}:{option.env}"
        for descriptor in WEBSEARCH_CATALOG.values()
        for option in descriptor.advanced_options
        if option.env not in env_example
    ]
    assert missing == []
