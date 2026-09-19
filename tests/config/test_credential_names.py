"""The credential name store: what it keeps, what it refuses to keep.

Two claims are load-bearing and both are asserted here: the file never holds a
secret, and a name is a display join rather than a stored dimension -- which is
only safe if an *ambiguous* masked label resolves to no name at all.
"""

import json
from pathlib import Path

from my_claude_code.config.credential_names import (
    MAX_NAME_LENGTH,
    credential_fingerprint,
    custom_pool_id,
    env_pool_id,
    forget_credentials,
    forget_pool,
    label_name_map,
    load_document,
    merged_label_names,
    names_for_secrets,
    oauth_credential_id,
    oauth_pool_id,
    pool_names,
    set_name,
    websearch_pool_id,
)
from my_claude_code.config.credentials import mask_key_label

POOL = env_pool_id("NVIDIA_NIM_API_KEY")
KEY_A = "nvapi-aaaaaaaaaaaaaaaaaaaaepz9"
KEY_B = "nvapi-bbbbbbbbbbbbbbbbbbbbzzz1"
KEY_C = "nvapi-cccccccccccccccccccc4444"


def _store(tmp_path: Path) -> Path:
    return tmp_path / "credential_names.json"


def test_a_name_survives_the_key_moving_to_a_new_position(tmp_path) -> None:
    path = _store(tmp_path)
    set_name(POOL, credential_fingerprint(KEY_B), "Personal", path)

    # The pool is rewritten in a new order; nothing about the store changes.
    assert names_for_secrets(POOL, [KEY_A, KEY_B, KEY_C], path) == [
        "",
        "Personal",
        "",
    ]
    assert names_for_secrets(POOL, [KEY_B, KEY_A, KEY_C], path) == [
        "Personal",
        "",
        "",
    ]


def test_the_store_never_holds_the_secret_only_its_fingerprint(tmp_path) -> None:
    path = _store(tmp_path)
    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)
    text = path.read_text(encoding="utf-8")

    assert KEY_A not in text
    assert mask_key_label(KEY_A) not in text
    assert credential_fingerprint(KEY_A) in text
    assert credential_fingerprint(KEY_A).startswith("sha256:")
    # 16 hex characters of digest, matching ``core/wire_capture``.
    assert len(credential_fingerprint(KEY_A)) == len("sha256:") + 16


def test_the_same_secret_always_gets_the_same_id(tmp_path) -> None:
    assert credential_fingerprint(KEY_A) == credential_fingerprint(KEY_A)
    assert credential_fingerprint(KEY_A) != credential_fingerprint(KEY_B)


def test_an_empty_name_deletes_the_entry_rather_than_storing_a_blank(
    tmp_path,
) -> None:
    path = _store(tmp_path)
    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)
    set_name(POOL, credential_fingerprint(KEY_A), "   ", path)

    assert pool_names(POOL, path) == {}
    assert load_document(path)["pools"] == {}


def test_a_name_is_trimmed_and_clamped_to_sixty_characters(tmp_path) -> None:
    path = _store(tmp_path)
    stored = set_name(POOL, credential_fingerprint(KEY_A), "  " + "x" * 90, path)

    assert stored == "x" * MAX_NAME_LENGTH
    assert len(stored) == 60


def test_removing_a_key_drops_its_name(tmp_path) -> None:
    path = _store(tmp_path)
    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)
    set_name(POOL, credential_fingerprint(KEY_B), "Personal", path)

    forget_credentials(POOL, [credential_fingerprint(KEY_A)], path)

    assert pool_names(POOL, path) == {credential_fingerprint(KEY_B): "Personal"}


def test_deleting_a_provider_drops_the_whole_pool(tmp_path) -> None:
    path = _store(tmp_path)
    pool = custom_pool_id("custom_b_ai")
    set_name(pool, credential_fingerprint(KEY_A), "Spare", path)

    forget_pool(pool, path)

    assert pool_names(pool, path) == {}


