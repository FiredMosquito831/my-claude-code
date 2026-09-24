"""The in-flight view of ``core/request_tasks``: phases, shape, bounds, privacy.

The registry itself -- register, unregister, reaping -- is proven for the
watchdog in ``tests/core/test_request_tasks.py``. These are the readers the
in-flight view adds on top of the same entries, and the rules they promise:
every phase has a stamp MCC itself took, ``None`` means "not measured", the
answer is bounded and says so when it cut something, and nothing in it is text
a client wrote.
"""

import asyncio
import dataclasses
import json
import time

import pytest

from my_claude_code.core import request_tasks
from my_claude_code.core.request_tasks import (
    INFLIGHT_PHASES,
    PHASE_ATTEMPT,
    PHASE_AWAITING_CONTENT,
    PHASE_AWAITING_FIRST_BYTE,
    PHASE_DESCRIBE,
    PHASE_RECEIVED,
    PHASE_ROUTING,
    PHASE_STREAMING,
    RequestProgress,
    RequestTaskEntry,
    inflight_phase,
    inflight_report,
    inflight_row,
)

FOLDER = "C:\\Users\\devuser\\Projects\\demo"
SESSION = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"


def _register(request_id: str = "req_a", progress=None, **extra):
    entry = request_tasks.register(
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        harness="claude",
        requested_model="mcc/best",
        progress=progress,
        **extra,
    )
    assert entry is not None
    return entry


def _progress(**fields) -> RequestProgress:
    return RequestProgress(phase=PHASE_AWAITING_FIRST_BYTE, **fields)


class TestPhases:
    def test_the_phase_vocabulary_is_ordered_and_closed(self) -> None:
        assert INFLIGHT_PHASES == (
            PHASE_RECEIVED,
            PHASE_DESCRIBE,
            PHASE_ROUTING,
            PHASE_ATTEMPT,
            PHASE_AWAITING_CONTENT,
            PHASE_STREAMING,
        )

    def test_a_request_nothing_has_happened_to_is_received(self) -> None:
        entry = _register(progress=lambda: _progress())
        phase, since = inflight_phase(entry, entry.read_progress())
        assert (phase, since) == (PHASE_RECEIVED, entry.started_at_mono)

    def test_a_request_without_a_reader_is_received(self) -> None:
        entry = _register(progress=None)
        assert inflight_phase(entry, None) == (PHASE_RECEIVED, entry.started_at_mono)

    def test_a_finished_describe_hop_before_the_plan_is_describe(self) -> None:
        entry = _register(progress=lambda: _progress(describe_hops=2))
        phase, since = inflight_phase(entry, entry.read_progress())
        # Describe starts on arrival; only its first report makes it visible.
        assert (phase, since) == (PHASE_DESCRIBE, entry.started_at_mono)

    def test_each_later_transition_carries_its_own_stamp(self) -> None:
        entry = _register()
        base = entry.started_at_mono
        routing = _progress(describe_hops=1, plan_mono=base + 1)
        attempt = _progress(plan_mono=base + 1, attempt_mono=base + 2)
        opened = _progress(
            plan_mono=base + 1, attempt_mono=base + 2, first_byte_mono=base + 3
        )
        streaming = _progress(
            plan_mono=base + 1,
            attempt_mono=base + 2,
            first_byte_mono=base + 3,
            first_content_mono=base + 4,
        )
        assert inflight_phase(entry, routing) == (PHASE_ROUTING, base + 1)
        assert inflight_phase(entry, attempt) == (PHASE_ATTEMPT, base + 2)
        # A byte went out (MCC's own opening frame) and the model has said
        # nothing yet: that is not streaming.
        assert inflight_phase(entry, opened) == (PHASE_AWAITING_CONTENT, base + 3)
        assert inflight_phase(entry, streaming) == (PHASE_STREAMING, base + 4)

    def test_the_first_byte_is_whichever_witness_saw_it_first(self) -> None:
        entry = _register()
        base = entry.started_at_mono
        entry.first_chunk_mono = base + 5
        observed = _progress(attempt_mono=base + 1, first_byte_mono=base + 4)
        assert inflight_phase(entry, observed) == (PHASE_AWAITING_CONTENT, base + 4)
        # With the log off the capture never sees the byte; the chunk counter
        # where bytes leave still does -- and content cannot be told from the
        # opening frame, so the row starts streaming at the first byte.
        unobserved = _progress(attempt_mono=base + 1, observed=False)
        assert inflight_phase(entry, unobserved) == (PHASE_STREAMING, base + 5)

    @pytest.mark.asyncio
    async def test_the_chunk_counter_stamps_the_first_chunk_once(self) -> None:
        entry = _register()
        request_tasks.note_stream_chunk()
        first = entry.first_chunk_mono
        await asyncio.sleep(0.01)
        request_tasks.note_stream_chunk()
        assert first is not None
        assert entry.first_chunk_mono == first
        assert entry.last_chunk_mono is not None
        assert entry.last_chunk_mono > first
        assert entry.chunks == 2


