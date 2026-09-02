"""Claude subscription OAuth provider, gated to Anthropic's own clients.

READ ``docs/ANTHROPIC-SUBSCRIPTION.md`` BEFORE USING OR CHANGING THIS.

Anthropic's published position (code.claude.com/docs/en/legal-and-compliance)
is that OAuth credentials from Claude Free, Pro and Max plans are for Claude
Code and Claude.ai only, and that third-party developers may not route requests
through them. This provider does exactly that. It exists because the operator
asked for it, for their own account, having been shown the policy.

The one thing this provider does to keep its own story straight: it refuses to
touch the subscription credential unless the request genuinely came from one of
Anthropic's own clients -- the Claude Code CLI or the Claude Agent SDK, which
drives the Claude Code binary -- proven by the ``cc_entrypoint`` marker Claude
Code stamps into the request body. Every other harness routed through MCC --
OpenCode, Cline, Crush, a bare API call -- is refused here rather than quietly
billed to the subscription.
"""

import time
from collections.abc import AsyncIterator, Mapping

from loguru import logger

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.constants import ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.client_fingerprint import current_fingerprint
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.anthropic import AnthropicProvider
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .auth import AnthropicOAuthAuth
from .credentials import OAuthTokens, load_tokens
from .entrypoint import (
    CLAUDE_CODE_ENTRYPOINTS,
    detect_entrypoint,
    is_claude_code_client,
)
from .rate_limit_headers import OBSERVER

PROVIDER_NAME = "ANTHROPIC_OAUTH"

REQUIRE_SETTING = "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE"

_REFUSAL = (
    "The Anthropic subscription provider only serves requests that come from "
    "Anthropic's own clients -- the Claude Code CLI or the Claude Agent SDK. "
    "This request reports cc_entrypoint={reported}, so it was refused rather "
    "than billed to your Claude subscription. Route it to the `anthropic` "
    f"provider (Claude Console API key) instead, or set {REQUIRE_SETTING}"
    "=false if you have read docs/ANTHROPIC-SUBSCRIPTION.md and accept what "
    "it says. Admitted entrypoints: {admitted}."
)

_ROTATION_REFUSAL = (
    "ANTHROPIC_OAUTH_ACCESS_TOKEN holds a comma-separated list. This provider "
    "takes exactly one credential: a raw access token carries no refresh "
    "token, so a list of them is not a rotation pool, it is several "
    "credentials that all expire and stay expired. Rotating subscription "
    "credentials is also the 'unusual traffic pattern' Anthropic's own policy "
    "names -- see docs/ANTHROPIC-SUBSCRIPTION.md. Configure one token, or "
    "sign in with `mcc-anthropic-oauth-login`."
)


def _auth_for(config: ProviderConfig) -> AnthropicOAuthAuth:
    """Prefer an explicitly configured token, else discover one from disk.

    A raw ``ANTHROPIC_OAUTH_ACCESS_TOKEN`` carries no refresh token, so it
    expires and stays expired -- the same trap Part IX records for
    ``CHATGPT_OAUTH_ACCESS_TOKEN``. It is deliberately left non-refreshing
    rather than taught to refresh: there is nothing to refresh it *with*.
    Discovery is the maintained path; the raw value is an escape hatch and is
    logged as such.
    """
    raw = (config.api_key or "").strip()
    if not raw or raw == ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE:
        return AnthropicOAuthAuth()
    if "," in raw:
        raise InvalidRequestError(_ROTATION_REFUSAL)
    logger.warning(
        "Using a raw ANTHROPIC_OAUTH_ACCESS_TOKEN; it cannot be refreshed and "
        "will stop working when it expires. Prefer mcc-anthropic-oauth-login."
    )
    return AnthropicOAuthAuth(OAuthTokens(access_token=raw, source="env"))


