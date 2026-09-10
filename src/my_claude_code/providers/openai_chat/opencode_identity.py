"""What MCC tells OpenCode Zen and OpenCode Go about the client calling them.

Both hosts read a set of identity headers off every request, and their own
client sends five of them. Before this module MCC sent none.

**Measured on 2026-09-10, on the user's own key, four requests.** This is not
a precaution against something that might one day happen. The free tier had
already stopped answering::

    POST /zen/v1/chat/completions, Authorization only        -> 400
        {"type": "MissingSessionID", "message": "Error from provider
         (Console): OpenCode|s free tier can only be used in OpenCode"}
    the same request plus the five headers below             -> 200
    the same, with a deliberately INVALID x-opencode-client  -> 200
    the same, plus an unknown x-mcc-probe header             -> 200

(The ``|`` above stands in for an apostrophe the vendor's message carries and
this docstring cannot.) Three things follow, and each one shaped a decision
in this file.

1. The header the host acts on is ``x-opencode-session``, and it acts by
   *refusing*, not by quietly shrinking a daily allowance. Every MCC release
   before 6.69.0 could not use the OpenCode Zen free tier at all.
2. The value naming the client is **not** checked -- an invalid one is
   answered 200 -- so the ``mcc`` opt-out below keeps the header that makes
   the request work and changes only the claim about which program sent it.
3. An unrecognised header is not refused, which answers the open question
   about whether MCC could safely add one of its own. It could; it still does
   not, for the privacy reason recorded further down.

The Go endpoint's own requirement (vendor notice 2026-09-03, enforced from
2026-09-06) could **not** be verified: both Go probes were answered
``401 CreditsError``, because the subscription behind the key has run out. Go
is sent the same set under the same rule, which is what the vendor asked for,
but nothing here claims to have watched it work.

**What is sent, and why it is written down here.** The five headers and their
shapes were read out of the `opencode-ai` 1.18.30 binary installed on the
machine this was written on, without running it -- the same method
``providers/anthropic_oauth/constants.py`` uses for Claude Code, and for the
same reason: a header set guessed from a blog post is a header set that is
wrong in a way nobody notices. The block, from the shipped bundle:

    var _i = `opencode/${Ci}`, ...
    headers: {
      ...providerID.startsWith("opencode")
        ? {...projectID ? {"x-opencode-project": projectID} : {},
           "x-opencode-session": sessionID, "x-opencode-request": user.id,
           "x-opencode-client": flags.client, "User-Agent": _i}
        : {"x-session-affinity": sessionID, "X-Session-Id": sessionID, ...},
      ...parentSessionID ? {"x-parent-session-id": parentSessionID} : {},
      ...model.headers, ...pluginHeaders }

with ``Ci = "1.18.30"``, ``flags.client`` defaulting to the literal ``cli``,
and the project id falling back to the literal ``global`` when the working
directory is not a git repository -- which is what the OpenCode install on
that machine actually stored.

**Two headers this module deliberately does not send.**
``x-parent-session-id`` is emitted only when the client is continuing a forked
session; MCC never forks one, so sending it would manufacture a relationship
that does not exist -- the objection ``core/client_fingerprint.py`` already
records. And the openai SDK's own ``x-stainless-*`` family stays exactly as
the SDK sends it: those headers are true statements about the HTTP client that
built the request, the rule this project follows forbids *manufacturing* a
false claim rather than obliging us to hide a true one, and the host's check
is a lookup of named headers, which an extra header cannot fail. The honest
consequence, stated rather than glossed: MCC's requests remain *distinguishable*
from OpenCode's by that SDK telemetry. What is faithful is the five headers the
limiter actually names.

**The opt-out.** ``OPENCODE_CLIENT_IDENTITY=mcc`` swaps the two headers that
name a program for MCC's own name and version and the project id for ``mcc``.
The three functional headers are unchanged: they are what the vendor asked
for, and what buys prompt-cache stickiness, and neither depends on which
program is claimed.
"""

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from loguru import logger

