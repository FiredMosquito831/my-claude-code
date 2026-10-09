"""``core/request_origin.py``: which conversation, subagent and folder sent a request.

Table-driven over the declared extractors, one case per row, plus the rules the
table exists to hold: first source wins in the declared order, the prompt is
read from the system blocks and then the first user message (where Claude Code
sends it, 7.87.1) and never past 64 KiB of either, a harness with no prompt
extractor pays nothing, NULL means "not stated", and nothing here widens what
MCC sends upstream or stores in the ``headers`` column.

Every path in this file is fake. None is the operator's own.
"""

import hashlib
import time
from typing import Any

import pytest

from my_claude_code.core import client_fingerprint
from my_claude_code.core.anthropic.models import (
    ContentBlockText,
    ContentBlockToolResult,
    Message,
    SystemContent,
)
from my_claude_code.core.request_headers import ALLOWED_HEADERS
from my_claude_code.core.request_origin import (
    BACKFILL_SIGNAL,
    EMPTY_ORIGIN,
    EXTRACTORS,
    MAX_ID_CHARS,
    MAX_PROJECT_DIR_CHARS,
    ORIGIN_HEADERS,
    PROMPT_HARNESSES,
    PROMPT_SCAN_MAX_CHARS,
    SOURCE_ORDER,
    first_user_message_text,
    format_origin_source,
    merge_origin_source,
    origin_inputs,
    origin_provenance,
    parse_origin_source,
    project_dir_from_prompt,
    project_short,
    resolve_origin,
    session_short,
    system_prompt_text,
)

SESSION = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
AGENT = "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
FOLDER = "C:\\Users\\devuser\\Projects\\demo"
ENV_BLOCK = (
    "# Environment\n"
    "You have been invoked in the following environment:\n"
    f" - Primary working directory: {FOLDER}\n"
    " - Is a git repository: false\n"
    " - Platform: win32\n"
)


def _resolve(
    headers: dict[str, str] | None = None,
    *,
    harness: str | None,
    metadata: Any = None,
    system: Any = None,
    messages: Any = None,
    capture_session: bool = True,
    capture_folder: bool = True,
):
    return resolve_origin(
        origin_inputs(headers, harness=harness, metadata=metadata),
        capture_session=capture_session,
        capture_folder=capture_folder,
        system=system,
        messages=messages,
    )


# One case per declared row: (id, harness, headers, metadata, system,
# field that must be filled, its value, its origin_source entry).
DECLARED_ROWS = [
    (
        "claude-session-header",
        "claude",
        {"X-Claude-Code-Session-Id": SESSION},
        None,
        None,
        "session_id",
        SESSION,
        "session_id=header.x-claude-code-session-id",
    ),
    (
        "sdk-session-header",
        "claude_agent_sdk",
        {"x-claude-code-session-id": SESSION},
        None,
        None,
        "session_id",
        SESSION,
        "session_id=header.x-claude-code-session-id",
    ),
    (
        "opencode-session-header",
        "opencode",
        {"x-opencode-session": "ses_demo123"},
        None,
        None,
        "session_id",
        "ses_demo123",
        "session_id=header.x-opencode-session",
    ),
    (
        "metadata-user-id-tail",
        "claude_agent_sdk",
        {},
        {"user_id": f"user_abc123_account_{AGENT}_session_{SESSION}"},
        None,
        "session_id",
        SESSION,
        "session_id=metadata.metadata.user_id",
    ),
    (
        "metadata-user-id-json",
        "claude",
        {},
        {"user_id": '{"device_id":"d1","session_id":"' + SESSION + '"}'},
        None,
        "session_id",
        SESSION,
        "session_id=metadata.metadata.user_id",
    ),
    (
        "launcher-session",
        "gemini_cli",
        {"x-mcc-session": "launch-42"},
        None,
        None,
        "session_id",
        "launch-42",
        "session_id=launcher.x-mcc-session",
    ),
    (
        "claude-agent-header",
        "claude",
        {"x-claude-code-agent-id": AGENT},
        None,
        None,
        "agent_id",
        AGENT,
        "agent_id=header.x-claude-code-agent-id",
    ),
    (
        "child-of-session",
        "claude",
        {"x-claude-code-session-id": SESSION, "x-claude-code-agent-id": AGENT},
        None,
        None,
        "parent_session_id",
        SESSION,
        "parent_session_id=header.x-claude-code-agent-id",
    ),
    (
        "prompt-env-block",
        "claude",
        {},
        None,
        [SystemContent(type="text", text="You are Claude Code.\n" + ENV_BLOCK)],
        "project_dir",
        FOLDER,
        "project_dir=prompt.env-block",
    ),
    (
        "launcher-cwd",
        "codex",
        {"x-mcc-cwd": "/home/devuser/projects/demo/"},
        None,
        None,
        "project_dir",
        "/home/devuser/projects/demo",
        "project_dir=launcher.x-mcc-cwd",
    ),
]


