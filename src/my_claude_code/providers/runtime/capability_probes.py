"""Ask a host what it actually does, instead of reading what it says it does.

``reasoning_probe.py`` established the method: send one value no correct host
can accept, and read the truth out of the 400 it answers with. Everything here
is the same move applied to a different field.

Every probe is **one tiny request**, has the same three outcomes, and stores no
response text:

``learned``   the host refused, in its own words, and the refusal is specific
              enough to name the capability. This is the only outcome that
              writes a fact.
``ignored``   the host answered 200. That proves the request was *accepted*,
              which is not the same as the feature working -- a host can accept
              an image block and never look at it -- so it records nothing.
``unknown``   401/402/403 before validation, a timeout, a 5xx. Nothing was
              measured, so nothing is claimed and the Models page says
              "could not be probed (403)" rather than a verdict.

**Narrow-only, per field, always.** A probe may lower a cap or remove a
capability; it may never raise a cap or assert a capability the catalogue
denied. That asymmetry is what makes it safe to rank a probe above the
provider's own ``/models``: tier 1 is what the host *says*, a probe is what the
host *does*, and a reseller gateway's catalogue routinely describes the
upstream model rather than the deployment it actually rents.

**Never in the request path.** Not on a first request, not on a fallback, not
lazily. A probe is an operator action; a served request must never wait on one.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from loguru import logger

from my_claude_code.providers.recovery import (
    FACT_OUTPUT_CAP,
    FACT_STREAM_USAGE_UNSUPPORTED,
    FACT_TOOL_CALLS_UNSUPPORTED,
    FACT_VISION_UNSUPPORTED,
    OPENAI_CHAT_OUTPUT_FIELDS,
    parse_output_token_cap,
)

from .reasoning_probe import PROBE_MAX_TOKENS, PROBE_TIMEOUT_SECONDS

#: How many models one press of *Probe capabilities* may cover. A gateway
#: listing 400 models behind that button is 400-1200 upstream requests, so the
#: ceiling is stated in the button's confirm text and enforced here.
MAX_MODELS_PER_PROBE_RUN = 25


def _now() -> str:
    """The timestamp a probe outcome is stamped with."""

    return datetime.now(UTC).isoformat()


#: The absurd budget P2 asks for. A host that validates says "at most N" and
#: tells us N for free, on a path that generates no output tokens at all.
IMPOSSIBLE_OUTPUT_TOKENS = 2_000_000_000

#: A 1x1 transparent PNG, as small as an image block can be.
ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: Statuses that mean the host never looked at the body. Nothing about the
#: model was measured, so the provider is recorded as unprobeable with the code
#: and no verdict is guessed at.
UNPROBEABLE_STATUSES: frozenset[int] = frozenset({401, 402, 403})

#: Words that make a 400 about the picture rather than about the request.
VISION_REJECTION_MARKERS: tuple[str, ...] = (
    "image",
    "image_url",
    "modality",
    "multimodal",
    "vision",
    "content type",
    "content_type",
)

#: Words that make a 400 about tool definitions rather than about the request.
TOOL_REJECTION_MARKERS: tuple[str, ...] = (
    "tools",
    "tool_choice",
    "functions",
    "function_call",
)

ProbeStatus = Literal["learned", "ignored", "unknown"]

#: The probes that ship on. Tool calling and streamed usage are specified and
#: implemented, but shipped off: a trivial tool call is the one probe a correct
#: host answers by *generating billable output tokens*, and a streamed probe
#: does the same.
DEFAULT_PROBES: tuple[str, ...] = ("output_cap", "vision")
OPTIONAL_PROBES: tuple[str, ...] = ("tool_calls", "stream_usage")
ALL_PROBES: tuple[str, ...] = DEFAULT_PROBES + OPTIONAL_PROBES

#: Which fact kind each probe writes when it learns something.
PROBE_FACT_KINDS: dict[str, str] = {
    "output_cap": FACT_OUTPUT_CAP,
    "vision": FACT_VISION_UNSUPPORTED,
    "tool_calls": FACT_TOOL_CALLS_UNSUPPORTED,
    "stream_usage": FACT_STREAM_USAGE_UNSUPPORTED,
}


@dataclass(frozen=True, slots=True)
class CapabilityProbeOutcome:
    """What one probe established about one model. Never carries response text."""

    probe: str
    model: str
    status: ProbeStatus
    #: The measured value a ``learned`` outcome writes: an int cap, or ``True``
    #: for a bare negative. ``None`` for every other status.
    value: Any = None
    #: A status word and at most an HTTP code. Never the host's response body.
    detail: str = ""
    probed_at: str = ""

    def as_payload(self) -> dict[str, Any]:
        """Render for the API and the card."""

        return {
            "probe": self.probe,
            "model": self.model,
            "status": self.status,
            "value": self.value,
            "detail": self.detail,
            "probed_at": self.probed_at,
        }


class _ProbeStatusError(Exception):
    """An upstream that answered before it validated. Not a measurement."""


def _base_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }


def _flatten(raw: str) -> str:
    """Flatten a JSON error envelope so a matcher can read it as one string."""

    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    return json.dumps(parsed, ensure_ascii=False)


class _StubComplaint(Exception):
    """Carry a probe's 400 into the matchers the recovery ladder already uses.

    The matchers read a complaint off an exception, because that is the shape a
    provider hands them. A probe has a plain response instead, so it is wrapped
    rather than the matchers being duplicated -- one output-cap parser, one set
    of comparator phrasings, one place to fix.
    """

    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(text)
        self.status_code = status_code
        self.response = httpx.Response(status_code, text=text)


def _probe_body(probe: str, model: str) -> dict[str, Any]:
    body = _base_body(model)
    if probe == "output_cap":
        # Both names in one body, so an OpenAI-compatible host names whichever
        # of them it owns. ``stream`` stays false and the timeout is short: a
        # host that does *not* validate has just been asked for two billion
        # tokens, and the 200 path is abandoned without reading the body.
        body["max_tokens"] = IMPOSSIBLE_OUTPUT_TOKENS
        body["max_completion_tokens"] = IMPOSSIBLE_OUTPUT_TOKENS
        return body
    if probe == "vision":
        body["messages"] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": ONE_PIXEL_PNG}},
                ],
            }
        ]
        return body
    if probe == "tool_calls":
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "mcc_probe",
                    "description": "A no-op used only to see whether tools parse.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        return body
    if probe == "stream_usage":
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        return body
    raise KeyError(f"unknown probe {probe!r}")


def _read_outcome(
    probe: str, model: str, status_code: int, text: str
) -> CapabilityProbeOutcome:
    if status_code in UNPROBEABLE_STATUSES:
        raise _ProbeStatusError(str(status_code))
    if status_code < 300:
        # Accepted is not the same as supported, so nothing is recorded --
        # except that the operator gets to see that the host said yes.
        return CapabilityProbeOutcome(
            probe=probe, model=model, status="ignored", detail="200", probed_at=_now()
        )
    if status_code not in (400, 422):
        return CapabilityProbeOutcome(
            probe=probe,
            model=model,
            status="unknown",
            detail=str(status_code),
            probed_at=_now(),
        )
    flattened = _flatten(text)
    if probe == "output_cap":
        cap = parse_output_token_cap(
            _StubComplaint(status_code, flattened), fields=OPENAI_CHAT_OUTPUT_FIELDS
        )
        if cap is None:
            return CapabilityProbeOutcome(
                probe=probe,
                model=model,
                status="unknown",
                detail="400 (no maximum named)",
                probed_at=_now(),
            )
        return CapabilityProbeOutcome(
            probe=probe,
            model=model,
            status="learned",
            value=cap,
            detail="400 named a maximum",
            probed_at=_now(),
        )
    markers = {
        "vision": VISION_REJECTION_MARKERS,
        "tool_calls": TOOL_REJECTION_MARKERS,
        "stream_usage": ("include_usage", "stream_options"),
    }[probe]
    haystack = flattened.lower()
    if not any(marker in haystack for marker in markers):
        return CapabilityProbeOutcome(
            probe=probe,
            model=model,
            status="unknown",
            detail="400 (unrelated)",
            probed_at=_now(),
        )
    return CapabilityProbeOutcome(
        probe=probe,
        model=model,
        status="learned",
        value=True,
        detail="400 named the field",
        probed_at=_now(),
    )


async def probe_capability(
    http: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    probe: str,
) -> CapabilityProbeOutcome:
    """Run exactly one probe against one model. One request, bounded."""

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = await http.post(url, headers=headers, json=_probe_body(probe, model))
    except Exception as exc:
        logger.debug("Capability probe {} failed: {}", probe, type(exc).__name__)
        return CapabilityProbeOutcome(
            probe=probe,
            model=model,
            status="unknown",
            detail=type(exc).__name__,
            probed_at=_now(),
        )
    # Read at most the error body; a 200 to the output-cap probe is abandoned
    # without reading anything, because the host may be generating right now.
    text = "" if response.status_code < 300 else response.text
    try:
        return _read_outcome(probe, model, response.status_code, text)
    except _ProbeStatusError as exc:
        return CapabilityProbeOutcome(
            probe=probe,
            model=model,
            status="unknown",
            detail=f"unprobeable ({exc})",
            probed_at=_now(),
        )


async def probe_model_capabilities(
    base_url: str,
    api_key: str,
    model: str,
    *,
    probes: Sequence[str] = DEFAULT_PROBES,
    proxy: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[CapabilityProbeOutcome, ...]:
    """Run the selected probes against one model, in order."""

    if not (base_url and api_key and model):
        return (
            CapabilityProbeOutcome(
                probe="all",
                model=model,
                status="unknown",
                detail="not configured",
                probed_at=_now(),
            ),
        )
    owned = client is None
    http = client or httpx.AsyncClient(
        proxy=proxy or None, timeout=PROBE_TIMEOUT_SECONDS
    )
    try:
        outcomes: list[CapabilityProbeOutcome] = []
        for probe in probes:
            outcome = await probe_capability(http, base_url, api_key, model, probe)
            outcomes.append(outcome)
            if outcome.detail.startswith("unprobeable"):
                # The host answers before it validates. Every further probe
                # would get the same non-answer and cost another request.
                break
        return tuple(outcomes)
    finally:
        if owned:
            await http.aclose()
