"""Wire constants for the Google Antigravity OAuth surface.

Direction, stated once because the same product name is already in this tree in
the opposite direction: **MCC is the client here.** ``config/harnesses.py`` and
``config/desktop_apps.py`` describe ``agy`` — Google's Antigravity CLI — talking
*to* MCC; this package is MCC talking *to* Antigravity's own upstream
(``cloudcode-pa``). Neither direction is aware of the other and nothing here
changes the harness integration.

Provenance
----------
Every value below is the public, documented shape of that surface as recorded
in ``specs/RESEARCH-ANTIGRAVITY-PROTOCOL.md``, ``specs/RESEARCH-ANTIGRAVITY-
IDENTITY.md`` and ``specs/IMPLEMENTATION-ANTIGRAVITY-PROVIDER.md`` (all dated
2026-11-14/15, read from ``agy.exe``'s string table and from six published
proxies). Where a value is second-hand the comment says so. Nothing here was
measured by MCC: P1 makes no upstream call of any kind.

One deliberate absence: the installed-app **OAuth client pair** is not carried
here. It is the sign-in surface's constant set (P2a) — the first code that can
spend it — and this repository's push protection refuses the literal
identifier pattern, which is the account owner's decision to enable. The
identity this release presents does not depend on it: see the section below.
The published client ids and secrets remain recorded in the spec, in full.
"""

from my_claude_code.core.version import package_version

# --- Provider identity ---------------------------------------------------

PROVIDER_ID = "antigravity_oauth"
PROVIDER_LABEL = "Antigravity"
DISPLAY_NAME = "Google Antigravity (OAuth, Caution)"

#: The env var (and Settings attribute) that can carry a raw access token.
CREDENTIAL_ENV = "ANTIGRAVITY_OAUTH_ACCESS_TOKEN"
CREDENTIAL_ATTR = "antigravity_oauth_access_token"

# --- Hosts, as declared ordered data -------------------------------------
#
# The references disagree on purpose and none of them is simply wrong: the host
# that accepts a call depends on the date and the account. MCC therefore keeps
# two ordered lists and remembers which host answered (a P2a/P3 learned fact);
# it never hard-codes one host for both jobs.
#
#   generation  daily -> prod   (every chat reference; 9router, antigravity-
#                                claude-proxy, the plugin)
#   discovery   prod -> daily   (9router: "the daily host rejects these auth/
#                                onboarding calls"; antigravity-claude-proxy
#                                orders loadCodeAssist prod first for the same
#                                reason)
PROD_HOST = "https://cloudcode-pa.googleapis.com"
DAILY_HOST = "https://daily-cloudcode-pa.googleapis.com"

#: Chat traffic: daily first, prod second.
CHAT_HOST_ORDER: tuple[str, ...] = (DAILY_HOST, PROD_HOST)
#: ``loadCodeAssist`` / ``onboardUser``: prod first, daily second.
DISCOVERY_HOST_ORDER: tuple[str, ...] = (PROD_HOST, DAILY_HOST)

#: The host a fresh install names on a card. It is the first rung of the chat
#: order, which is where every inference call goes.
DEFAULT_BASE_URL = DAILY_HOST

# --- Endpoint paths ------------------------------------------------------

LOAD_CODE_ASSIST_PATH = "/v1internal:loadCodeAssist"
ONBOARD_USER_PATH = "/v1internal:onboardUser"
FETCH_AVAILABLE_MODELS_PATH = "/v1internal:fetchAvailableModels"
STREAM_GENERATE_CONTENT_PATH = "/v1internal:streamGenerateContent?alt=sse"

# --- OAuth client (the sign-in surface's constant set, P2a) ---------------
#
# **The installed-app client pair is not carried in P1.** No request this
# release makes spends it: there is no sign-in, and the refresh path is
# exercised only against a loopback fake, so the identifiers land with the
# surface that can actually use them (P2a). The spec quotes both public ids
# and both public secrets in full (specs/RESEARCH-ANTIGRAVITY-PROTOCOL.md §6.1,
# read from ``agy.exe``), together with the fact that **B ``884354919052-…``**
# is the official client this provider defaults to in words and **A
# ``1071006060591-…``** is the legacy Code Assist client Google began refusing
# on 2026-11-14 — a documented constant, never a default.
#
# It is left out here for one concrete reason, recorded rather than hidden: the
# repository's GitHub push protection refuses a commit containing the literal
# identifier pattern, and enabling an exception for it is the account owner's
# decision, not an implementer's. The identity every P1 request presents — the
# user-agent grammar, the metadata shape, the redirect below — is unaffected.
OAUTH_CLIENT_ID = ""
OAUTH_CLIENT_SECRET = ""