@pytest.mark.parametrize(
    ("harness", "headers", "metadata", "system", "field", "value", "source"),
    [case[1:] for case in DECLARED_ROWS],
    ids=[case[0] for case in DECLARED_ROWS],
)
def test_request_origin_extractors(
    harness, headers, metadata, system, field, value, source
) -> None:
    origin = _resolve(headers, harness=harness, metadata=metadata, system=system)

    assert getattr(origin, field) == value
    assert source in (origin.origin_source or "").split(";")


def test_every_declared_row_has_a_case_above() -> None:
    """A row added to the table without a case is a signal nobody has exercised."""

    covered = {case[7].split("=", 1)[1] for case in DECLARED_ROWS}
    covered |= {
        f"{extractor.source}.{extractor.signal}"
        for extractor in EXTRACTORS
        if extractor.field == "parent_session_id"
    }
    declared = {extractor.id for extractor in EXTRACTORS}
    assert declared <= covered


def test_request_origin_null_when_absent() -> None:
    """Nothing stated means nothing stored -- never a placeholder."""

    assert _resolve(None, harness="claude") is EMPTY_ORIGIN
    assert _resolve({}, harness="unknown", metadata={}, system="") is EMPTY_ORIGIN
    assert (
        _resolve({"user-agent": "curl/8.0", "x-api-key": "secret"}, harness="script")
        is EMPTY_ORIGIN
    )
    # The Agent SDK's prompt has no environment block, and it has no prompt
    # extractor either: an env-looking line in its prompt is not read.
    sdk = _resolve({}, harness="claude_agent_sdk", system=ENV_BLOCK)
    assert sdk.project_dir is None
    # A header declared for another harness is not believed here.
    assert _resolve({"x-opencode-session": "x1"}, harness="claude").session_id is None


def test_a_header_beats_metadata_for_the_same_field() -> None:
    origin = _resolve(
        {"x-claude-code-session-id": SESSION},
        harness="claude",
        metadata={
            "user_id": "user_x_account_y_session_ffffffff-0000-0000-0000-000000000000"
        },
    )

    assert origin.session_id == SESSION
    assert parse_origin_source(origin.origin_source)["session_id"] == (
        "header",
        "x-claude-code-session-id",
    )


def test_the_prompt_beats_a_launcher_for_the_folder() -> None:
    origin = _resolve(
        {"x-mcc-cwd": "D:\\elsewhere"}, harness="claude", system=ENV_BLOCK
    )

    assert origin.project_dir == FOLDER
    assert "project_dir=prompt.env-block" in (origin.origin_source or "")


def test_the_declared_order_is_header_metadata_prompt_launcher() -> None:
    assert SOURCE_ORDER == ("header", "metadata", "prompt", "launcher")


def test_a_subagent_without_a_session_has_no_parent() -> None:
    origin = _resolve({"x-claude-code-agent-id": AGENT}, harness="claude")

    assert origin.agent_id == AGENT
    assert origin.parent_session_id is None


def test_the_agent_sdk_records_the_agent_id_but_no_parent_link() -> None:
    """The child signal is declared for Claude Code only (71 % of its rows);
    the SDK sent the header on 0.1 % of rows and nothing says what it means there."""

    origin = _resolve(
        {"x-claude-code-session-id": SESSION, "x-claude-code-agent-id": AGENT},
        harness="claude_agent_sdk",
    )

    assert origin.agent_id == AGENT
    assert origin.parent_session_id is None


class _Exploding:
    """A system block whose text must never be read."""

    @property
    def text(self) -> str:
        raise AssertionError("the system prompt was read")


def test_a_harness_without_a_prompt_extractor_never_reads_the_prompt() -> None:
    """Three quarters of real traffic is the Agent SDK; it must pay nothing."""

    origin = _resolve(
        {"x-claude-code-session-id": SESSION},
        harness="claude_agent_sdk",
        system=[_Exploding()],
    )

    assert origin.session_id == SESSION
    assert frozenset({"claude"}) == PROMPT_HARNESSES


