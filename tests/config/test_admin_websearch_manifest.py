"""Admin manifest contract for catalog-derived web search fields."""

from types import SimpleNamespace

from my_claude_code.config.admin import websearch_manifest
from my_claude_code.config.admin.manifest import (
    FIELD_BY_KEY,
    SECTIONS,
    ConfigFieldSpec,
    ConfigOptionSpec,
)
from my_claude_code.config.admin.websearch_manifest import (
    ROTATION_POLICY_OPTIONS,
    websearch_field_specs,
)
from my_claude_code.config.websearch_catalog import (
    SUPPORTED_WEBSEARCH_PROVIDER_IDS,
    WEBSEARCH_CATALOG,
)
from my_claude_code.websearch.rotation import ROTATION_POLICIES


def _option_values(field) -> tuple[str, ...]:
    return tuple(
        option.value if isinstance(option, ConfigOptionSpec) else option
        for option in field.options
    )


def _fake_option(
    env: str,
    label: str,
    field_type: str,
    *,
    default: str = "",
    options: tuple[tuple[str, str], ...] = (),
    cost_note: str = "",
):
    """Duck-typed stand-in for the catalog's WebSearchOptionSpec."""

    return SimpleNamespace(
        env=env,
        label=label,
        field_type=field_type,
        default=default,
        options=options,
        cost_note=cost_note,
    )


def _fake_descriptor(provider_id: str, options: list | None = None):
    """Duck-typed WebSearchDescriptor; advanced_options only when provided."""

    descriptor = SimpleNamespace(
        provider_id=provider_id,
        display_name=f"Fake {provider_id}",
        credential_env=None,
        credential_url=None,
        settings_attr=None,
        free_tier="free",
    )
    if options is not None:
        descriptor.advanced_options = tuple(options)
    return descriptor


def _fake_fields(catalog: dict, monkeypatch) -> dict[str, ConfigFieldSpec]:
    monkeypatch.setattr(websearch_manifest, "WEBSEARCH_CATALOG", catalog)
    return {spec["key"]: ConfigFieldSpec(**spec) for spec in websearch_field_specs()}


def test_websearch_section_follows_web_tools_section() -> None:
    section_ids = [section.section_id for section in SECTIONS]
    assert "websearch" in section_ids
    assert section_ids.index("websearch") == section_ids.index("web_tools") + 1
    section = next(s for s in SECTIONS if s.section_id == "websearch")
    assert section.label == "Web Search"


def test_web_search_provider_select_lists_catalog_in_order() -> None:
    field = FIELD_BY_KEY["WEB_SEARCH_PROVIDER"]
    assert field.section_id == "websearch"
    assert field.field_type == "select"
    assert field.settings_attr == "web_search_provider"
    assert field.default == "auto"
    assert _option_values(field) == (
        "auto",
        "off",
        "disabled",
        *SUPPORTED_WEBSEARCH_PROVIDER_IDS,
    )
    labels = {
        option.value: option.label
        for option in field.options
        if isinstance(option, ConfigOptionSpec)
    }
    for provider_id, descriptor in WEBSEARCH_CATALOG.items():
        assert labels[provider_id] == descriptor.display_name


def test_web_search_fallback_policy_is_explicit() -> None:
    field = FIELD_BY_KEY["WEB_SEARCH_FALLBACK_POLICY"]
    assert field.section_id == "websearch"
    assert field.field_type == "select"
    assert field.settings_attr == "web_search_fallback_policy"
    assert field.default == "auto"
    assert _option_values(field) == ("auto", "none", "ddgs", "legacy")
    assert "named provider" in field.description
    assert "Missing credentials always fail visibly" in field.description


def test_secret_fields_generated_for_every_keyed_catalog_provider() -> None:
    keyed = [
        descriptor
        for descriptor in WEBSEARCH_CATALOG.values()
        if descriptor.credential_env is not None
    ]
    assert len(keyed) == 12
    for descriptor in keyed:
        field = FIELD_BY_KEY.get(descriptor.credential_env)
        assert field is not None, f"{descriptor.credential_env} missing from manifest"
        assert field.section_id == "websearch"
        assert field.field_type == "secret"
        assert field.secret is True
        assert field.settings_attr == descriptor.settings_attr


