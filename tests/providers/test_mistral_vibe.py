"""Mistral Vibe: the second Mistral credential, and the boundary around it.

8.12.0 adds one provider, ``mistral_vibe``, beside the existing ``mistral``.
Both authenticate to ``https://api.mistral.ai/v1`` and differ in what they draw
on -- a Studio key spends the plan's API allowance, a Vibe plan key spends the
subscription's coding budget -- so the whole risk of this change is that the
two get conflated somewhere. Three things are proved here rather than asserted:

1. **The Studio provider is the object it always was.** ``MistralProvider``
   gained a keyword-only ``provider_id`` defaulting to ``"mistral"``, so the
   existing instance is constructed from the same arguments and sends the same
   bytes. The equality test builds both and compares their outbound header
   names and their request bodies.
2. **The two credentials are separate pools.** Each descriptor reads exactly
   one settings attribute, and neither falls back to the other. The test that
   matters is the negative one: with only ``MISTRAL_VIBE_KEY`` set, the Studio
   provider is not configured, and with only ``MISTRAL_API_KEY`` set, the Vibe
   provider is not.
3. **The identity guard.** ``mistral_vibe`` must never be aliased onto
   ``mistral``'s models.dev bucket. That alias is the OpenCode Go defect in a
   new place: it would hand every Vibe model the Studio bucket's prices, which
   is a number the Vibe pool may not be billed at.

No model id is typed anywhere in this file. The ids used below are fixtures
named ``fixture-*``; the real ones arrive from the host's own ``/v1/models``.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import openai
import pytest
from httpx import Request, Response
from openai._models import FinalRequestOptions

from my_claude_code.config.provider_catalog import (
    MISTRAL_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.mistral.client import MISTRAL_REASONING_DIALECT
from my_claude_code.providers.runtime import factory
from my_claude_code.providers.runtime.config import (
    provider_credential,
    require_provider_credential,
)
from my_claude_code.providers.runtime.models_dev import (
    PROVIDER_ID_ALIASES,
    models_dev_bucket_id,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter

#: The two ids this release owns, and the settings attribute each reads.
STUDIO = "mistral"
VIBE = "mistral_vibe"

STUDIO_KEY = "sk-studio-0000aaaa1111bbbb"
VIBE_KEY = "sk-vibe-9999cccc2222dddd"


def _config(api_key: str) -> ProviderConfig:
    return ProviderConfig(
        api_key=api_key,
        base_url=MISTRAL_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
    )


def _build(provider_id: str, api_key: str) -> MistralProvider:
    """Build one provider through the factory the runtime actually uses."""
    settings = Settings.model_validate({})
    provider = factory._SPECIAL_PROVIDER_FACTORIES[provider_id](
        _config(api_key), settings, passthrough_rate_limiter()
    )
    assert isinstance(provider, MistralProvider)
    return provider


# --------------------------------------------------------------------------
# 1. The descriptor pair
# --------------------------------------------------------------------------


def test_both_ids_are_in_the_catalog_and_the_vibe_one_is_new() -> None:
    assert STUDIO in PROVIDER_CATALOG
    assert VIBE in PROVIDER_CATALOG


def test_the_two_cards_are_distinguishable_at_a_glance() -> None:
    """Q2: two bare "Mistral" cards would be a UI defect that looks like data."""

    assert PROVIDER_CATALOG[STUDIO].display_name == "Mistral Studio (API)"
    assert PROVIDER_CATALOG[VIBE].display_name == "Mistral Vibe (subscription)"
    assert PROVIDER_CATALOG[STUDIO].display_name != PROVIDER_CATALOG[VIBE].display_name


def test_the_two_credentials_are_named_and_never_aliased() -> None:
    """Q3 Option A, restated by the user: no ``MISTRAL_STUDIO_KEY`` alias."""

    studio = PROVIDER_CATALOG[STUDIO]
    vibe = PROVIDER_CATALOG[VIBE]
    assert studio.credential_env == "MISTRAL_API_KEY"
    assert studio.credential_attr == "mistral_api_key"
    assert vibe.credential_env == "MISTRAL_VIBE_KEY"
    assert vibe.credential_attr == "mistral_vibe_api_key"
    assert studio.credential_env != vibe.credential_env
    assert "MISTRAL_STUDIO_KEY" not in Settings.model_fields
    assert "MISTRAL_STUDIO_KEY" not in {
        str(field.validation_alias) for field in Settings.model_fields.values()
    }


def test_the_new_provider_is_in_the_subscription_group_and_studio_stays_direct() -> (
    None
):
    """Q4. Placement only -- the Providers page is a flat grid within a group."""

    assert PROVIDER_CATALOG[STUDIO].group == "direct"
    assert PROVIDER_CATALOG[VIBE].group == "subscription"


def test_both_dial_the_same_host_and_the_constant_is_not_duplicated() -> None:
    """§4.3: no new base-URL constant, and no ``*_BASE_URL`` setting either."""

    assert PROVIDER_CATALOG[STUDIO].default_base_url == MISTRAL_DEFAULT_BASE
    assert PROVIDER_CATALOG[VIBE].default_base_url == MISTRAL_DEFAULT_BASE
    assert PROVIDER_CATALOG[STUDIO].base_url_attr is None
    assert PROVIDER_CATALOG[VIBE].base_url_attr is None


def test_the_vibe_provider_declares_no_media_surface() -> None:
    """§7: nothing outside chat and the model list is established for this pool.

    ``mistral``'s streaming transcription surface is untouched -- it is pinned
    by its own test in ``tests/api/test_media_transcriptions.py`` and by the
    catalogue's own contract.
    """

    assert PROVIDER_CATALOG[VIBE].media_surfaces == ()
    assert len(PROVIDER_CATALOG[STUDIO].media_surfaces) == 1


def test_each_provider_has_exactly_one_construction_owner() -> None:
    """The import-time assertion in ``factory`` is load-bearing; state it here.

    A descriptor with no construction owner raises ``AssertionError`` at
    import, which is the desired failure -- but an import that never happens
    in a test run would hide it, so the two owners are named explicitly.
    """

    assert VIBE in factory._SPECIAL_PROVIDER_FACTORIES
    assert STUDIO in factory._SPECIAL_PROVIDER_FACTORIES
    assert VIBE not in factory.OPENAI_CHAT_PROFILES
    assert STUDIO not in factory.OPENAI_CHAT_PROFILES


# --------------------------------------------------------------------------
# 2. The class: one implementation, two ids
# --------------------------------------------------------------------------


def test_the_provider_id_argument_defaults_to_studio() -> None:
    """Every call site before this release passed it implicitly."""

    provider = MistralProvider(
        _config(STUDIO_KEY), rate_limiter=passthrough_rate_limiter()
    )
    assert provider._provider_id == STUDIO


def test_the_vibe_instance_carries_its_own_id() -> None:
    provider = _build(VIBE, VIBE_KEY)
    assert provider._provider_id == VIBE


def test_the_two_instances_share_one_class_and_one_dialect() -> None:
    """Reuse, not a copy: the reasoning shape cannot drift between them."""

    studio = _build(STUDIO, STUDIO_KEY)
    vibe = _build(VIBE, VIBE_KEY)
    assert type(studio) is type(vibe) is MistralProvider
    assert studio.reasoning_dialect("fixture-model") is MISTRAL_REASONING_DIALECT
    assert vibe.reasoning_dialect("fixture-model") is MISTRAL_REASONING_DIALECT


def test_the_two_instances_send_the_same_header_names() -> None:
    """The equality promise, at the layer the identity contract records.

    ``test_outbound_identity_is_declared`` cannot carry ``mistral_vibe``: that
    file's map is keyed on ``OPENAI_CHAT_PROFILES`` and neither Mistral id has
    a profile entry -- both are special-factory providers. So the same claim
    is made here, from the two real constructors.
    """

    def header_names(provider: MistralProvider) -> frozenset[str]:
        request = provider._client._build_request(
            FinalRequestOptions.construct(
                method="post",
                url="/chat/completions",
                json_data={"model": "fixture-model", "messages": []},
            )
        )
        return frozenset(name.lower() for name in request.headers)

    assert header_names(_build(STUDIO, STUDIO_KEY)) == header_names(
        _build(VIBE, VIBE_KEY)
    )


def test_the_two_instances_build_the_same_body() -> None:
    """Same request in, same bytes out -- the provider id is not on the wire."""

    request = make_messages_request("fixture-model")
    studio = _build(STUDIO, STUDIO_KEY)._build_request_body(request)
    vibe = _build(VIBE, VIBE_KEY)._build_request_body(request)
    assert studio == vibe
    assert json.dumps(studio, sort_keys=True) == json.dumps(vibe, sort_keys=True)


def test_the_provider_id_is_not_sent_upstream() -> None:
    """It keys MCC's own tables; nothing about it reaches the host."""

    body = _build(VIBE, VIBE_KEY)._build_request_body(
        make_messages_request("fixture-model")
    )
    assert VIBE not in json.dumps(body)