def test_a_store_from_a_newer_version_is_read_as_empty_and_left_alone(
    tmp_path,
) -> None:
    path = _store(tmp_path)
    original = json.dumps({"version": 99, "pools": {"env:X": {"credentials": {}}}})
    path.write_text(original, encoding="utf-8")

    assert load_document(path)["pools"] == {}
    # Read as empty, and *not* rewritten: a downgrade must not eat the names a
    # newer build wrote.
    assert path.read_text(encoding="utf-8") == original


def test_unparsable_json_is_read_as_empty_and_never_raises(tmp_path) -> None:
    path = _store(tmp_path)
    path.write_text("{not json", encoding="utf-8")

    assert load_document(path)["pools"] == {}
    assert pool_names(POOL, path) == {}


def test_a_missing_file_is_read_as_empty_and_never_raises(tmp_path) -> None:
    assert load_document(tmp_path / "nope.json")["pools"] == {}
    assert pool_names(POOL, tmp_path / "nope.json") == {}


def test_two_keys_sharing_a_masked_label_resolve_to_no_name(tmp_path) -> None:
    path = _store(tmp_path)
    # ``mask_key_label`` is first4...last4, and real vendor keys share prefixes.
    twin_a = "nvapi-0000000000000000zzzz"
    twin_b = "nvapi-1111111111111111zzzz"
    assert mask_key_label(twin_a) == mask_key_label(twin_b)
    set_name(POOL, credential_fingerprint(twin_a), "Work", path)

    assert label_name_map(POOL, [twin_a, twin_b], path) == {}
    # The unambiguous key beside them is still named.
    set_name(POOL, credential_fingerprint(KEY_C), "Spare", path)
    assert label_name_map(POOL, [twin_a, twin_b, KEY_C], path) == {
        mask_key_label(KEY_C): "Spare"
    }


def test_the_display_join_maps_a_mask_to_its_name(tmp_path) -> None:
    path = _store(tmp_path)
    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)

    assert label_name_map(POOL, [KEY_A, KEY_B], path) == {mask_key_label(KEY_A): "Work"}


def test_two_pools_that_disagree_about_a_mask_resolve_to_no_name(tmp_path) -> None:
    path = _store(tmp_path)
    other = websearch_pool_id("EXA_API_KEY")
    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)

    merged = merged_label_names([(POOL, [KEY_A]), (other, [KEY_A])], path)

    # The same secret is configured in two pools and named in only one of them,
    # so the label cannot be resolved with certainty and is dropped.
    assert merged == {}


def test_an_oauth_account_id_is_a_valid_credential_id(tmp_path) -> None:
    path = _store(tmp_path)
    pool = oauth_pool_id("anthropic_oauth")
    credential = oauth_credential_id("acct_123")

    set_name(pool, credential, "Personal Max", path)

    assert credential == "account:acct_123"
    assert pool_names(pool, path) == {credential: "Personal Max"}


def test_the_store_is_written_atomically_and_an_unchanged_write_is_skipped(
    tmp_path,
) -> None:
    path = _store(tmp_path)
    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)
    first = path.stat().st_mtime_ns
    payload = path.read_bytes()

    set_name(POOL, credential_fingerprint(KEY_A), "Work", path)

    assert path.read_bytes() == payload
    assert path.stat().st_mtime_ns == first
    # No staging file survives a completed write.
    assert list(tmp_path.glob("*.fcc-tmp")) == []


def test_pool_ids_are_namespaced_so_oauth_needs_no_migration() -> None:
    assert env_pool_id("NVIDIA_NIM_API_KEY") == "env:NVIDIA_NIM_API_KEY"
    assert custom_pool_id("custom_b_ai") == "custom:custom_b_ai"
    assert websearch_pool_id("EXA_API_KEY") == "websearch:EXA_API_KEY"
    assert oauth_pool_id("anthropic_oauth") == "oauth:anthropic_oauth"
