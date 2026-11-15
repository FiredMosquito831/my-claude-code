"""The dated client-identity profile MCC presents to Antigravity.

The identity is a **set**, not a field. OmniRoute's controlled A/B (same
Google account, same host, 2026-08-25) measured a 403 on
``loadCodeAssist``/``onboardUser`` for a metadata shape missing
``platform``/``pluginType`` where the full numeric IDE shape succeeded, and the
CLI's bare-string shape is separately captured 200. Mixing one profile's
user-agent with the other's metadata is an identity mismatch.

So this module declares **two profiles as data**, exactly one of which is ever
used:

* :data:`CLI_PROFILE` — the default. Go-token user-agent, bare-string metadata,
  **no ``Accept`` header on the stream** (the native Go CLI asks for SSE through
  ``?alt=sse`` alone).
* :data:`IDE_PROFILE` — a declared second object, reachable only through
  ``ANTIGRAVITY_CLIENT_PROFILE=ide``, never the default and never measured by
  MCC.

``ANTIGRAVITY_CLIENT_IDENTITY=mcc`` swaps the claim for MCC's own name. It is
expected to be refused; that is what makes it honest, and it is the only way an
operator can say "stop presenting Google's client".

The user-agent carries the client version, so it is asserted as a **grammar
plus a dated constant**, never as a literal buried in a baseline (the DDG-1
rule, WORKING-NOTES IX.9).
"""

import platform
import sys
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    AUTH_METHOD_CONSUMER,
    CLI_CHANGELIST,
    CLI_VERSION,
    CLIENT_IDENTITY_DEFAULT,
    CLIENT_IDENTITY_MCC,
    CLIENT_PROFILE_DEFAULT,
    CLIENT_PROFILE_IDE,
    IDE_METADATA_IDE_TYPE,
    IDE_METADATA_PLATFORM,
    IDE_METADATA_PLUGIN_TYPE,
    IDE_VERSION,
    IDENTITY_ID,
    LOAD_CODE_ASSIST_METADATA,
    METADATA_SHAPE_NUMERIC,
    METADATA_SHAPE_STRING,
    ONBOARD_USER_IDE_NAME,
    PROFILE_VERIFIED_ON,
    UA_CLIENT_TAG,
    UA_FLAVOUR_CLI,
    UA_FLAVOUR_IDE,
    mcc_user_agent,
)

#: The headers this surface sends, by name. ``Authorization`` carries the
#: credential; the other three are the whole claim.
HEADERS_PRESENT: tuple[str, ...] = (
    "authorization",
    "content-type",
    "user-agent",
    "accept-encoding",
)

#: What must never be sent. ``Client-Metadata`` / ``X-Goog-Api-Client`` are an
#: *active* refusal — 9router records that Google's backend fingerprints them
#: and silently refuses to provision a project (9router appConstants.js:171-175,
#: applied to this arc's specs as AGY-DEEP extension (a)). ``x-goog-user-project``
#: belongs to the Aicode surface, not this one.
HEADERS_ABSENT: tuple[str, ...] = (
    "client-metadata",
    "x-goog-api-client",
    "x-goog-user-project",
    "x-goog-api-key",
    "x-stainless-*",
    "x-parent-session-id",
)

#: Profile-declared content type. The CLI's only explicit header besides the UA.
CONTENT_TYPE_JSON = "application/json"
#: What the native Go client offers Go's HTTP stack. Honouring it matters: a
#: peer measured that ``Accept-Encoding: identity`` returns a stripped model
#: list.
ACCEPT_ENCODING_GZIP = "gzip"
ACCEPT_EVENT_STREAM = "text/event-stream"


def go_os_type() -> str:
    """``windows`` / ``linux`` / ``darwin`` — Go's spelling, never Node's.

    Node's own names (``win32``/``x64``) are, as one peer puts it, "a giveaway".
    """

    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def go_arch() -> str:
    """``amd64`` / ``arm64`` — Go's spelling of the machine architecture."""

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return machine or "amd64"


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    """One complete identity claim, as declared data."""

    identity_id: str
    flavour: str
    version: str
    #: Empty when the version→changelist pair is unknown; the ``cl`` segment is
    #: then omitted rather than guessed.
    changelist: str
    os_type: str
    arch: str
    auth_method: str
    #: ``string`` (CLI) or ``numeric`` (IDE). Declared so a future profile is a
    #: second object and not a code branch.
    metadata_shape: str
    #: ``None`` means "send no ``Accept`` header at all" — the CLI's measured
    #: behaviour, and the one NQ5 makes binding.
    accept_header: str | None
    verified_on: str
    #: The metadata block per call. Only the two provisioning calls carry one;
    #: the door's identity rides the envelope.
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)

    def user_agent(self) -> str:
        """The user-agent, assembled from the grammar and this profile's data."""

        if self.flavour == UA_FLAVOUR_IDE:
            # The IDE's short form (OmniRoute antigravityHeaders.ts:125-127).
            return f"{self.identity_id.split('-')[0]}/{self.flavour}/{self.version} {self.os_type}/{self.arch}"
        segments = [
            f"{UA_CLIENT_TAG}",
            f"os_type={self.os_type}",
            f"arch={self.arch}",
        ]
        if self.changelist:
            segments.append(f"cl={self.changelist}")
        if self.auth_method:
            segments.append(f"auth_method={self.auth_method}")
        tail = "; ".join(segments)
        return (
            f"{self.identity_id.split('-')[0]}/{self.flavour}/{self.version} ({tail})"
        )

    def metadata_for(self, call: str) -> dict[str, Any]:
        """The body's metadata block for one provisioning call."""

        return dict(self.metadata.get(call, {}))


