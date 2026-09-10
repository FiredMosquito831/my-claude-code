"""Asking a host, in two requests, whether it acts on who is calling.

**Measured, 2026-09-10, on the free tier of OpenCode Zen.** A request
carrying nothing but a bearer token -- what every MCC release before
6.69.0 sent -- is answered ``400 MissingSessionID``, *"OpenCode's free
tier can only be used in OpenCode"*. The same request carrying the
identity is answered 200. So the check is not dormant and it is not a
quota tier: it is a refusal, and the header it names is the conversation
id. A request carrying a deliberately **invalid** ``x-opencode-client``
was answered 200, and so was one carrying an unknown header, so neither
the client name nor an unrecognised header is what the host acts on.
That measurement is why the leg below omits a header rather than
corrupting one.

The passive half of this question is answered for free in
:mod:`my_claude_code.providers.openai_chat.identity_enforcement`: a 429 naming
the free daily quota is the enforcement fingerprint, and it costs no traffic.
This is the active half, reached from the dashboard's *Probe capabilities*
button, and it exists because the passive one only speaks after the damage.

**What it can and cannot prove, stated plainly.** Two requests are sent to one
cheap model: one carrying the identity the profile declares, one carrying the
same set with a deliberately invalid value in the header that names the client.
An invalid value is used rather than an absent header because a check that is
switched *on* and a check that is switched *off* both answer 200 to a missing
header -- the difference shows up in a daily counter, which no two-request
probe can see. What the probe *can* see is a refusal: a 4xx that names a
header, or a quota error on one leg and not the other. When it sees neither it
records exactly that, with a date: *checked, no difference observed*. That is a
real answer, and the Models page says so rather than implying the host was
found innocent.

No response text is stored. The evidence is a status word and two HTTP codes,
the same bound :mod:`my_claude_code.providers.recovery.facts` puts on every
other learned negative.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    free_quota_reset,
)
from my_claude_code.providers.recovery import (
    FACT_CLIENT_IDENTITY_REQUIRED,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_PROBE,
    learned_fact_store,
)
from my_claude_code.providers.runtime.reasoning_probe import (
    PROBE_MAX_TOKENS,
    PROBE_TIMEOUT_SECONDS,
)

#: The detail that separates a probe's answer from the passive observation's.
DETAIL_PROBED = "probed"

#: Words a host uses when it is refusing because of a header. Matched against
#: the refusal only to decide *whether* something was learned; none of the
#: matched text is stored.
_HEADER_REFUSAL_MARKERS = (
    "x-opencode",
    "missingsessionid",
    "session",
    "header",
    "can only be used in",
)


@dataclass(frozen=True, slots=True)
class IdentityProbeOutcome:
    """What two requests taught us about one host's identity check."""

    provider_id: str
    model: str
    #: ``True`` the host was seen acting on it, ``False`` no difference seen,
    #: ``None`` the host could not be probed at all.
    required: bool | None
    detail: str
    with_identity_status: int = 0
    without_identity_status: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "probe": "client_identity",
            "provider_id": self.provider_id,
            "model": self.model,
            "status": "unknown" if self.required is None else "learned",
            "value": self.required,
            "detail": self.detail,
            "with_identity_status": self.with_identity_status,
            "without_identity_status": self.without_identity_status,
        }


def declared_identity_headers(provider_id: str) -> tuple[dict[str, str], str] | None:
    """The identity this provider declares, and the header a probe omits.

    ``None`` for every provider that declares none, which is what makes this
    module a no-op for all but the profiles that opted in -- no provider name
    is written down here.
    """
    profile = OPENAI_CHAT_PROFILES.get(provider_id)
    identity = getattr(profile, "client_identity", None)
    if identity is None or identity.probe_omit_header is None:
        return None
    return (
        identity.headers_for(f"probe:{provider_id}"),
        identity.probe_omit_header,
    )


def _probe_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }


class _BodyError(Exception):
    """Carries a refusal body in the shape the quota reader already reads."""

    def __init__(self, body: object) -> None:
        super().__init__("probe refusal")
        self.body = body


def _decoded(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _names_a_header(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _HEADER_REFUSAL_MARKERS)


async def _leg(
    http: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    headers: Mapping[str, str],
) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    sent = {"Authorization": f"Bearer {api_key}", **headers}
    response = await http.post(url, headers=sent, json=_probe_body(model))
    return response.status_code, ("" if response.status_code < 300 else response.text)


async def probe_client_identity(
    provider_id: str,
    base_url: str,
    api_key: str,
    model: str,
    *,
    proxy: str | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> IdentityProbeOutcome | None:
    """Two requests, one answer. ``None`` when this provider declares no identity."""
    declared = declared_identity_headers(provider_id)
    if declared is None or not (base_url and api_key and model):
        return None
    good, omitted = declared
    bad = {name: value for name, value in good.items() if name != omitted}
    owned = client is None
    http = client or httpx.AsyncClient(
        proxy=proxy or None, timeout=PROBE_TIMEOUT_SECONDS
    )
    try:
        good_status, good_text = await _leg(http, base_url, api_key, model, good)
        bad_status, bad_text = await _leg(http, base_url, api_key, model, bad)
    except Exception as exc:
        logger.debug("Identity probe failed: {}", type(exc).__name__)
        return IdentityProbeOutcome(
            provider_id, model, None, f"unprobeable ({type(exc).__name__})"
        )
    finally:
        if owned:
            await http.aclose()

    moment = now or datetime.now(UTC)
    checked = moment.strftime("%Y-%m-%d")
    required: bool | None
    if 400 <= bad_status < 500 and good_status < 400 and _names_a_header(bad_text):
        required, detail = True, "refused the request without the identity"
    elif free_quota_reset(_BodyError(_decoded(bad_text)), now=moment) is not None and (
        free_quota_reset(_BodyError(_decoded(good_text)), now=moment) is None
    ):
        required, detail = True, "quota error only without the identity"
    elif good_status >= 500 and bad_status >= 500:
        required, detail = None, f"unprobeable (host answered {good_status})"
    else:
        required, detail = False, f"checked {checked}, no difference observed"

    outcome = IdentityProbeOutcome(
        provider_id, model, required, detail, good_status, bad_status
    )
    if required is not None:
        learned_fact_store().record(
            provider_id,
            PROVIDER_WIDE_MODEL_ID,
            FACT_CLIENT_IDENTITY_REQUIRED,
            required,
            source=SOURCE_PROBE,
            detail=DETAIL_PROBED,
            evidence=f"{detail} ({good_status}/{bad_status})",
        )
    return outcome


__all__ = [
    "DETAIL_PROBED",
    "IdentityProbeOutcome",
    "declared_identity_headers",
    "probe_client_identity",
]