# --------------------------------------------------------------------------
# 3. Two credentials, never one
# --------------------------------------------------------------------------


def test_each_descriptor_reads_exactly_its_own_setting() -> None:
    studio_only = Settings.model_validate({"MISTRAL_API_KEY": STUDIO_KEY})
    vibe_only = Settings.model_validate({"MISTRAL_VIBE_KEY": VIBE_KEY})

    assert provider_credential(PROVIDER_CATALOG[STUDIO], studio_only) == STUDIO_KEY
    assert provider_credential(PROVIDER_CATALOG[VIBE], studio_only) == ""
    assert provider_credential(PROVIDER_CATALOG[VIBE], vibe_only) == VIBE_KEY
    assert provider_credential(PROVIDER_CATALOG[STUDIO], vibe_only) == ""


def test_a_missing_vibe_key_names_the_vibe_variable_and_never_the_studio_one() -> None:
    """The error a user sees must not send them to the wrong key."""

    studio_only = Settings.model_validate({"MISTRAL_API_KEY": STUDIO_KEY})
    vibe = PROVIDER_CATALOG[VIBE]
    with pytest.raises(Exception) as raised:
        require_provider_credential(vibe, provider_credential(vibe, studio_only))
    assert "MISTRAL_VIBE_KEY" in str(raised.value)
    assert "MISTRAL_API_KEY" not in str(raised.value)