#: The only ``oauth-callback`` literal in ``agy.exe`` (E1s:32783).
REDIRECT_URI = "https://antigravity.google/oauth-callback"

#: The ID-token/metadata document the official client may present instead of a
#: numeric client id (CONFIRMED string in the binary; whether the flow uses it
#: is UNVERIFIED). Declared so P2a does not have to rediscover it.
CLIENT_METADATA_DOCUMENT_URL = "https://antigravity.google/oauth/client-metadata.json"

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: specs/RESEARCH-ANTIGRAVITY-PROTOCOL.md §6.2: ``aicode`` (E1s:32725),
#: ``cloud-platform``, ``cclog`` (E1s:32715), ``experimentsandconfigs``,
#: ``userinfo.profile``. ``userinfo.email`` is not a literal in the binary but
#: appears in every published scope set (CLIProxyAPI constants.go:12-19) and is
#: included here for the identity call P2a makes.
OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/aicode",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
)

#: specs/RESEARCH-ANTIGRAVITY-PROTOCOL.md §6.1b: the CLI stores its token
#: keyring-first. MCC never reads that store (binding Q1.13b/Q14); this names
#: the file only so a comment or a card can say what is NOT read.
CLI_TOKEN_STORAGE_NOTE = (
    "The official CLI stores its token in the OS keyring first, with an "
    "unverified file fallback under ~/.gemini/antigravity-cli/. MCC never "
    "reads it and never writes it."
)

#: Confirmed in code: the real client uses Go's default User-Agent for OAuth
#: token refresh (CLIProxyAPI antigravity_executor_auth.go:167). NQ2 makes it
#: the UA of BOTH token calls, because the whole profile claims to be the Go
#: client.
OAUTH_TOKEN_USER_AGENT = "Go-http-client/2.0"

# --- The dated CLI identity profile --------------------------------------
#
# The UA grammar, assembled at runtime in the binary and captured live by
# two peers:
#
#   antigravity/cli/<version> (aidev_client; os_type=<goos>; arch=<goarch>
#                              [; cl=<changelist>][; auth_method=consumer])
#
# ``cl`` is sent only when the version→changelist pair is known: a stale
# changelist beside a newer version is a worse fingerprint than none.
IDENTITY_ID = "antigravity-cli"
UA_PRODUCT = "antigravity"
UA_FLAVOUR_CLI = "cli"
UA_FLAVOUR_IDE = "ide"
UA_CLIENT_TAG = "aidev_client"
AUTH_METHOD_CONSUMER = "consumer"
UA_GRAMMAR = (
    "antigravity/cli/<version> (aidev_client; os_type=<goos>; "
    "arch=<goarch>[; cl=<changelist>]; auth_method=consumer)"
)

#: The dated constant. P1 has no credential and therefore no live source; the
#: GitHub CLI release feed (6 h TTL, floor 1.4.4) arrives in P2a/P3. The value
#: is the peer-captured ``agy 1.4.4`` (OmniRoute, live on Windows x64), which
#: is also the floor NQ1 names.
CLI_VERSION = "1.4.4"
#: The ``cl`` captured beside 1.4.4 (OmniRoute antigravityHeaders.ts:129-137).
CLI_CHANGELIST = "1003595037"
#: The date the profile above was verified by its own sources.
PROFILE_VERIFIED_ON = "2026-11-14"

#: NQ1: the reported floor and the pinned fallback are the same release in P1.
VERSION_FLOOR = "1.4.4"
VERSION_FALLBACK = "1.4.4"

#: The IDE profile is a declared second object, off by default (NQ6). Its
#: numbers are peer-declared and UNVERIFIED — ``platform`` in particular: the
#: spec gives the range 1..5, not the darwin/arm64 value. It is never measured
#: by MCC and never the default.
IDE_VERSION = "2.5.5"
IDE_METADATA_IDE_TYPE = 9
IDE_METADATA_PLATFORM = 1
IDE_METADATA_PLUGIN_TYPE = 2

# --- Client identity switch ----------------------------------------------

CLIENT_IDENTITY_ANTIGRAVITY = "antigravity"
CLIENT_IDENTITY_MCC = "mcc"
CLIENT_IDENTITY_DEFAULT = CLIENT_IDENTITY_ANTIGRAVITY
CLIENT_IDENTITY_OPTIONS: tuple[str, ...] = (
    CLIENT_IDENTITY_ANTIGRAVITY,
    CLIENT_IDENTITY_MCC,
)

