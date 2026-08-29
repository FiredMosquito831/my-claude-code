"""The admin Models page API: visibility, overrides, and the capability tiers.

The dashboard had no repeatable coverage of its own, and a cross-page
regression once reached a browser because of it. These exercise the endpoints
the Models page talks to rather than the DOM, so they run everywhere.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.model_admin import (
    apply_visibility_toggle,
    build_models_page_payload,
    capability_payload,
    with_override_row,
)
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    reset_model_overrides_cache,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.reasoning import ReasoningEffort
from tests.api.support import create_test_app, provider_manager_for_app

MODELS_ENDPOINT = "/admin/api/model-admin"


def _local_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _isolated_home(monkeypatch, tmp_path: Path) -> None:
    """Point ~/.fcc at a temp directory and forget any cached override table."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for key in (
        "MODEL",
        "MODEL_VISIBILITY_ALLOW",
        "MODEL_VISIBILITY_DENY",
        "OPEN_ROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_model_overrides_cache()


def _app_with_models(**visibility: str):
    settings = Settings()
    settings.model = "open_router/routed"
    settings.open_router_api_key = "open-router-key"
    for name, value in visibility.items():
        setattr(settings, name, value)
    app = create_test_app(settings)
    provider_manager_for_app(app).cache_model_infos(
        "open_router",
        {
            ProviderModelInfo("routed", max_output_tokens=16384),
            ProviderModelInfo("extra", context_length=128000),
        },
    )
    return app


# --------------------------------------------------------------------- access


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", MODELS_ENDPOINT, None),
        ("post", f"{MODELS_ENDPOINT}/visibility", {"allow": "", "deny": ""}),
        ("post", f"{MODELS_ENDPOINT}/visibility/preview", {"allow": "", "deny": ""}),
        (
            "post",
            f"{MODELS_ENDPOINT}/visibility/toggle",
            {"model_ref": "open_router/routed", "visible": False},
        ),
        (
            "post",
            f"{MODELS_ENDPOINT}/visibility/bulk",
            {"scope": "provider", "action": "hide", "provider_id": "open_router"},
        ),
        (
            "post",
            f"{MODELS_ENDPOINT}/overrides",
            {"scope": "model", "key": "open_router/routed", "updates": {}},
        ),
    ],
)
def test_every_models_endpoint_refuses_a_non_loopback_client(method, path, body):
    """The admin surface is loopback-only and these routes must not be a hole."""

    remote = TestClient(_app_with_models(), client=("203.0.113.10", 50000))
    response = getattr(remote, method)(path, json=body) if body else remote.get(path)

    assert response.status_code == 403


# -------------------------------------------------------------------- payload


def test_the_page_payload_carries_the_tree_overrides_and_capabilities(
    monkeypatch, tmp_path
):
    _isolated_home(monkeypatch, tmp_path)

    body = _local_client(_app_with_models()).get(MODELS_ENDPOINT).json()

    providers = {row["provider_id"]: row for row in body["providers"]}
    assert "open_router" in providers
    refs = {model["model_ref"] for model in providers["open_router"]["models"]}
    assert refs == {"open_router/routed", "open_router/extra"}

    routed = next(
        model
        for model in providers["open_router"]["models"]
        if model["model_ref"] == "open_router/routed"
    )
    assert routed["configured"] is True
    assert routed["visible"] is True
    assert routed["capabilities"]["max_output_tokens"]["value"] == 16384
    assert routed["capabilities"]["max_output_tokens"]["source"] == "provider"
    # Nine editable parameters, each resolving to "not sent" until overridden.
    assert [row["action"] for row in routed["effective"]] == ["inherit"] * 9

    assert body["overrides"]["editable_parameters"] == [
        "frequency_penalty",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "temperature",
        "top_k",
        "top_p",
    ]
    # The fields this page deliberately cannot edit are named with a reason,
    # rather than being silently absent.
    owned = body["overrides"]["owned_elsewhere"]
    assert "max_tokens" in owned and "reasoning_effort" in owned
    assert "output_tokens" in owned["max_tokens"]
    assert "max_tokens" not in body["overrides"]["editable_parameters"]


