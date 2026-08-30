"""Catalog-derived Admin web search fields (selection, credentials, options)."""

from typing import Any

from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG

from .spec import ConfigOptionSpec

# Mirrors websearch.rotation.ROTATION_POLICIES. The import boundary forbids
# config/ from importing websearch/, so the literal tuple lives here and
# tests/config/test_admin_websearch_manifest.py asserts parity.
ROTATION_POLICY_OPTIONS: tuple[str, ...] = (
    "single",
    "round_robin",
    "least_used",
    "failover",
)

_ROTATION_OPTION_LABELS: dict[str, str] = {
    "single": "Single key",
    "round_robin": "Round robin",
    "least_used": "Least used",
    "failover": "Failover",
}

ROTATION_DEFAULT_OPTION = ConfigOptionSpec(
    "",
    "Auto (failover across multiple keys, single otherwise)",
)


def websearch_field_specs() -> tuple[dict[str, Any], ...]:
    """Return web search fields generated from the web search catalog."""

    return (
        _provider_select_spec(),
        _fallback_policy_spec(),
        *_analytics_field_specs(),
        _searxng_base_url_spec(),
        *_credential_field_specs(),
        *_advanced_option_field_specs(),
    )


def _analytics_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "WEBSEARCH_LOG_ENABLED",
            "label": "Web Search Analytics",
            "section_id": "websearch",
            "field_type": "boolean",
            "settings_attr": "websearch_log_enabled",
            "default": "true",
            "description": "Store local route and provider-attempt telemetry.",
        },
        {
            "key": "WEBSEARCH_LOG_CAPTURE_CONTENT",
            "label": "Capture Search Input and Output",
            "section_id": "websearch",
            "field_type": "boolean",
            "settings_attr": "websearch_log_capture_content",
            "default": "true",
            "restart_required": True,
            "description": (
                "Store complete normalized provider input/output for drill-down. "
                "Disable to retain only lengths and SHA-256 hashes."
            ),
        },
        {
            "key": "WEBSEARCH_LOG_CONTENT_MAX_CHARS",
            "label": "Search I/O Character Cap",
            "section_id": "websearch",
            "field_type": "number",
            "settings_attr": "websearch_log_content_max_chars",
            "default": "2000000",
            "restart_required": True,
            "description": (
                "Maximum stored characters for each input/output JSON payload "
                "(minimum 512); the default (~2 MB) retains real provider output "
                "in full. Larger payloads keep a bounded preview and hash."
            ),
        },
        {
            "key": "WEBSEARCH_LOG_MAX_ROWS",
            "label": "Search Analytics Retention",
            "section_id": "websearch",
            "field_type": "number",
            "settings_attr": "websearch_log_max_rows",
            "default": "50000",
            "restart_required": True,
            "description": "Maximum retained provider-attempt and route rows.",
        },
        {
            "key": "WEBSEARCH_DIGEST_CHARS",
            "label": "Result Snippet Cap",
            "section_id": "websearch",
            "field_type": "number",
            "settings_attr": "websearch_digest_chars",
            "default": "600",
            "description": (
                "Characters kept from each result's snippet in the digest "
                "handed to the model. Raising it costs prompt tokens on every "
                "search; some providers return snippets longer than this and "
                "the remainder is discarded."
            ),
        },
        {
            "key": "WEBSEARCH_DIGEST_CONTENT_CHARS",
            "label": "Extracted Page Text Cap",
            "section_id": "websearch",
            "field_type": "number",
            "settings_attr": "websearch_digest_content_chars",
            "default": "2000",
            "description": (
                "Separate, larger cap for the full page text a provider "
                "extracted, which only arrives when you opted into it "
                "(EXA_CONTENTS, TAVILY_INCLUDE_RAW_CONTENT, "
                "FIRECRAWL_SCRAPE_FORMAT). It has its own cap so turning "
                "content on is not trimmed back to snippet size; 0 keeps "
                "snippets only."
            ),
        },
        {
            "key": "WEBSEARCH_DIGEST_ANSWER",
            "label": "Lead With The Provider Answer",
            "section_id": "websearch",
            "field_type": "boolean",
            "settings_attr": "websearch_digest_answer",
            "default": "true",
            "description": (
                "Put the provider's own direct answer, when it returns one, "
                "above the numbered results. Turn it off to send only the "
                "results the model can cite."
            ),
        },
    )


