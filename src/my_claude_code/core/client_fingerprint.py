"""What the inbound client said about itself, available to a provider.

One provider -- the Claude subscription OAuth provider -- has to reproduce the
inbound request's own identity headers upstream rather than invent its own,
because the credential it presents is only allowed to serve requests that
genuinely came from Anthropic's own client. Mirroring is the honest shape:
MCC forwards a claim the client already made, and never manufactures one.

The API boundary installs one immutable record for the life of a request and
providers read it. Unlike :mod:`my_claude_code.core.credential_attribution`,
nothing writes back, so an immutable value in a ``ContextVar`` is enough: a
child task inherits a copy of the context and reads the same record.

Only header values that are already allow-listed for the request log reach
this record. It is a description of the client, never of its credential.

The same module answers the *other* question a self-description can answer:
**which coding agent sent this?** That classifier lives here rather than in a
module of its own because it reads exactly the headers this one already owns,
and a second parser of the same user-agent string would drift from the first
the day Claude Code changes its shape.
"""

import re
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass

# Claude Code's user-agent shape, offset 183634762 of the 2.1.258 binary:
# ``claude-cli/<version> (external, <entrypoint>[, agent-sdk/x][, ...])``.
_USER_AGENT_RE = re.compile(
    r"^claude-cli/(?P<version>[0-9A-Za-z.\-]+)\s+\(external,\s*(?P<entrypoint>[A-Za-z0-9_-]+)"
)

# Bound on any single mirrored value. A real Claude Code user-agent plus a full
# anthropic-beta list is well under this; anything longer is a client MCC has
# no reason to reproduce byte-for-byte.
MAX_MIRRORED_CHARS = 1_024

# Exactly the four headers this proxy reproduces upstream. The
# ``x-claude-code-*`` correlation ids a real client also sends are deliberately
# NOT here: MCC is not the session they identify, and mirroring them would put
# a client-side correlation id into a durable store for no diagnostic gain.
_MIRRORED = (
    "user-agent",
    "x-app",
    "anthropic-version",
    "anthropic-beta",
)


@dataclass(frozen=True, slots=True)
class ClientFingerprint:
    """The inbound client's self-description, as it arrived."""

    user_agent: str | None = None
    x_app: str | None = None
    anthropic_version: str | None = None
    anthropic_beta: str | None = None

    @property
    def ua_entrypoint(self) -> str | None:
        """The entrypoint the user-agent reports, or ``None``.

        A corroborant, never an authenticator: it is a header, so anything can
        send it. The request body's ``cc_entrypoint`` marker is what admits a
        request; this is what makes a disagreement between the two visible.
        """
        if not self.user_agent:
            return None
        match = _USER_AGENT_RE.match(self.user_agent)
        return match.group("entrypoint") if match else None

    @property
    def ua_version(self) -> str | None:
        """The Claude Code version the user-agent reports, or ``None``."""
        if not self.user_agent:
            return None
        match = _USER_AGENT_RE.match(self.user_agent)
        return match.group("version") if match else None

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.user_agent,
                self.x_app,
                self.anthropic_version,
                self.anthropic_beta,
            )
        )


EMPTY_FINGERPRINT = ClientFingerprint()

_CURRENT: ContextVar[ClientFingerprint] = ContextVar(
    "fcc_client_fingerprint", default=EMPTY_FINGERPRINT
)


def fingerprint_from_headers(
    headers: Mapping[str, str] | None,
) -> ClientFingerprint:
    """Build the record from one request's raw inbound headers."""
    if not headers:
        return EMPTY_FINGERPRINT
    seen: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name in _MIRRORED and isinstance(raw_value, str) and raw_value.strip():
            seen[name] = raw_value.strip()[:MAX_MIRRORED_CHARS]
    if not seen:
        return EMPTY_FINGERPRINT
    return ClientFingerprint(
        user_agent=seen.get("user-agent"),
        x_app=seen.get("x-app"),
        anthropic_version=seen.get("anthropic-version"),
        anthropic_beta=seen.get("anthropic-beta"),
    )


