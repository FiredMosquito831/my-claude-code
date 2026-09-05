"""The offline evidence chain that replaced the hand-written ChatGPT allowlist.

Every test here is hermetic: not one of them looks at the developer's own
Codex install, request log or credential, except the single drift guard at the
bottom, which skips when Codex is absent. That is on purpose -- the defect
being fixed is a catalogue that was right on somebody's machine once.
"""

import base64
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
)
from my_claude_code.core.request_log import (
    ServedModelObservation,
    observed_served_models,
)
from my_claude_code.providers.chatgpt_oauth import codex_catalogue
from my_claude_code.providers.chatgpt_oauth.codex_catalogue import (
    CodexCatalogue,
    CodexCatalogueEntry,
    catalogue_evidence,
    embedded_json_document,
    listing_vetoes,
    load_codex_catalogue,
    parse_codex_catalogue,
    retired_entries,
)
from my_claude_code.providers.chatgpt_oauth.credentials import (
    stored_chatgpt_plan_type,
)
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_SEED_MODELS,
    WithheldModelIds,
    is_model_denial,
)
from my_claude_code.providers.chatgpt_oauth.response_headers import (
    CODEX_RESPONSE_HEADERS,
    CodexResponseObserver,
    capture_codex_response_headers,
)
from my_claude_code.providers.runtime.served_models import resolve_served_models

NOW = datetime(2026, 9, 5, tzinfo=UTC)

#: Shaped exactly like Codex 0.151.0's own document, trimmed to the keys this
#: rung reads plus two it must ignore.
DOCUMENT = {
    "models": [
        {
            "slug": "gpt-5.6-luna",
            "visibility": "list",
            "context_window": 272000,
            "max_context_window": 872000,
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "ultra"},
            ],
            "available_in_plans": ["plus", "pro", "business"],
        },
        {
            "slug": "gpt-5.4",
            "visibility": "hide",
            "context_window": 272000,
            "available_in_plans": ["plus", "pro"],
            "upgrade": {
                "model": "gpt-5.6-terra",
                "retirement_at": "2026-08-31T19:00:00Z",
            },
        },
        {
            "slug": "gpt-daybreak-red-latest",
            "visibility": "hide",
            "available_in_plans": ["plus"],
        },
        {
            # Synthetic, and deliberately so: every really-retired id in
            # Codex 0.151.0 is also ``visibility: hide``, which would let the
            # hide filter pass a retirement test it never exercised. This
            # entry is retired and still ``list``, so the two fixes are
            # proved independently of each other.
            "slug": "listed-and-retired",
            "visibility": "list",
            "available_in_plans": ["plus"],
            "upgrade": {
                "model": "gpt-5.6-terra",
                "retirement_at": "2026-08-31T19:00:00Z",
            },
        },
        {
            "slug": "enterprise-only",
            "visibility": "list",
            "available_in_plans": ["enterprise"],
        },
        {
            "slug": "no-plan-list",
            "visibility": "list",
        },
    ]
}


@pytest.fixture
def catalogue() -> CodexCatalogue:
    parsed = parse_codex_catalogue(DOCUMENT, version="0.151.0", source_path="codex.exe")
    assert parsed is not None
    return parsed


# --------------------------------------------------------------- extraction


def test_embedded_json_document_reads_a_catalogue_out_of_a_binary(tmp_path):
    """The document is found inside surrounding bytes and parsed cleanly.

    Parsed with ``raw_decode`` from the marker rather than by counting braces,
    so a ``{`` inside a description string cannot end the object early -- the
    trailing brace in the description below is the case that would.
    """
    payload = json.dumps(
        {"models": [{"slug": "gpt-x", "description": "brace } inside a string"}]},
        indent=2,
    )
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"\x00\x01rustpad" + payload.encode("utf-8") + b"\xff\xfetail")

    document = embedded_json_document(binary)

    assert document is not None
    assert document["models"][0]["slug"] == "gpt-x"


def test_embedded_json_document_declines_when_there_is_no_marker(tmp_path):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"no catalogue in here at all")
    assert embedded_json_document(binary) is None


def test_a_document_with_no_parseable_entry_declines(tmp_path):
    """Half a vendor list is indistinguishable from a vendor that retired half."""
    assert parse_codex_catalogue({"models": []}, version="", source_path="") is None
    assert (
        parse_codex_catalogue({"models": [{"no": "slug"}]}, version="", source_path="")
        is None
    )
    assert parse_codex_catalogue({}, version="", source_path="") is None


