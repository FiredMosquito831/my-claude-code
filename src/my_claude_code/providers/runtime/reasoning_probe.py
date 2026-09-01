"""Ask a custom provider's host, once, which effort words it accepts.

The method is the one that settled every static profile's vocabulary: send a
``reasoning_effort`` value no host could implement and read the enum out of the
400 it answers with. It is cheap (``max_tokens`` is 16 and the 400 arrives
before any tokens are generated), it is honest (the host is the only authority
on its own wire), and it is bounded (never more than two requests).

Three outcomes, and the difference between them matters:

``learned``   the 400 named an enum -- that enum becomes the provider's dialect.
``ignored``   the host answered 200 to a value it could not possibly support,
              so it does not read the field. Sending one is harmless and
              meaningless.
``unknown``   anything else -- a 401 before validation, a balance error, a
              timeout. Nothing was measured, so nothing is claimed and the
              generic profile keeps the wire exactly as it is today.

No response text is stored anywhere: the outcome carries a status word and,
at most, an HTTP code.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from loguru import logger

from my_claude_code.config.reasoning_enum import parse_effort_enum

PROBE_INVALID_EFFORT = "bogus_value"
"""A value no effort scale can contain, so a 200 proves the field is ignored."""

PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 30.0

ProbeStatus = Literal["learned", "ignored", "unknown"]


@dataclass(frozen=True, slots=True)
class ReasoningProbeOutcome:
    """What one dialect probe established about one host."""

    status: ProbeStatus
    effort_enum: tuple[str, ...] = ()
    field_ignored: bool = False
    detail: str = ""
    probed_at: str = ""

    def as_payload(self) -> dict[str, Any]:
        """Render for the API and the card. Never carries response text."""
        return {
            "status": self.status,
            "effort_enum": list(self.effort_enum),
            "field_ignored": self.field_ignored,
            "detail": self.detail,
            "probed_at": self.probed_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _body(model: str, effort: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }
    if effort is not None:
        body["reasoning_effort"] = effort
    return body


def _message_text(raw: str) -> str:
    """Flatten a JSON error envelope so the enum is reachable in one string."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    return json.dumps(parsed, ensure_ascii=False)


async def probe_reasoning_dialect(
    base_url: str,
    api_key: str,
    model: str,
    *,
    proxy: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ReasoningProbeOutcome:
    """Probe one host's effort vocabulary with at most two tiny requests."""
    if not (base_url and api_key and model):
        return ReasoningProbeOutcome(
            status="unknown", detail="not configured", probed_at=_now()
        )
    owned = client is None
    http = client or httpx.AsyncClient(
        proxy=proxy or None, timeout=PROBE_TIMEOUT_SECONDS
    )
    try:
        return await _probe(http, base_url.rstrip("/"), api_key, model)
    finally:
        if owned:
            await http.aclose()


async def _probe(
    http: httpx.AsyncClient, base_url: str, api_key: str, model: str
) -> ReasoningProbeOutcome:
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url}/chat/completions"
    try:
        first = await http.post(
            url, headers=headers, json=_body(model, PROBE_INVALID_EFFORT)
        )
    except Exception as exc:
        logger.debug("Reasoning dialect probe failed: {}", type(exc).__name__)
        return ReasoningProbeOutcome(
            status="unknown", detail=type(exc).__name__, probed_at=_now()
        )

    if first.status_code < 300:
        # The host accepted a value no effort scale contains, so it is not
        # reading the field. That is a measurement, not a failure.
        return ReasoningProbeOutcome(
            status="ignored", field_ignored=True, detail="200", probed_at=_now()
        )

    if first.status_code not in (400, 422):
        return ReasoningProbeOutcome(
            status="unknown", detail=str(first.status_code), probed_at=_now()
        )

    words = parse_effort_enum(_message_text(first.text), sent=PROBE_INVALID_EFFORT)
    if words:
        return ReasoningProbeOutcome(
            status="learned", effort_enum=words, detail="400", probed_at=_now()
        )

    # A 400 that names no enum has two readings: the field was rejected, or
    # the request was wrong for a reason that has nothing to do with it. The
    # second request is what tells them apart, and it is the last one.
    try:
        second = await http.post(url, headers=headers, json=_body(model, None))
    except Exception as exc:
        return ReasoningProbeOutcome(
            status="unknown", detail=type(exc).__name__, probed_at=_now()
        )
    detail = "400 (no enum named)" if second.status_code < 300 else "400 (unrelated)"
    return ReasoningProbeOutcome(status="unknown", detail=detail, probed_at=_now())