def install_fingerprint(headers: Mapping[str, str] | None) -> ClientFingerprint:
    """Record the inbound client's headers for the current request."""
    fingerprint = fingerprint_from_headers(headers)
    _CURRENT.set(fingerprint)
    return fingerprint


def current_fingerprint() -> ClientFingerprint:
    """The inbound client in flight right now; empty outside a request."""
    return _CURRENT.get()


# ----------------------------------------------------------- harness id --
#
# Two signals, explicit wins.
#
# (a) ``x-mcc-harness``: a header MCC's own launchers put in the provider
#     document (or environment) they generate for a CLI that supports custom
#     request headers. It is the only signal that is not a guess.
# (b) The user-agent, matched against the table below.
#
# Neither is an authenticator -- both are headers, and anything can send them.
# ``harness`` is a diagnostic label on a request row, never an authorisation
# input, which is why an unverifiable claim is acceptable here and would not be
# in ``ua_entrypoint``.

#: The header our launchers set. Lowercase: header names are compared lowered.
HARNESS_HEADER = "x-mcc-harness"

#: Optional companion carrying the harness's own version string. MCC does not
#: emit it today -- it never probes a harness binary for its version, and a
#: version we do not have is worse than none -- but a launcher that learns one
#: can send it and the detail pane will show it.
HARNESS_VERSION_HEADER = "x-mcc-harness-version"

#: Values of :attr:`HarnessAttribution.source`.
HARNESS_SOURCE_HEADER = "header"
HARNESS_SOURCE_USER_AGENT = "user-agent"
HARNESS_SOURCE_NONE = "none"

#: The bucket for a request whose client said nothing recognisable.
UNKNOWN_HARNESS = "unknown"

#: Bound on a harness id or version taken from a header. Registry ids are far
#: shorter; anything longer is a client with no reason to be believed verbatim.
MAX_HARNESS_CHARS = 64

# Ids this module can emit that are NOT harness registry ids. They exist
# because the registry is "coding agents MCC can launch" and this column is
# "who sent this request" -- a curl one-liner is a real answer to the second
# question and can never be an entry in the first.
#
# ``claude_agent_sdk`` is the largest of them on a real log: the Claude Agent
# SDK identifies itself as ``claude-cli/<v> (external, sdk-py, agent-sdk/<v>)``
# and is a different program from the Claude Code CLI, so folding it into
# ``claude`` would hide the single busiest client on the box.
NON_REGISTRY_HARNESS_IDS: frozenset[str] = frozenset(
    {
        "claude_agent_sdk",
        "anthropic_sdk",
        "openai_sdk",
        "google_sdk",
        "ai_sdk",
        "script",
        UNKNOWN_HARNESS,
    }
)

#: Display names for the ids above. Registry ids take their label from
#: ``config/harnesses.py`` instead; this is only the fallback set.
NON_REGISTRY_HARNESS_LABELS: dict[str, str] = {
    "claude_agent_sdk": "Claude Agent SDK",
    "anthropic_sdk": "Anthropic SDK",
    "openai_sdk": "OpenAI SDK",
    "google_sdk": "Google GenAI SDK",
    "ai_sdk": "Vercel AI SDK",
    "script": "Script or curl",
    UNKNOWN_HARNESS: "Unknown",
}