def test_keyless_providers_have_no_credential_field() -> None:
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None:
            assert f"{descriptor.provider_id.upper()}_API_KEY" not in FIELD_BY_KEY


def test_rotation_select_generated_per_credential_env() -> None:
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env is None:
            continue
        field = FIELD_BY_KEY.get(f"{descriptor.credential_env}_ROTATION")
        assert field is not None, (
            f"{descriptor.credential_env}_ROTATION missing from manifest"
        )
        assert field.section_id == "websearch"
        assert field.field_type == "select"
        # Rotation is dotenv-only: it must not bind a Settings attribute.
        assert field.settings_attr is None
        assert _option_values(field) == ("", *ROTATION_POLICIES)


def test_rotation_options_mirror_websearch_rotation_policies() -> None:
    assert ROTATION_POLICY_OPTIONS == ROTATION_POLICIES


def test_searxng_base_url_field() -> None:
    field = FIELD_BY_KEY["SEARXNG_BASE_URL"]
    assert field.section_id == "websearch"
    assert field.settings_attr == "searxng_base_url"
    assert field.secret is False


def test_websearch_field_specs_cover_section_fields() -> None:
    keys = {spec["key"] for spec in websearch_field_specs()}
    expected = {
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_FALLBACK_POLICY",
        "WEBSEARCH_LOG_ENABLED",
        "WEBSEARCH_LOG_CAPTURE_CONTENT",
        "WEBSEARCH_LOG_CONTENT_MAX_CHARS",
        "WEBSEARCH_LOG_MAX_ROWS",
        "WEBSEARCH_DIGEST_CHARS",
        "WEBSEARCH_DIGEST_CONTENT_CHARS",
        "WEBSEARCH_DIGEST_ANSWER",
        "SEARXNG_BASE_URL",
        *(
            descriptor.credential_env
            for descriptor in WEBSEARCH_CATALOG.values()
            if descriptor.credential_env is not None
        ),
        *(
            f"{descriptor.credential_env}_ROTATION"
            for descriptor in WEBSEARCH_CATALOG.values()
            if descriptor.credential_env is not None
        ),
        *(
            option.env
            for descriptor in WEBSEARCH_CATALOG.values()
            for option in getattr(descriptor, "advanced_options", ())
        ),
    }
    assert keys == expected
    assert all(spec["section_id"] == "websearch" for spec in websearch_field_specs())


def test_websearch_capture_fields_are_explicit_and_restart_aware() -> None:
    capture = FIELD_BY_KEY["WEBSEARCH_LOG_CAPTURE_CONTENT"]
    cap = FIELD_BY_KEY["WEBSEARCH_LOG_CONTENT_MAX_CHARS"]

    assert capture.field_type == "boolean"
    assert capture.settings_attr == "websearch_log_capture_content"
    assert capture.default == "true"
    assert capture.restart_required is True
    assert cap.field_type == "number"
    assert cap.settings_attr == "websearch_log_content_max_chars"
    assert cap.default == "2000000"
    assert cap.restart_required is True