CLIENT_PROFILE_CLI = "cli"
CLIENT_PROFILE_IDE = "ide"
CLIENT_PROFILE_DEFAULT = CLIENT_PROFILE_CLI
CLIENT_PROFILE_OPTIONS: tuple[str, ...] = (CLIENT_PROFILE_CLI, CLIENT_PROFILE_IDE)

CLIENT_VERSION_DEFAULT = ""  # empty = the dated constant
CLIENT_VERSION_ATTR = "antigravity_client_version"
CLIENT_IDENTITY_ATTR = "antigravity_client_identity"
CLIENT_PROFILE_ATTR = "antigravity_client_profile"


def mcc_user_agent() -> str:
    """The honest identity: MCC's own name, for ``ANTIGRAVITY_CLIENT_IDENTITY=mcc``.

    Expected to be refused — that is the point of the switch, and the card says
    so. It proves what the gate is and it is the only way an operator can say
    "stop presenting Google's client".
    """

    return f"my-claude-code/{package_version()}"


# --- Body metadata shapes -------------------------------------------------

#: ``loadCodeAssist`` on the CLI profile: the bare string form, 38 bytes,
#: captured 200. ``pluginType: "GEMINI"`` is the legacy shape that now draws
#: UNSUPPORTED_CLIENT and is never sent.
METADATA_SHAPE_STRING = "string"
METADATA_SHAPE_NUMERIC = "numeric"
LOAD_CODE_ASSIST_METADATA: dict[str, object] = {"ideType": "ANTIGRAVITY"}

#: ``onboardUser`` uses the snake_case spelling (CLIProxyAPI auth.go:85-91).
ONBOARD_USER_TIER_FALLBACK = "free-tier"
ONBOARD_USER_IDE_NAME = "antigravity"

# --- The door ------------------------------------------------------------

REQUEST_TYPE_AGENT = "agent"
ENVELOPE_USER_AGENT = "antigravity"
REQUEST_ID_PREFIX = "agent-"
#: The CLI's own synthetic project when ``loadCodeAssist`` returns none
#: (PROTOCOL §5.3; CONFIRMED in the CLI's own log).
DEFAULT_PROJECT = "default-cli-project"

#: Refusal identifiers. Classification reads these STRUCTURED values and never
#: a phrase out of a message (WORKING-NOTES IX.7).
REASON_UNSUPPORTED_CLIENT = "UNSUPPORTED_CLIENT"
REASON_TOS_VIOLATION = "TOS_VIOLATION"
REASON_SUBSCRIPTION_REQUIRED = "SUBSCRIPTION_REQUIRED"
STATUS_PERMISSION_DENIED = "PERMISSION_DENIED"
GOOGLE_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
APPEAL_URL_KEY = "appeal_url"
APPEAL_URL_LINK_TEXT_KEY = "appeal_url_link_text"

# --- Usage ---------------------------------------------------------------

#: The six ``usageMetadata`` counters, named so the mapping is a declared table
#: rather than a set of literals. specs/IMPLEMENTATION §3.I: the counters are
#: DISJOINT; ``output = candidates + thoughts`` and
#: ``input + cache_read == promptTokenCount``.
PROMPT_TOKENS_KEY = "promptTokenCount"
CANDIDATES_TOKENS_KEY = "candidatesTokenCount"
THOUGHTS_TOKENS_KEY = "thoughtsTokenCount"
CACHED_CONTENT_TOKENS_KEY = "cachedContentTokenCount"
TOOL_USE_PROMPT_TOKENS_KEY = "toolUsePromptTokenCount"
TOTAL_TOKENS_KEY = "totalTokenCount"

# --- Refresh policy ------------------------------------------------------

#: 9router's measured lead (``refreshLeadMs`` 300000) and the IDE's own cadence
#: of a refresh every ~55 minutes.
REFRESH_LEEWAY_SECONDS = 300

# --- Listing -------------------------------------------------------------

#: ``fetchAvailableModels`` publishes ``models`` as an OBJECT keyed by model id
#: (specs/IMPLEMENTATION §3.J). A model id ending in ``-disabled`` is a
#: placeholder: hidden from the listing, still valid if a client names it.
DISABLED_MODEL_SUFFIX = "-disabled"
#: Old Gemini families are hidden from the listing (the peer filter) while
#: staying routable. The minimum family this build lists is 3.5.
LISTED_GEMINI_MIN_MINOR = 5
LISTED_GEMINI_MAJOR = 3

#: The S5 seed, used only when the gateway rung answers nothing: the slugs the
#: binary bakes in (PROTOCOL §7.2), dated 2026-11-14. Never an existence source
#: and never preferred over the roster.
SEED_MODEL_IDS: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-pro",
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
)
SEED_VERIFIED_ON = "2026-11-14"
