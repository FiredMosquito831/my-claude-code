"""The harness classifier in ``core/client_fingerprint``.

Every user-agent asserted here as "real" was copied verbatim out of the live
request log (``~/.fcc/logs/requests.db``, read-only) or out of the installed
CLI's own bundle. A classifier rule written from a guess matches nothing and is
never noticed, because "no rows for this agent" and "wrong rule for this agent"
look identical in a breakdown -- so the provenance is part of the test.
"""

import pytest

from my_claude_code.config.harnesses import harness_ids
from my_claude_code.core.client_fingerprint import (
    CLAUDE_CLI_COLLISION,
    HARNESS_HEADER,
    HARNESS_SOURCE_HEADER,
    HARNESS_SOURCE_NONE,
    HARNESS_SOURCE_USER_AGENT,
    NON_REGISTRY_HARNESS_IDS,
    NON_REGISTRY_HARNESS_LABELS,
    UNKNOWN_HARNESS,
    classify_user_agent,
    fingerprint_from_headers,
    harness_from_headers,
)

# (user-agent, expected harness id, expected version). Row counts in the
# comments are from the 272,132-row live log on 2026-09-03.
REAL_USER_AGENTS: tuple[tuple[str, str, str | None], ...] = (
    # 80,982 rows -- the busiest client on the box, and NOT the Claude Code CLI.
    (
        "claude-cli/2.1.223 (external, sdk-py, agent-sdk/0.2.131)",
        "claude_agent_sdk",
        "0.2.131",
    ),
    ("claude-cli/2.1.258 (external, cli)", "claude", "2.1.258"),  # 443 rows
    ("claude-cli/2.1.241 (external, sdk-cli)", "claude", "2.1.241"),  # 291 rows
    (
        "claude-cli/2.1.247 (external, cli, workload/cron)",
        "claude",
        "2.1.247",
    ),  # 108 rows
    (
        "opencode/1.18.26 ai-sdk/provider-utils/4.0.46 runtime/bun/1.3.14",
        "opencode",
        "1.18.26",
    ),  # 4 rows
    (
        "Charm-Crush/v0.92.0 (https://charm.land/crush)",
        "crush",
        "0.92.0",
    ),  # 9 rows
    ("factory-cli/0.210.0", "droid", "0.210.0"),  # 1 row
    (
        "codex_exec/0.151.0 (Windows 10.0.26100; x86_64)"
        " WindowsTerminal (codex_exec; 0.151.0)",
        "codex",
        "0.151.0",
    ),  # 2 rows
    (
        "GeminiCLI-tui/0.49.0/anthropic/nous_portal/meituan/longcat-2.0:free"
        " (win32; x64; terminal)",
        "gemini_cli",
        "0.49.0",
    ),  # 2 rows
    ("AsyncAnthropic/Python 1.3.0", "anthropic_sdk", None),  # 1 row
    ("OpenAI/Python 2.20.0", "openai_sdk", None),  # 1 row
    ("Python-urllib/3.13", "script", "3.13"),  # 9,266 rows
    (
        "ai-sdk/openai-compatible/3.0.37 ai-sdk/provider-utils/5.0.30"
        " runtime/bun/1.3.13",
        "ai_sdk",
        None,
    ),  # 1 row
    # From the installed bundles rather than the log.
    ("opencode2/0.0.0-beta-18866", "opencode2", "0.0.0-beta-18866"),
    ("QwenCode/0.15.11 (win32; x64)", "qwen_code", "0.15.11"),
    ("commandcode/0.1.0", "commandcode_cli", "0.1.0"),
)


@pytest.mark.parametrize(("user_agent", "harness", "version"), REAL_USER_AGENTS)
def test_a_real_user_agent_classifies_to_its_agent(
    user_agent: str, harness: str, version: str | None
) -> None:
    """Each observed user-agent maps to the agent that actually sent it."""
    attribution = classify_user_agent(user_agent)
    assert attribution.harness == harness
    assert attribution.version == version
    assert attribution.source == HARNESS_SOURCE_USER_AGENT
    assert not attribution.is_explicit


@pytest.mark.parametrize(
    "user_agent",
    [
        None,
        "",
        "   ",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "something-nobody-has-ever-shipped/1",
    ],
)
def test_an_unrecognised_client_is_unknown_not_a_guess(user_agent: str | None) -> None:
    """Better an honest empty bucket than a plausible wrong label."""
    attribution = classify_user_agent(user_agent)
    assert attribution.harness == UNKNOWN_HARNESS
    assert attribution.source == HARNESS_SOURCE_NONE


