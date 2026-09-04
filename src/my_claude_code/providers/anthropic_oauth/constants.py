"""Wire constants for the Claude Code OAuth surface.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` BEFORE CHANGING ANYTHING HERE.

Anthropic's published position is that OAuth credentials from Claude Free, Pro
and Max plans are for Claude Code and Claude.ai only, and that third-party
products may not route requests through them. This provider does that anyway,
at the operator's explicit instruction and on their own account. The constants
below are the documented shape of that surface, not an endorsement of using it.

Provenance
----------
Every value here was re-derived for 6.43.0 from **Claude Code 2.1.260**::

    ~/.local/share/claude/versions/2.1.260
    PE32+ Bun single-file bundle, 217,771,680 bytes
    VERSION:"2.1.260"  BUILD_TIME:"2026-09-03T19:41:35Z"
    GIT_SHA:"e51f681183f733dbe9a81bf35921c786ee26dbc6"

Byte offsets below are offsets into *that* file. Releases before 6.43.0 cited
offsets into 2.1.258 (a 218,507,936-byte bundle); those are not valid here, and
at least one of them described a request shape no Claude Code login path makes
(see :data:`TOKEN_ENDPOINT_HEADERS_NOTE`). Where Claude Code and any other
source disagree, Claude Code wins: it is the client this surface is built for.
"""

# --- OAuth client -------------------------------------------------------

# Offset 180721780, the production endpoint table, verbatim:
#
#   _={BASE_API_URL:"https://api.anthropic.com",
#      CONSOLE_AUTHORIZE_URL:"https://platform.claude.com/oauth/authorize",
#      CLAUDE_AI_AUTHORIZE_URL:"https://claude.com/cai/oauth/authorize",
#      TOKEN_URL:"https://platform.claude.com/v1/oauth/token",
#      MANUAL_REDIRECT_URL:"https://platform.claude.com/oauth/code/callback",
#      CLIENT_ID:"9d1c250a-e61b-44d9-88ed-5944d1962f5e", ...}
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

# The pre-2.1.258 hosts. Kept as a fallback rather than deleted: it is only
# ever tried on a 301/308/404 from the current host, so it costs nothing, and a
# host migration should not become a forced re-login. It is a guess with a
# comment, not a documented endpoint -- 2.1.260's table (offset 180721780)
# contains neither host.
LEGACY_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
LEGACY_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
LEGACY_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"

# Offset 180721391, the scope table, verbatim:
#
#   o_="user:inference", H1="user:profile", s="org:create_api_key",
#   Ic="oauth-2025-04-20",
#   r=[s,H1],
#   p8=[H1,o_,"user:sessions:claude_code","user:mcp_servers","user:file_upload"],
#   mPn=J([...r,...p8])
#
# ``mPn`` is the **authorize** scope set; ``p8`` is the **refresh** default.
# ``user:inference`` is the one that enables inference at all, so a login
# missing it produces a credential that authenticates and then refuses to
# answer.
OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