class AnthropicOAuthProvider(AnthropicProvider):
    """Stream Anthropic Messages using a Claude subscription OAuth token."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
        auth: AnthropicOAuthAuth | None = None,
        require_claude_code_cli: bool = True,
    ) -> None:
        resolved = auth if auth is not None else _auth_for(config)
        super().__init__(
            config,
            rate_limiter=rate_limiter,
            auth=resolved,
            provider_name=PROVIDER_NAME,
        )
        self._oauth = resolved
        self._require_claude_code_cli = require_claude_code_cli
        # Tool names go upstream verbatim, exactly as Claude Code sends them.
        # Releases before 6.36.0 renamed every tool to ``cc_<name>`` and undid
        # it with a blind string replace over each SSE frame. A full scan of
        # Claude Code 2.1.258 finds zero occurrences of any ``cc_`` tool name
        # (against 137 for the real ``mcp__`` prefix): the machinery was
        # OpenCode's ``mcp_`` disguise with the string swapped, and the reverse
        # pass corrupted any *content* that happened to contain the literal.
        self._messages.set_response_observer(self._observe_response_headers)
        self._messages.set_auth_retry(self._retry_after_auth_failure)

    # -- entrypoint gate ---------------------------------------------------

    def _enforce_entrypoint(self, request: MessagesRequest) -> None:
        reported = detect_entrypoint(request) or "none"
        client = current_fingerprint()
        ua_entrypoint = client.ua_entrypoint
        admitted = is_claude_code_client(request)
        # Corroborants, recorded and never used to refuse. The body marker is
        # the rule because MCC can neither forge nor strip it; the headers are
        # trivially settable, so making them a second admission test would add
        # a thing to keep in sync with an upstream nobody here controls. What
        # they are good for is making a disagreement visible.
        agreed = ua_entrypoint == reported and bool(ua_entrypoint)
        logger.info(
            "Claude subscription gate: marker={} ua_entrypoint={} x_app={} "
            "ua_version={} agreed={} admitted={} enforced={}",
            reported,
            ua_entrypoint or "none",
            client.x_app or "none",
            client.ua_version or "none",
            agreed,
            admitted,
            self._require_claude_code_cli,
        )
        if not self._require_claude_code_cli or admitted:
            return
        logger.warning(
            "Refused a non-Claude-Code request on the Claude subscription "
            "credential (cc_entrypoint={}, admitted={})",
            reported,
            sorted(CLAUDE_CODE_ENTRYPOINTS),
        )
        raise InvalidRequestError(
            _REFUSAL.format(
                reported=reported,
                admitted=", ".join(sorted(CLAUDE_CODE_ENTRYPOINTS)),
            )
        )

    # -- response headers --------------------------------------------------

    def _observe_response_headers(
        self, headers: Mapping[str, str], status_code: int
    ) -> None:
        """Record Anthropic's unified rate-limit headers, verbatim.

        The dashboard may say "you hit your 5-hour window" only because a real
        response said so. Nothing here infers a window from a status code.
        """
        OBSERVER.observe(headers, status_code=status_code, now=time.time())

    # -- 401 -> refresh once, retry once ------------------------------------

    async def _retry_after_auth_failure(self, status_code: int) -> bool:
        """Refresh the credential once, so one stale token costs one retry.

        An access token can be rejected before its stated expiry: revoked,
        rotated by the real Claude Code client, or clock-skewed. Trying a
        refresh once is what tells that apart from a dead credential. Exactly
        one retry, inside the attempt the rotating loop already budgeted, so
        this does not add a layer to the retry ladder.
        """
        if status_code != 401:
            return False
        refreshed = await self._oauth.force_refresh()
        if refreshed is None:
            return False
        logger.info(
            "Anthropic answered 401; refreshed the subscription credential "
            "and retrying the request once."
        )
        return True

    # -- analytics ---------------------------------------------------------

    @property
    def credential_label(self) -> str | None:
        """The plan and the credential's origin -- never a token, never an email.

        Every OAuth request-log row used to carry the masked *reference*
        string ("fcc-...auth"), which is the same for every account and says
        nothing. MCC never fetches the profile, so there is no email to use and
        adding a ``user:profile`` call per credential to populate a log label
        would be a new upstream request for a cosmetic gain.
        """
        label = self._oauth.label()
        if label is not None:
            return label
        try:
            tokens = load_tokens()
        except Exception:
            return None
        plan = tokens.subscription_type or "unknown-plan"
        return f"{plan} · {tokens.source}"

    # -- streaming ---------------------------------------------------------

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        # The gate runs in preflight so a refusal happens before any credential
        # is read and before the fallback chain commits to this hop.
        self._enforce_entrypoint(request)
        super().preflight_stream(request, reasoning=reasoning)

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        self._enforce_entrypoint(request)
        return super().stream_response(
            request,
            input_tokens=input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )

    # -- model listing -----------------------------------------------------

    async def list_model_ids(self) -> frozenset[str]:
        """List models with the subscription credential.

        Listing carries no conversation, so the entrypoint gate cannot apply
        and deliberately does not: discovery is what populates the model picker
        and is not a billed inference request.
        """
        return await super().list_model_ids()