def test_folder_capture_off_never_reads_the_prompt() -> None:
    origin = _resolve(
        {"x-claude-code-session-id": SESSION},
        harness="claude",
        system=[_Exploding()],
        capture_folder=False,
    )

    assert origin.session_id == SESSION
    assert origin.project_dir is None
    assert "project_dir" not in (origin.origin_source or "")


def test_session_capture_off_stores_no_client_id() -> None:
    origin = _resolve(
        {"x-claude-code-session-id": SESSION, "x-claude-code-agent-id": AGENT},
        harness="claude",
        system=ENV_BLOCK,
        capture_session=False,
    )

    assert (origin.session_id, origin.agent_id, origin.parent_session_id) == (
        None,
        None,
        None,
    )
    assert origin.project_dir == FOLDER
    assert origin.origin_source == "project_dir=prompt.env-block"


def test_both_off_is_the_empty_origin() -> None:
    assert (
        _resolve(
            {"x-claude-code-session-id": SESSION},
            harness="claude",
            system=ENV_BLOCK,
            capture_session=False,
            capture_folder=False,
        )
        is EMPTY_ORIGIN
    )


class TestTheSystemBlocksAreReadFirstAndCapped:
    def test_a_700k_system_prompt_is_read_only_to_64_kib(self) -> None:
        """The block is found where real traffic puts it (max offset 15,722)
        and not found past the cap -- the closed failure the spec asks for."""

        filler = "x" * 70 + "\n"
        huge_tail = filler * (700_000 // len(filler))
        near = "p" * 15_722 + "\n" + ENV_BLOCK + huge_tail
        beyond = "p" * (PROMPT_SCAN_MAX_CHARS + 10) + "\n" + ENV_BLOCK

        assert len(near) > 700_000
        started = time.perf_counter()
        found = _resolve({}, harness="claude", system=near)
        elapsed = time.perf_counter() - started
        assert found.project_dir == FOLDER
        assert _resolve({}, harness="claude", system=beyond).project_dir is None
        # Only the capped head is ever joined, however large the prompt.
        assert len(system_prompt_text(near)) == PROMPT_SCAN_MAX_CHARS
        assert elapsed < 0.5

    def test_the_cap_applies_across_a_list_of_blocks(self) -> None:
        blocks = [
            SystemContent(type="text", text="a" * 40_000),
            SystemContent(type="text", text="b" * 40_000),
            SystemContent(type="text", text=ENV_BLOCK),
        ]

        assert _resolve({}, harness="claude", system=blocks).project_dir is None
        assert len(system_prompt_text(blocks)) == PROMPT_SCAN_MAX_CHARS

    def test_dict_shaped_blocks_are_read_too(self) -> None:
        blocks = [{"type": "text", "text": ENV_BLOCK}]

        assert _resolve({}, harness="claude", system=blocks).project_dir == FOLDER

    def test_an_over_long_line_does_not_match(self) -> None:
        long_path = "C:\\" + "d" * 300
        system = f" - Primary working directory: {long_path}\n"

        assert _resolve({}, harness="claude", system=system).project_dir is None

    def test_crlf_line_endings_are_tolerated(self) -> None:
        system = ENV_BLOCK.replace("\n", "\r\n")

        assert _resolve({}, harness="claude", system=system).project_dir == FOLDER

    def test_a_prose_mention_before_the_block_does_not_hide_it(self) -> None:
        """The literal pre-check starts the search at the first mention's line;
        a real block further down must still be found."""

        system = "See the Primary working directory: field below.\n" + ENV_BLOCK

        assert _resolve({}, harness="claude", system=system).project_dir == FOLDER

    def test_the_anchor_must_start_a_line(self) -> None:
        system = "Note: the Primary working directory: C:\\fake is prose here\n"

        assert _resolve({}, harness="claude", system=system).project_dir is None


# A Claude Code request as it reaches MCC today (2.1.271-2.1.281, measured
# 2026-10-08): the system blocks end without the environment block, and the
# first user message opens with a ``<system-reminder>`` that carries it.
CC_FOLDER = "C:\\x\\y"
CC_SYSTEM = [
    SystemContent(type="text", text="x-anthropic-billing-header: cc_version=2.1.280;"),
    SystemContent(
        type="text",
        text="You are Claude Code.\n...\n<total_tokens>15000000 tokens left</total_tokens>",
    ),
]
CC_REMINDER = (
    "<system-reminder>\n"
    "# Environment\n"
    "You have been invoked in the following environment:\n"
    f" - Primary working directory: {CC_FOLDER}\n"
    " - Is a git repository: false\n"
    " - Platform: win32\n"
    "</system-reminder>"
)


def _cc_messages(*blocks: str) -> list[Message]:
    texts = blocks or (CC_REMINDER, "fix the failing test")
    return [
        Message(
            role="user",
            content=[ContentBlockText(type="text", text=text) for text in texts],
        )
    ]


class _ExplodingBlock:
    """A message block whose text must never be read."""

    @property
    def text(self) -> str:
        raise AssertionError("the first user message was read")


_EXPLODING_MESSAGES = [{"role": "user", "content": [_ExplodingBlock()]}]


class TestTheFirstUserMessageIsReadWhereClaudeCodeSendsIt:
    def test_a_claude_code_shaped_request_names_its_folder(self) -> None:
        origin = _resolve(
            {"x-claude-code-session-id": SESSION},
            harness="claude",
            system=CC_SYSTEM,
            messages=_cc_messages(),
        )

        assert origin.project_dir == CC_FOLDER
        assert origin.origin_source == (
            "session_id=header.x-claude-code-session-id;project_dir=prompt.env-block"
        )
        # The system blocks alone -- all 7.87.0 read -- state nothing.
        assert _resolve({}, harness="claude", system=CC_SYSTEM).project_dir is None

    def test_string_content_and_dict_shaped_messages_are_read_too(self) -> None:
        as_string = [Message(role="user", content=CC_REMINDER + "\nhello")]
        as_dicts = [
            {"role": "user", "content": [{"type": "text", "text": CC_REMINDER}]}
        ]

        assert _resolve({}, harness="claude", messages=as_string).project_dir == (
            CC_FOLDER
        )
        assert _resolve({}, harness="claude", messages=as_dicts).project_dir == (
            CC_FOLDER
        )

    def test_the_system_blocks_still_win(self) -> None:
        origin = _resolve(
            {}, harness="claude", system=ENV_BLOCK, messages=_cc_messages()
        )

        assert origin.project_dir == FOLDER
        assert origin.origin_source == "project_dir=prompt.env-block"

    def test_the_message_is_not_read_when_the_system_blocks_answered(self) -> None:
        origin = _resolve(
            {}, harness="claude", system=ENV_BLOCK, messages=_EXPLODING_MESSAGES
        )

        assert origin.project_dir == FOLDER

    @pytest.mark.parametrize(
        "harness", ["claude_agent_sdk", "opencode", "codex", "unknown", None]
    )
    def test_a_harness_other_than_claude_never_reads_the_message(
        self, harness: str | None
    ) -> None:
        origin = _resolve(
            {"x-claude-code-session-id": SESSION},
            harness=harness,
            system=CC_SYSTEM,
            messages=_EXPLODING_MESSAGES,
        )

        assert origin.project_dir is None

    def test_folder_capture_off_never_reads_the_message(self) -> None:
        origin = _resolve(
            {"x-claude-code-session-id": SESSION},
            harness="claude",
            messages=_EXPLODING_MESSAGES,
            capture_folder=False,
        )

        assert origin.session_id == SESSION
        assert origin.project_dir is None

    def test_a_line_beyond_64_kib_of_the_message_is_ignored(self) -> None:
        past_in_one_string = [
            Message(
                role="user",
                content="p" * (PROMPT_SCAN_MAX_CHARS + 10) + "\n" + CC_REMINDER,
            )
        ]
        past_across_blocks = _cc_messages("a" * 40_000, "b" * 40_000, CC_REMINDER)
        # Where a current subagent's block sits (anchor at 41,971 of the
        # stored prompt): inside the cap, so found.
        inside = _cc_messages("c" * 41_971, CC_REMINDER)

        assert (
            _resolve({}, harness="claude", messages=past_in_one_string).project_dir
            is None
        )
        assert (
            _resolve({}, harness="claude", messages=past_across_blocks).project_dir
            is None
        )
        assert _resolve({}, harness="claude", messages=inside).project_dir == CC_FOLDER
        # Only the capped head is ever joined.
        assert len(first_user_message_text(past_across_blocks)) == PROMPT_SCAN_MAX_CHARS

    def test_a_request_without_the_line_stays_null(self) -> None:
        messages = _cc_messages(
            "<system-reminder>\nAs you answer, use this context.\n</system-reminder>",
            "fix the failing test",
        )

        origin = _resolve(
            {"x-claude-code-session-id": SESSION},
            harness="claude",
            system=CC_SYSTEM,
            messages=messages,
        )

        assert origin.project_dir is None
        assert origin.origin_source == "session_id=header.x-claude-code-session-id"
        assert _resolve({}, harness="claude", messages=[]) is EMPTY_ORIGIN
        assert _resolve({}, harness="claude", messages=None) is EMPTY_ORIGIN

    def test_a_message_of_another_role_is_not_read(self) -> None:
        cases = {
            "an assistant turn before it": [
                Message(role="assistant", content=CC_REMINDER),
                Message(role="user", content="hello"),
            ],
            "a system-role turn before it": [
                Message(role="system", content=CC_REMINDER),
                Message(role="user", content="hello"),
            ],
            "an assistant turn after it": [
                Message(role="user", content="hello"),
                Message(role="assistant", content=CC_REMINDER),
            ],
            "a later user turn": [
                Message(role="user", content="hello"),
                Message(role="assistant", content="ok"),
                Message(role="user", content=CC_REMINDER),
            ],
            "a tool result in the first user turn": [
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            type="tool_result",
                            tool_use_id="toolu_1",
                            content=CC_REMINDER,
                        )
                    ],
                )
            ],
        }

        for name, messages in cases.items():
            origin = _resolve({}, harness="claude", system=CC_SYSTEM, messages=messages)
            assert origin.project_dir is None, name

    def test_the_first_user_message_after_an_assistant_turn_is_read(self) -> None:
        messages = [
            Message(role="assistant", content="ready"),
            Message(role="user", content=CC_REMINDER),
        ]

        assert _resolve({}, harness="claude", messages=messages).project_dir == (
            CC_FOLDER
        )

    def test_the_anchor_must_start_a_line_in_the_message_too(self) -> None:
        messages = _cc_messages("Set the Primary working directory: C:\\fake please")

        assert _resolve({}, harness="claude", messages=messages).project_dir is None