def test_a_hidden_model_stays_listed_and_a_hidden_route_is_flagged(
    monkeypatch, tmp_path
):
    """Hiding is display-only, and this page is where you undo it."""

    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models(model_visibility_deny="open_router/routed")

    body = _local_client(app).get(MODELS_ENDPOINT).json()

    routed = next(
        model
        for model in body["providers"][0]["models"]
        if model["model_ref"] == "open_router/routed"
    )
    assert routed["visible"] is False
    assert [
        route["model_ref"] for route in body["visibility"]["hidden_route_refs"]
    ] == ["open_router/routed"]
    assert "still resolves" in body["visibility"]["hide_only_notice"]


def test_preview_reports_what_a_pattern_set_would_hide_without_saving(
    monkeypatch, tmp_path
):
    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models()
    client = _local_client(app)

    preview = client.post(
        f"{MODELS_ENDPOINT}/visibility/preview",
        json={"allow": "", "deny": "open_router/ext*"},
    ).json()

    assert preview["hidden_model_refs"] == ["open_router/extra"]
    assert preview["visible_count"] == 1
    # Nothing was persisted: the live payload still shows both as visible.
    live = client.get(MODELS_ENDPOINT).json()
    assert all(model["visible"] for model in live["providers"][0]["models"])


# ------------------------------------------------------------------ visibility


def test_toggling_a_model_off_writes_an_exact_pattern_and_round_trips(
    monkeypatch, tmp_path
):
    """A tick is stored as a one-model glob in the same two lists as any other."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    hidden = client.post(
        f"{MODELS_ENDPOINT}/visibility/toggle",
        json={"model_ref": "open_router/extra", "visible": False},
    )

    assert hidden.status_code == 200
    assert hidden.json()["visibility"]["deny"] == ["open_router/extra"]
    assert hidden.json()["honored"] is True

    shown = client.post(
        f"{MODELS_ENDPOINT}/visibility/toggle",
        json={"model_ref": "open_router/extra", "visible": True},
    )

    assert shown.json()["visibility"]["deny"] == []
    assert shown.json()["visible"] is True


def test_a_toggle_a_user_glob_overrules_is_reported_as_not_honored():
    """The checkbox must not lie when a broader deny pattern still wins."""

    visibility = ModelVisibility(deny=("*:free",))

    updated = apply_visibility_toggle(visibility, "open_router/qwen:free", visible=True)

    assert updated.deny == ("*:free",)
    assert updated.is_visible("open_router/qwen:free") is False


def test_showing_a_model_under_an_opt_in_allow_list_names_it_there():
    visibility = ModelVisibility(allow=("open_router/kept",))

    updated = apply_visibility_toggle(visibility, "open_router/extra", visible=True)

    assert updated.allow == ("open_router/kept", "open_router/extra")
    assert updated.is_visible("open_router/extra") is True


# ------------------------------------------------------------------- overrides


def test_the_three_override_states_survive_the_api(monkeypatch, tmp_path):
    """inherit, force-unset and a forced value must stay distinguishable.

    A single text box could not express the middle one, which is why the store
    has three states and the editor has a mode select rather than a value box.
    """

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    saved = client.post(
        f"{MODELS_ENDPOINT}/overrides",
        json={
            "scope": "model",
            "key": "open_router/routed",
            "updates": {
                "top_p": 0.95,
                "temperature": None,
                "seed": "inherit",
            },
        },
    )

    assert saved.status_code == 200
    routed = next(
        model
        for model in saved.json()["providers"][0]["models"]
        if model["model_ref"] == "open_router/routed"
    )
    assert routed["override"]["top_p"] == {"state": "value", "value": 0.95}
    assert routed["override"]["temperature"] == {"state": "unset", "value": None}
    assert "seed" not in routed["override"]

    effective = {row["name"]: row for row in routed["effective"]}
    assert effective["top_p"]["action"] == "force"
    assert effective["temperature"]["action"] == "unset"
    assert effective["seed"]["action"] == "inherit"

    on_disk = json.loads(
        (tmp_path / ".fcc" / "model_overrides.json").read_text(encoding="utf-8")
    )
    assert on_disk["models"]["open_router/routed"] == {
        "top_p": 0.95,
        "temperature": None,
    }


def test_a_model_row_beats_its_provider_row_per_parameter(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    client.post(
        f"{MODELS_ENDPOINT}/overrides",
        json={
            "scope": "provider",
            "key": "open_router",
            "updates": {"top_p": 0.9, "temperature": 0.4},
        },
    )
    body = client.post(
        f"{MODELS_ENDPOINT}/overrides",
        json={
            "scope": "model",
            "key": "open_router/routed",
            "updates": {"temperature": None},
        },
    ).json()

    routed = next(
        model
        for model in body["providers"][0]["models"]
        if model["model_ref"] == "open_router/routed"
    )
    effective = {row["name"]: row for row in routed["effective"]}
    assert effective["top_p"] == {
        "name": "top_p",
        "action": "force",
        "value": 0.9,
        "from": "provider",
    }
    assert effective["temperature"] == {
        "name": "temperature",
        "action": "unset",
        "value": None,
        "from": "model",
    }


def test_a_parameter_outside_the_allow_list_never_reaches_the_file(
    monkeypatch, tmp_path
):
    """The allow-list is a security boundary: this value lands in a request body."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    client.post(
        f"{MODELS_ENDPOINT}/overrides",
        json={
            "scope": "model",
            "key": "open_router/routed",
            "updates": {"max_tokens": 999999, "api_key": "leak", "top_k": 40},
        },
    )

    on_disk = json.loads(
        (tmp_path / ".fcc" / "model_overrides.json").read_text(encoding="utf-8")
    )
    assert on_disk["models"]["open_router/routed"] == {"top_k": 40}


