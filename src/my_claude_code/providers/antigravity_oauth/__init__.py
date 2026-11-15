"""Google Antigravity OAuth provider (P1 scaffold).

**Direction, stated once.** MCC is the *client* of Antigravity's own upstream
(``cloudcode-pa``) here. MCC's existing ``agy`` integration — ``agy`` talking
*to* MCC through ``/v1beta`` — is the opposite direction, lives in
``config/harnesses.py`` and ``config/desktop_apps.py``, and is untouched.

**What P1 is.** The shape-agnostic scaffold: the provider id MCC recognises,
the dated CLI identity profile it presents, the ChatGPT-shaped account/token
store, the ``v1internal`` door transport, the ``fetchAvailableModels`` listing
parser, and the refusal classifier — all proven against a loopback fake
upstream with **no network and no credential**.

**What P1 is not.** There is no way to sign in yet. The provider does nothing
until an account is signed in, and with no credential it is never listed by
discovery, so every frozen baseline stays byte-identical.
"""

from .constants import (
    CLIENT_IDENTITY_DEFAULT,
    DISPLAY_NAME,
    PROVIDER_ID,
    REDIRECT_URI,
)
from .credentials import (
    AccountRecord,
    AntigravityOAuthError,
    AntigravityRefreshError,
    AntigravityRefreshRejected,
    AntigravityRefreshUnavailable,
    OAuthTokens,
    classify_refresh_failure,
    load_accounts,
    managed_store_path,
    refresh_tokens,
)
from .identity import (
    CLI_PROFILE,
    HEADERS_ABSENT,
    HEADERS_PRESENT,
    IDE_PROFILE,
    IdentityProfile,
    request_headers,
)
from .listing import (
    ModelRosterEntry,
    RosterParse,
    model_infos,
    output_cap_for,
    parse_available_models,
)
from .provider import (
    AntigravityOAuthProvider,
    AntigravityUnavailableError,
    UsageRecord,
    derived_session_id,
    usage_from_metadata,
)
from .refusals import (
    AntigravityRefusal,
    classify_antigravity_failure,
    refusal_from_error_response,
    refusal_from_load_code_assist,
)

__all__ = [
    "CLIENT_IDENTITY_DEFAULT",
    "CLI_PROFILE",
    "DISPLAY_NAME",
    "HEADERS_ABSENT",
    "HEADERS_PRESENT",
    "IDE_PROFILE",
    "PROVIDER_ID",
    "REDIRECT_URI",
    "AccountRecord",
    "AntigravityOAuthError",
    "AntigravityOAuthProvider",
    "AntigravityRefreshError",
    "AntigravityRefreshRejected",
    "AntigravityRefreshUnavailable",
    "AntigravityRefusal",
    "AntigravityUnavailableError",
    "IdentityProfile",
    "ModelRosterEntry",
    "OAuthTokens",
    "RosterParse",
    "UsageRecord",
    "classify_antigravity_failure",
    "classify_refresh_failure",
    "derived_session_id",
    "load_accounts",
    "managed_store_path",
    "model_infos",
    "output_cap_for",
    "parse_available_models",
    "refresh_tokens",
    "refusal_from_error_response",
    "refusal_from_load_code_assist",
    "request_headers",
    "usage_from_metadata",
]