class TestNormalisation:
    def test_ids_are_capped(self) -> None:
        origin = _resolve({"x-claude-code-session-id": "s" * 500}, harness="claude")

        assert origin.session_id == "s" * MAX_ID_CHARS

    def test_a_folder_is_capped_and_loses_its_trailing_separator(self) -> None:
        origin = _resolve(
            {"x-mcc-cwd": "C:\\" + "f" * 900 + "\\"}, harness="claude_agent_sdk"
        )

        assert origin.project_dir is not None
        assert len(origin.project_dir) == MAX_PROJECT_DIR_CHARS

    def test_a_drive_root_and_a_posix_root_survive(self) -> None:
        assert _resolve({"x-mcc-cwd": "C:\\"}, harness="x").project_dir == "C:\\"
        assert _resolve({"x-mcc-cwd": "/"}, harness="x").project_dir == "/"

    def test_control_characters_reject_the_value(self) -> None:
        origin = _resolve(
            {"x-claude-code-session-id": "abc\x00def", "x-mcc-cwd": "C:\\a\x1bb"},
            harness="claude",
        )

        assert origin is EMPTY_ORIGIN


class TestDisplayForms:
    def test_last_two_segments_and_a_stable_six_hex_hash(self) -> None:
        digest = hashlib.sha256(FOLDER.encode("utf-8")).hexdigest()[:6]

        assert project_short(FOLDER) == f"Projects\\demo · #{digest}"
        assert project_short(FOLDER) == project_short(FOLDER)

    def test_two_roots_with_the_same_tail_stay_apart(self) -> None:
        one = project_short("C:\\Users\\devuser\\work\\app")
        two = project_short("D:\\archive\\work\\app")

        assert one is not None and two is not None
        assert one.split(" · ")[0] == two.split(" · ")[0] == "work\\app"
        assert one != two

    def test_posix_paths_keep_their_separator(self) -> None:
        short = project_short("/home/devuser/projects/demo")

        assert short is not None and short.startswith("projects/demo · #")

    def test_a_one_segment_path(self) -> None:
        short = project_short("C:")

        assert short is not None and short.startswith("C: · #")

    def test_absent_values_have_no_display_form(self) -> None:
        assert project_short(None) is None
        assert project_short("") is None
        assert session_short(None) is None
        assert session_short(SESSION) == "0f3c2a1b"