def test_an_unknown_override_scope_is_rejected(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)

    response = _local_client(_app_with_models()).post(
        f"{MODELS_ENDPOINT}/overrides",
        json={"scope": "everything", "key": "open_router", "updates": {}},
    )

    assert response.status_code == 400


def test_clearing_every_parameter_removes_the_row_entirely():
    overrides = ModelParameterOverrides(models={"open_router/routed": {"top_p": 0.9}})

    cleared = with_override_row(
        overrides,
        scope="model",
        key="open_router/routed",
        updates={"top_p": "inherit"},
    )

    assert cleared.models == {}


# ---------------------------------------------------------------- capabilities


def test_a_provider_reported_capability_is_labelled_authoritative():
    info = ProviderModelInfo(
        "m",
        max_output_tokens=16384,
        supported_parameters=frozenset({"top_p"}),
        default_parameters=(("top_p", 0.95),),
        reasoning_capability=ModelReasoningCapability(
            can_reason=True,
            mandatory=True,
            supports_effort_control=True,
            supported_efforts=frozenset({ReasoningEffort.HIGH}),
        ),
    )

    payload = capability_payload("open_router", "m", info)

    assert payload["max_output_tokens"] == {
        "value": 16384,
        "source": "provider",
        "source_label": "provider /models",
        "approximate": False,
        "reference": False,
        "tier": 1,
        "tier_label": "provider /models, exact id",
    }
    assert payload["reasoning"]["can_reason"]["source"] == "provider"
    assert payload["reasoning"]["mandatory"]["value"] is True
    assert payload["reasoning"]["supported_efforts"]["value"] == ["high"]
    assert payload["default_parameters"]["value"] == [["top_p", 0.95]]
    assert payload["supported_parameters"]["source"] == "provider"