def test_advanced_option_fields_generated_per_provider(monkeypatch) -> None:
    catalog = {
        "exa": _fake_descriptor(
            "exa",
            [
                _fake_option(
                    "EXA_SEARCH_TYPE",
                    "Search type",
                    "select",
                    options=(
                        ("", "auto"),
                        ("instant", "instant"),
                        ("deep", "deep"),
                    ),
                    cost_note="deep* = $0.015/query vs $0.005",
                ),
                _fake_option("EXA_MAX_AGE_HOURS", "Max age hours", "number"),
            ],
        ),
        "ddgs": _fake_descriptor(
            "ddgs",
            [
                _fake_option(
                    "DDGS_SAFESEARCH",
                    "Safe search",
                    "select",
                    options=(("", "moderate"), ("on", "on"), ("off", "off")),
                ),
            ],
        ),
    }
    fields = _fake_fields(catalog, monkeypatch)

    search_type = fields["EXA_SEARCH_TYPE"]
    assert search_type.section_id == "websearch"
    assert search_type.field_type == "select"
    assert search_type.advanced is True
    # Dotenv-only: it must not bind a Settings attribute or be secret.
    assert search_type.settings_attr is None
    assert search_type.secret is False
    assert search_type.label == "Search type"
    assert search_type.default == ""
    assert search_type.description == "deep* = $0.015/query vs $0.005"
    assert search_type.options == (
        ConfigOptionSpec("", "auto"),
        ConfigOptionSpec("instant", "instant"),
        ConfigOptionSpec("deep", "deep"),
    )

    max_age = fields["EXA_MAX_AGE_HOURS"]
    assert max_age.field_type == "number"
    assert max_age.advanced is True
    assert max_age.settings_attr is None
    assert max_age.options == ()

    safesearch = fields["DDGS_SAFESEARCH"]
    assert safesearch.field_type == "select"
    assert safesearch.advanced is True
    assert _option_values(safesearch) == ("", "on", "off")


def test_advanced_option_field_type_mapping(monkeypatch) -> None:
    catalog = {
        "probe": _fake_descriptor(
            "probe",
            [
                _fake_option(
                    "PROBE_SELECT",
                    "Select",
                    "select",
                    options=(("a", "A"), ("b", "B")),
                ),
                _fake_option("PROBE_TEXT", "Text", "text", default="us-en"),
                _fake_option("PROBE_NUMBER", "Number", "number"),
                _fake_option("PROBE_BOOL", "Bool", "boolean", default="true"),
            ],
        ),
    }
    fields = _fake_fields(catalog, monkeypatch)

    assert fields["PROBE_SELECT"].field_type == "select"
    assert fields["PROBE_SELECT"].options == (
        ConfigOptionSpec("a", "A"),
        ConfigOptionSpec("b", "B"),
    )
    assert fields["PROBE_TEXT"].field_type == "text"
    assert fields["PROBE_TEXT"].default == "us-en"
    assert fields["PROBE_NUMBER"].field_type == "number"
    assert fields["PROBE_BOOL"].field_type == "boolean"
    assert fields["PROBE_BOOL"].default == "true"
    for key in ("PROBE_TEXT", "PROBE_NUMBER", "PROBE_BOOL"):
        assert fields[key].options == ()


def test_advanced_option_description_uses_cost_note_or_fallback(
    monkeypatch,
) -> None:
    catalog = {
        "probe": _fake_descriptor(
            "probe",
            [
                _fake_option("PROBE_PLAIN", "Plain", "text"),
                _fake_option(
                    "PROBE_COSTLY",
                    "Costly",
                    "text",
                    cost_note="2 credits/query",
                ),
            ],
        ),
    }
    fields = _fake_fields(catalog, monkeypatch)

    assert fields["PROBE_COSTLY"].description == "2 credits/query"
    # Empty cost notes still yield a short dotenv-only hint for the UI.
    assert fields["PROBE_PLAIN"].description


def test_descriptors_without_advanced_options_emit_no_advanced_fields(
    monkeypatch,
) -> None:
    catalog = {
        "legacy": _fake_descriptor("legacy"),
        "empty": _fake_descriptor("empty", []),
    }
    monkeypatch.setattr(websearch_manifest, "WEBSEARCH_CATALOG", catalog)

    specs = websearch_field_specs()
    assert [spec for spec in specs if spec.get("advanced")] == []
    # The legacy descriptor still participates in the provider select.
    provider = next(spec for spec in specs if spec["key"] == "WEB_SEARCH_PROVIDER")
    assert "legacy" in tuple(option.value for option in provider["options"])