class TestProvenance:
    def test_the_column_round_trips(self) -> None:
        text = format_origin_source(
            [
                ("session_id", "header", "x-claude-code-session-id"),
                ("project_dir", "prompt", "env-block"),
            ]
        )

        assert text == (
            "session_id=header.x-claude-code-session-id;project_dir=prompt.env-block"
        )
        assert parse_origin_source(text) == {
            "session_id": ("header", "x-claude-code-session-id"),
            "project_dir": ("prompt", "env-block"),
        }

    def test_merging_keeps_the_other_fields_and_their_order(self) -> None:
        merged = merge_origin_source(
            "session_id=header.x-claude-code-session-id",
            "project_dir",
            "prompt",
            BACKFILL_SIGNAL,
        )

        assert merged == (
            "session_id=header.x-claude-code-session-id;"
            "project_dir=prompt.stored-prompt"
        )
        assert merge_origin_source(None, "project_dir", "prompt", BACKFILL_SIGNAL) == (
            "project_dir=prompt.stored-prompt"
        )

    def test_the_sentences_the_modal_shows(self) -> None:
        provenance = origin_provenance(
            "session_id=header.x-claude-code-session-id;"
            "agent_id=header.x-claude-code-agent-id;"
            "project_dir=prompt.env-block"
        )

        assert provenance["session_id"]["sentence"] == (
            "stated by the x-claude-code-session-id header"
        )
        assert provenance["project_dir"]["sentence"] == (
            "read from the prompt's environment block"
        )
        assert (
            origin_provenance("project_dir=prompt.stored-prompt")["project_dir"][
                "sentence"
            ]
            == "read later from the stored prompt's environment block (backfill)"
        )
        assert origin_provenance(
            "session_id=metadata.metadata.user_id;project_dir=launcher.x-mcc-cwd"
        ) == {
            "session_id": {
                "source": "metadata",
                "signal": "metadata.user_id",
                "sentence": "read from the request's metadata.user_id",
            },
            "project_dir": {
                "source": "launcher",
                "signal": "x-mcc-cwd",
                "sentence": "stated by MCC's launcher (x-mcc-cwd header)",
            },
        }

    def test_garbage_is_tolerated(self) -> None:
        assert parse_origin_source(None) == {}
        assert parse_origin_source("nonsense;=x;a=") == {}
        assert origin_provenance(42) == {}