def test_an_unreported_capability_says_unknown_rather_than_guessing(
    monkeypatch, tmp_path
):
    """`None` is "nobody said", which must not render as a published zero."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    payload = capability_payload("a_provider_nobody_describes", "a-model", None)

    assert payload["max_output_tokens"]["value"] is None
    assert payload["max_output_tokens"]["source"] == "unknown"
    assert payload["max_output_tokens"]["approximate"] is False
    assert payload["reasoning"]["can_reason"]["source"] == "unknown"


def test_the_approximate_tier_is_marked_and_carries_its_sample_size(monkeypatch):
    """An approximate answer must show its rung, its sample and real agreement."""

    monkeypatch.setattr(
        "my_claude_code.api.model_admin.models_dev_describes_provider",
        lambda provider_id: False,
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.model_output_limit_tiered",
        lambda provider_id, model_id: (
            512000,
            ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED,
        ),
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.model_reasoning_capability_tiered",
        lambda provider_id, model_id: (
            ModelReasoningCapability(can_reason=True),
            {"can_reason": ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED},
        ),
    )

    class _Match:
        match_count = 51
        output_agreement = 0.6
        output_reporters = 45
        capability = ModelReasoningCapability(can_reason=True)

    monkeypatch.setattr(
        "my_claude_code.api.model_admin.cross_provider_match",
        lambda provider_id, model_id: _Match(),
    )

    payload = capability_payload("commandcode", "minimax/minimax-m3-free", None)

    output = payload["max_output_tokens"]
    assert output["value"] == 512000
    assert output["source"] == "approximate"
    assert output["approximate"] is True
    assert output["tier"] == 10
    assert output["tier_label"] == "cross-provider, bare model"
    assert output["match_count"] == 51
    assert output["agreement"] == 0.6
    assert output["reporters"] == 45
    assert payload["reasoning"]["can_reason"]["source"] == "approximate"
    assert payload["reasoning"]["can_reason"]["tier"] == 10


def test_an_under_sampled_approximate_limit_renders_as_unknown(monkeypatch):
    """The guard's whole point: no number, so no tier and no agreement."""

    monkeypatch.setattr(
        "my_claude_code.api.model_admin.models_dev_describes_provider",
        lambda provider_id: False,
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.model_output_limit_tiered",
        lambda provider_id, model_id: (None, None),
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.model_reasoning_capability_tiered",
        lambda provider_id, model_id: (
            ModelReasoningCapability(can_reason=True),
            {"can_reason": ResolutionTier.CROSS_PROVIDER_EXACT},
        ),
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.cross_provider_match",
        lambda provider_id, model_id: None,
    )

    payload = capability_payload("commandcode", "minimax/minimax-m3-free", None)

    assert payload["max_output_tokens"]["value"] is None
    assert payload["max_output_tokens"]["source"] == "unknown"
    assert payload["max_output_tokens"]["tier"] is None
    assert "agreement" not in payload["max_output_tokens"]
    assert payload["reasoning"]["can_reason"]["tier"] == 7


def test_a_tag_stripped_provider_hit_is_tier_two_not_tier_one():
    """Tier 2 is still the provider's own answer, but must not pass as exact."""

    info = ProviderModelInfo("minimax/minimax-m3", max_output_tokens=512000)

    payload = capability_payload(
        "commandcode",
        "minimax/minimax-m3-free",
        info,
        ResolutionTier.PROVIDER_TAG_STRIPPED,
    )

    assert payload["max_output_tokens"]["value"] == 512000
    assert payload["max_output_tokens"]["source"] == "provider"
    assert payload["max_output_tokens"]["approximate"] is False
    assert payload["max_output_tokens"]["tier"] == 2
    assert (
        payload["max_output_tokens"]["tier_label"] == "provider /models, tag stripped"
    )


def test_a_configured_model_with_no_discovered_metadata_still_appears():
    """A route the user typed must be listed even before any refresh ran."""

    from my_claude_code.config.model_refs import ConfiguredChatModelRef

    payload = build_models_page_payload(
        (),
        (
            ConfiguredChatModelRef(
                model_ref="ghost/model",
                provider_id="ghost",
                model_id="model",
                sources=("MODEL",),
            ),
        ),
        ModelVisibility(),
        ModelParameterOverrides(),
    )

    assert payload["providers"][0]["provider_id"] == "ghost"
    model = payload["providers"][0]["models"][0]
    assert model["has_metadata"] is False
    assert model["configured"] is True


# --------------------------------------------------------------------------- #
# The measured reasoning chip: what the log saw, not what metadata declares.
# --------------------------------------------------------------------------- #


def _payload_with_measurement(measured):
    from my_claude_code.config.model_refs import ConfiguredChatModelRef

    return build_models_page_payload(
        (),
        (
            ConfiguredChatModelRef(
                model_ref="ghost/model",
                provider_id="ghost",
                model_id="model",
                sources=("MODEL",),
            ),
        ),
        ModelVisibility(),
        ModelParameterOverrides(),
        measured=measured,
    )


def test_the_models_payload_carries_the_measured_reasoning_counts():
    payload = _payload_with_measurement(
        {
            "ghost/model": {
                "model_ref": "ghost/model",
                "attempts": 317,
                "requested": 3,
                "returned": 0,
                "unmeasured": 170,
            }
        }
    )
    model = payload["providers"][0]["models"][0]
    assert model["reasoning_measured"]["requested"] == 3
    assert model["reasoning_measured"]["returned"] == 0
    assert payload["measured_days"] == 7


