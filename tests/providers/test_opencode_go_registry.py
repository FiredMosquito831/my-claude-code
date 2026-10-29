"""OpenCode Go resolves its model surfaces from its own registry bucket.

The defect 7.113.5 fixes: both OpenCode profiles were handed the same constant,
``OPENCODE_REGISTRY_PROVIDER = "opencode"``, so a Go model was looked up in
Zen's roster. Zen does not publish most Go ids, so the lookup missed, and every
one of them fell to the profile's floor -- Chat Completions, source ``profile``,
for a roster the vendor had catalogued door by door.

models.dev files Go separately (``opencode-go``: 37 models, of which 21 are also
Zen's), and every other reader in this codebase already resolved
``opencode_go`` -> ``opencode-go`` through ``PROVIDER_ID_ALIASES``. The surface
reader was the seventh and the only one that skipped the map. These tests hold
the repair from every side:

* the Go profile names its own provider id, and the bucket it names is the one
  the alias map says;
* a model only Go publishes now resolves from Go's registry, and a model neither
  bucket describes still takes the floor;
* the very same cache leaves Zen's answers byte-identical;
* a ``@ai-sdk/google`` model reads ``unservable`` and says why;
* the published roster's before/after table is a committed golden, so a vendor
  change is a diff and not something absorbed.

Nothing here calls an upstream. The two buckets are a trimmed copy of the
models.dev cache (``opencode_go_registry_buckets.json``), holding only the
``provider.npm`` the resolver reads.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.api.model_admin import response_surface_payload
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    resolve_response_surface,
)
from my_claude_code.providers.openai_chat.profiles import (
    OPENCODE_GO_REGISTRY_PROVIDER,
    OPENCODE_REGISTRY_PROVIDER,
)
from my_claude_code.providers.openai_chat.response_surface import registry_silence
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.models_dev import (
    PROVIDER_ID_ALIASES,
    models_dev_bucket_id,
    models_dev_provider_npm_overrides,
)

HERE = Path(__file__).parent
BUCKETS_PATH = HERE / "opencode_go_registry_buckets.json"
GOLDEN_PATH = HERE / "opencode_go_registry_golden.json"

#: The seven the identity fix moves, and where each lands.
MOVED_TO_RESPONSES = ("muse-spark-1.2-contributor", "muse-spark-1.3-contributor")
MOVED_TO_MESSAGES = ("minimax-m2.7", "minimax-m3", "qwen3.7-plus", "qwen3.8-max")
MOVED_TO_UNSERVABLE = ("gemini-4-argon-contributor",)


# --------------------------------------------------------------------------
# support
# --------------------------------------------------------------------------


def _buckets() -> dict[str, Any]:
    return json.loads(BUCKETS_PATH.read_text(encoding="utf-8"))


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _install(buckets: dict[str, Any] | None = None) -> None:
    """Cache the two buckets where every reader in this module finds them."""

    path = models_dev.models_dev_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    models_dev.write_models_dev_cache(buckets if buckets is not None else _buckets())
    models_dev.reset_models_dev_payload_cache()


def _resolve(provider_id: str, model_id: str, registry_provider: str) -> Any:
    profile = OPENAI_CHAT_PROFILES[provider_id]
    return resolve_response_surface(
        provider_id,
        model_id,
        registry_provider=registry_provider,
        declared=profile.response_surfaces,
        no_information=profile.no_information_surface,
        by_upstream=profile.surface_by_upstream,
    )


def _go(model_id: str) -> Any:
    profile = OPENAI_CHAT_PROFILES["opencode_go"]
    return _resolve("opencode_go", model_id, profile.surface_registry_provider)


def _zen(model_id: str) -> Any:
    profile = OPENAI_CHAT_PROFILES["opencode"]
    return _resolve("opencode", model_id, profile.surface_registry_provider)


# --------------------------------------------------------------------------
# the identity
# --------------------------------------------------------------------------


def test_the_go_profile_names_its_own_registry() -> None:
    """One constant per host. Sharing Zen's is what the bug was."""

    zen = OPENAI_CHAT_PROFILES["opencode"]
    go = OPENAI_CHAT_PROFILES["opencode_go"]
    assert zen.surface_registry_provider == OPENCODE_REGISTRY_PROVIDER == "opencode"
    assert (
        go.surface_registry_provider == OPENCODE_GO_REGISTRY_PROVIDER == "opencode_go"
    )
    assert PROVIDER_ID_ALIASES["opencode_go"] == "opencode-go"


