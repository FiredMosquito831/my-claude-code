"""Wire constants for the Claude Code OAuth surface.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` BEFORE CHANGING ANYTHING HERE.

Anthropic's published position is that OAuth credentials from Claude Free, Pro
and Max plans are for Claude Code and Claude.ai only, and that third-party
products may not route requests through them. This provider does that anyway,
at the operator's explicit instruction and on their own account. The constants
below are the documented shape of that surface, not an endorsement of using it.

Every value here was read out of Claude Code 2.1.258's own binary
(``claude.exe``, 218,507,936 bytes, Bun single-file bundle) at the byte offset
named beside it, or corroborated against ``opencode-anthropic-auth@0.0.13``.
Where the two disagree, Claude Code wins: it is the client this surface is
built for.
"""

# --- OAuth client -------------------------------------------------------

# Offset 181433527, verbatim from the 2.1.258 endpoint table.
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

# The pre-2.1.258 hosts. Kept as a documented fallback rather than deleted:
# nothing in-tree proves the new hosts answer for a third-party client, and a
# 404/301 from the new token endpoint is exactly the case where the old one
# still working is the difference between a refresh and a forced re-login.
LEGACY_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
LEGACY_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
LEGACY_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"

# Offset 181432509: Claude Code's full scope set. MCC used to ask for three of
# these; the live credential on this machine holds five. ``user:inference`` is
# the one that enables inference at all (the client's own gate at offset
# 183645139 reads exactly that scope), so a login missing it produces a
# credential that authenticates and then refuses to answer.
OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)
INFERENCE_SCOPE = "user:inference"
PKCE_METHOD = "S256"

# The token endpoint rejects a generic client.
TOKEN_ENDPOINT_USER_AGENT = "anthropic"

# --- Messages API -------------------------------------------------------

ANTHROPIC_OAUTH_DEFAULT_BASE = "https://api.anthropic.com/v1"

# ``oauth-2025-04-20`` is what makes the API accept an OAuth token at all
# (offset 183023150, ``Te("oauth_auth", ad)``); ``claude-code-20250219``
# selects the Claude Code request surface. Both are protocol selectors and
# both are always sent, whatever the client asked for.
#
# ``opencode-anthropic-auth@0.0.12`` dropped ``claude-code-20250219`` from its
# own floor. MCC keeps it: this provider only ever serves genuine Claude Code
# traffic, which sends it inbound anyway, so keeping it in the floor makes the
# header correct even if a future client stops sending it.
ANTHROPIC_OAUTH_BETA_FLOOR: tuple[str, ...] = (
    "oauth-2025-04-20",
    "claude-code-20250219",
)

# Closed allow-list. The first block is the beta registry decompiled from
# 2.1.258 at offset 183023150; the second is what real Claude Code sessions
# were measured sending into MCC over 14 days (120,969 logged requests).
#
# A closed list rather than passthrough because a beta the account is not
# entitled to is a 400 that looks like a model problem. Names that are not on
# it are dropped and reported, never forwarded.
ANTHROPIC_OAUTH_BETA_ALLOWLIST: frozenset[str] = frozenset(
    {
        # -- registry, offset 183023150 --
        "oauth-2025-04-20",
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "context-1m-2025-08-07",
        "context-management-2025-06-27",
        "structured-outputs-2025-12-15",
        "web-search-2025-03-05",
        "advanced-tool-use-2025-11-20",
        "tool-search-tool-2025-10-19",
        "effort-2025-11-24",
        "task-budgets-2026-03-13",
        "prompt-caching-scope-2026-01-05",
        "prompt-caching-evict-2026-05-12",
        "extended-cache-ttl-2025-04-11",
        # -- observed inbound from real Claude Code sessions --
        "thinking-token-count-2026-05-13",
        "redact-thinking-2026-02-12",
        "mid-conversation-system-2026-04-07",
        "advisor-tool-2026-03-01",
        "fallback-credit-2026-06-01",
        "fine-grained-tool-streaming-2025-05-14",
    }
)

# The refresh POST carries this one beta and nothing else (offset 180990503).
OAUTH_REFRESH_BETA = "oauth-2025-04-20"

# Identity headers, used only when the inbound Claude Code request did not
# carry its own. They claim the request came from Anthropic's official CLI.
# They are sent because the request body -- forwarded verbatim from a real
# Claude Code session -- already carries the same claim truthfully in its
# ``x-anthropic-billing-header`` line. The entrypoint gate in ``entrypoint.py``
# is what keeps that claim honest: without it, this header would be a lie.
#
# Shape from offset 183634762: ``claude-cli/<version> (external, <entrypoint>)``.
# The value MCC shipped before 6.36.0 was ``claude-cli/2.1.235.2db (external,
# cli)`` -- a ``cc_version`` build suffix spliced into a user-agent, which no
# Claude Code release has ever emitted.
CLAUDE_CODE_APP = "cli"
CLAUDE_CODE_VERSION = "2.1.258"
CLAUDE_CODE_USER_AGENT = f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"

# Refresh this many seconds before the token actually expires. Claude Code's
# bundled SDK uses 30 s (``hHe``, offset 180979163) and OpenCode uses zero;
# 120 s is the compromise that survives a slow token endpoint without
# refreshing a credential that has most of an hour left.
REFRESH_LEEWAY_SECONDS = 120