def test_a_model_with_no_traffic_reports_no_measurement():
    """Absence is not zero: a zeroed row would claim a measurement never made."""
    payload = _payload_with_measurement({})
    model = payload["providers"][0]["models"][0]
    assert model["reasoning_measured"] is None


def test_the_models_payload_still_builds_with_the_request_log_disabled():
    payload = _payload_with_measurement(None)
    model = payload["providers"][0]["models"][0]
    assert model["reasoning_measured"] is None
    assert payload["measured_days"] == 7


def test_the_models_page_names_the_dialects_origin() -> None:
    """Three provenances, three operator-facing labels, one payload key.

    The page used to say only "what this host parses". After PR F the same
    sentence can be a probe someone wrote down, the OpenAI standard assumed of
    every compatible host, or the host's own 400 -- and an operator deciding
    whether to trust it needs to know which.
    """
    from my_claude_code.api.model_admin import dialect_payload
    from my_claude_code.core.reasoning import (
        ReasoningDialect,
        ReasoningDialectOrigin,
        ReasoningEffort,
    )

    default = dialect_payload(
        ReasoningDialect(
            effort_values=frozenset({ReasoningEffort.LOW}),
            effort_field="reasoning_effort",
            origin=ReasoningDialectOrigin.DEFAULT,
        )
    )
    assert default["origin"] == "default"
    assert default["origin_label"] == "default OpenAI dialect"
    assert default["learned_rejections"] == []

    declared = dialect_payload(ReasoningDialect(toggle=True, toggle_field="thinking"))
    assert declared["origin"] == "declared"
    assert declared["origin_label"] == "declared by this provider"

    # An undeclared dialect stays exactly as it was: unknown never restricts,
    # and it gains no provenance keys to misread.
    unknown = dialect_payload(None)
    assert unknown["known"] is False
    assert "origin" not in unknown


def test_a_learned_rejection_is_dated_on_the_page() -> None:
    """The date is what makes a learned rejection answerable rather than eerie."""
    from my_claude_code.api.model_admin import dialect_payload
    from my_claude_code.core.reasoning import (
        ReasoningDialect,
        ReasoningDialectOrigin,
    )

    payload = dialect_payload(
        ReasoningDialect(
            origin=ReasoningDialectOrigin.LEARNED,
            learned_rejections=(("reasoning_effort", "2026-08-29"),),
        )
    )
    assert payload["origin_label"] == "learned from the host's own rejection"
    assert payload["learned_rejections"] == [
        {"field": "reasoning_effort", "since": "2026-08-29"}
    ]


def test_a_field_the_vote_won_is_labelled_at_the_vote_rung(monkeypatch):
    """Two rungs on one model, and each field says honestly where it came from.

    For a gateway models.dev has no section of its own for, the reasoning
    *controls* now resolve to the more capable of the reference row and the
    cross-provider vote, so a single model can carry a field decided at tier 5
    beside one decided at tier 7. The page must report the rung that actually
    produced each value; labelling a voted value as the reference catalogue
    would be exactly the kind of over-confident provenance this ladder exists
    to remove.
    """

    monkeypatch.setattr(
        "my_claude_code.api.model_admin.models_dev_describes_provider",
        lambda provider_id: False,
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.model_output_limit_tiered",
        lambda provider_id, model_id: (100000, ResolutionTier.OPENROUTER_EXACT),
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.model_reasoning_capability_tiered",
        lambda provider_id, model_id: (
            ModelReasoningCapability(
                can_reason=True,
                supports_effort_control=True,
                supports_toggle_control=True,
            ),
            {
                "can_reason": ResolutionTier.OPENROUTER_EXACT,
                "supports_toggle_control": ResolutionTier.OPENROUTER_EXACT,
                "supports_effort_control": ResolutionTier.CROSS_PROVIDER_EXACT,
            },
        ),
    )
    monkeypatch.setattr(
        "my_claude_code.api.model_admin.cross_provider_match",
        lambda provider_id, model_id: None,
    )

    payload = capability_payload("commandcode", "openai/o3", None)
    reasoning = payload["reasoning"]

    won_by_the_vote = reasoning["supports_effort_control"]
    assert won_by_the_vote["tier"] == ResolutionTier.CROSS_PROVIDER_EXACT.value
    assert won_by_the_vote["tier_label"] == "cross-provider, exact id"
    assert won_by_the_vote["reference"] is False

    kept_by_the_reference = reasoning["supports_toggle_control"]
    assert kept_by_the_reference["tier"] == ResolutionTier.OPENROUTER_EXACT.value
    assert kept_by_the_reference["tier_label"] == "OpenRouter catalogue, exact id"
    assert kept_by_the_reference["reference"] is True

    # The limit is resolved by a different function and is deliberately not
    # subject to the same tie-break: a limit is a deployment property and stays
    # at the tightest rung that stated it.
    assert payload["max_output_tokens"]["tier"] == ResolutionTier.OPENROUTER_EXACT.value


