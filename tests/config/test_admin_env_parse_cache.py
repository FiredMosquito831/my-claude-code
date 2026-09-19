"""The managed layers are parsed once per content, and the file is unchanged.

Two separate promises, proved separately:

* **Speed.** ``dotenv`` costs 50 ms on a 52 KB managed file and one pause click
  paid for six such parses. They are memoised on the bytes that produced them,
  so a file that changed -- by any means, at any resolution of any filesystem
  clock -- is parsed again.
* **Byte identity.** The file an apply writes, and the masked preview the
  response carries, are what they were before 7.31.0, character for character,
  on a managed file that carries every field and unmanaged entries besides.
"""

from pathlib import Path

import pytest

from my_claude_code.config.admin import persistence, sources
from my_claude_code.config.admin.manifest import FIELDS


@pytest.fixture(autouse=True)
def _cold_cache() -> None:
    sources.clear_env_parse_cache()


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_a_parsed_layer_is_the_same_values_the_uncached_read_returned(
    tmp_path: Path,
) -> None:
    """The cache is an optimisation, never a different answer."""

    from dotenv import dotenv_values

    path = tmp_path / ".env"
    _write(
        path,
        "\n".join(
            [
                "# a comment",
                "PLAIN=value",
                'QUOTED="two words"',
                "EMPTY=",
                "BARE",
                'ESCAPED="a \\"quoted\\" word"',
                "TRAILING=x   ",
            ]
        )
        + "\n",
    )
    expected = {
        key: "" if value is None else value
        for key, value in dotenv_values(path).items()
    }
    assert sources.dotenv_values_from_file(path) == expected
    # And the second read, which is the one that comes from the cache.
    assert sources.dotenv_values_from_file(path) == expected


def test_a_crlf_file_parses_exactly_as_the_uncached_read_did(tmp_path: Path) -> None:
    """The decode inside the cache is the one ``io.open`` does, newlines and all."""

    from dotenv import dotenv_values

    path = tmp_path / ".env"
    path.write_bytes(b'A=1\r\nB="two words"\r\n# comment\r\nC=\r\n')
    expected = {
        key: "" if value is None else value
        for key, value in dotenv_values(path).items()
    }
    assert sources.dotenv_values_from_file(path) == expected


def test_a_rewrite_of_the_same_size_is_parsed_again(tmp_path: Path) -> None:
    """No timestamp anywhere in the key, so there is no staleness window.

    Written back to back and to exactly the same length: a cache keyed on
    ``(mtime, size)`` would serve the first answer for the second file on any
    filesystem whose clock is coarser than this loop, which on Windows is every
    one of them.
    """

    path = tmp_path / ".env"
    _write(path, "KEY=aaa\n")
    assert sources.dotenv_values_from_file(path) == {"KEY": "aaa"}
    _write(path, "KEY=bbb\n")
    assert sources.dotenv_values_from_file(path) == {"KEY": "bbb"}


def test_going_back_to_a_previous_content_returns_that_content(tmp_path: Path) -> None:
    """Keyed on content, so a revert is a hit rather than a wrong answer."""

    path = tmp_path / ".env"
    _write(path, "KEY=aaa\n")
    assert sources.dotenv_values_from_file(path)["KEY"] == "aaa"
    _write(path, "KEY=bbb\n")
    assert sources.dotenv_values_from_file(path)["KEY"] == "bbb"
    _write(path, "KEY=aaa\n")
    assert sources.dotenv_values_from_file(path)["KEY"] == "aaa"


def test_a_caller_that_mutates_what_it_got_does_not_edit_the_next_read(
    tmp_path: Path,
) -> None:
    """The managed layer is the base a save edits. Every reader gets its own."""

    path = tmp_path / ".env"
    _write(path, "KEY=aaa\n")
    first = sources.dotenv_values_from_file(path)
    first["KEY"] = "clobbered"
    first["ADDED"] = "x"
    assert sources.dotenv_values_from_file(path) == {"KEY": "aaa"}


def test_an_absent_file_is_still_no_values(tmp_path: Path) -> None:
    assert sources.dotenv_values_from_file(tmp_path / "nothing-here") == {}