from my_claude_code.config.constants import (
    OPENCODE_CLIENT_IDENTITY_DEFAULT,
    OPENCODE_CLIENT_VERSION_FALLBACK,
)
from my_claude_code.config.settings import (
    configured_opencode_client_identity,
    configured_opencode_client_version,
)
from my_claude_code.core.version import package_version

from .client_identity import ClientIdentity

#: Header carrying the project the call belongs to.
OPENCODE_PROJECT_HEADER = "x-opencode-project"
#: Header carrying the conversation, stable across its turns.
OPENCODE_SESSION_HEADER = "x-opencode-session"
#: Header carrying this one call.
OPENCODE_REQUEST_HEADER = "x-opencode-request"
#: Header naming which front end of the client is calling.
OPENCODE_CLIENT_HEADER = "x-opencode-client"
#: The user-agent both branches of the client's own header block send.
USER_AGENT_HEADER = "User-Agent"

#: The five, in the order the client emits them.
OPENCODE_HEADER_ORDER: tuple[str, ...] = (
    OPENCODE_PROJECT_HEADER,
    OPENCODE_SESSION_HEADER,
    OPENCODE_REQUEST_HEADER,
    OPENCODE_CLIENT_HEADER,
    USER_AGENT_HEADER,
)

#: The two whose values name a program rather than a call. Only these change
#: when the operator opts out, and only these are worth showing in a log row.
OPENCODE_IDENTITY_CLAIM_HEADERS: tuple[str, ...] = (
    USER_AGENT_HEADER,
    OPENCODE_CLIENT_HEADER,
)

#: The shipped default of the client's own ``OPENCODE_CLIENT`` variable.
OPENCODE_CLIENT_VALUE = "cli"

#: What the client stores for a working directory that is not a git repository.
OPENCODE_PROJECT_VALUE = "global"

#: What MCC calls itself when the operator asks for the truthful identity.
MCC_CLIENT_VALUE = "mcc"

#: How long a version read off this machine is trusted before it is re-read.
#: An hour, matching the model-discovery refresh: an `opencode upgrade` in
#: between costs one hour of a stale but real version string, and re-reading a
#: small JSON file more often than that buys nothing.
VERSION_CACHE_SECONDS = 3600.0

#: Where the version last came from: ``installed``, ``pinned`` or ``operator``.
VERSION_SOURCE_INSTALLED = "installed"
VERSION_SOURCE_PINNED = "pinned"
VERSION_SOURCE_OPERATOR = "operator"

_VERSION_CACHE: tuple[float, str, str] | None = None


def _npm_package_roots() -> tuple[Path, ...]:
    """Directories that may hold a global ``node_modules`` tree.

    No subprocess, for the reason the Codex catalogue reader states: ``npm
    prefix -g`` would be authoritative and would also be an execution, and
    nothing here may run anything.
    """
    roots: list[Path] = []
    prefix = os.environ.get("NPM_CONFIG_PREFIX", "").strip()
    if prefix:
        roots.extend((Path(prefix) / "node_modules", Path(prefix) / "lib"))
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules")
    home = Path.home()
    roots.extend(
        (
            home / "AppData" / "Roaming" / "npm" / "node_modules",
            home / ".npm-global" / "lib" / "node_modules",
            home / ".local" / "lib" / "node_modules",
            Path("/usr/local/lib/node_modules"),
            Path("/usr/lib/node_modules"),
            Path("/opt/homebrew/lib/node_modules"),
        )
    )
    return tuple(roots)


def _version_from_disk() -> str:
    """The ``opencode-ai`` version installed on this machine, or ``""``."""
    for root in _npm_package_roots():
        manifest = root / "opencode-ai" / "package.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        version = payload.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return ""


def opencode_client_version(*, now: float | None = None) -> tuple[str, str]:
    """The OpenCode version to name, and where it came from.

    Returned as a pair so the learned-facts row can say *which* of the three
    sources answered: a user-agent naming a release this machine really has is
    a different claim from one naming a release the constant remembers.
    """
    global _VERSION_CACHE
    pinned = configured_opencode_client_version().strip()
    if pinned:
        return pinned, VERSION_SOURCE_OPERATOR
    moment = time.monotonic() if now is None else now
    cached = _VERSION_CACHE
    if cached is not None and moment - cached[0] < VERSION_CACHE_SECONDS:
        return cached[1], cached[2]
    found = _version_from_disk()
    resolved = (
        (found, VERSION_SOURCE_INSTALLED)
        if found
        else (OPENCODE_CLIENT_VERSION_FALLBACK, VERSION_SOURCE_PINNED)
    )
    _VERSION_CACHE = (moment, resolved[0], resolved[1])
    return resolved


