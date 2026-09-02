"""Detect which Anthropic client produced a request.

Claude Code stamps an attribution line at the head of the system prompt, in the
request body rather than in an HTTP header:

    x-anthropic-billing-header: cc_version=2.1.258; cc_entrypoint=cli;

Claude Code 2.1.258 builds that line at binary offset 183645094 and recognises
it at offset 187232942. The value after ``cc_entrypoint=`` is
``process.env.CLAUDE_CODE_ENTRYPOINT``.

The marker travels with the body, so it is the one client signal a proxy can
neither forge for traffic it did not receive nor strip from traffic it did.
That property is what this module exists for, and it is also the limit of what
it proves: **the marker is a good-faith attribution field, not an
authenticator.** Anything that sets ``CLAUDE_CODE_ENTRYPOINT`` and reuses
Claude Code's system-prompt shape can claim any value here. MCC forwards a
claim the client made; it does not verify it, and this file should never be
described as if it did.

Who is admitted, and why: the subscription credential may serve requests from
Anthropic's own clients only -- the Claude Code CLI and the Claude Agent SDK,
which drives the Claude Code binary. Those are the entrypoints below. Every
other harness routed through MCC -- OpenCode, Cline, Crush, a bare API call --
is refused by :mod:`.provider` and sent to a provider with its own credential.

Measured on the operator's own traffic over 14 days (120,969 requests carrying
a captured user-agent), three entrypoints appear live: ``cli`` (30,391),
``sdk-py`` (77,064) and ``sdk-cli`` (291). Before 6.36.0 the gate admitted
``cli`` alone, so 64% of this user's Claude Code traffic -- all of it genuinely
from Anthropic's own SDK -- was refused.
"""

import re
from typing import Any

from my_claude_code.core.anthropic.models import MessagesRequest

BILLING_HEADER_MARKER = "x-anthropic-billing-header:"

_ENTRYPOINT_RE = re.compile(r"cc_entrypoint\s*=\s*([A-Za-z0-9._-]+)")
_VERSION_RE = re.compile(r"cc_version\s*=\s*([A-Za-z0-9._-]+)")

# The terminal CLI.
CLI_ENTRYPOINT = "cli"

# A closed set rather than a prefix match. ``sdk-*`` as a wildcard would admit
# whatever a future -- or a hostile -- client decided to call itself, and the
# point of this gate is that its membership is a decision somebody made on
# purpose. Adding an entrypoint here is a policy change, not a typo fix.
#
#   cli      the terminal client
#   cli-bg   the same client running a background task (offset 187124946)
#   sdk-cli  the Agent SDK driving the Claude Code binary
#   sdk-py   the Python Agent SDK
#   sdk-ts   the TypeScript Agent SDK
CLAUDE_CODE_ENTRYPOINTS: frozenset[str] = frozenset(
    {CLI_ENTRYPOINT, "cli-bg", "sdk-cli", "sdk-py", "sdk-ts"}
)


def system_prompt_text(request: MessagesRequest) -> str:
    """Return the request's system prompt as flat text.

    ``system`` is either a string or a list of content blocks, and the marker
    sits in the first block, so both shapes have to be flattened before the
    line can be read.
    """
    system: Any = request.system
    if system is None:
        return ""
    if isinstance(system, str):
        return system

    parts: list[str] = []
    for block in system:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def detect_entrypoint(request: MessagesRequest) -> str | None:
    """Return the reported ``cc_entrypoint``, or ``None`` when unmarked."""
    match = _ENTRYPOINT_RE.search(system_prompt_text(request))
    return match.group(1) if match else None


def detect_client_version(request: MessagesRequest) -> str | None:
    """Return the reported ``cc_version``, or ``None`` when unmarked."""
    match = _VERSION_RE.search(system_prompt_text(request))
    return match.group(1) if match else None


def is_claude_code_client(request: MessagesRequest) -> bool:
    """Whether this request came from Claude Code or the Claude Agent SDK."""
    return detect_entrypoint(request) in CLAUDE_CODE_ENTRYPOINTS


def is_claude_code_cli(request: MessagesRequest) -> bool:
    """Whether this request came from the Claude Code terminal CLI itself.

    Narrower than :func:`is_claude_code_client` and no longer what the gate
    asks: kept because "was this the terminal, or the SDK?" is a real question
    and the answer is worth a name.
    """
    return detect_entrypoint(request) in {CLI_ENTRYPOINT, "cli-bg"}
