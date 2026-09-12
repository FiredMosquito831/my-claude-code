"""The store behind payloads that survive a restart.

Everything here is about one promise: a cache must never be the reason a page
fails. A missing file, a corrupt file, a file from another version, a payload
that will not serialise -- each is a miss, and a miss is recoverable, because
the answer can always be computed again.
"""

import json
import time
from pathlib import Path

from my_claude_code.core.derived_cache import (
    DERIVED_CACHE_VERSION,
    DerivedCache,
    DerivedEntry,
)


def test_a_payload_round_trips(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")

    assert cache.write(
        "cost", key="log-7", payload={"totals": {"priced": 3}}, computed_at=1.5
    )

    entry = cache.read("cost")
    assert entry is not None
    assert entry == DerivedEntry(
        key="log-7", computed_at=1.5, payload={"totals": {"priced": 3}}
    )
    assert entry.matches("log-7")
    assert not entry.matches("log-8")


def test_a_missing_entry_is_a_miss_not_a_failure(tmp_path: Path) -> None:
    assert DerivedCache(tmp_path / "derived").read("never-written") is None


def test_a_corrupt_document_is_ignored_not_fatal(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    cache.write("cost", key="log-7", payload={"a": 1}, computed_at=1.0)
    cache.path_for("cost").write_text("{not json at all", encoding="utf-8")

    assert cache.read("cost") is None


def test_a_document_from_another_version_is_ignored(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    cache.path_for("cost").parent.mkdir(parents=True, exist_ok=True)
    cache.path_for("cost").write_text(
        json.dumps(
            {
                "version": DERIVED_CACHE_VERSION + 1,
                "key": "log-7",
                "computed_at": 1.0,
                "payload": {"a": 1},
            }
        ),
        encoding="utf-8",
    )

    assert cache.read("cost") is None


def test_a_document_missing_its_envelope_is_ignored(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    cache.path_for("cost").parent.mkdir(parents=True, exist_ok=True)
    for document in (
        {"version": DERIVED_CACHE_VERSION, "computed_at": 1.0, "payload": {}},
        {"version": DERIVED_CACHE_VERSION, "key": "k", "payload": {}},
        {"version": DERIVED_CACHE_VERSION, "key": "k", "computed_at": 1.0},
        {"version": DERIVED_CACHE_VERSION, "key": 7, "computed_at": 1.0, "payload": {}},
        ["not", "a", "document"],
    ):
        cache.path_for("cost").write_text(json.dumps(document), encoding="utf-8")
        assert cache.read("cost") is None, document


def test_a_payload_that_cannot_be_serialised_is_refused_not_raised(
    tmp_path: Path,
) -> None:
    cache = DerivedCache(tmp_path / "derived")

    assert not cache.write("cost", key="k", payload={1, 2, 3}, computed_at=1.0)
    assert cache.read("cost") is None


def test_an_entry_name_can_never_escape_the_cache_root(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")

    for name in ("../escape", "sub/dir", "back\\slash", "", ".hidden"):
        assert not cache.write(name, key="k", payload={}, computed_at=1.0), name
        assert cache.read(name) is None, name
    assert not list(tmp_path.glob("**/escape*"))


def test_a_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")

    for index in range(5):
        cache.write("cost", key=f"log-{index}", payload={"n": index}, computed_at=1.0)

    assert [path.name for path in sorted(cache.root.iterdir())] == ["cost.json"]
    latest = cache.read("cost")
    assert latest is not None
    assert latest.payload == {"n": 4}


def test_the_document_is_readable_by_a_person(tmp_path: Path) -> None:
    """It lives in the user's config directory; it should not be a blob."""

    cache = DerivedCache(tmp_path / "derived")
    cache.write("cost", key="log-7", payload={"a": 1}, computed_at=time.time())

    text = cache.path_for("cost").read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert '"key": "log-7"' in text


def test_forgetting_an_entry_is_idempotent(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    cache.write("cost", key="k", payload={}, computed_at=1.0)

    cache.forget("cost")
    cache.forget("cost")
    cache.forget("../escape")

    assert cache.read("cost") is None


def test_an_unwritable_root_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    # A file where the directory should be: mkdir fails, and the cache has to
    # answer "no" rather than take the page down.
    blocked = tmp_path / "derived"
    blocked.write_text("not a directory", encoding="utf-8")
    cache = DerivedCache(blocked)

    assert not cache.write("cost", key="k", payload={}, computed_at=1.0)
    assert cache.read("cost") is None