# ----------------------------------------------------------------------- bulk

BULK_ENDPOINT = f"{MODELS_ENDPOINT}/visibility/bulk"


def _bulk(client: TestClient, **body):
    payload = {"scope": "provider", "action": "hide", "model_refs": []}
    payload.update(body)
    return client.post(BULK_ENDPOINT, json=payload)


def test_hiding_a_whole_provider_writes_one_glob_not_one_pattern_per_model(
    monkeypatch, tmp_path
):
    """317 exact refs is a 10 KB env line that stops applying the moment the
    provider publishes model 318. One glob keeps meaning what it said."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    body = _bulk(client, provider_id="open_router").json()

    assert body["visibility"]["deny"] == ["open_router/*"]
    assert len(body["visibility"]["deny"]) == 1
    assert body["wrote_glob"] == "open_router/*"
    assert {row["visible"] for row in body["results"]} == {False}
    assert all(row["honored"] for row in body["results"])


def test_hiding_a_filtered_subset_writes_exact_patterns_not_a_glob(
    monkeypatch, tmp_path
):
    """A filtered subset is a selection, not a standing policy about a provider."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    body = _bulk(
        client, provider_id="open_router", model_refs=["open_router/routed"]
    ).json()

    assert body["visibility"]["deny"] == ["open_router/routed"]
    assert body["wrote_glob"] is None


def test_showing_a_whole_provider_removes_its_glob_and_its_exact_patterns(
    monkeypatch, tmp_path
):
    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models(
        model_visibility_deny="open_router/*,open_router/extra,*:free"
    )
    client = _local_client(app)

    body = _bulk(client, action="show", provider_id="open_router").json()

    assert body["visibility"]["deny"] == ["*:free"]
    assert set(body["removed_patterns"]) == {"open_router/*", "open_router/extra"}


def test_showing_a_whole_provider_never_deletes_a_user_glob_that_merely_overlaps(
    monkeypatch, tmp_path
):
    """Deleting a pattern we did not write is unrecoverable and its blast
    radius is not confined to the provider the user clicked on."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models(model_visibility_deny="open*"))

    body = _bulk(client, action="show", provider_id="open_router").json()

    assert body["visibility"]["deny"] == ["open*"]
    assert body["removed_patterns"] == []
    assert all(row["honored"] is False for row in body["results"])
    assert {row["blocked_by"] for row in body["results"]} == {"open*"}


def test_a_bulk_result_reports_honored_per_ref_and_names_the_blocking_pattern(
    monkeypatch, tmp_path
):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models(model_visibility_deny="*extra"))

    body = _bulk(client, action="show", provider_id="open_router").json()

    assert body["honored_count"] == 1
    assert body["unhonored_count"] == 1
    blocked = [row for row in body["results"] if not row["honored"]]
    assert blocked[0]["model_ref"] == "open_router/extra"
    assert blocked[0]["blocked_by"] == "*extra"
    assert all("blocked_by" not in row for row in body["results"] if row["honored"])


def test_a_bulk_show_under_an_opt_in_allow_list_names_the_provider_there(
    monkeypatch, tmp_path
):
    """The bulk mirror of the single toggle's allow-list rule."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models(model_visibility_allow="other/*"))

    body = _bulk(client, action="show", provider_id="open_router").json()

    assert body["visibility"]["allow"] == ["other/*", "open_router/*"]
    assert all(row["honored"] for row in body["results"])


def test_inverting_a_provider_writes_exact_patterns_and_flips_each_model(
    monkeypatch, tmp_path
):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models(model_visibility_deny="open_router/extra"))

    body = _bulk(client, action="invert", provider_id="open_router").json()

    assert body["wrote_glob"] is None
    states = {row["model_ref"]: row["visible"] for row in body["results"]}
    assert states == {"open_router/routed": False, "open_router/extra": True}