# User-agent table, in match order. Each entry is (pattern, harness id); the
# optional ``version`` group is what the detail pane shows beside the name.
#
# Every pattern below except the generic SDK and script ones was taken from a
# real row of the live request log or from the installed CLI's own bundle --
# guessing a user-agent produces a rule that matches nothing and is never
# noticed, because "no rows" and "wrong rule" look identical in a breakdown.
_HARNESS_UA_TABLE: tuple[tuple[re.Pattern[str], str], ...] = (
    # ``opencode2/<v>`` must precede ``opencode/<v>``: the OpenCode 2 binary's
    # own string starts with the OpenCode 1 prefix.
    (re.compile(r"^opencode2/(?P<version>\S+)"), "opencode2"),
    (re.compile(r"^opencode/(?P<version>\S+)"), "opencode"),
    (re.compile(r"^Charm-Crush/v?(?P<version>[^\s)]+)"), "crush"),
    (re.compile(r"^factory-cli/(?P<version>\S+)"), "droid"),
    (re.compile(r"^codex[_-](?:exec|cli|cli_rs|mcp)/(?P<version>\S+)"), "codex"),
    (re.compile(r"^codex/(?P<version>\S+)"), "codex"),
    (re.compile(r"^GeminiCLI(?:-[a-z]+)?/(?P<version>[0-9][^/\s]*)"), "gemini_cli"),
    (re.compile(r"^QwenCode/(?P<version>\S+)"), "qwen_code"),
    (re.compile(r"^[Kk]ilo[-_ ]?[Cc]ode/(?P<version>\S+)"), "kilo"),
    (re.compile(r"^[Cc]line/(?P<version>\S+)"), "cline_cli"),
    (re.compile(r"^commandcode/(?P<version>\S+)"), "commandcode_cli"),
    (re.compile(r"^[Kk]imi[-_]?[Cc]ode/(?P<version>\S+)"), "kimi_code"),
    (re.compile(r"^goose/(?P<version>\S+)"), "goose"),
    (re.compile(r"^[Aa]ider(?:-chat)?/(?P<version>\S+)"), "aider"),
    (re.compile(r"^[Aa]ntigravity/(?P<version>\S+)"), "antigravity"),
    (re.compile(r"^pi(?:-coding-agent)?/(?P<version>[0-9]\S*)"), "pi"),
    # Bare Vercel AI SDK: OpenCode's OpenAI-compatible path and several other
    # TypeScript agents share it, so it identifies a library, not an agent.
    (re.compile(r"^ai-sdk/"), "ai_sdk"),
    (
        re.compile(r"^(?:Async)?Anthropic[/ ](?:Python|JS|TypeScript|Node)"),
        "anthropic_sdk",
    ),
    (re.compile(r"^anthropic-sdk-"), "anthropic_sdk"),
    (re.compile(r"^OpenAI/(?:Python|NodeJS|JS)"), "openai_sdk"),
    (re.compile(r"^openai-python/"), "openai_sdk"),
    (re.compile(r"^(?:google-genai|google-api-|google-generativeai)"), "google_sdk"),
    (
        re.compile(
            r"^(?:curl|Wget|Python-urllib|python-requests|httpx|HTTPie"
            r"|PostmanRuntime|node-fetch|axios|undici|Go-http-client|okhttp)"
            r"[/ ]?(?P<version>[0-9]\S*)?"
        ),
        "script",
    ),
)

# The Claude Code family, checked before the table because two other agents
# borrow its user-agent verbatim (see ``CLAUDE_CLI_COLLISION`` below).
_CLAUDE_CLI_RE = re.compile(
    r"^claude-cli/(?P<version>[0-9A-Za-z.\-]+)(?:\s+\((?P<detail>[^)]*)\))?"
)
_AGENT_SDK_RE = re.compile(r"agent-sdk/(?P<version>[0-9A-Za-z.\-]+)")

#: Agents observed sending Claude Code's user-agent rather than their own.
#:
#: Qwen Code emits ``claude-cli/<its own version> (external, cli)`` on its
#: Anthropic path (``@qwen-code/qwen-code`` 0.15.11, ``cli.js``) and Pi does the
#: same (``@earendil-works/pi-ai``, ``anthropic-messages.js``). There is no
#: reliable way to tell them apart from the user-agent alone -- a version-range
#: rule would misfile the day Claude Code ships a 0.x or Qwen Code a 2.x -- so
#: fingerprinting answers ``claude`` for all three and the explicit
#: ``x-mcc-harness`` header is what separates them going forward.
CLAUDE_CLI_COLLISION: tuple[str, ...] = ("qwen_code", "pi")