def cli_profile(
    *,
    version: str = "",
    changelist: str | None = None,
    os_type: str = "",
    arch: str = "",
) -> IdentityProfile:
    """The default CLI profile, with the dated constant as its version."""

    resolved_version = version.strip() or CLI_VERSION
    # The changelist travels with its version. A version nobody captured a
    # changelist for sends none.
    resolved_changelist = (
        changelist
        if changelist is not None
        else (CLI_CHANGELIST if resolved_version == CLI_VERSION else "")
    )
    return IdentityProfile(
        identity_id=IDENTITY_ID,
        flavour=UA_FLAVOUR_CLI,
        version=resolved_version,
        changelist=resolved_changelist,
        os_type=os_type or go_os_type(),
        arch=arch or go_arch(),
        auth_method=AUTH_METHOD_CONSUMER,
        metadata_shape=METADATA_SHAPE_STRING,
        accept_header=None,
        verified_on=PROFILE_VERIFIED_ON,
        metadata={
            "loadCodeAssist": {"metadata": dict(LOAD_CODE_ASSIST_METADATA)},
            "onboardUser": {
                "metadata": {
                    "ide_type": "ANTIGRAVITY",
                    "ide_name": ONBOARD_USER_IDE_NAME,
                }
            },
        },
    )


def ide_profile(
    *, version: str = "", os_type: str = "darwin", arch: str = "arm64"
) -> IdentityProfile:
    """The declared IDE profile. Never the default; Darwin/arm64 pinned."""

    return IdentityProfile(
        identity_id="antigravity-ide",
        flavour=UA_FLAVOUR_IDE,
        version=version.strip() or IDE_VERSION,
        changelist="",
        os_type=os_type,
        arch=arch,
        auth_method="",
        metadata_shape=METADATA_SHAPE_NUMERIC,
        accept_header=ACCEPT_EVENT_STREAM,
        verified_on=PROFILE_VERIFIED_ON,
        metadata={
            "loadCodeAssist": {
                "metadata": {
                    "ideType": IDE_METADATA_IDE_TYPE,
                    "platform": IDE_METADATA_PLATFORM,
                    "pluginType": IDE_METADATA_PLUGIN_TYPE,
                }
            },
            "onboardUser": {
                "metadata": {
                    "ideType": IDE_METADATA_IDE_TYPE,
                    "ideVersion": version.strip() or IDE_VERSION,
                }
            },
        },
    )


#: The two declared profiles. A future flavour is a third object, not a branch.
CLI_PROFILE = cli_profile()
IDE_PROFILE = ide_profile()


def profile_named(name: str) -> IdentityProfile:
    """The declared profile ``name`` chooses, defaulting to the CLI.

    An unknown value is the default rather than an error: a typo in a settings
    field must not make a provider unbuildable, and the dashboard shows the
    value in use.
    """

    return IDE_PROFILE if name.strip().lower() == CLIENT_PROFILE_IDE else CLI_PROFILE


def client_identity(value: str) -> str:
    """``antigravity`` (default) or ``mcc``; anything else is the default."""

    return (
        CLIENT_IDENTITY_MCC
        if value.strip().lower() == CLIENT_IDENTITY_MCC
        else CLIENT_IDENTITY_DEFAULT
    )


def active_profile(
    *, profile: str = "", version: str = "", identity: str = ""
) -> IdentityProfile:
    """The profile that will be presented, honouring the operator's settings.

    ``identity`` does **not** change the returned object: the mcc switch moves
    only the user-agent, which :func:`user_agent_for` and
    :func:`request_headers` apply. A profile object that silently reported the
    wrong user-agent would be the "lying object" class this file exists to
    avoid.
    """

    chosen = profile_named(profile or CLIENT_PROFILE_DEFAULT)
    if version.strip():
        return (
            cli_profile(version=version)
            if chosen.flavour == UA_FLAVOUR_CLI
            else ide_profile(version=version)
        )
    return chosen


def user_agent_for(profile: IdentityProfile, identity: str = "") -> str:
    """The user-agent a request presents under ``profile`` and ``identity``."""

    if client_identity(identity) == CLIENT_IDENTITY_MCC:
        return mcc_user_agent()
    return profile.user_agent()


def request_headers(
    profile: IdentityProfile,
    access_token: str,
    *,
    identity: str = "",
) -> dict[str, str]:
    """The declared header set, in declared order, for one call.

    Exactly :data:`HEADERS_PRESENT`, plus ``Accept`` only when the profile
    declares one. Nothing here manufactures a claim the profile did not state.
    """

    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": CONTENT_TYPE_JSON,
        "user-agent": user_agent_for(profile, identity),
        "accept-encoding": ACCEPT_ENCODING_GZIP,
    }
    if profile.accept_header is not None:
        headers["accept"] = profile.accept_header
    return headers


def header_names(headers: dict[str, str]) -> tuple[str, ...]:
    """The lower-cased names of a header mapping, sorted, for assertions."""

    return tuple(sorted(name.lower() for name in headers))


def is_forbidden_header(name: str) -> bool:
    """Whether ``name`` is one this identity must never send."""

    lowered = name.strip().lower()
    for forbidden in HEADERS_ABSENT:
        if forbidden.endswith("*") and lowered.startswith(forbidden[:-1]):
            return True
        if lowered == forbidden:
            return True
    return False


__all__ = [
    "CLI_PROFILE",
    "HEADERS_ABSENT",
    "HEADERS_PRESENT",
    "IDE_PROFILE",
    "IdentityProfile",
    "active_profile",
    "cli_profile",
    "client_identity",
    "go_arch",
    "go_os_type",
    "header_names",
    "ide_profile",
    "is_forbidden_header",
    "profile_named",
    "request_headers",
    "user_agent_for",
]