def reset_version_cache() -> None:
    """Forget the cached version, so the next read goes back to disk."""
    global _VERSION_CACHE
    _VERSION_CACHE = None


def opencode_identity_mode() -> str:
    """Which identity the operator asked for, normalised."""
    mode = configured_opencode_client_identity().strip().lower()
    return mode or OPENCODE_CLIENT_IDENTITY_DEFAULT


def opencode_constant_headers() -> dict[str, str]:
    """The half of the set that does not change between requests."""
    if opencode_identity_mode() == MCC_CLIENT_VALUE:
        return {
            OPENCODE_PROJECT_HEADER: MCC_CLIENT_VALUE,
            OPENCODE_CLIENT_HEADER: MCC_CLIENT_VALUE,
            USER_AGENT_HEADER: f"my-claude-code/{package_version()}",
        }
    version, _source = opencode_client_version()
    return {
        OPENCODE_PROJECT_HEADER: OPENCODE_PROJECT_VALUE,
        OPENCODE_CLIENT_HEADER: OPENCODE_CLIENT_VALUE,
        USER_AGENT_HEADER: f"opencode/{version}",
    }


#: The declaration the two OpenCode profiles carry. One instance, shared, so
#: Zen and Go cannot drift apart -- the vendor's requirement landed on Go
#: first and the free-tier limiter reads Zen, and a proxy that satisfied one
#: of them would look like it had satisfied both.
OPENCODE_CLIENT_IDENTITY = ClientIdentity(
    order=OPENCODE_HEADER_ORDER,
    constant=opencode_constant_headers,
    session_header=OPENCODE_SESSION_HEADER,
    request_header=OPENCODE_REQUEST_HEADER,
    probe_omit_header=OPENCODE_SESSION_HEADER,
)


def identity_wire_record(headers: Mapping[str, str]) -> dict[str, object]:
    """What the request log may keep about an outbound identity set.

    Names always; values only for the two headers that name a *program*, whose
    values are constants this repository publishes. The conversation and call
    ids are correlation ids and are recorded by name alone -- the rule
    ``core/client_fingerprint.py`` already states for the client's own
    correlation headers, applied to the ones MCC mints.
    """
    if not headers:
        return {}
    record: dict[str, object] = {"names": sorted(headers)}
    for name in OPENCODE_IDENTITY_CLAIM_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value:
            record[name.lower()] = value
    return record


def log_identity(provider_id: str, headers: Mapping[str, str]) -> None:
    """Say, when a provider is built, which identity it will present."""
    claim = headers.get(USER_AGENT_HEADER, "")
    logger.info(
        "{}: identifying as {} ({} headers)",
        provider_id.upper(),
        claim or "(none)",
        len(headers),
    )


__all__ = [
    "MCC_CLIENT_VALUE",
    "OPENCODE_CLIENT_HEADER",
    "OPENCODE_CLIENT_IDENTITY",
    "OPENCODE_CLIENT_VALUE",
    "OPENCODE_HEADER_ORDER",
    "OPENCODE_IDENTITY_CLAIM_HEADERS",
    "OPENCODE_PROJECT_HEADER",
    "OPENCODE_PROJECT_VALUE",
    "OPENCODE_REQUEST_HEADER",
    "OPENCODE_SESSION_HEADER",
    "USER_AGENT_HEADER",
    "VERSION_CACHE_SECONDS",
    "VERSION_SOURCE_INSTALLED",
    "VERSION_SOURCE_OPERATOR",
    "VERSION_SOURCE_PINNED",
    "identity_wire_record",
    "log_identity",
    "opencode_client_version",
    "opencode_constant_headers",
    "opencode_identity_mode",
    "reset_version_cache",
]