# Offset 182768825, ``$U`` (the refresh builder), verbatim:
#
#   let O={grant_type:"refresh_token",refresh_token:e,client_id:o??Vt().CLIENT_ID,
#          scope:(Array.isArray(n)&&n.length?n:p8).join(" ")};
#
# Claude Code sends ``scope`` on every refresh and its default is ``p8`` -- the
# authorize set **minus** ``org:create_api_key``, which is authorize-only. MCC
# omitted this field entirely before 6.43.0.
OAUTH_REFRESH_SCOPES = (
    "user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)

INFERENCE_SCOPE = "user:inference"
PKCE_METHOD = "S256"

# --- Token endpoint -----------------------------------------------------

# Offset 182768825 (``$U``, refresh) and offset 182768091 (``EAn``, exchange).
# Both post to ``TOKEN_URL`` with, verbatim::
#
#   {headers:{"Content-Type":"application/json"},timeout:30000}
#
# That is the complete set of headers Claude Code sets *explicitly*. It is not
# the complete set that reaches the wire: its HTTP client fills in a
# ``User-Agent`` of its own, and the consented live test for 6.43.0 proved that
# the edge in front of the token endpoint cares about that value --
#
#   request 1: Content-Type only, on-the-wire UA ``Python-urllib/3.13``
#              -> HTTP 403, 17-byte non-JSON body (an edge block; the OAuth
#                 handler was never reached)
#   request 2: same refresh token, a plausible UA
#              -> HTTP 429 {"error":{"type":"rate_limit_error",...}}
#                 (a first-class answer from the OAuth API)
#
# So MCC sends a real ``User-Agent``: the same Claude Code identity it already
# presents on ``/v1/messages`` (:data:`CLAUDE_CODE_USER_AGENT`), which the
# entrypoint gate in :mod:`.entrypoint` is what keeps honest. Not the literal
# string ``anthropic``, which no Claude Code release has ever emitted and which
# releases before 6.43.0 sent here. ``anthropic-beta`` is **not** sent: neither
# ``$U`` nor ``EAn`` carries it.
TOKEN_ENDPOINT_HEADERS_NOTE = (
    "Claude Code sets only Content-Type explicitly on the token endpoint; its "
    "HTTP client supplies a User-Agent. Sending none produces a 403 at the "
    "edge. See docs/ANTHROPIC-SUBSCRIPTION.md."
)

# --- Messages API -------------------------------------------------------

ANTHROPIC_OAUTH_DEFAULT_BASE = "https://api.anthropic.com/v1"

# Offset 182165469, the beta registry, verbatim:
#
#   function _e(e,n){return Object.freeze({name:e,header:n})}
#   var qn =_e("claude_code","claude-code-20250219"),
#       Mke=_e("oauth_auth",Ic),                 // Ic="oauth-2025-04-20"
#       Nr =_e("interleaved_thinking","interleaved-thinking-2025-05-14"), ...
#
# and offset 182339086, ``Dre`` (the per-request beta list builder):
#
#   function Dre(e){let n=[],r=Fe(e),o=r.includes("haiku"),...;
#     if(!o)n.push(qn);                            // claude-code-20250219
#     if(gt()||ks()&&!Nvt()&&Vc())n.push(Mke);     // oauth-2025-04-20  <- OAuth gate
#     ...}
#
# ``oauth-2025-04-20`` is what makes the API accept an OAuth token at all;
# ``claude-code-20250219`` selects the Claude Code request surface. Both are
# protocol selectors and both are always sent, whatever the client asked for.
ANTHROPIC_OAUTH_BETA_FLOOR: tuple[str, ...] = (
    "oauth-2025-04-20",
    "claude-code-20250219",
)

# Closed allow-list. The first block is the beta registry at offset 182165469;
# the second is what real Claude Code sessions were measured sending into MCC
# over 14 days (120,969 logged requests).
#
# A closed list rather than passthrough because a beta the account is not
# entitled to is a 400 that looks like a model problem. Names that are not on
# it are dropped and reported, never forwarded.
ANTHROPIC_OAUTH_BETA_ALLOWLIST: frozenset[str] = frozenset(
    {
        # -- registry, offset 182165469 --
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
        # -- registry entries new in 2.1.260, same offset --
        "auto-mode-classifier-2026-07-16",
        "thinking-display-updates-2026-08-18",
        # -- observed inbound from real Claude Code sessions --
        "thinking-token-count-2026-05-13",
        "redact-thinking-2026-02-12",
        "mid-conversation-system-2026-04-07",
        "advisor-tool-2026-03-01",
        "fallback-credit-2026-06-01",
        "fine-grained-tool-streaming-2025-05-14",
    }
)

# Identity headers, used only when the inbound Claude Code request did not
# carry its own. They claim the request came from Anthropic's official CLI.
# The entrypoint gate in ``entrypoint.py`` is what keeps that claim honest:
# without it, this header would be a lie.
#
# Offset 182853338, ``zR`` (the user-agent builder), verbatim:
#
#   return `claude-cli/${{...VERSION:"2.1.260"...}.VERSION} (external, ${
#          a.CLAUDE_CODE_ENTRYPOINT??"cli"}${e}${n}${o})`
#
# Offset 186629277, ``iF`` (the API client factory), verbatim:
#
#   G={"x-app": ht()?"cli-bg":"cli", "User-Agent": zR(), [RFe]: Y(), ...}
#
# ``cli-bg`` is a real value on real traffic and MCC used to flatten it to
# ``cli``.
CLAUDE_CODE_APP = "cli"
CLAUDE_CODE_VERSION = "2.1.260"
CLAUDE_CODE_USER_AGENT = f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"

# Offset 180347171, the bundled Anthropic TS SDK's ``buildHeaders``, verbatim:
#
#   let o=c([s,{Accept:"application/json","User-Agent":this.getUserAgent(),
#               "X-Stainless-Retry-Count":String(n),
#               ...e.timeout?{"X-Stainless-Timeout":...}:{}, ...St(),
#               ...this._options.dangerouslyAllowBrowser?{
#                   "anthropic-dangerous-direct-browser-access":"true"}:void 0,
#               "anthropic-version":"2023-06-01"},
#             await this.authHeaders(e), this._options.defaultHeaders, r, e.headers]);
#
# ``Accept`` is sent unconditionally and is a true statement about what MCC
# wants back, so MCC sends it too.
ANTHROPIC_OAUTH_ACCEPT = "application/json"

# Two header families Claude Code sends that MCC deliberately does NOT send.
# Both are recorded here so the next reader does not "fix" the gap.
#
# ``X-Stainless-*`` (eight headers; ``St()`` = ``ki()`` at offset 180207351,
#   plus ``X-Stainless-Retry-Count``/``-Timeout`` at offset 180347171) describe
#   the runtime that *built* the request: ``X-Stainless-Lang: js``,
#   ``X-Stainless-Runtime: node``, ``X-Stainless-Package-Version: 0.112.1``
#   (``var ne="0.112.1"``, offset 180207016), an OS and an arch. MCC is
#   Python/httpx. Emitting them would manufacture a claim about a JavaScript
#   runtime that is not running -- the one thing this provider's header table
#   forbids. They are SDK telemetry, carry no entitlement, and gate nothing:
#   the OAuth gate is ``anthropic-beta: oauth-2025-04-20`` and the credential
#   gate is ``Authorization``. MCC sends both.
#
# ``X-Claude-Code-Session-Id`` (``RFe``, offset 182250447) is a client-side
#   correlation id. Same manufacturing objection, plus the privacy one already
#   recorded in ``core/client_fingerprint.py``: MCC is not the session it
#   identifies, so mirroring it would put that id into a durable store for no
#   diagnostic gain.
CLAUDE_CODE_HEADERS_NOT_MIRRORED: tuple[str, ...] = (
    "x-stainless-lang",
    "x-stainless-package-version",
    "x-stainless-os",
    "x-stainless-arch",
    "x-stainless-runtime",
    "x-stainless-runtime-version",
    "x-stainless-retry-count",
    "x-stainless-timeout",
    "x-claude-code-session-id",
)

# --- Loopback callback --------------------------------------------------

# Offset 182767278, ``l3t`` (the authorize-URL builder), verbatim:
#
#   N.searchParams.append("redirect_uri",
#       o ? Vt().MANUAL_REDIRECT_URL : `http://localhost:${r}/callback`);
#
# The port is caller-supplied, so MCC binds an ephemeral one on 127.0.0.1.
# Note the host is spelled ``localhost``, not ``127.0.0.1``: the authorization
# server matches the redirect URI as a string.
LOOPBACK_REDIRECT_HOST = "localhost"
LOOPBACK_REDIRECT_PATH = "/callback"
LOOPBACK_BIND_HOST = "127.0.0.1"

#: How long a loopback sign-in waits for the browser to come back.
LOOPBACK_TIMEOUT_SECONDS = 300.0


def loopback_redirect_uri(port: int) -> str:
    """The ``redirect_uri`` for a loopback sign-in served on ``port``."""
    return f"http://{LOOPBACK_REDIRECT_HOST}:{port}{LOOPBACK_REDIRECT_PATH}"


# Refresh this many seconds before the token actually expires. Claude Code's
# bundled SDK uses 30 s and OpenCode uses zero; 120 s is the compromise that
# survives a slow token endpoint without refreshing a credential that has most
# of an hour left.
REFRESH_LEEWAY_SECONDS = 120
