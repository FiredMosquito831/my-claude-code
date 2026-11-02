"""Import smoke: the new modules load, and the declared table is coherent."""

import importlib

from my_claude_code.config.usage_sources import (
    MECHANISM_ENDPOINT,
    PROVIDER_SOURCES,
    endpoint_providers,
    source_for,
)


def test_the_package_imports() -> None:
    for name in (
        "my_claude_code.core.usage",
        "my_claude_code.core.usage.reading",
        "my_claude_code.core.usage.fields",
        "my_claude_code.api.admin_usage_routes",
        "my_claude_code.providers.runtime.usage_reads",
        "my_claude_code.providers.runtime.usage_credentials",
    ):
        assert importlib.import_module(name) is not None


def test_every_readable_row_names_a_module_that_exists() -> None:
    from my_claude_code.core.usage import MODULES

    for declared in PROVIDER_SOURCES:
        endpoint = declared.endpoint
        if endpoint is None:
            continue
        assert endpoint.adapter in MODULES, declared.provider_id


def test_every_declared_adapter_is_used_by_a_row() -> None:
    from my_claude_code.core.usage import MODULES

    used = {row.endpoint.adapter for row in PROVIDER_SOURCES if row.endpoint}
    assert used == set(MODULES)


def test_an_endpoint_mechanism_always_names_a_host_and_a_path() -> None:
    for declared in PROVIDER_SOURCES:
        endpoint = declared.endpoint
        if endpoint is None:
            continue
        assert endpoint.host.startswith("https://"), declared.provider_id
        assert endpoint.path.startswith("/"), declared.provider_id
        assert MECHANISM_ENDPOINT in declared.mechanisms, declared.provider_id


def test_a_row_with_no_mechanism_is_unsupported_and_unreadable() -> None:
    novita = source_for("novita")
    assert novita.mechanisms == ()
    assert novita.supported is False
    assert novita.readable is False
    assert novita.source == "unsupported"


def test_an_unknown_provider_id_is_unsupported_rather_than_missing() -> None:
    row = source_for("some_custom_provider")
    assert row.provider_id == "some_custom_provider"
    assert row.supported is False
    assert row.readable is False


def test_the_readable_rows_are_exactly_the_seven_that_need_no_new_credential() -> None:
    """The scope of USAGE PR 3, stated as a test rather than as a promise.

    Seven routes, each carried by a credential MCC already holds: five API-key
    rows and the two OAuth subscriptions. Nothing here needs a management key, a
    cookie, an admin key or a third-party host.
    """

    assert set(endpoint_providers()) == {
        "open_router",
        "deepseek",
        "kimi",
        "commandcode",
        "opencode_go",
        "anthropic_oauth",
        "chatgpt_oauth",
    }


def test_openrouter_reads_the_key_route_and_never_the_credits_one() -> None:
    """§3.2: ``/credits`` needs a management key. It is not built, in any form."""

    endpoint = source_for("open_router").endpoint
    assert endpoint is not None
    assert endpoint.path == "/api/v1/key"
    assert endpoint.credential == "api-key-env"
    assert endpoint.credential_env == "OPENROUTER_API_KEY"


def test_no_row_carries_a_management_key_or_a_cookie_credential() -> None:
    for declared in PROVIDER_SOURCES:
        endpoint = declared.endpoint
        if endpoint is None:
            continue
        assert endpoint.credential in {"api-key-env", "oauth-store"}, (
            declared.provider_id
        )
        lowered = endpoint.url.lower()
        assert "management" not in lowered
        assert "/credits" not in lowered or declared.provider_id == "commandcode"


def test_every_row_declares_its_provenance() -> None:
    """Only a verified row may produce a number (§3 of the research dossier)."""

    for declared in PROVIDER_SOURCES:
        endpoint = declared.endpoint
        if endpoint is None:
            continue
        assert endpoint.provenance in {"vendor-doc", "vendor-binary", "third-party"}


#: Every provider name and route string the release must never carry. This is
#: the scope fence as a test: a later release that quietly adds one of these is
#: doing something the user's decisions forbade.
_FORBIDDEN_SUBSTRINGS = (
    "/api/v1/credits",  # OpenRouter account credits: needs a management key
    "/organizations/usage_report",
    "/organization/costs",
    "/copilot_internal",
    "/coding/v1/usages",  # Kimi Coding: undocumented, unbuilt
    "/api/monitor/usage/quota/limit",  # Z.ai: not in the user's list
    "/v1/token_plan/remains",  # MiniMax
    "_server?id=",  # OpenCode's browser-cookie console
)


def test_no_row_names_a_route_the_scope_forbids() -> None:
    for declared in PROVIDER_SOURCES:
        endpoint = declared.endpoint
        if endpoint is None:
            continue
        url = endpoint.url.lower()
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden.lower() not in url, (declared.provider_id, forbidden)


def test_no_row_uses_a_credential_MCC_does_not_hold() -> None:
    """A read uses what the user already has, from where they already have it."""

    allowed = {"api-key-env", "oauth-store"}
    for declared in PROVIDER_SOURCES:
        endpoint = declared.endpoint
        if endpoint is None:
            continue
        assert endpoint.credential in allowed


def test_opencode_go_is_third_party_verified_and_says_so() -> None:
    endpoint = source_for("opencode_go").endpoint
    assert endpoint is not None
    assert endpoint.url == "https://opencode.ai/zen/go/v1/usage"
    assert endpoint.provenance == "third-party"


def test_kimi_coding_is_declared_and_deliberately_not_read() -> None:
    """Its route is third-party evidence only; the row must say "not read yet"."""

    row = source_for("kimi_coding")
    assert row.mechanisms == ("provider-endpoint",)
    assert row.supported is True
    assert row.endpoint is None
    assert row.readable is False