class TestTheRow:
    def test_an_unobserved_request_reports_null_counters_not_zero(self) -> None:
        entry = _register(
            progress=lambda: _progress(observed=False, output_chars=0),
        )
        row = inflight_row(entry, now=time.monotonic())
        assert row["output_chars"] is None
        assert row["thinking_chars"] is None
        assert row["attempt_tries"] is None

    def test_an_observed_request_reports_what_streamed(self) -> None:
        entry = _register(
            progress=lambda: _progress(
                output_chars=120,
                thinking_chars=30,
                attempt_index=1,
                provider="opencode",
                model_ref="opencode/big-pickle",
                key_label="sk-8...Kofx",
                proxy_label="10.0.0.1:1080",
                attempt_tries=2,
                last_try_status=429,
                waited_seconds=3.25,
                tier="best",
                tier_source="global",
            ),
        )
        row = inflight_row(entry, now=entry.started_at_mono + 2.0)
        assert row["output_chars"] == 120
        assert row["thinking_chars"] == 30
        assert row["attempt_index"] == 1
        assert row["provider"] == "opencode"
        assert row["model_ref"] == "opencode/big-pickle"
        assert row["key_label"] == "sk-8...Kofx"
        assert row["proxy_label"] == "10.0.0.1:1080"
        assert row["attempt_tries"] == 2
        assert row["last_try_status"] == 429
        assert row["waited_s"] == 3.25
        assert row["tier"] == "best"
        assert row["tier_source"] == "global"
        assert row["elapsed_ms"] == 2000.0

    def test_ttft_falls_back_to_the_chunk_counter(self) -> None:
        entry = _register(progress=lambda: _progress(observed=False))
        entry.first_chunk_mono = entry.started_at_mono + 0.25
        row = inflight_row(entry, now=entry.started_at_mono + 1)
        assert row["ttft_ms"] == 250.0
        assert row["phase"] == PHASE_STREAMING

    def test_the_origin_rides_along_with_its_short_forms(self) -> None:
        entry = _register(
            session_id=SESSION,
            project_dir=FOLDER,
            origin_source="session_id=header.x-claude-code-session-id",
            tools_count=12,
            input_chars=4096,
            image_count=1,
        )
        row = inflight_row(entry, now=time.monotonic())
        assert row["session_id"] == SESSION
        assert row["session_short"]
        assert row["project_dir"] == FOLDER
        assert row["project_short"] and "demo" in row["project_short"]
        assert row["tools_count"] == 12
        assert row["input_chars"] == 4096
        assert row["image_count"] == 1
        assert row["project_dir_pending"] is False

    def test_every_value_is_json_native(self) -> None:
        entry = _register(progress=lambda: _progress(output_chars=1))
        row = inflight_row(entry, now=time.monotonic())
        assert json.loads(json.dumps(row)) == row

    def test_a_reader_that_raises_leaves_a_received_row(self) -> None:
        def broken() -> RequestProgress:
            raise RuntimeError("observer bug")

        entry = _register(progress=broken)
        row = inflight_row(entry, now=time.monotonic())
        assert row["phase"] == PHASE_RECEIVED
        assert row["provider"] is None


class TestTheReport:
    def test_oldest_first_and_bounded_with_the_cut_declared(self) -> None:
        entries = [_register(f"req_{index}") for index in range(5)]
        for offset, entry in enumerate(entries):
            entry.started_at_mono -= 100 - offset
        report = inflight_report(limit=3)
        assert report["enabled"] is True
        assert report["total"] == 5
        assert report["shown"] == 3
        assert report["truncated"] is True
        assert [row["id"] for row in report["rows"]] == ["req_0", "req_1", "req_2"]

    def test_the_view_switched_off_answers_the_pulse_shape(self) -> None:
        request_tasks.configure(enabled=True, inflight=False)
        _register()
        assert inflight_report() == {"enabled": False}
        assert request_tasks.inflight_count() is None
        # The watchdog still has its registry.
        assert request_tasks.count() == 1

    def test_the_view_alone_keeps_the_registry_on(self) -> None:
        request_tasks.configure(enabled=False, inflight=True)
        assert request_tasks.enabled() is True
        _register()
        assert request_tasks.inflight_count() == 1

    def test_both_readers_off_empties_and_stops_the_registry(self) -> None:
        _register()
        request_tasks.configure(enabled=False, inflight=False)
        assert request_tasks.count() == 0
        assert (
            request_tasks.register(
                request_id="req_after",
                endpoint="/v1/messages",
                protocol="anthropic",
                stream=True,
                harness=None,
                requested_model=None,
                progress=None,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_an_entry_whose_task_is_done_is_reaped_and_counted(self) -> None:
        async def serve() -> None:
            _register("req_abandoned")

        await asyncio.create_task(serve())
        before = request_tasks.reaped_total()
        report = inflight_report()
        assert report["total"] == 0
        assert report["rows"] == []
        assert report["reaped"] == before + 1


class TestPrivacy:
    # ``content`` is deliberately absent: ``first_content_mono`` is a clock
    # stamp. Anything that could hold text would be spelled ``*_text``.
    TEXT_WORDS = ("text", "prompt", "body", "header", "message", "tool_calls")

    @pytest.mark.parametrize("cls", [RequestTaskEntry, RequestProgress])
    def test_no_slot_can_hold_prompt_or_reply_text(self, cls) -> None:
        names = {field.name for field in dataclasses.fields(cls)}
        assert names == set(cls.__slots__) - {"__weakref__"}
        for name in names:
            for word in self.TEXT_WORDS:
                assert word not in name, (cls.__name__, name)

    def test_the_row_keys_carry_no_text_field(self) -> None:
        entry = _register(progress=lambda: _progress())
        row = inflight_row(entry, now=time.monotonic())
        for key in row:
            for word in self.TEXT_WORDS:
                assert word not in key, key


@pytest.mark.asyncio
async def test_the_count_reaps_before_it_counts() -> None:
    """A log-off stream never finalizes; the pulse count must not keep it."""

    async def serve() -> None:
        _register("req_finished_unlogged")

    await asyncio.create_task(serve())
    assert request_tasks.count() == 1
    assert request_tasks.inflight_count() == 0
    assert request_tasks.count() == 0