def test_invert_is_computed_against_the_state_before_any_mutation(
    monkeypatch, tmp_path
):
    """Folding as it goes would let the first write decide the second target."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models(model_visibility_deny="open_router/extra"))

    body = _bulk(client, action="invert", provider_id="open_router").json()

    assert all(row["honored"] for row in body["results"])


def test_a_bulk_action_writes_the_settings_exactly_once(monkeypatch, tmp_path):
    """The §0.6 correctness claim: one gesture, one commit, one lock."""

    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models()
    runtime = app.state.services.admin
    calls: list[int] = []
    original = runtime.apply_admin_config_with

    async def counted(build):
        calls.append(1)
        return await original(build)

    runtime.apply_admin_config_with = counted
    client = _local_client(app)

    _bulk(client, provider_id="open_router")

    assert calls == [1]


def test_a_bulk_response_carries_the_previous_patterns_for_undo(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models(model_visibility_deny="*:free"))

    body = _bulk(client, provider_id="open_router").json()
    assert body["previous"] == {"allow": [], "deny": ["*:free"]}

    restored = client.post(
        f"{MODELS_ENDPOINT}/visibility",
        json={
            "allow": ",".join(body["previous"]["allow"]),
            "deny": ",".join(body["previous"]["deny"]),
        },
    ).json()

    assert restored["visibility"] == {"allow": [], "deny": ["*:free"]}


def test_a_failed_bulk_write_reports_no_results_and_no_undo(monkeypatch, tmp_path):
    """A response that claims a write it never made is worse than an error."""

    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models()
    runtime = app.state.services.admin

    async def refuse(build):
        return {"errors": ["nope"]}

    runtime.apply_admin_config_with = refuse
    body = _bulk(_local_client(app), provider_id="open_router").json()

    assert body["errors"] == ["nope"]
    for key in ("results", "honored_count", "previous", "wrote_glob"):
        assert key not in body


def test_a_failed_single_toggle_no_longer_claims_it_was_honored(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    app = _app_with_models()
    runtime = app.state.services.admin

    async def refuse(build):
        return {"errors": ["nope"]}

    runtime.apply_admin_config_with = refuse
    body = (
        _local_client(app)
        .post(
            f"{MODELS_ENDPOINT}/visibility/toggle",
            json={"model_ref": "open_router/routed", "visible": False},
        )
        .json()
    )

    assert body["errors"] == ["nope"]
    assert "honored" not in body


def test_a_bulk_request_over_the_limit_is_refused_by_name(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    response = _bulk(
        client,
        scope="selection",
        model_refs=[f"open_router/m{index}" for index in range(2001)],
    )

    assert response.status_code == 400
    assert "MODEL_VISIBILITY_BULK_LIMIT" in response.json()["detail"]


def test_an_unknown_bulk_scope_or_action_is_rejected(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    assert _bulk(client, scope="everything").status_code == 400
    assert _bulk(client, action="delete", provider_id="open_router").status_code == 400


def test_a_bulk_action_never_returns_the_whole_model_tree(monkeypatch, tmp_path):
    """Returning the tree would cost the same 3.4 MB the per-tick refetch does."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    assert "providers" not in _bulk(client, provider_id="open_router").json()


def test_a_hidden_model_is_still_listed_and_still_routes_after_a_bulk_hide(
    monkeypatch, tmp_path
):
    """The hide-only product principle, at bulk scale."""

    _isolated_home(monkeypatch, tmp_path)
    client = _local_client(_app_with_models())

    # Asked before the write, because persisting settings in an isolated home
    # rebuilds the provider from an env file that has no API key and so drops
    # its cached catalogue -- an artefact of the fixture, not of hiding.
    preview = client.post(
        f"{MODELS_ENDPOINT}/visibility/preview",
        json={"allow": "", "deny": "open_router/*"},
    ).json()

    # Hidden, and still a configured route that resolves and serves.
    assert "open_router/routed" in preview["hidden_model_refs"]
    assert any(
        route["model_ref"] == "open_router/routed"
        for route in preview["hidden_route_refs"]
    )
    assert "still resolves" in preview["hide_only_notice"]

    body = _bulk(client, provider_id="open_router").json()

    assert body["visibility"]["deny"] == ["open_router/*"]
    assert {row["visible"] for row in body["results"]} == {False}
