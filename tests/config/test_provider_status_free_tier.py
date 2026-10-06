"""A keyless OpenCode Zen says "Free models ready", because it is (7.78.0).

Since 7.34.0 free Zen models go out on OpenCode's anonymous ``public``
credential, so an install with no ``OPENCODE_API_KEY`` serves them -- and a
fresh install routes four Claude tiers there. The Providers card still said
"Missing key" in amber. The truth is decided once, on the server, in the status
payload every surface reads; these tests pin it against the runtime's own rule
so the card and the request path cannot disagree about one install.
"""

import pytest

from my_claude_code.api.route_status import NO_CREDENTIALS, credential_problems
from my_claude_code.config.admin.status import (
    FREE_TIER_READY_LABEL,
    FREE_TIER_READY_STATUS,
    provider_config_status,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.opencode_credentials import (
    public_credential_enabled,
)


def _by_id(state: dict) -> dict[str, dict]:
    return {entry["provider_id"]: entry for entry in provider_config_status(state)}


def _credential(mode: str) -> dict:
    return {"OPENCODE_FREE_TIER_CREDENTIAL": {"value": mode}}


def test_a_keyless_zen_on_the_default_says_free_models_ready() -> None:
    zen = _by_id({})["opencode"]

    assert zen["status"] == FREE_TIER_READY_STATUS == "free_ready"
    assert zen["label"] == FREE_TIER_READY_LABEL == "Free models ready"
    assert zen["key_count"] == 0
    assert zen["summary"] == (
        "Configured by default: free models work with no key. A key is needed "
        "only for paid models and for OpenCode Go."
    )


def test_the_opt_out_with_no_key_still_says_missing_key() -> None:
    zen = _by_id(_credential("key"))["opencode"]

    assert zen["status"] == "missing_key"
    assert zen["label"] == "Missing key"
    assert "summary" not in zen


def test_a_key_still_says_configured() -> None:
    for mode in ("public", "key"):
        state = {
            **_credential(mode),
            "OPENCODE_API_KEY": {"value": "sk-test-fake-0001,sk-test-fake-0002"},
        }
        zen = _by_id(state)["opencode"]
        assert zen["status"] == "configured", mode
        assert zen["label"] == "Configured", mode
        assert zen["key_count"] == 2, mode
        assert "summary" not in zen, mode


@pytest.mark.parametrize("mode", ["public", "key"])
def test_opencode_go_is_never_affected(mode: str) -> None:
    """Go is a paid subscription endpoint: no key is a missing key, always."""

    go = _by_id(_credential(mode))["opencode_go"]

    assert go["status"] == "missing_key"
    assert go["label"] == "Missing key"
    assert "summary" not in go


@pytest.mark.parametrize(
    "mode", ["public", "key", "", "  ", "KEY", " Key ", "Public", "nonsense"]
)
def test_the_card_reads_the_credential_setting_exactly_as_the_runtime_does(
    mode: str,
) -> None:
    """One rule: free-ready exactly when the runtime would use ``public``."""

    runtime = public_credential_enabled(
        Settings.model_validate({"OPENCODE_FREE_TIER_CREDENTIAL": mode})
    )
    status = _by_id(_credential(mode))["opencode"]["status"]

    assert (status == FREE_TIER_READY_STATUS) is runtime
    assert status == (FREE_TIER_READY_STATUS if runtime else "missing_key")


def test_only_a_provider_that_declares_a_free_tier_credential_can_be_free_ready() -> (
    None
):
    declared = {
        provider_id
        for provider_id, descriptor in PROVIDER_CATALOG.items()
        if descriptor.free_tier_credential_attr is not None
    }
    ready = {
        entry["provider_id"]
        for entry in provider_config_status({})
        if entry["status"] == FREE_TIER_READY_STATUS
    }

    assert declared == {"opencode"}
    assert ready == declared
    assert PROVIDER_CATALOG["opencode"].free_tier_credential_attr == (
        "opencode_free_tier_credential"
    )


def test_every_other_card_payload_is_unchanged_in_shape() -> None:
    """``summary`` is added only to a free-ready card, never to anything else."""

    for entry in provider_config_status({}):
        if entry["status"] != FREE_TIER_READY_STATUS:
            assert "summary" not in entry, entry["provider_id"]


def _zen_and_go(state: dict) -> list[dict]:
    # Only the two OpenCode cards: the sign-in providers consult stored logins,
    # which are not what this test is about.
    return [
        entry
        for entry in provider_config_status(state)
        if entry["provider_id"] in ("opencode", "opencode_go")
    ]


def test_the_model_config_hints_agree_with_the_card() -> None:
    """The route banner reads the same payload: free-ready is not "no credentials"."""

    ready = credential_problems(_zen_and_go({}), {}, lambda _: None)
    assert "opencode" not in ready
    assert ready["opencode_go"]["state"] == NO_CREDENTIALS

    opted_out = credential_problems(_zen_and_go(_credential("key")), {}, lambda _: None)
    assert opted_out["opencode"] == {
        "state": NO_CREDENTIALS,
        "action": "add_key",
        "display_name": "OpenCode Zen",
    }