def test_load_codex_catalogue_declines_on_an_unreadable_binary(tmp_path):
    codex_catalogue.clear_codex_catalogue_cache()
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"not a catalogue")
    assert load_codex_catalogue(binary_path=binary) is None


def test_load_codex_catalogue_memoises_per_file_identity(tmp_path, monkeypatch):
    codex_catalogue.clear_codex_catalogue_cache()
    binary = tmp_path / "codex.exe"
    binary.write_bytes(json.dumps(DOCUMENT, indent=2).encode("utf-8"))

    first = load_codex_catalogue(binary_path=binary, version="0.151.0")
    assert first is not None

    calls: list[Path] = []
    real = codex_catalogue.embedded_json_document
    monkeypatch.setattr(
        codex_catalogue,
        "embedded_json_document",
        lambda path: (calls.append(path), real(path))[1],
    )
    second = load_codex_catalogue(binary_path=binary, version="0.151.0")

    assert second is first
    assert calls == []


# ------------------------------------------------------------------ evidence


def test_a_retired_model_leaves_the_listing_on_the_vendors_schedule(catalogue):
    evidence = catalogue_evidence(catalogue, plan_type="plus", now=NOW)
    assert "listed-and-retired" not in evidence
    assert {entry.slug for entry in retired_entries(catalogue, now=NOW)} == {
        "gpt-5.4",
        "listed-and-retired",
    }
    (retired,) = [
        entry
        for entry in retired_entries(catalogue, now=NOW)
        if entry.slug == "listed-and-retired"
    ]
    assert retired.replacement_model_id == "gpt-5.6-terra"


def test_a_model_not_yet_retired_is_kept_with_its_date(catalogue):
    """Before the instant passes, the model is listed and the date is shown."""
    early = datetime(2026, 8, 1, tzinfo=UTC)
    evidence = catalogue_evidence(catalogue, plan_type="plus", now=early)
    assert evidence["listed-and-retired"].retirement_at == "2026-08-31T19:00:00Z"
    assert evidence["listed-and-retired"].replacement_model_id == "gpt-5.6-terra"


def test_the_plan_gate_only_fires_when_both_halves_are_known(catalogue):
    on_plus = catalogue_evidence(catalogue, plan_type="plus", now=NOW)
    assert "enterprise-only" not in on_plus
    # No plan list published: kept, because unknown is not excluded.
    assert "no-plan-list" in on_plus

    unknown_plan = catalogue_evidence(catalogue, plan_type="", now=NOW)
    assert "enterprise-only" in unknown_plan


def test_a_vendor_hidden_model_is_not_listed_at_all(catalogue):
    """``visibility: hide`` means not listed -- OpenAI saying do not show this.

    Replaces ``test_visibility_is_a_hint_not_a_filter``, which asserted the
    opposite and is why 6.48.0 put three unreleased internals
    (``gpt-daybreak-blue-latest``, ``gpt-daybreak-red-latest``,
    ``codex-auto-review``) in the operator picker badged "not offered by
    default". The model stays routable; it just stops being advertised.
    """
    evidence = catalogue_evidence(catalogue, plan_type="plus", now=NOW)
    assert "gpt-daybreak-red-latest" not in evidence
    assert evidence["gpt-5.6-luna"].offered_by_default is True
    # An entry that published no visibility says nothing either way, and
    # unknown is never read as hidden.
    assert CodexCatalogueEntry(slug="x").visibility == ""
    assert CodexCatalogueEntry(slug="x").hidden is False
    assert CodexCatalogueEntry(slug="x").listed is False
    no_visibility = parse_codex_catalogue(
        {"models": [{"slug": "x"}]}, version="", source_path=""
    )
    assert no_visibility is not None
    evidence = catalogue_evidence(no_visibility, plan_type="plus", now=NOW)
    assert evidence["x"].offered_by_default is None