def test_the_backfill_helper_reads_the_head_of_a_stored_prompt() -> None:
    stored = "You are Claude Code.\n" + ENV_BLOCK + "\nuser: hello"

    assert project_dir_from_prompt(stored) == FOLDER
    assert project_dir_from_prompt(None) is None
    assert project_dir_from_prompt("x" * PROMPT_SCAN_MAX_CHARS + ENV_BLOCK) is None


class TestNothingNewLeavesTheMachine:
    def test_the_headers_column_allow_list_is_unchanged(self) -> None:
        """Origin values go to their own columns; the ``headers`` JSON keeps
        exactly the eight values it always kept."""

        assert (
            frozenset(
                {
                    "user-agent",
                    "x-app",
                    "anthropic-version",
                    "anthropic-beta",
                    "accept",
                    "content-type",
                    "x-mcc-harness",
                    "x-mcc-harness-version",
                }
            )
            == ALLOWED_HEADERS
        )
        assert not (ORIGIN_HEADERS & ALLOWED_HEADERS)

    def test_the_mirrored_upstream_set_is_unchanged(self) -> None:
        assert client_fingerprint._MIRRORED == (
            "user-agent",
            "x-app",
            "anthropic-version",
            "anthropic-beta",
        )
        assert not (ORIGIN_HEADERS & set(client_fingerprint._MIRRORED))

    def test_the_fingerprint_is_blind_to_the_origin_headers(self) -> None:
        with_origin = client_fingerprint.fingerprint_from_headers(
            {
                "user-agent": "claude-cli/2.1.0 (external, cli)",
                "x-claude-code-session-id": SESSION,
                "x-claude-code-agent-id": AGENT,
                "x-mcc-cwd": FOLDER,
            }
        )
        without = client_fingerprint.fingerprint_from_headers(
            {"user-agent": "claude-cli/2.1.0 (external, cli)"}
        )

        assert with_origin == without

    def test_only_declared_header_values_are_copied(self) -> None:
        inputs = origin_inputs(
            {
                "Authorization": "Bearer secret",
                "x-api-key": "sk-secret",
                "X-Claude-Code-Session-Id": SESSION,
            },
            harness="claude",
        )

        assert inputs.headers == (("x-claude-code-session-id", SESSION),)