def test_the_studio_provider_still_configures_from_its_own_key_alone() -> None:
    """The regression this release must not cause: existing installs keep working."""

    studio_only = Settings.model_validate({"MISTRAL_API_KEY": STUDIO_KEY})
    studio = PROVIDER_CATALOG[STUDIO]
    require_provider_credential(studio, provider_credential(studio, studio_only))


# --------------------------------------------------------------------------
# 4. The identity guard (§4.6)
# --------------------------------------------------------------------------


def test_mistral_vibe_does_not_resolve_to_the_studio_bucket() -> None:
    """The alias that would recreate the OpenCode Go defect, refused as a test."""

    index = {"mistral": {"models": {}}, "mistral_vibe_other": {"models": {}}}
    assert models_dev_bucket_id(index, VIBE) is None
    assert models_dev_bucket_id(index, VIBE) != "mistral"


def test_no_alias_points_mistral_vibe_at_its_sibling() -> None:
    assert VIBE not in PROVIDER_ID_ALIASES
    assert PROVIDER_ID_ALIASES.get(VIBE) != "mistral"


def test_the_two_shared_bucket_pairs_are_the_only_ones() -> None:
    """The generalised rule, with today's two deliberate exceptions named.

    Two MCC ids may share a models.dev bucket only when they share a
    credential source -- one account, one budget. ``anthropic``/
    ``anthropic_oauth`` and ``openai``/``chatgpt_oauth`` qualify: same product,
    same money, a different credential *kind*. ``mistral``/``mistral_vibe`` do
    not: different keys, different pools, different money.

    Written as data so that adding ``"mistral_vibe": "mistral"`` to the alias
    map fails here as well as in the test above.
    """

    shared_credential_pairs = {
        frozenset({"anthropic", "anthropic_oauth"}),
        frozenset({"openai", "chatgpt_oauth"}),
    }
    for provider_id in PROVIDER_CATALOG:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is None:
            continue
        pair = frozenset({provider_id, alias})
        if provider_id in {STUDIO, VIBE} or alias in {STUDIO, VIBE}:
            assert pair in shared_credential_pairs, (
                f"{provider_id} -> {alias} would let one Mistral credential "
                "read the other's catalogue"
            )