def test_a_directory_in_place_of_a_file_is_still_no_values(tmp_path: Path) -> None:
    directory = tmp_path / "a-directory"
    directory.mkdir()
    assert sources.dotenv_values_from_file(directory) == {}


def test_the_cache_is_bounded(tmp_path: Path) -> None:
    """A long-lived process pointed at many contents cannot accumulate them."""

    path = tmp_path / ".env"
    for index in range(sources._PARSE_CACHE_MAX * 3):
        _write(path, f"KEY=value-{index}\n")
        sources.dotenv_values_from_file(path)
    assert len(sources._PARSE_CACHE) <= sources._PARSE_CACHE_MAX


# --------------------------------------------------------------------------
# Byte identity: the file, and the preview beside it.
# --------------------------------------------------------------------------


def _every_field_plus_unmanaged() -> dict[str, str]:
    """A managed value for every field the manifest knows, secrets included."""

    values: dict[str, str] = {}
    for field in FIELDS:
        if field.field_type == "bool":
            values[field.key] = "true"
        elif field.secret:
            values[field.key] = f"sk-{field.key.lower()}-0123456789"
        else:
            values[field.key] = field.default or "chosen"
    return values


def test_the_pair_renderer_is_byte_identical_to_the_two_it_replaced() -> None:
    """One walk, two texts, and each text is the one that shipped."""

    values = _every_field_plus_unmanaged()
    preserved = {
        "MCC_SMOKE_THING": "kept",
        "AN_ALIASED_KEY": "also kept",
        "QUOTED_UNMANAGED": "two words",
    }
    plain, masked = persistence.render_env_pair(values, preserved=preserved)
    assert plain == persistence.render_env_file(values, preserved=preserved)
    assert masked == persistence.render_env_file(
        values, mask_secrets=True, preserved=preserved
    )
    assert plain != masked


def test_the_pair_renderer_matches_with_nothing_preserved_and_nothing_set() -> None:
    """The two degenerate shapes a fresh install and a full one bracket."""

    for values in ({}, _every_field_plus_unmanaged()):
        plain, masked = persistence.render_env_pair(values, preserved=None)
        assert plain == persistence.render_env_file(values, preserved=None)
        assert masked == persistence.render_env_file(
            values, mask_secrets=True, preserved=None
        )


def test_a_commit_writes_exactly_what_the_old_two_call_path_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden: an apply's bytes, against the pre-7.31.0 render.

    The managed file carries an unmanaged entry as well as managed ones, which
    is the case where the two reads of ``unmanaged_env_values`` -- one before
    the write, one after -- could have disagreed.
    """

    config_dir = tmp_path / ".mcc"
    config_dir.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config_dir))
    managed = config_dir / ".env"
    _write(
        managed,
        "MODEL=claude-sonnet-4-5\nMCC_SMOKE_THING=kept\nHOST=127.0.0.1\n",
    )

    prepared = persistence.prepare_admin_update({"PORT": "8123"})
    assert prepared.valid, prepared.errors
    # What the two-call path would have produced, computed before the write.
    expected_file = persistence.render_env_file(
        prepared.target_values,
        preserved=persistence.unmanaged_env_values(prepared.path),
    )
    response = persistence.commit_prepared_admin_update(prepared)
    assert managed.read_text(encoding="utf-8") == expected_file
    # And the preview, against a render taken from the file as it now stands --
    # which is what ``applied_response`` used to read.
    assert response["env_preview"] == persistence.render_env_file(
        prepared.target_values,
        mask_secrets=True,
        preserved=persistence.unmanaged_env_values(),
    )
    assert "MCC_SMOKE_THING=kept" in managed.read_text(encoding="utf-8")


def test_a_commit_is_visible_to_the_very_next_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached parse must never outlive the file it described."""

    config_dir = tmp_path / ".mcc"
    config_dir.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config_dir))
    managed = config_dir / ".env"
    _write(managed, "PORT=8123\n")
    assert sources.dotenv_values_from_file(managed)["PORT"] == "8123"

    prepared = persistence.prepare_admin_update({"PORT": "8124"})
    assert prepared.valid, prepared.errors
    persistence.commit_prepared_admin_update(prepared)
    assert sources.dotenv_values_from_file(managed)["PORT"] == "8124"