def test_the_vetoes_say_which_facts_an_observation_may_overturn(catalogue):
    """A retirement is a dated fact; ``hide`` is not a fact with a date.

    The whole of Fix A lives in this asymmetry. A logged success beats a
    retirement it postdates -- that is the safety property the observed rung
    exists for, and a stale vendor catalogue must never hide a model the
    operator is actively using. It does not beat a retirement it predates,
    and nothing beats ``hide``.
    """
    vetoes = listing_vetoes(catalogue, now=NOW)

    assert set(vetoes) == {
        "gpt-5.4",
        "gpt-daybreak-red-latest",
        "listed-and-retired",
    }
    # Hidden wins when an entry is both, because it is the stronger claim.
    assert vetoes["gpt-5.4"].reason == "hidden"
    assert vetoes["gpt-5.4"].retirement_at is None
    assert vetoes["gpt-5.4"].overturned_by("2099-01-01T00:00:00+00:00") is False

    retired = vetoes["listed-and-retired"]
    assert retired.reason == "retired"
    assert retired.overturned_by("2026-09-01T00:00:00+00:00") is True
    assert retired.overturned_by("2026-08-04T22:22:55.191619+00:00") is False
    # Exactly at the instant is not after it.
    assert retired.overturned_by("2026-08-31T19:00:00Z") is False
    # Unparseable or missing overturns nothing.
    assert retired.overturned_by("") is False
    assert retired.overturned_by("last tuesday") is False
    # A naive timestamp is read as UTC, never as this machine local time --
    # +03:00 here, which would flip the two assertions below.
    assert retired.overturned_by("2026-08-31T20:00:00") is True
    assert retired.overturned_by("2026-08-31T18:00:00") is False


def test_a_retirement_still_in_the_future_vetoes_nothing(catalogue):
    early = datetime(2026, 8, 1, tzinfo=UTC)
    assert "listed-and-retired" not in listing_vetoes(catalogue, now=early)


def test_the_vendor_rung_publishes_no_numbers_at_all(catalogue):
    """The Q6 guard: existence facts only, whatever else the document carries.

    The source entries carry ``context_window``, ``max_context_window`` and an
    ``ultra`` effort rung ``ReasoningEffort`` has no member for. None of it may
    reach ``ProviderModelInfo``: numeric limits keep coming off the resolution
    ladder through the existing models.dev alias, and an unrecognised effort is
    dropped rather than folded onto ``max``.
    """
    evidence = catalogue_evidence(catalogue, plan_type="plus", now=NOW)
    for item in evidence.values():
        assert set(vars(type(item))["__slots__"]) == {
            "provenance",
            "detail",
            "retirement_at",
            "replacement_model_id",
            "offered_by_default",
        }
        assert "272000" not in item.detail
        assert "ultra" not in item.detail


# --------------------------------------------------------------- the chain


def _evidence(provenance: ModelListingProvenance, *ids: str):
    return lambda: {
        model_id: ModelListingEvidence(provenance=provenance) for model_id in ids
    }


def test_the_first_three_rungs_union_and_the_strongest_wins_a_shared_id():
    resolved = resolve_served_models(
        vendor_client=_evidence(ModelListingProvenance.VENDOR_CLIENT, "a", "shared"),
        observed=_evidence(ModelListingProvenance.OBSERVED, "b", "shared"),
        seed=_evidence(ModelListingProvenance.SEED, "never"),
    )

    assert set(resolved) == {"a", "b", "shared"}
    assert resolved["shared"].provenance is ModelListingProvenance.VENDOR_CLIENT


def test_an_empty_rung_is_a_decline_not_an_empty_catalogue():
    resolved = resolve_served_models(
        vendor_client=dict,
        observed=dict,
        reference=_evidence(ModelListingProvenance.MODELS_DEV, "ref"),
        seed=_evidence(ModelListingProvenance.SEED, "seed"),
    )
    assert set(resolved) == {"ref"}


def test_the_seed_is_reached_only_when_every_other_rung_is_silent():
    resolved = resolve_served_models(
        vendor_client=dict,
        observed=dict,
        reference=dict,
        seed=_evidence(ModelListingProvenance.SEED, "seed"),
    )
    assert set(resolved) == {"seed"}


def test_a_rung_that_raises_is_skipped_rather_than_fatal():
    def explode():
        raise RuntimeError("the vendor changed its packaging")

    resolved = resolve_served_models(
        vendor_client=explode,
        observed=_evidence(ModelListingProvenance.OBSERVED, "b"),
    )
    assert set(resolved) == {"b"}


def test_no_rung_at_all_answers_with_nothing_rather_than_raising():
    assert resolve_served_models() == {}


# ------------------------------------------------------------ observed rung


