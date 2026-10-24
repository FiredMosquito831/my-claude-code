"""Every DDG-harness fixture says where it came from, and none is copyleft.

Each file under ``tests/fixtures/ddg`` and ``tests/fixtures/extraction``
carries a ``SOURCE`` line (origin, licence, date; ``corpus.source_line_of``).
The licence must be permissive or MCC's own: the dual licence forbids copying
AGPL/GPL/FSL material into the commercial half, and the standing rule of
2026-10-25 (USER DECISIONS Q13) never copies SearXNG, Crush, Firecrawl or
html2text -- code or fixtures. A page copied from upstream must still be the
upstream bytes its ``SOURCE`` line names, and an archive must match the hashes
in its sidecar.
"""

import hashlib
import json

import pytest

from tests.support.ddg_harness import corpus

FILES = corpus.fixture_files()


def _label(path) -> str:
    return path.relative_to(corpus.TESTS_DIR).as_posix()


def test_the_fixture_directories_are_not_empty() -> None:
    assert len(FILES) >= 10


@pytest.mark.parametrize("path", FILES, ids=_label)
def test_every_fixture_has_a_source_line_with_a_permitted_licence(path) -> None:
    line = corpus.source_line_of(path)
    assert line is not None, f"{_label(path)} has no SOURCE line"
    fields = corpus.source_fields(line)

    for required in ("origin", "licence", "fetched"):
        assert fields.get(required), f"{_label(path)}: SOURCE has no {required}="
    licence = fields["licence"]
    assert not corpus.DENIED_LICENCE_PATTERN.search(licence), (
        f"{_label(path)}: licence {licence!r} is copyleft or source-available"
    )
    named = {
        word
        for word in licence.replace("(", " ").replace(")", " ").split()
        if any(ch.isdigit() for ch in word) or word.startswith("MCC")
    }
    assert named, f"{_label(path)}: licence {licence!r} names no licence id"
    assert named <= corpus.ALLOWED_LICENCES, (
        f"{_label(path)}: licence {licence!r} is not on the allow-list"
    )
    origin = fields["origin"].lower()
    assert not any(denied in origin for denied in corpus.DENIED_ORIGINS), (
        f"{_label(path)}: origin {fields['origin']!r} is a project we never copy"
    )


@pytest.mark.parametrize(
    "path",
    [
        path
        for path in FILES
        if "upstream_sha256" in corpus.source_fields(corpus.source_line_of(path) or "")
    ],
    ids=_label,
)
def test_a_copied_page_is_still_the_upstream_bytes(path) -> None:
    fields = corpus.source_fields(corpus.source_line_of(path) or "")
    body = corpus.body_after_source_line(path)

    assert hashlib.sha256(body).hexdigest() == fields["upstream_sha256"]


def test_a_named_licence_file_exists_beside_its_fixture() -> None:
    for path in FILES:
        fields = corpus.source_fields(corpus.source_line_of(path) or "")
        licence_file = fields.get("licence_file", "")
        if licence_file and "inside the archive" not in licence_file:
            assert (path.parent / licence_file).is_file(), (
                f"{_label(path)} names {licence_file}, which is not beside it"
            )


@pytest.mark.parametrize(
    ("archive", "licence_members"),
    [
        (corpus.TRAFILATURA_ARCHIVE, {"LICENSE"}),
        (
            corpus.READABILITY_ARCHIVE,
            {"LICENSE", "NOTICE", "pages/dragnet/LICENSE", "pages/jcharum/NOTICE"},
        ),
    ],
    ids=lambda value: getattr(value, "name", "licences"),
)
def test_an_archive_matches_its_sidecar_and_carries_its_licences(
    archive, licence_members
) -> None:
    members = corpus.archive_members(archive)

    assert licence_members <= set(members)
    for name in licence_members:
        assert members[name].strip(), f"{archive.name}: {name} is empty"


def test_every_search_page_is_used_and_every_used_page_exists() -> None:
    document = json.loads(
        (corpus.DDG_DIR / "scenarios.json").read_text(encoding="utf-8")
    )
    used = {row["page"] for row in document["scenarios"] if "page" in row}
    declared = set(document["pages"])
    on_disk = {path.name for path in corpus.DDG_DIR.glob("*.html")}

    assert used == declared == on_disk
