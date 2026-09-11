"""Recognise a rejection that means "you knocked on the wrong endpoint".

A gateway that fronts several APIs behind one base URL has to answer a request
aimed at the wrong one somehow, and the honest answer -- a 404, or a 400 naming
the endpoint that would have worked -- is not the one OpenCode Zen gives. It
returns a bare ``HTTP 500 {"type":"error","error":{"type":"error","message":
"Internal server error"}}`` with nothing in it that names an endpoint, and
*sometimes* a ``403`` claiming the model is unavailable in the caller's
country, because its region check runs in front of its endpoint routing. Both
are recorded in the vendor's own open issue ``anomalyco/opencode#47969``
(2026-09-08), which calls the 500 a missing ``WrongEndpointError``.

So the signature has to be read rather than looked up, and it has to be read
*narrowly*. Three halves of one rule, and the last two are what keep it honest:

* a 4xx that **names an endpoint** is the direct evidence, and the shape the
  vendor's issue asks them to start sending;
* a **bare** 500 -- nothing readable came back, or only the generic sentence a
  gateway emits when it has nothing to say -- is surface-shaped. A 500 that
  carries a real complaint is an upstream having a bad moment and stays
  retryable, because five of Zen's free models are on the chat-completions
  surface and a genuine outage there must still be retried;
* a 401, a 404, a 429, and any 403 that is about credit or a key are **never**
  surface-shaped. They are answers about who is calling, what was named, or
  how often, and moving to another endpoint would spend a request learning
  nothing.

Nothing here decides anything. It returns the words that matched, and the
caller -- the only thing that knows whether this profile has a second surface
at all -- decides whether to probe.
"""

import re

from .complaint import (
    complaint_evidence_snippet,
    upstream_complaint,
    upstream_status_code,
)

#: The whole vocabulary of a gateway with nothing to say. A 500 whose complaint
#: is only these words told us nothing about *this* request -- which is
#: precisely the evidence that it never reached the code that would have had an
#: opinion about it.
_GENERIC_FAILURE_PATTERN = re.compile(
    r"^(?:"
    r"error"
    r"|internal(?: server)? error"
    r"|internal_server_error"
    r"|server error"
    r"|service unavailable"
    r"|unknown error"
    r"|upstream error"
    r"|[\s\W]+"
    r")*$"
)

#: A host naming an endpoint. The leading slash is required: it is what tells a
#: path apart from the English word.
_ENDPOINT_PATTERN = re.compile(r"/(?:\w+/)*(?:responses|chat/completions|messages)\b")

#: The region refusal the vendor documents as arriving *instead of* the 500.
#: Matched on the host's own words, never on the bare status.
_REGION_PATTERN = re.compile(
    r"regionerror|not available in your country|region[_ ]not[_ ]supported"
)

#: Statuses that can carry a wrong-endpoint meaning at all. 401, 404 and 429
#: are absent on purpose: they are answers about the credential, the model name
#: and the rate, and each already has its own established handling.
_SURFACE_STATUSES = frozenset({400, 403, 405, 422, 500, 501})


def surface_shaped_failure(error: Exception) -> str | None:
    """Return the evidence that this rejection is about the *endpoint*.

    ``None`` -- the answer for almost everything -- means "an ordinary
    failure", and the caller must treat it exactly as it always has.
    """

    status = upstream_status_code(error)
    if status not in _SURFACE_STATUSES:
        return None
    # Only what the host actually sent. ``upstream_complaint`` falls back to
    # ``str(error)`` when nothing readable parses, and that string is MCC's own
    # wording of the failure -- reading it would be this module quoting itself
    # back as evidence, and an empty body is exactly the case that matters
    # here. The fallback is recognised rather than guessed at: it is returned
    # verbatim, so it can be compared.
    spoken = upstream_complaint(error)
    own_words = str(error).lower()
    complaint = "" if spoken == own_words else spoken
    if _ENDPOINT_PATTERN.search(complaint) or _ENDPOINT_PATTERN.search(
        str(error).lower()
    ):
        return (
            complaint_evidence_snippet(complaint) or f"HTTP {status} named an endpoint"
        )
    if status == 403:
        if _REGION_PATTERN.search(complaint):
            return complaint_evidence_snippet(complaint)
        return None
    if status in {500, 501} and _GENERIC_FAILURE_PATTERN.match(complaint.strip()):
        # Reported as the status alone on purpose: there is nothing to quote,
        # and "a 500 with nothing in it" is the finding.
        return f"HTTP {status} with no complaint"
    return None
