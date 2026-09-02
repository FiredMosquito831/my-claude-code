"""Auth strategy presenting a Claude subscription OAuth token.

The upstream header set, and where each value comes from. Every offset is a
byte offset into Claude Code 2.1.258's own binary (``claude.exe``, a
218,507,936-byte Bun single-file bundle).

``Authorization`` -> ``Bearer <access_token>``
    Offset 187124317: ``uX()`` returns ``{...jm(), Authorization: null,
    ...!TC() && {"X-Api-Key": null}}`` and sets ``Authorization`` from the
    wire credential when one is an OAuth token. The bundled Anthropic TS SDK
    (offset 181109007) spells the two modes ``apiKeyAuth -> X-Api-Key`` and
    ``bearerAuth -> Authorization: Bearer``. Claude Code's own in-bundle curl
    example (offset 96384881) uses ``Authorization: Bearer
    $CLAUDE_CODE_OAUTH_TOKEN``. ``opencode-anthropic-auth@0.0.13`` arrives at
    the same shape independently.

``x-api-key`` -> never sent
    Same sources: Claude Code nulls it whenever an OAuth credential is in
    play. Before 6.36.0 MCC put the subscription token here and never set
    ``Authorization`` at all, which is the most likely reason this provider
    had served zero successful requests in its whole life. Sending both would
    be a fingerprint neither Claude Code nor any Anthropic SDK produces.

``anthropic-version`` -> the client's value, else ``2023-06-01``
    Offset 181109007, the SDK's ``buildHeaders``.

``anthropic-beta`` -> MCC's floor, unioned with the client's own list
    Registry at offset 183023150, plus 120,969 measured inbound requests. The
    merge rule and the closed allow-list live in :mod:`.betas`.

``user-agent`` -> the client's value, else ``claude-cli/2.1.258 (external, cli)``
    Shape from offset 183634762. Mirroring is not asserting: the value is the
    one the client itself sent. The fallback exists only for a request that
    carried no user-agent, and the gate refuses those anyway.

``x-app`` -> the client's value, else ``cli``
    Offset 187124946: ``"x-app": St() ? "cli-bg" : "cli"``. ``cli-bg`` is a
    real value on real traffic and MCC used to flatten it to ``cli``.

``anthropic-dangerous-direct-browser-access`` -> ``true``
    Observed in the unlisted header names of real inbound Claude Code traffic.

Everything mirrored is a claim the client already made. Nothing here is
manufactured, and the entrypoint gate in :mod:`.provider` is what keeps that
true: a request that did not come from Anthropic's own client never gets far
enough to be given this header set.
"""

import asyncio
import time

from loguru import logger

from my_claude_code.core.client_fingerprint import (
    ClientFingerprint,
    current_fingerprint,
)
from my_claude_code.providers.anthropic_messages import ANTHROPIC_API_VERSION

from .betas import merge_betas
from .constants import CLAUDE_CODE_APP, CLAUDE_CODE_USER_AGENT
from .credentials import OAuthTokens, load_tokens, refresh_tokens


class AnthropicOAuthAuth:
    """Present the subscription credential, refreshing it as it ages.

    The token is resolved per request rather than captured once, because a
    long-lived server outlives any single access token.

    Refresh is *proactive and non-blocking*: once the credential enters its
    leeway window a background refresh starts and the request in hand goes out
    on the token it already has, which is still valid. Only a credential that
    has genuinely expired makes a request wait. Single-flight is enforced in
    :mod:`.credentials` per credential *file*, so two provider instances --
    which a hot reload routinely produces -- perform one exchange between them
    rather than two that clobber each other.
    """

    def __init__(self, tokens: OAuthTokens | None = None) -> None:
        self._tokens = tokens
        self._lock = asyncio.Lock()
        self._background: asyncio.Task[None] | None = None

    @property
    def tokens(self) -> OAuthTokens | None:
        return self._tokens

    async def current_tokens(self) -> OAuthTokens:
        async with self._lock:
            if self._tokens is None:
                self._tokens = load_tokens()
            tokens = self._tokens
        if not tokens.needs_refresh() or not tokens.has_refresh_token:
            return tokens
        if tokens.is_expired():
            return await self._refresh_now(tokens)
        self._start_background_refresh(tokens)
        return tokens

    async def force_refresh(self) -> OAuthTokens | None:
        """Refresh once, on demand. Returns ``None`` when it cannot.

        Called by the 401 path: an access token can be rejected before its
        stated expiry -- revoked, rotated elsewhere, or simply wrong -- and
        trying once is the only way to tell that apart from a dead credential.
        """
        async with self._lock:
            if self._tokens is None:
                self._tokens = load_tokens()
            tokens = self._tokens
        if not tokens.has_refresh_token:
            return None
        try:
            return await self._refresh_now(tokens)
        except Exception as error:
            logger.warning("Claude subscription refresh after a 401 failed: {}", error)
            return None

    async def _refresh_now(self, tokens: OAuthTokens) -> OAuthTokens:
        refreshed = await refresh_tokens(tokens)
        async with self._lock:
            self._tokens = refreshed
        return refreshed

    def _start_background_refresh(self, tokens: OAuthTokens) -> None:
        """Refresh out of band, at most one task at a time per instance."""
        if self._background is not None and not self._background.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - always inside a loop here
            return

        async def _run() -> None:
            try:
                await self._refresh_now(tokens)
            except Exception as error:
                # Nothing waits on this. A failure here only becomes fatal
                # once the token actually expires, and that path blocks and
                # reports for itself.
                logger.warning(
                    "Background refresh of the Claude subscription credential "
                    "failed; the current token stays in use until it expires: "
                    "{}",
                    error,
                )

        self._background = loop.create_task(_run())

    async def headers(self) -> dict[str, str]:
        tokens = await self.current_tokens()
        return self._headers_for(tokens, current_fingerprint())

    def _headers_for(
        self, tokens: OAuthTokens, client: ClientFingerprint
    ) -> dict[str, str]:
        """Build the upstream header set; see this module's table."""
        betas, dropped = merge_betas(client.anthropic_beta)
        if dropped:
            logger.info(
                "Dropped {} anthropic-beta value(s) the allow-list does not know: {}",
                len(dropped),
                ",".join(dropped),
            )
        return {
            # An OAuth token goes in Authorization: Bearer, and x-api-key is
            # not merely unnecessary here -- it is wrong. See the table above.
            "Authorization": f"Bearer {tokens.access_token}",
            "anthropic-version": client.anthropic_version or ANTHROPIC_API_VERSION,
            "anthropic-beta": betas,
            "anthropic-dangerous-direct-browser-access": "true",
            "x-app": client.x_app or CLAUDE_CODE_APP,
            "user-agent": client.user_agent or CLAUDE_CODE_USER_AGENT,
        }

    def label(self) -> str | None:
        """A log label for this credential: the plan and where it came from.

        Never the token, never an email. ``subscription_type`` and ``source``
        are both already on the credential and neither is a secret, while the
        masked reference string every OAuth request-log row used to carry
        ("fcc-...auth") said nothing about anything.
        """
        tokens = self._tokens
        if tokens is None:
            return None
        plan = tokens.subscription_type or "unknown-plan"
        return f"{plan} · {tokens.source}"

    def seconds_until_expiry(self, *, now: float | None = None) -> float | None:
        tokens = self._tokens
        if tokens is None:
            return None
        return tokens.seconds_remaining(now=now if now is not None else time.time())