def _write_log(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE requests (id TEXT PRIMARY KEY, ts_iso TEXT, provider TEXT,"
        " resolved_model TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO requests (id, ts_iso, provider, resolved_model, status)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (f"r{index}", ts, provider, model, status)
            for index, (ts, provider, model, status) in enumerate(rows)
        ],
    )
    conn.commit()
    conn.close()


def test_observed_models_are_the_successes_for_one_provider(tmp_path):
    log = tmp_path / "requests.db"
    _write_log(
        log,
        [
            ("2026-09-01", "chatgpt_oauth", "gpt-5.6-luna", "success"),
            ("2026-09-02", "chatgpt_oauth", "gpt-5.6-luna", "success"),
            ("2026-09-02", "chatgpt_oauth", "never-worked", "error"),
            ("2026-09-02", "anthropic_oauth", "claude-x", "success"),
        ],
    )

    observed = observed_served_models("chatgpt_oauth", db_path=log)

    assert [(item.model_id, item.successes) for item in observed] == [
        ("gpt-5.6-luna", 2)
    ]
    assert observed[0].last_ts_iso == "2026-09-02"


def test_the_observed_rung_declines_rather_than_raising(tmp_path):
    """Missing file, unregistered path, wrong schema: all declines."""
    assert observed_served_models("chatgpt_oauth", db_path=tmp_path / "gone.db") == ()
    assert observed_served_models("", db_path=tmp_path / "gone.db") == ()
    broken = tmp_path / "broken.db"
    sqlite3.connect(broken).close()
    assert observed_served_models("chatgpt_oauth", db_path=broken) == ()


# ------------------------------------ the observed rung against the vetoes

#: Codex 0.151.0 verbatim, trimmed: the two ids Fix A and Fix B each turn on,
#: plus a plain listed one as the control.
VETO_CATALOGUE = CodexCatalogue(
    version="0.151.0",
    source_path="codex.exe",
    entries=(
        CodexCatalogueEntry(slug="gpt-5.6-luna", visibility="list"),
        CodexCatalogueEntry(
            slug="gpt-5.4",
            visibility="hide",
            retirement_at="2026-08-31T19:00:00Z",
            replacement_model_id="gpt-5.6-terra",
        ),
        CodexCatalogueEntry(
            slug="listed-and-retired",
            visibility="list",
            retirement_at="2026-08-31T19:00:00Z",
            replacement_model_id="gpt-5.6-terra",
        ),
    ),
)


def _observed_rung(monkeypatch, observations, *, catalogue=VETO_CATALOGUE):
    """Drive ``observed_evidence`` off synthetic rows and a synthetic catalogue.

    Neither the developer request log nor their 300 MB Codex binary is
    touched: a rung whose answer depends on what the developer happens to
    have installed is not a test.
    """
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    monkeypatch.setattr(provider_module, "load_codex_catalogue", lambda: catalogue)
    monkeypatch.setattr(
        provider_module,
        "observed_served_models",
        lambda provider_id: (
            tuple(
                ServedModelObservation(
                    model_id=model_id, successes=successes, last_ts_iso=last
                )
                for model_id, successes, last in observations
            )
            if provider_id == "chatgpt_oauth"
            else ()
        ),
    )
    return provider_module.observed_evidence()


def test_an_observation_newer_than_the_retirement_keeps_the_model_listed(
    monkeypatch,
):
    """The safety property, and the reason the observed rung exists at all.

    A vendor catalogue that is stale or simply wrong must never hide a model
    the operator is actively and successfully using.
    """
    evidence = _observed_rung(
        monkeypatch, [("listed-and-retired", 3, "2026-09-01T10:00:00+00:00")]
    )

    assert set(evidence) == {"listed-and-retired"}
    assert "newer than the vendor retirement" in evidence["listed-and-retired"].detail


def test_an_observation_older_than_the_retirement_does_not(monkeypatch):
    """The ``gpt-5.4`` case, on its real dates, with ``hide`` taken out of it.

    145 successes, every one dated 2026-08-04, against a retirement dated
    2026-08-31. Evidence 27 days older than the fact contradicting it does not
    get to win.
    """
    evidence = _observed_rung(
        monkeypatch, [("listed-and-retired", 145, "2026-08-04T22:22:55.191619+00:00")]
    )

    assert evidence == {}