# --------------------------------------------------------------------------
# 5. A refused request is not a bad key
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vibe_refusal_is_classified_by_the_shared_policy_not_as_a_bad_key() -> (
    None
):
    """§4.8 with the evidence that exists -- and no invented matcher.

    Mistral's refusal shape for an operation outside a Vibe key's entitlement
    is **unverified** (spec §10.1.3): no captured body exists, so this release
    adds no phrase matcher and no 403 -> ``FREE_TIER`` rule. What it can prove
    is the part that does not depend on the shape: a 403 is not silently
    retried on the Studio key, because the Studio credential is never
    consulted for a Vibe request at all.

    The assertion is mechanical -- the Studio provider is never constructed --
    which is the only version of "never a silent fallback" that is a proof
    rather than a promise.
    """

    built: list[str] = []
    real_factories = dict(factory._SPECIAL_PROVIDER_FACTORIES)

    def recording(provider_id: str):
        make = real_factories[provider_id]

        def wrapper(config, settings, rate_limiter):
            built.append(provider_id)
            return make(config, settings, rate_limiter)

        return wrapper

    with patch.dict(
        factory._SPECIAL_PROVIDER_FACTORIES,
        {STUDIO: recording(STUDIO), VIBE: recording(VIBE)},
    ):
        vibe = _build(VIBE, VIBE_KEY)
        request = make_messages_request("fixture-model")

        refusal = openai.PermissionDeniedError(
            "this operation is not available for this key",
            response=Response(
                403,
                request=Request("POST", "https://api.mistral.ai/v1/chat/completions"),
            ),
            body={"error": {"message": "this operation is not available for this key"}},
        )
        with patch.object(
            vibe._client.chat.completions, "create", new_callable=AsyncMock
        ) as create:
            create.side_effect = refusal
            with pytest.raises(ExecutionFailure) as raised:
                async for _event in vibe.stream_response(request):
                    pass

    assert built == [VIBE], "the Studio credential was consulted for a Vibe request"
    # The generic classification, unchanged: no matcher exists for this shape,
    # so a 403 stays the shared policy's answer rather than becoming a guessed
    # ``FREE_TIER``. The provider's own words reach the operator.
    assert raised.value.kind is FailureKind.UPSTREAM
    assert "this operation is not available for this key" in raised.value.message


def test_the_vibe_provider_installs_no_failure_override() -> None:
    """No matcher exists, so no override is installed -- stated, not implied.

    The base returns ``None`` for every error, which is the generic
    classification. An override that guessed would be worse than none: it
    would take a 403 away from the authentication branch on a shape nobody has
    captured.
    """

    vibe = _build(VIBE, VIBE_KEY)
    refusal = openai.PermissionDeniedError(
        "not available for this key",
        response=Response(
            403, request=Request("POST", "https://api.mistral.ai/v1/chat/completions")
        ),
        body={"error": {"message": "not available for this key"}},
    )
    assert vibe._provider_failure_override(refusal) is None


# --------------------------------------------------------------------------
# 6. The two cards' honest state
# --------------------------------------------------------------------------


def test_the_usage_rows_say_two_different_true_things() -> None:
    from my_claude_code.config.usage_sources import source_for

    studio = source_for(STUDIO)
    vibe = source_for(VIBE)
    assert studio.supported is False
    assert studio.readable is False
    assert vibe.credential_bound is True
    assert vibe.readable is False
    assert vibe.needs_credential == "a browser session cookie"


def test_neither_usage_row_carries_a_number_or_a_route() -> None:
    """No figure, no percentage, no reset date, no dollar conversion, no call."""

    from my_claude_code.config.usage_sources import source_for

    for provider_id in (STUDIO, VIBE):
        row = source_for(provider_id)
        assert row.endpoint is None
        assert row.mechanisms == ()
        assert not any(char.isdigit() for char in row.detail)


# --------------------------------------------------------------------------
# 7. No model id is typed in source (§8.5.7)
# --------------------------------------------------------------------------


def test_no_source_file_names_a_mistral_model_id() -> None:
    """The ids arrive from the host's own list; nothing here may guess one.

    A name ladder that guesses lands on the wrong spelling 68 times out of 69
    for the DeepSeek id alone (spec §5.3), and a hard-coded id is a stale id
    the first time the vendor renames one.

    One occurrence predates this release and is named here rather than
    excluded by a wildcard: ``models_dev.py``'s "deliberately NOT aliased"
    audit quotes ``codestral-latest`` in a comment about the Codestral bucket
    it refuses to point at Studio. Anything else appearing is new.
    """

    root = Path(__file__).resolve().parents[2] / "src"
    forbidden = (
        "mistral-large-4",
        "mistral-small-latest",
        "zai-glm-5-3",
        "deepseek-v4-1-flash-1017",
        "deepseek-v4.1-flash-1017",
        "codestral-latest",
        "mistral-vibe-cli-latest",
    )
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(root).as_posix()}: {name}"
            for name in forbidden
            if name in text
        )
    assert offenders == [
        "my_claude_code/providers/runtime/models_dev.py: codestral-latest"
    ], offenders
