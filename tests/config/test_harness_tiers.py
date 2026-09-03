"""The per-coding-agent tier store: ``~/.fcc/harness_tiers.json``.

The file is read on the request path and written by the dashboard, so both
halves matter: what a malformed document does (nothing, loudly) and what a
round trip preserves (exactly the three states the feature is built on).
"""

import json
from pathlib import Path

from my_claude_code.config.harness_tiers import (
    HarnessTierOverride,
    HarnessTiers,
    current_harness_tiers,
    is_valid_tier_override_ref,
    load_harness_tiers,
    reset_harness_tiers_cache,
    save_harness_tiers,
)
from my_claude_code.core.tier_refs import ModelTier


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_round_trip_preserves_all_three_states(tmp_path: Path) -> None:
    """Absent, present-but-empty, and present-with-a-model are three answers.

    The middle one is the point of the file, and a round trip that folded it
    into either neighbour would silently change what an agent routes to.
    """

    path = tmp_path / "harness_tiers.json"
    tiers = HarnessTiers(
        harnesses={
            "opencode": {
                "best": HarnessTierOverride(
                    model="open_router/grok",
                    fallbacks=("nous_portal/hy3",),
                    paused=("nous_portal/hy3",),
                ),
                "cheap": HarnessTierOverride(fallbacks=("open_router/small",)),
                "medium": HarnessTierOverride(),
            }
        }
    )

    save_harness_tiers(tiers, path)
    loaded = load_harness_tiers(path)

    assert loaded.override("opencode", ModelTier.BEST) == HarnessTierOverride(
        model="open_router/grok",
        fallbacks=("nous_portal/hy3",),
        paused=("nous_portal/hy3",),
    )
    assert loaded.override("opencode", ModelTier.CHEAP) == HarnessTierOverride(
        fallbacks=("open_router/small",)
    )
    assert loaded.override("opencode", ModelTier.MEDIUM) == HarnessTierOverride()
    assert loaded.override("opencode", ModelTier.GOOD) is None
    assert loaded.override("crush", ModelTier.BEST) is None


def test_the_document_is_written_atomically(tmp_path: Path) -> None:
    """The proxy reads this file back in the same process it writes it.

    A truncated document does not parse, and the reader cannot tell "the user
    emptied this" from "the writer died halfway".
    """

    path = tmp_path / "harness_tiers.json"
    save_harness_tiers(
        HarnessTiers(harnesses={"crush": {"best": HarnessTierOverride(model="a/b")}}),
        path,
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "harnesses": {"crush": {"best": {"model": "a/b"}}}
    }
    assert list(tmp_path.glob("*.fcc-tmp")) == []


def test_an_unknown_agent_id_is_dropped_with_a_log_line(tmp_path: Path) -> None:
    """A typo must be told about, not honoured against nothing.

    Every id is checked against the harness registry, so a block written for an
    agent that does not exist cannot sit in the file looking like it works.
    """

    path = _write(
        tmp_path / "harness_tiers.json",
        {
            "harnesses": {
                "not-a-real-agent": {"best": {"model": "open_router/x"}},
                "opencode": {"best": {"model": "open_router/y"}},
            }
        },
    )

    loaded = load_harness_tiers(path)

    assert set(loaded.harnesses) == {"opencode"}


def test_an_unknown_tier_name_is_dropped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "harness_tiers.json",
        {"harnesses": {"opencode": {"turbo": {"model": "open_router/x"}}}},
    )

    assert load_harness_tiers(path).harnesses == {}


def test_a_ref_in_the_mcc_namespace_is_rejected(tmp_path: Path) -> None:
    """A tier pointing at a tier is a loop the file that caused it cannot show.

    It would also reach ``_validate_provider_id`` as the provider ``mcc``, which
    does not exist -- one attempt later, where nobody can see this file.
    """

    path = _write(
        tmp_path / "harness_tiers.json",
        {
            "harnesses": {
                "opencode": {
                    "best": {
                        "model": "mcc/cheap",
                        "fallbacks": ["MCC/best", "open_router/real"],
                    }
                }
            }
        },
    )

    entry = load_harness_tiers(path).override("opencode", ModelTier.BEST)

    assert entry is not None
    assert entry.model is None
    assert entry.fallbacks == ("open_router/real",)


def test_a_slashless_ref_is_rejected(tmp_path: Path) -> None:
    """``parse_model_name`` splits on the first slash and raises without one."""

    assert not is_valid_tier_override_ref("just-a-model")
    assert not is_valid_tier_override_ref("open_router/")
    assert not is_valid_tier_override_ref("/model")
    assert is_valid_tier_override_ref("open_router/x-ai/grok-5")


def test_a_malformed_document_means_no_overrides_not_a_crash(tmp_path: Path) -> None:
    """The worst honest outcome is that the tiers resolve globally."""

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_harness_tiers(broken).is_empty

    wrong_shape = _write(tmp_path / "list.json", ["nope"])
    assert load_harness_tiers(wrong_shape).is_empty

    empty = tmp_path / "empty.json"
    empty.write_text("   \n", encoding="utf-8")
    assert load_harness_tiers(empty).is_empty

    assert load_harness_tiers(tmp_path / "absent.json").is_empty


def test_with_override_adds_replaces_and_removes(tmp_path: Path) -> None:
    """ "Revert to global" deletes the entry rather than emptying it.

    An empty entry means "this agent overrides this tier and has not said what
    yet", which is a different state from following the global chain.
    """

    tiers = HarnessTiers()

    added = tiers.with_override(
        "crush", ModelTier.CHEAP, HarnessTierOverride(model="open_router/small")
    )
    assert added.override("crush", ModelTier.CHEAP) is not None

    removed = added.with_override("crush", ModelTier.CHEAP, None)
    assert removed.override("crush", ModelTier.CHEAP) is None
    assert removed.harnesses == {}


def test_the_cache_re_reads_only_when_the_file_changed(tmp_path: Path) -> None:
    """Read per request, so a JSON parse per request would be a real cost.

    Keyed on the file's own stat, so a dashboard edit lands without a restart --
    which is what lets the admin route write the file and nothing else.
    """

    path = tmp_path / "harness_tiers.json"
    reset_harness_tiers_cache()
    assert current_harness_tiers(path).is_empty

    save_harness_tiers(
        HarnessTiers(
            harnesses={"opencode": {"best": HarnessTierOverride(model="open_router/x")}}
        ),
        path,
    )

    assert current_harness_tiers(path).override("opencode", ModelTier.BEST) is not None
    reset_harness_tiers_cache()