def test_the_bucket_lookup_walks_the_alias_map() -> None:
    """Direct hit first, alias second -- and ``None`` when neither names one."""

    index = _buckets()
    assert models_dev_bucket_id(index, "opencode") == "opencode"
    assert models_dev_bucket_id(index, "opencode_go") == "opencode-go"
    assert models_dev_bucket_id(index, "opencode-go") == "opencode-go"
    assert models_dev_bucket_id(index, "not_a_provider") is None
    assert models_dev_bucket_id(index, "") is None


def test_the_npm_overrides_are_read_from_the_bucket_the_alias_names() -> None:
    """The reader every surface resolution goes through, with both spellings."""

    _install()
    go = models_dev_provider_npm_overrides("opencode_go")
    assert go["minimax-m3"] == "@ai-sdk/anthropic"
    assert go["muse-spark-1.3-contributor"] == "@ai-sdk/openai"
    # Zen's roster does not carry the Go-only ids at all.
    zen = models_dev_provider_npm_overrides("opencode")
    assert "muse-spark-1.3-contributor" not in zen
    assert "claude-haiku-5-5" in zen


# --------------------------------------------------------------------------
# what the repair moves, and what it does not
# --------------------------------------------------------------------------


def test_a_go_only_model_resolves_from_the_go_bucket() -> None:
    """Before 7.113.5 this model missed in Zen's roster and took the floor."""

    _install()
    resolved = _go("muse-spark-1.3-contributor")
    assert resolved.surface is ResponseSurface.RESPONSES
    assert resolved.source is ResponseSurfaceSource.REGISTRY
    assert resolved.detail == "@ai-sdk/openai"
    assert resolved.label == "responses (registry)"
    # The floor it used to take, for contrast: same model, Zen's bucket.
    assert _resolve("opencode_go", "muse-spark-1.3-contributor", "opencode").label == (
        "chat_completions (profile)"
    )


def test_a_shared_model_keeps_the_door_both_buckets_publish() -> None:
    """``claude-haiku-5-5`` is in both, and both say the same thing."""

    _install()
    after = _go("claude-haiku-5-5")
    before = _resolve("opencode_go", "claude-haiku-5-5", "opencode")
    assert before.label == after.label == "messages (registry)"
    assert after.detail == before.detail == "@ai-sdk/anthropic"


def test_a_model_neither_bucket_describes_still_takes_the_floor() -> None:
    """``deepseek-v4.1-flash`` carries no ``npm`` in either -- the brief's worry."""

    _install()
    resolved = _go("deepseek-v4.1-flash")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.PROFILE
    assert resolved.label == "chat_completions (profile)"


def test_a_google_declared_model_reads_unservable_and_names_google() -> None:
    """``gemini-4-argon-contributor`` is Go-only, ``@ai-sdk/google``, and stays
    listed with the vendor's reason rather than vanishing or being sent
    somewhere it cannot answer."""

    _install()
    resolved = _go("gemini-4-argon-contributor")
    assert resolved.surface is ResponseSurface.UNSERVABLE
    assert resolved.source is ResponseSurfaceSource.REGISTRY
    assert "Google" in resolved.detail
    assert "@ai-sdk/google" in resolved.detail
    assert resolved.label == "unservable (registry)"


def test_the_same_cache_leaves_every_zen_answer_alone() -> None:
    """The fix is Go's bucket. Zen's resolution must not move by a letter."""

    _install()
    golden = _golden()
    shared = [model for model, row in golden.items() if row["before"]["source"] != ""]
    checked = 0
    for model_id in shared:
        before = golden[model_id]["before"]
        after = _zen(model_id)
        assert (after.surface.value, after.source.value, after.detail) == (
            before["surface"],
            before["source"],
            before["detail"],
        ), model_id
        checked += 1
    assert checked == len(golden), "every Go id, including the Go-only ones"


def test_an_unknown_provider_falls_through_to_the_floor() -> None:
    """No bucket, no alias: the profile's own door, exactly as before."""

    _install()
    assert models_dev_provider_npm_overrides("not_a_provider") == {}
    resolved = _resolve("opencode_go", "muse-spark-1.3-contributor", "not_a_provider")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.PROFILE


