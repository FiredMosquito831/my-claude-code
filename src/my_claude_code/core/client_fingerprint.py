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


__all__ = [
    "EMPTY_FINGERPRINT",
    "MAX_MIRRORED_CHARS",
    "ClientFingerprint",
    "current_fingerprint",
    "fingerprint_from_headers",
    "install_fingerprint",
]
