"""Claude subscription OAuth provider.

Using a Claude Free/Pro/Max OAuth credential from a third-party product is
against Anthropic's published terms. See ``docs/ANTHROPIC-SUBSCRIPTION.md``.
"""

from .auth import AnthropicOAuthAuth
from .betas import merge_betas, split_betas
from .cli import anthropic_oauth_login_command
from .constants import (
    ANTHROPIC_OAUTH_BETA_ALLOWLIST,
    ANTHROPIC_OAUTH_BETA_FLOOR,
    CLAUDE_CODE_USER_AGENT,
)
from .credentials import (
    AnthropicOAuthRefreshError,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
    claude_credentials_path,
    detect_available_sources,
    load_claude_code_tokens,
    load_managed_tokens,
    load_tokens,
    managed_store_path,
    refresh_tokens,
    store_tokens,
)
from .entrypoint import (
    CLAUDE_CODE_ENTRYPOINTS,
    CLI_ENTRYPOINT,
    detect_client_version,
    detect_entrypoint,
    is_claude_code_cli,
    is_claude_code_client,
)
from .provider import PROVIDER_NAME, REQUIRE_SETTING, AnthropicOAuthProvider
from .rate_limit_headers import (
    UNIFIED_RATE_LIMIT_HEADERS,
    UNIFIED_STATUS_VALUES,
    capture_rate_limit_headers,
)

__all__ = [
    "ANTHROPIC_OAUTH_BETA_ALLOWLIST",
    "ANTHROPIC_OAUTH_BETA_FLOOR",
    "CLAUDE_CODE_ENTRYPOINTS",
    "CLAUDE_CODE_USER_AGENT",
    "CLI_ENTRYPOINT",
    "PROVIDER_NAME",
    "REQUIRE_SETTING",
    "UNIFIED_RATE_LIMIT_HEADERS",
    "UNIFIED_STATUS_VALUES",
    "AnthropicOAuthAuth",
    "AnthropicOAuthProvider",
    "AnthropicOAuthRefreshError",
    "AnthropicOAuthUnavailableError",
    "OAuthTokens",
    "anthropic_oauth_login_command",
    "capture_rate_limit_headers",
    "claude_credentials_path",
    "detect_available_sources",
    "detect_client_version",
    "detect_entrypoint",
    "is_claude_code_cli",
    "is_claude_code_client",
    "load_claude_code_tokens",
    "load_managed_tokens",
    "load_tokens",
    "managed_store_path",
    "merge_betas",
    "refresh_tokens",
    "split_betas",
    "store_tokens",
]