def test_a_vendor_hidden_model_is_not_listed_however_recent_the_success(
    monkeypatch,
):
    """Fix B, proved independently of Fix A: ``hide`` has no date to beat."""
    evidence = _observed_rung(
        monkeypatch, [("gpt-5.4", 145, "2099-01-01T00:00:00+00:00")]
    )

    assert evidence == {}


def test_an_unvetoed_observation_is_untouched(monkeypatch):
    evidence = _observed_rung(
        monkeypatch, [("gpt-5.6-luna", 8337, "2026-09-05T08:59:39+00:00")]
    )

    assert set(evidence) == {"gpt-5.6-luna"}
    assert evidence["gpt-5.6-luna"].provenance is ModelListingProvenance.OBSERVED
    assert evidence["gpt-5.6-luna"].detail.startswith("served 8337x, last ")
    assert "retirement" not in evidence["gpt-5.6-luna"].detail


def test_with_no_vendor_catalogue_every_observation_stands(monkeypatch):
    """A rung going quiet must cost its own ids and never veto another one.

    Uninstall Codex and the retirement facts are simply not available; the
    log is then the only evidence there is, and it is not overruled by a
    document nobody can read.
    """
    evidence = _observed_rung(
        monkeypatch,
        [
            ("gpt-5.4", 145, "2026-08-04T22:22:55.191619+00:00"),
            ("listed-and-retired", 1, "2026-08-04T00:00:00+00:00"),
        ],
        catalogue=None,
    )

    assert set(evidence) == {"gpt-5.4", "listed-and-retired"}


def test_a_model_the_vendor_retired_with_no_observation_is_not_listed(monkeypatch):
    """Neither rung answers for it, so nothing puts it in the picker."""
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    monkeypatch.setattr(provider_module, "load_codex_catalogue", lambda: VETO_CATALOGUE)
    monkeypatch.setattr(provider_module, "stored_chatgpt_plan_type", lambda: "plus")
    monkeypatch.setattr(provider_module, "observed_served_models", lambda _: ())
    monkeypatch.setattr(
        provider_module, "WITHHELD_MODEL_IDS", provider_module.WithheldModelIds()
    )

    served = provider_module.chatgpt_oauth_served_models()

    assert set(served) == {"gpt-5.6-luna"}
    assert "gpt-5.4" not in served
    assert "listed-and-retired" not in served


def test_the_seed_ids_are_never_vetoed_by_this(monkeypatch):
    """The five seed ids and ``gpt-5.2`` are outside both fixes.

    ``gpt-5.2`` is the id the heuristic 6.48.0 deleted used to drop; nothing
    in this change may put it back in the bin.
    """
    assert CHATGPT_OAUTH_SEED_MODELS == (
        "gpt-5.2",
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    )
    listed = CodexCatalogue(
        version="0.151.0",
        source_path="codex.exe",
        entries=tuple(
            CodexCatalogueEntry(slug=slug, visibility="list")
            for slug in CHATGPT_OAUTH_SEED_MODELS
        ),
    )
    assert listing_vetoes(listed, now=NOW) == {}
    assert set(catalogue_evidence(listed, plan_type="plus", now=NOW)) == set(
        CHATGPT_OAUTH_SEED_MODELS
    )


# ------------------------------------------------------------- learned 404


def test_a_model_shaped_refusal_is_recognised_and_a_field_error_is_not():
    assert is_model_denial(404, "") is True
    assert is_model_denial(400, "the model `x` does not exist") is True
    assert is_model_denial(400, '{"error":{"code":"model_not_found"}}') is True
    # The most common real 400 on this endpoint, and it is about the request.
    assert is_model_denial(400, "Invalid 'input[3].name': string too long") is False
    assert is_model_denial(429, "usage_limit_reached") is False


def test_a_withheld_id_is_remembered_once_and_never_persisted():
    withheld = WithheldModelIds()
    assert withheld.remember("gpt-ghost") is True
    assert withheld.remember("gpt-ghost") is False
    assert "gpt-ghost" in withheld
    assert withheld.snapshot() == frozenset({"gpt-ghost"})
    # Non-persistent by construction: a fresh process starts empty, so a model
    # OpenAI adds later costs one wasted request rather than a permanent hole.
    assert WithheldModelIds().snapshot() == frozenset()


