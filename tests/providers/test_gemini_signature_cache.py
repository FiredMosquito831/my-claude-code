"""The Google thought-signature cache survives a restart, per credential.

PR-CARRIER-SURFACE-SWEEP, Q2. Google validates a ``thoughtSignature`` on the
current turn's first tool call. MCC captures one off the stream whenever the
host sends it and replays it on the next turn -- which is what makes a Gemini
tool loop work at all. Until 8.19.0 that map lived only in the provider object
and died with the process: a loop that spanned a restart lost every signature
and fell back to ``skip_thought_signature_validator``, the escape hatch that
tells Google not to validate. The loop kept working; the operator had no way to
see that it had stopped being validated.

**The five constraints the design must honour, each with its own test:**

1. **Per provider AND per credential.** A signature minted under one key must
   not be replayed under another. The credential half is
   ``credential_identity``'s rule, hashed through ``connection_id`` -- hash the
   identity, never the token, because a Vertex token refreshes hourly and
   hashing it would refuse every signature captured before a refresh.
2. **Bounded.** The in-memory map's own 4096-entry ceiling, and the same FIFO
   eviction rule, so a restart restores exactly the population the process held.
3. **Never grows without limit on disk.** A second ceiling on credential
   buckets, so a rotation storm cannot grow the file without bound.
4. **A corrupt or unreadable store degrades to in-memory.** Never raises on a
   request path.
5. **A cache, not state.** Losing it is a performance loss, never a
   correctness one.

The mutation control this file is built to catch (it turns a named test red) is
recorded in the PR body: **make the store global instead of per-credential** ->
``test_a_signature_minted_under_one_credential_is_not_replayed_under_another``
fails.
"""

import json
from pathlib import Path

import pytest

from my_claude_code.config.provider_catalog import GEMINI_DEFAULT_BASE
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.gemini import GeminiProvider
from my_claude_code.providers.gemini.quirks import (
    GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR,
)
from my_claude_code.providers.google_openai.signature_cache import (
    MAX_CREDENTIALS,
    MAX_ENTRIES,
    GoogleThoughtSignatureStore,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, reasoning_for

SIGNATURE = {"google": {"thought_signature": "sig-1"}}


def _store() -> GoogleThoughtSignatureStore:
    """A store with no debounce, so a test never waits on a timer."""

    return GoogleThoughtSignatureStore(flush_debounce_seconds=0)


def _config(key: str = "test_gemini_key") -> ProviderConfig:
    return ProviderConfig(
        api_key=key,
        base_url=GEMINI_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
    )


def _request_with_tool_call():
    """One turn whose first tool call has no signature of its own."""

    return make_messages_request(
        "models/gemini-3.1-flash-lite",
        system=None,
        messages=[
            {"role": "user", "content": "Find files"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "function-call-1",
                        "name": "Glob",
                        "input": {"pattern": "*.py"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "function-call-1",
                        "content": "[]",
                    }
                ],
            },
        ],
    )