def test_qwen_code_and_pi_borrow_claude_codes_user_agent() -> None:
    """The documented collision, pinned so nobody "fixes" it with a version rule.

    ``@qwen-code/qwen-code@0.15.11`` builds ``claude-cli/<its own version>
    (external, cli)`` on its Anthropic path, and ``@earendil-works/pi-ai`` sends
    ``claude-cli/<claudeCodeVersion>``. Seven rows of the live log are Qwen Code
    wearing Claude Code's name. Fingerprinting answers ``claude`` for all three
    on purpose: a version-range rule would misfile the day Claude Code ships a
    0.x or Qwen Code a 2.x. The explicit header is what separates them.
    """
    assert classify_user_agent("claude-cli/0.15.11 (external, cli)").harness == "claude"
    assert set(CLAUDE_CLI_COLLISION) == {"qwen_code", "pi"}
    assert all(harness in harness_ids() for harness in CLAUDE_CLI_COLLISION)


def test_the_explicit_header_beats_a_contradicting_user_agent() -> None:
    """A launcher that states its identity is believed over an inference."""
    attribution = harness_from_headers(
        {
            "User-Agent": "claude-cli/0.15.11 (external, cli)",
            "X-MCC-Harness": "qwen_code",
        }
    )
    assert attribution.harness == "qwen_code"
    assert attribution.source == HARNESS_SOURCE_HEADER
    assert attribution.is_explicit


def test_the_explicit_header_carries_an_optional_version() -> None:
    """MCC does not emit one today, but the parser accepts one."""
    attribution = harness_from_headers(
        {HARNESS_HEADER: "opencode", "x-mcc-harness-version": "1.18.26"}
    )
    assert (attribution.harness, attribution.version) == ("opencode", "1.18.26")


def test_a_claimed_harness_id_is_sanitised_to_id_shape() -> None:
    """The header is a claim from an arbitrary client, so it is not stored raw."""
    attribution = harness_from_headers({HARNESS_HEADER: "OpenCode 2"})
    assert attribution.harness == "opencode_2"
    assert " " not in attribution.harness


def test_an_oversized_header_value_is_truncated_not_rejected() -> None:
    """A hostile client cannot write an unbounded string into a row."""
    attribution = harness_from_headers({HARNESS_HEADER: "a" * 5_000})
    assert len(attribution.harness) == 64


def test_a_blank_header_falls_back_to_the_user_agent() -> None:
    """An empty claim is not a claim."""
    attribution = harness_from_headers(
        {HARNESS_HEADER: "   ", "user-agent": "factory-cli/0.210.0"}
    )
    assert attribution.harness == "droid"
    assert attribution.source == HARNESS_SOURCE_USER_AGENT


def test_the_stored_headers_dict_classifies_the_same_as_a_live_request() -> None:
    """One classifier serves the request path and the historical backfill.

    The backfill reads the allow-listed ``headers`` blob off a row; the request
    path reads the raw inbound mapping. If those were two parsers they would
    drift, and a re-backfill would silently relabel history.
    """
    raw = {
        "User-Agent": "opencode/1.18.26 ai-sdk/provider-utils/4.0.46",
        "Authorization": "Bearer never-stored",
        "anthropic-version": "2023-06-01",
    }
    stored = fingerprint_from_headers(raw)
    assert stored.user_agent is not None
    live = harness_from_headers(raw)
    replayed = harness_from_headers({"user-agent": stored.user_agent})
    assert live.harness == replayed.harness == "opencode"


def test_every_id_the_classifier_emits_is_declared_somewhere() -> None:
    """No id may be invented in the table without a home and a label.

    The registry answers "coding agents MCC can launch"; this column answers
    "who sent this request". A curl one-liner is a real answer to the second and
    can never be an entry in the first, so the non-registry set exists -- but it
    is closed, and every member has a display name.
    """
    from my_claude_code.core import client_fingerprint

    emitted = {harness for _pattern, harness in client_fingerprint._HARNESS_UA_TABLE}
    emitted |= {"claude", "claude_agent_sdk", UNKNOWN_HARNESS}
    registry = set(harness_ids())
    for harness in emitted:
        assert harness in registry or harness in NON_REGISTRY_HARNESS_IDS, harness
    assert set(NON_REGISTRY_HARNESS_LABELS) == set(NON_REGISTRY_HARNESS_IDS)


def test_the_oauth_mirroring_record_is_untouched_by_the_classifier() -> None:
    """The harness column must not widen what the OAuth provider mirrors.

    ``ClientFingerprint`` is reproduced upstream on the subscription path. The
    classifier reads the same headers but adds nothing to that record, so the
    OAuth wire contract cannot move because attribution changed.
    """
    fingerprint = fingerprint_from_headers(
        {
            "user-agent": "claude-cli/2.1.258 (external, cli)",
            HARNESS_HEADER: "claude",
            "x-app": "cli",
        }
    )
    assert fingerprint.user_agent == "claude-cli/2.1.258 (external, cli)"
    assert fingerprint.x_app == "cli"
    assert not hasattr(fingerprint, "harness")