def _provider_select_spec() -> dict[str, Any]:
    return {
        "key": "WEB_SEARCH_PROVIDER",
        "label": "Web Search Provider",
        "section_id": "websearch",
        "field_type": "select",
        "settings_attr": "web_search_provider",
        "default": "auto",
        "options": (
            ConfigOptionSpec("auto", "Auto-select (resilient by default)"),
            ConfigOptionSpec("off", "Legacy DuckDuckGo scrape only"),
            ConfigOptionSpec("disabled", "Disabled (reject web searches)"),
            *(
                ConfigOptionSpec(descriptor.provider_id, descriptor.display_name)
                for descriptor in WEBSEARCH_CATALOG.values()
            ),
        ),
        "description": (
            "Backend for Claude Code's web_search server tool. Auto selects the "
            "first configured provider (or keyless DDGS). A named provider is "
            "strict unless the fallback policy below explicitly allows fallback. "
            "Legacy uses only the old HTML scrape; Disabled performs no search."
        ),
    }


def _fallback_policy_spec() -> dict[str, Any]:
    return {
        "key": "WEB_SEARCH_FALLBACK_POLICY",
        "label": "Fallback Policy",
        "section_id": "websearch",
        "field_type": "select",
        "settings_attr": "web_search_fallback_policy",
        "default": "auto",
        "options": (
            ConfigOptionSpec(
                "auto",
                "Context-aware (auto-select resilient, named provider strict)",
            ),
            ConfigOptionSpec("none", "Strict (no fallback)"),
            ConfigOptionSpec("ddgs", "Fallback to DDGS only"),
            ConfigOptionSpec("legacy", "Fallback to DDGS, then legacy scrape"),
        ),
        "description": (
            "Controls failures after the selected provider. Auto preserves the "
            "resilient DDGS-to-legacy chain only for auto-selection; a named "
            "provider fails visibly. Missing credentials always fail visibly."
        ),
    }


def _searxng_base_url_spec() -> dict[str, Any]:
    return {
        "key": "SEARXNG_BASE_URL",
        "label": "SearXNG Base URL",
        "section_id": "websearch",
        "settings_attr": "searxng_base_url",
        "description": (
            "Self-hosted SearXNG instance URL; the instance must enable "
            "format=json in its settings.yml."
        ),
    }


def _credential_field_specs() -> tuple[dict[str, Any], ...]:
    specs: list[dict[str, Any]] = []
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None or descriptor.settings_attr is None:
            continue
        specs.append(
            {
                "key": descriptor.credential_env,
                "label": f"{descriptor.display_name} API Key",
                "section_id": "websearch",
                "field_type": "secret",
                "settings_attr": descriptor.settings_attr,
                "secret": True,
                "description": (
                    f"{descriptor.free_tier}. Comma-separate multiple keys for "
                    f"rotation. Obtain a key at {descriptor.credential_url}."
                ),
            }
        )
        specs.append(
            {
                "key": f"{descriptor.credential_env}_ROTATION",
                "label": f"{descriptor.display_name} Key Rotation",
                "section_id": "websearch",
                "field_type": "select",
                "options": (
                    ROTATION_DEFAULT_OPTION,
                    *(
                        ConfigOptionSpec(policy, _ROTATION_OPTION_LABELS[policy])
                        for policy in ROTATION_POLICY_OPTIONS
                    ),
                ),
                "description": (
                    "Rotation policy across the comma-separated keys above "
                    "(dotenv-only, hot-reloaded)."
                ),
            }
        )
    return tuple(specs)


# WebSearchOptionSpec.field_type -> ConfigFieldSpec.field_type. Unrecognized
# option types degrade to a plain text input so the admin UI keeps working if
# the catalog later grows new option types.
_ADVANCED_OPTION_FIELD_TYPES: dict[str, str] = {
    "select": "select",
    "text": "text",
    "number": "number",
    "boolean": "boolean",
}


def _advanced_option_field_specs() -> tuple[dict[str, Any], ...]:
    """Return dotenv-only advanced option fields from catalog descriptors."""

    specs: list[dict[str, Any]] = []
    for descriptor in WEBSEARCH_CATALOG.values():
        # Descriptors predate advanced_options on this branch; tolerate both
        # shapes so the manifest works before and after the catalog lands.
        for option in getattr(descriptor, "advanced_options", ()):
            spec: dict[str, Any] = {
                "key": option.env,
                "label": option.label,
                "section_id": "websearch",
                "field_type": _ADVANCED_OPTION_FIELD_TYPES.get(
                    option.field_type,
                    "text",
                ),
                "default": option.default,
                "advanced": True,
                "description": option.cost_note or "Dotenv-only advanced option.",
            }
            if option.field_type == "select":
                spec["options"] = tuple(
                    ConfigOptionSpec(value, label) for value, label in option.options
                )
            specs.append(spec)
    return tuple(specs)