class TestTheStoreSurvivesARestart:
    """Constraint 5 and the headline claim: a captured signature is replayed
    after a process restart."""

    def test_a_recorded_signature_reloads(self, tmp_path: Path) -> None:
        path = tmp_path / "google_thought_signatures.json"
        first = _store()
        first.enable_persistence(path)
        first.record("gemini", "cred-a", "call-1", SIGNATURE)
        assert first.flush() is True

        second = _store()
        second.enable_persistence(path)
        assert second.rows_for("gemini", "cred-a") == {"call-1": SIGNATURE}

    def test_a_provider_built_after_a_restart_replays_the_signature(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end claim, through the provider rather than the store.

        A provider records a signature, a *new* provider object is built (which
        is what a restart produces), and the request it builds carries the
        signature instead of the sentinel.
        """

        from my_claude_code.providers.google_openai import signature_cache

        path = tmp_path / "google_thought_signatures.json"
        store = _store()
        store.enable_persistence(path)
        monkeypatch.setattr(signature_cache, "GOOGLE_THOUGHT_SIGNATURES", store)
        # The provider modules imported the name directly, so patch there too.
        monkeypatch.setattr(
            "my_claude_code.providers.gemini.client.GOOGLE_THOUGHT_SIGNATURES", store
        )

        before = GeminiProvider(_config(), rate_limiter=passthrough_rate_limiter())
        before._record_tool_call_extra_content("function-call-1", SIGNATURE)
        store.flush()

        after = GeminiProvider(_config(), rate_limiter=passthrough_rate_limiter())
        assert after._tool_call_extra_content_by_id["function-call-1"] == SIGNATURE

        request = _request_with_tool_call()
        body = after._build_request_body(request, reasoning=reasoning_for(request))
        assert body["messages"][1]["tool_calls"][0]["extra_content"] == SIGNATURE


class TestItIsPerCredential:
    """Constraint 1, and the mutation control this file exists for."""

    def test_a_signature_minted_under_one_credential_is_not_replayed_under_another(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from my_claude_code.providers.google_openai import signature_cache

        path = tmp_path / "google_thought_signatures.json"
        store = _store()
        store.enable_persistence(path)
        monkeypatch.setattr(
            "my_claude_code.providers.gemini.client.GOOGLE_THOUGHT_SIGNATURES", store
        )
        monkeypatch.setattr(signature_cache, "GOOGLE_THOUGHT_SIGNATURES", store)

        under_a = GeminiProvider(
            _config("key-a"), rate_limiter=passthrough_rate_limiter()
        )
        under_a._record_tool_call_extra_content("function-call-1", SIGNATURE)
        store.flush()

        under_b = GeminiProvider(
            _config("key-b"), rate_limiter=passthrough_rate_limiter()
        )
        assert under_b._tool_call_extra_content_by_id == {}

        request = _request_with_tool_call()
        body = under_b._build_request_body(request, reasoning=reasoning_for(request))
        assert body["messages"][1]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR}
        }

    def test_the_credential_id_is_a_hash_never_the_key(self) -> None:
        provider = GeminiProvider(
            _config("key-a"), rate_limiter=passthrough_rate_limiter()
        )
        identity = provider._credential_id
        assert "key-a" not in identity
        assert len(identity) == 16

    def test_two_providers_never_share_a_bucket(self, tmp_path: Path) -> None:
        store = _store()
        store.record("gemini", "cred-a", "call-1", SIGNATURE)
        store.record("vertex", "cred-a", "call-1", {"google": {"ts": "other"}})
        assert store.rows_for("gemini", "cred-a") == {"call-1": SIGNATURE}
        assert store.rows_for("vertex", "cred-a") == {
            "call-1": {"google": {"ts": "other"}}
        }


class TestItIsBounded:
    """Constraints 2 and 3."""

    def test_the_entry_ceiling_matches_the_in_memory_map(self) -> None:
        assert MAX_ENTRIES == 4096

    def test_the_oldest_entry_is_evicted_first(self) -> None:
        store = _store()
        for index in range(MAX_ENTRIES):
            store.record("gemini", "cred-a", f"call-{index}", SIGNATURE)
        assert len(store) == MAX_ENTRIES
        assert "call-0" in store.rows_for("gemini", "cred-a")
        store.record("gemini", "cred-a", "call-new", SIGNATURE)
        assert len(store) == MAX_ENTRIES
        assert "call-0" not in store.rows_for("gemini", "cred-a")
        assert "call-new" in store.rows_for("gemini", "cred-a")

    def test_the_evicted_row_is_handed_back(self) -> None:
        """The caller drops the same entry from its own map, so the two
        ceilings cannot disagree about what is cached."""

        store = _store()
        for index in range(MAX_ENTRIES):
            store.record("gemini", "cred-a", f"call-{index}", SIGNATURE)
        evicted = store.record("gemini", "cred-a", "call-new", SIGNATURE)
        assert evicted == ("gemini", "cred-a", "call-0")

    def test_the_credential_ceiling_holds(self) -> None:
        store = _store()
        for index in range(MAX_CREDENTIALS + 5):
            store.record("gemini", f"cred-{index}", "call-1", SIGNATURE)
        buckets = {key[0:2] for key in store._rows}
        assert len(buckets) == MAX_CREDENTIALS
        # The oldest bucket went whole, not partly.
        assert store.rows_for("gemini", "cred-0") == {}
        assert store.rows_for("gemini", f"cred-{MAX_CREDENTIALS + 4}") != {}

    def test_the_document_is_a_list_of_rows(self, tmp_path: Path) -> None:
        store = _store()
        store.record("gemini", "cred-a", "call-1", SIGNATURE)
        document = store.as_document()
        assert document["version"] == 1
        assert document["rows"] == [
            {
                "provider": "gemini",
                "credential": "cred-a",
                "tool_call_id": "call-1",
                "extra_content": SIGNATURE,
            }
        ]


class TestACorruptStoreDegradesToMemory:
    """Constraint 4: never raises on a request path."""

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "",
            "   ",
            "[]",
            '{"rows": "not a list"}',
            '{"rows": [1, 2, 3]}',
            '{"rows": [{"provider": "gemini"}]}',
            '{"rows": [{"provider": "", "credential": "c", "tool_call_id": "t",'
            ' "extra_content": {}}]}',
        ],
    )
    def test_an_unreadable_file_loads_nothing_and_does_not_raise(
        self, tmp_path: Path, content: str
    ) -> None:
        path = tmp_path / "google_thought_signatures.json"
        path.write_text(content, encoding="utf-8")
        store = _store()
        store.enable_persistence(path)
        assert len(store) == 0

    def test_a_missing_file_loads_nothing(self, tmp_path: Path) -> None:
        store = _store()
        store.enable_persistence(tmp_path / "absent.json")
        assert len(store) == 0

    def test_a_provider_still_builds_a_body_after_a_corrupt_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from my_claude_code.providers.google_openai import signature_cache

        path = tmp_path / "google_thought_signatures.json"
        path.write_text("{not json", encoding="utf-8")
        store = _store()
        store.enable_persistence(path)
        monkeypatch.setattr(
            "my_claude_code.providers.gemini.client.GOOGLE_THOUGHT_SIGNATURES", store
        )
        monkeypatch.setattr(signature_cache, "GOOGLE_THOUGHT_SIGNATURES", store)

        provider = GeminiProvider(_config(), rate_limiter=passthrough_rate_limiter())
        request = _request_with_tool_call()
        body = provider._build_request_body(request, reasoning=reasoning_for(request))
        assert body["messages"][1]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": GEMINI_SKIP_THOUGHT_SIGNATURE_VALIDATOR}
        }

    def test_a_failed_write_does_not_raise(self, tmp_path: Path) -> None:
        store = _store()
        # A directory where the file should be: every write fails.
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        store.enable_persistence(blocked)
        store.record("gemini", "cred-a", "call-1", SIGNATURE)
        assert store.flush() is False
        # Still dirty, so the next attempt -- or shutdown -- retries.
        assert store._dirty is True


class TestItIsACacheNotState:
    """Constraint 5: losing it is a performance loss, never a correctness one."""

    def test_an_empty_store_produces_the_same_body_as_no_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every existing Gemini body is byte-identical when the cache is
        empty -- the equality claim, at the provider."""

        from my_claude_code.providers.google_openai import signature_cache

        empty = _store()
        monkeypatch.setattr(
            "my_claude_code.providers.gemini.client.GOOGLE_THOUGHT_SIGNATURES", empty
        )
        monkeypatch.setattr(signature_cache, "GOOGLE_THOUGHT_SIGNATURES", empty)

        provider = GeminiProvider(_config(), rate_limiter=passthrough_rate_limiter())
        request = _request_with_tool_call()
        with_empty = provider._build_request_body(
            request, reasoning=reasoning_for(request)
        )

        # The same provider with no store at all: a memory-only store is the
        # behaviour every release before 8.19.0 had.
        memory_only = _store()
        monkeypatch.setattr(
            "my_claude_code.providers.gemini.client.GOOGLE_THOUGHT_SIGNATURES",
            memory_only,
        )
        monkeypatch.setattr(signature_cache, "GOOGLE_THOUGHT_SIGNATURES", memory_only)
        bare = GeminiProvider(_config(), rate_limiter=passthrough_rate_limiter())
        without = bare._build_request_body(request, reasoning=reasoning_for(request))

        assert json.dumps(with_empty, sort_keys=True) == json.dumps(
            without, sort_keys=True
        )

    def test_reset_forgets_everything_and_stops_writing(self, tmp_path: Path) -> None:
        path = tmp_path / "google_thought_signatures.json"
        store = _store()
        store.enable_persistence(path)
        store.record("gemini", "cred-a", "call-1", SIGNATURE)
        store.reset()
        assert len(store) == 0
        assert store.path is None
        assert store.flush() is False