# --------------------------------------------------------------------------
# what the Models page says
# --------------------------------------------------------------------------


def test_the_page_names_the_same_seven_moves() -> None:
    """The page reads the same resolver; its labels are the visible receipt.

    7.114.0 adds one steady-state move on top of 7.113.5's seven:
    ``muse-spark-1.3-contributor`` has a measured multi-door set, and the page
    -- which has no request in flight, so no client preference -- shows the
    first door the measurement was authored with, Messages. Its sibling
    ``muse-spark-1.2-contributor`` was never measured and keeps Responses.
    """

    _install()
    first = response_surface_payload("opencode_go", "muse-spark-1.3-contributor")
    assert first is not None
    assert first["value"] == "messages"
    assert first["source"] == "measured"
    for model_id in ("muse-spark-1.2-contributor",):
        row = response_surface_payload("opencode_go", model_id)
        assert row is not None, model_id
        assert row["label"] == "responses (registry)", model_id
        assert row["note"] == "@ai-sdk/openai", model_id
        assert row["value"] == "responses", model_id
        assert [offered["value"] for offered in row["offered"]] == [
            "chat_completions",
            "responses",
            "messages",
        ], model_id
    for model_id in MOVED_TO_MESSAGES:
        row = response_surface_payload("opencode_go", model_id)
        assert row is not None, model_id
        assert row["label"] == "messages (registry)", model_id
        assert row["note"] == "@ai-sdk/anthropic", model_id
    for model_id in MOVED_TO_UNSERVABLE:
        row = response_surface_payload("opencode_go", model_id)
        assert row is not None, model_id
        assert row["label"] == "unservable (registry)", model_id
        assert "Google" in row["note"], model_id


def test_the_two_silences_name_the_bucket_that_answered() -> None:
    """A registry that does not carry a model and a cache that is not there are
    different facts, and 7.113.5 made Go report the first one correctly."""

    _install()
    assert registry_silence("opencode_go") == "no registry entry for this model"
    assert registry_silence("opencode") == "no registry entry for this model"
    assert registry_silence("") == ""
    # And a bucket the alias does not reach is still the other silence.
    assert (
        registry_silence("not_a_provider")
        == "no cached copy of the vendor's registry yet"
    )


def test_a_reply_with_no_cache_yet_says_so() -> None:
    """The install that has never fetched models.dev: the floor, and no lie."""

    models_dev.reset_models_dev_payload_cache()
    assert (
        registry_silence("opencode_go") == "no cached copy of the vendor's registry yet"
    )
    resolved = _go("muse-spark-1.3-contributor")
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.PROFILE


# --------------------------------------------------------------------------
# the golden
# --------------------------------------------------------------------------


def test_the_published_go_roster_moves_exactly_seven_models() -> None:
    """Every published ``opencode_go`` model, before and after, as recorded.

    A vendor roster change shows up here as a diff rather than being absorbed
    silently -- which is the whole reason the table is committed.
    """

    _install()
    golden = _golden()
    assert len(golden) == 37, sorted(set(golden))

    current = {
        model_id: {
            "before": {
                "label": f"{before.surface.value} ({before.source.value})",
                "surface": before.surface.value,
                "source": before.source.value,
                "detail": before.detail,
            },
            "after": {
                "label": f"{after.surface.value} ({after.source.value})",
                "surface": after.surface.value,
                "source": after.source.value,
                "detail": after.detail,
            },
        }
        for model_id in golden
        for before in (_resolve("opencode_go", model_id, "opencode"),)
        for after in (_go(model_id),)
    }
    differing = [
        model_id
        for model_id in golden
        if json.dumps(current[model_id], sort_keys=True)
        != json.dumps(golden[model_id], sort_keys=True)
    ]
    assert not differing, differing

    moved = [
        model_id
        for model_id in golden
        if golden[model_id]["before"] != (golden[model_id]["after"])
    ]
    assert set(moved) == (
        set(MOVED_TO_RESPONSES) | set(MOVED_TO_MESSAGES) | set(MOVED_TO_UNSERVABLE)
    ), sorted(moved)
    assert len(golden) - len(moved) == 30