@dataclass(frozen=True, slots=True)
class HarnessAttribution:
    """Which coding agent sent a request, and how confidently we know."""

    #: Registry harness id, or one of :data:`NON_REGISTRY_HARNESS_IDS`.
    harness: str = UNKNOWN_HARNESS
    #: The agent's own version, when it published one.
    version: str | None = None
    #: ``header`` (explicit and exact), ``user-agent`` (inferred), or ``none``.
    source: str = HARNESS_SOURCE_NONE

    @property
    def is_explicit(self) -> bool:
        """Whether a launcher stated this, rather than us inferring it."""
        return self.source == HARNESS_SOURCE_HEADER


UNKNOWN_ATTRIBUTION = HarnessAttribution()


def _clean_harness_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:MAX_HARNESS_CHARS]
    return text or None


def _harness_id_from_header(value: object) -> str | None:
    """Sanitise a claimed harness id to the shape an id may take."""
    text = _clean_harness_value(value)
    if text is None:
        return None
    slug = "".join(
        char if (char.isalnum() or char in "._-") else "_" for char in text.lower()
    )
    return slug or None


def classify_user_agent(user_agent: str | None) -> HarnessAttribution:
    """Classify one user-agent string into a harness id and version."""
    text = _clean_harness_value(user_agent)
    if text is None:
        return UNKNOWN_ATTRIBUTION
    claude = _CLAUDE_CLI_RE.match(text)
    if claude is not None:
        agent_sdk = _AGENT_SDK_RE.search(claude.group("detail") or "")
        if agent_sdk is not None:
            return HarnessAttribution(
                harness="claude_agent_sdk",
                version=agent_sdk.group("version"),
                source=HARNESS_SOURCE_USER_AGENT,
            )
        return HarnessAttribution(
            harness="claude",
            version=claude.group("version"),
            source=HARNESS_SOURCE_USER_AGENT,
        )
    for pattern, harness in _HARNESS_UA_TABLE:
        match = pattern.match(text)
        if match is None:
            continue
        return HarnessAttribution(
            harness=harness,
            version=match.groupdict().get("version"),
            source=HARNESS_SOURCE_USER_AGENT,
        )
    return UNKNOWN_ATTRIBUTION


def harness_from_headers(headers: Mapping[str, str] | None) -> HarnessAttribution:
    """Attribute one request to a harness. Explicit header beats user-agent.

    Accepts either a live request's raw headers or the allow-listed ``headers``
    dict already stored on a request row, which is what makes the historical
    backfill and the live path share one classifier instead of two.
    """
    if not headers:
        return UNKNOWN_ATTRIBUTION
    lowered = {
        str(name).strip().lower(): value
        for name, value in headers.items()
        if isinstance(name, str)
    }
    claimed = _harness_id_from_header(lowered.get(HARNESS_HEADER))
    if claimed is not None:
        return HarnessAttribution(
            harness=claimed,
            version=_clean_harness_value(lowered.get(HARNESS_VERSION_HEADER)),
            source=HARNESS_SOURCE_HEADER,
        )
    return classify_user_agent(lowered.get("user-agent"))


__all__ = [
    "CLAUDE_CLI_COLLISION",
    "EMPTY_FINGERPRINT",
    "HARNESS_HEADER",
    "HARNESS_SOURCE_HEADER",
    "HARNESS_SOURCE_NONE",
    "HARNESS_SOURCE_USER_AGENT",
    "HARNESS_VERSION_HEADER",
    "MAX_HARNESS_CHARS",
    "MAX_MIRRORED_CHARS",
    "NON_REGISTRY_HARNESS_IDS",
    "NON_REGISTRY_HARNESS_LABELS",
    "UNKNOWN_ATTRIBUTION",
    "UNKNOWN_HARNESS",
    "ClientFingerprint",
    "HarnessAttribution",
    "classify_user_agent",
    "current_fingerprint",
    "fingerprint_from_headers",
    "harness_from_headers",
    "install_fingerprint",
]