@pytest.mark.asyncio
async def test_a_withheld_id_leaves_the_listing_and_nothing_else(monkeypatch):
    from my_claude_code.providers.chatgpt_oauth import provider as provider_module

    monkeypatch.setattr(
        provider_module,
        "vendor_client_evidence",
        _evidence(ModelListingProvenance.VENDOR_CLIENT, "gpt-real", "gpt-ghost"),
    )
    monkeypatch.setattr(provider_module, "observed_evidence", dict)
    monkeypatch.setattr(
        provider_module, "WITHHELD_MODEL_IDS", provider_module.WithheldModelIds()
    )
    provider_module.WITHHELD_MODEL_IDS.remember("gpt-ghost")

    served = provider_module.chatgpt_oauth_served_models()

    assert set(served) == {"gpt-real"}


# ------------------------------------------------------- response headers


def test_only_the_allow_listed_headers_are_stored():
    captured = capture_codex_response_headers(
        {
            "X-Codex-Primary-Used-Percent": "42.5",
            "x-codex-credits-balance": "17",
            "set-cookie": "session=secret",
            "authorization": "Bearer nope",
            "x-codex-primary-window-minutes": "   ",
        }
    )

    assert captured == {
        "x-codex-primary-used-percent": "42.5",
        "x-codex-credits-balance": "17",
    }
    assert "set-cookie" not in captured
    assert "authorization" not in captured


def test_the_allow_list_is_the_seventeen_names_codex_itself_parses():
    assert len(CODEX_RESPONSE_HEADERS) == 17
    assert "x-models-etag" in CODEX_RESPONSE_HEADERS
    assert all(name == name.lower() for name in CODEX_RESPONSE_HEADERS)


def test_the_observer_keeps_the_latest_snapshot_and_ignores_bare_responses():
    observer = CodexResponseObserver()
    assert observer.latest is None

    observer.observe({"x-codex-credits-balance": "5"}, status_code=200, now=1.0)
    assert observer.latest is not None
    assert observer.latest.values["x-codex-credits-balance"] == "5"

    # A response carrying none of the allow-listed names must not blank the
    # snapshot: no news is not the same fact as a zeroed window.
    observer.observe({"content-type": "text/event-stream"}, status_code=200, now=2.0)
    assert observer.latest.observed_at == 1.0


# ------------------------------------------------------------- plan claims


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode()
    return f"header.{body.rstrip('=')}.signature"


def test_the_plan_is_decoded_locally_from_the_stored_id_token(tmp_path):
    """No network, no refresh: the claim is read off the file already on disk."""
    auth = tmp_path / "chatgpt_oauth_auth.json"
    auth.write_text(
        json.dumps(
            {
                "version": 1,
                "tokens": {
                    "access_token": "a.b.c",
                    "refresh_token": "r",
                    "id_token": _jwt(
                        {
                            "sub": "must-never-be-read-out",
                            "email": "must-never-be-read-out",
                            "https://api.openai.com/auth": {
                                "chatgpt_plan_type": "plus",
                                "chatgpt_account_id": "acct",
                            },
                        }
                    ),
                    "account_id": "acct",
                },
            }
        ),
        encoding="utf-8",
    )

    assert stored_chatgpt_plan_type(auth_path=auth) == "plus"


def test_an_unreadable_credential_reports_an_unknown_plan(tmp_path):
    """Unknown must apply no filter at all, so it must not raise either."""
    assert stored_chatgpt_plan_type(auth_path=tmp_path / "absent.json") == ""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "tokens": {}}), encoding="utf-8")
    assert stored_chatgpt_plan_type(auth_path=bad) == ""


# ----------------------------------------------------------- the drift guard


def test_every_seed_id_is_still_in_the_installed_codex_catalogue():
    """The seed list is five literals, so drift must be a failing build.

    Skipped where Codex is not installed, which is every CI runner: the point
    is that a developer who *does* have Codex cannot let the seed rot the way
    the allowlist it replaced did.
    """
    codex_catalogue.clear_codex_catalogue_cache()
    catalogue = load_codex_catalogue()
    if catalogue is None:
        pytest.skip("Codex CLI is not installed on this machine")
    slugs = {entry.slug for entry in catalogue.entries}
    missing = sorted(set(CHATGPT_OAUTH_SEED_MODELS) - slugs)
    assert not missing, (
        f"Codex {catalogue.version} no longer publishes {missing}. "
        "Update CHATGPT_OAUTH_SEED_MODELS to the ids it lists."
    )
