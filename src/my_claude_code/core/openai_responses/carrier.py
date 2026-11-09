"""The opaque reasoning carrier the client's own transcript carries.

The Responses API can return a reasoning item's ``encrypted_content`` -- an
opaque blob the host minted -- and, on a ``store: false`` conversation, the
*only* way reasoning survives to the next turn is for the caller to hand that
item back. Until 8.16.0 MCC asked for it (``include:
["reasoning.encrypted_content"]``) and then threw it away
(``RESPONSES_REASONING_REPLAY = DISABLED``): 13,856 of 26,566 measured ChatGPT
OAuth attempts carried ``history_thinking_omitted``.

This module is the envelope that carries it through the one place a relay is
guaranteed to get back: the client's own transcript. An Anthropic client echoes
a ``thinking`` block's ``signature`` and a ``redacted_thinking`` block's ``data``
back verbatim, so either field can hold the blob; the prefix tells it apart from
a signature Anthropic minted.

**The format, and why.**

    mcc:reasoning:v1:<base64url(compact JSON)>

``payload = {"origin": {...}, "native": {...}}``. ``origin`` is five strings --
provider, protocol, endpoint, connection, model -- and ``native`` is the whole
reasoning item the host sent, verbatim, ``id`` and all. The shape is
the upstream project's (``core/history_replay.py``); the namespace is MCC's own, so
that a blob can never be mistaken for one another product wrote, and a future
widening of the upstream's accepted prefixes cannot silently widen ours.

The base64 layer is load-bearing for a second reason: the ``native`` item
carries the host's ``summary`` text, and encoding the payload means that text is
**not** present as readable text anywhere in the outbound body. A design that
put the summary in plaintext beside the ciphertext would re-open the 7.58.1
echo class, where a model shown its own past turns as `` thinking`` text writes
its next turn in that format.

**Why the origin exists.** A carrier is host- AND model-bound: the host
validates the ciphertext against the item id and the model it issued it under,
and a blob replayed elsewhere is a hard 400 (OmniRoute #6953/#17324, Bifrost
#5186). The five fields are compared with no wildcards; an empty field is
evidence of a malformed envelope, not of a wildcard.

Nothing here imports ``providers/`` or ``config/``: the encoder is a pure
function of its arguments, so the same predicate can be called once per door by
the provenance guard that follows this PR.
"""

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: The namespace every envelope carries, checked at byte offset 0.
#:
#: A prefix at offset 0 rather than an infix separator, deliberately: the
#: payloads are long opaque blobs, so an infix search has a small but nonzero
#: chance of matching inside genuine Anthropic-issued data (Bifrost's comment
#: on ``ReasoningItemIDMarker`` makes the same argument). It can never collide
#: with an Anthropic signature -- base64 of a protobuf has no ``:`` in its
#: alphabet -- nor with a JSON container, whose first non-space character is
#: ``{`` or ``[``.
#:
#: ``reasoning:v1`` rather than a bare ``v1`` so a second carrier kind (a
#: Chat-door ``reasoning_details`` blob, a Gemini ``thoughtSignature``) can be
#: added later without ambiguity, and so a ``v2`` decoder can reject a ``v1``
#: prefix rather than guess.
CARRIER_PREFIX = "mcc:reasoning:v1:"

#: The one protocol a v1 envelope describes. A ``messages`` or ``chat`` blob is
#: a different envelope, and a later PR adds it under its own name.
CARRIER_PROTOCOL_RESPONSES = "responses"

#: The largest finished carrier string the encoder will mint. A guard, not a
#: claim about the client: ``encrypted_content`` is opaque and the only
#: measured round trip is 133 characters, so this is 246x the only measured
#: value and exists so a pathological item cannot inflate every subsequent
#: request body without bound.
#:
#: **Never truncate.** A truncated blob is a corrupted blob and the host would
#: reject it, which is worse than no continuity: the caller does not mint a
#: block at all and reports ``history_carrier_oversize`` instead.
CARRIER_MAX_CHARS = 32768

#: The five origin fields, in the order the envelope writes them. Fixed order
#: and no ``sort_keys``: the bytes must be stable across Python versions, and
#: both dicts are built here in this order.
ORIGIN_FIELDS: tuple[str, ...] = (
    "provider",
    "protocol",
    "endpoint",
    "connection",
    "model",
)


def connection_id(account_identity: str) -> str:
    """The stable ``connection`` field for one account identity.

    ``sha256(identity)[:16]``, exactly as the upstream project computes it
    (``providers/history_replay.py:124-141``).

    **The access token is never an input.** The ChatGPT OAuth access token
    refreshes roughly hourly; hashing it would refuse every carrier minted
    before a refresh, breaking continuity every hour, silently, with the only
    symptom a count nobody reads. The identity is the account (the ChatGPT OAuth
    ``account_id``) or the provider's configured API key, and ``""`` -- a stable
    constant -- when neither exists.

    The consequence is stated rather than hidden: a key rotation changes this
    value and therefore refuses every carrier minted before it. That is the
    correct behaviour (the new key may be a different account) and it costs one
    turn of continuity per rotation. Today there is no continuity at all.
    """

    return hashlib.sha256(account_identity.encode("utf-8")).hexdigest()[:16]


def normalise_endpoint(base_url: str) -> str:
    """The ``endpoint`` field: scheme + host (+ port) + path, trailing ``/`` off.

    No query, no fragment, no trailing slash -- the upstream project's ``address``
    computation (``providers/history_replay.py:142-148``). A base URL that does
    not parse as an absolute URL is used as given, minus the trailing slash, so
    a caller that hands this a bare host still gets a stable string rather than
    an exception on the request path.
    """

    text = (base_url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text.rstrip("/")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


@dataclass(frozen=True, slots=True)
class ReplayOrigin:
    """The five strings that say which host, account and model minted a blob.

    ``provider`` is the catalogue id that served the request (``chatgpt_oauth``,
    ``opencode``, ``opencode_go``); ``protocol`` is the literal ``"responses"``;
    ``endpoint`` is the normalised base URL; ``connection`` is
    :func:`connection_id` of the account; ``model`` is the **routed** model id
    that was sent upstream.

    ``model`` is compared, which the upstream project deliberately does not do. Two
    independent reasons: OmniRoute documents a hard 400 on a cross-model replay,
    and MCC routes per request -- one Claude Code session routinely sends
    ``claude-opus-5-5`` to ``gpt-6.1-sol`` and ``claude-haiku-5-5`` to
    ``gpt-6-luna``, so without the model in the match a luna blob would be
    replayed to sol on the very next turn. The cost is that a session
    alternating between two models keeps two independent continuity chains,
    which is the correct model of what the backend does.
    """

    provider: str
    protocol: str
    endpoint: str
    connection: str
    model: str

    def to_dict(self) -> dict[str, str]:
        """The envelope's ``origin`` object, in the fixed field order."""

        return {
            "provider": self.provider,
            "protocol": self.protocol,
            "endpoint": self.endpoint,
            "connection": self.connection,
            "model": self.model,
        }

    def is_complete(self) -> bool:
        """Whether all five fields are non-empty strings.

        An envelope always writes all five, so an empty field is evidence of a
        malformed envelope rather than of a wildcard -- which is why
        :func:`accepts` fails on one instead of matching anything.
        """

        return all(
            isinstance(value, str) and value
            for value in (
                self.provider,
                self.protocol,
                self.endpoint,
                self.connection,
                self.model,
            )
        )

    @classmethod
    def from_mapping(cls, value: Any) -> ReplayOrigin | None:
        """Read one ``origin`` object, or ``None`` when it is not a complete one.

        Total: a missing key, a non-string value and an empty string are all
        the same answer -- ``None`` -- because the caller counts a malformed
        envelope and drops it rather than guessing at a wildcard.
        """

        if not isinstance(value, Mapping):
            return None
        fields: dict[str, str] = {}
        for name in ORIGIN_FIELDS:
            item = value.get(name)
            if not isinstance(item, str) or not item:
                return None
            fields[name] = item
        return cls(**fields)


def accepts(destination: ReplayOrigin | None, source: ReplayOrigin) -> bool:
    """Whether ``destination`` may be sent a blob ``source`` minted.

    All five fields must be equal and all ten values non-empty. **No
    wildcards**: the upstream project's ``accepts()`` treats an empty
    ``source.endpoint``/``source.connection`` as "match anything", but that rule
    exists for its legacy ``fcc:anthropic-reasoning:v1:`` records. MCC's v1
    envelope always writes all five, so an empty field fails the match here.

    This is the predicate the provenance guard (the next PR) calls once per
    door, and the reason it is a free function rather than a method: one rule,
    one implementation, every caller.
    """

    if destination is None:
        return False
    if not destination.is_complete() or not source.is_complete():
        return False
    return (
        destination.provider == source.provider
        and destination.protocol == source.protocol
        and destination.endpoint == source.endpoint
        and destination.connection == source.connection
        and destination.model == source.model
    )


def is_carrier(value: Any) -> bool:
    """Whether ``value`` claims to be one of our envelopes.

    The test is ``str.startswith(CARRIER_PREFIX)`` and nothing else -- no
    heuristic, no "looks like base64", no length test. A block that does not
    start with that exact prefix is not ours and is never parsed.
    """

    return isinstance(value, str) and value.startswith(CARRIER_PREFIX)


@dataclass(frozen=True, slots=True)
class CarrierPayload:
    """One decoded envelope: where it came from, and the item it carries."""

    origin: ReplayOrigin
    native: dict[str, Any]


def encode_carrier(origin: ReplayOrigin, native: Mapping[str, Any]) -> str | None:
    """Render one carrier, or ``None`` when it would exceed the guard.

    ``native`` is the **whole reasoning item the host sent**, verbatim, as it
    arrived on ``response.output_item.done`` -- ``id``, ``type``, ``status``,
    ``summary``, ``content``, ``encrypted_content``, whatever else the host put
    there. It is not a projection and it is not re-encoded: the host validates
    the ciphertext against the id it minted, so a synthesised or omitted id is
    the fake-id/real-ciphertext pair Bifrost documents.

    ``None`` means "do not mint a block at all". The caller counts
    ``history_carrier_oversize`` and emits the block it emits today. A
    serialisation failure cannot happen for an item that came off
    ``json.loads``, but it is folded into the same answer rather than allowed
    to raise on the response path.
    """

    payload = {"origin": origin.to_dict(), "native": dict(native)}
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except TypeError, ValueError:
        return None
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    carrier = CARRIER_PREFIX + encoded
    if len(carrier) > CARRIER_MAX_CHARS:
        return None
    return carrier


def decode_carrier(value: Any) -> CarrierPayload | None:
    """Read one carrier, or ``None`` when it does not decode.

    **Total and never raises.** Any of -- wrong prefix, empty payload, invalid
    base64, invalid UTF-8, invalid JSON, a non-object, a missing/short
    ``origin``, a ``native`` that is not an object, a ``native["type"]`` that is
    not ``"reasoning"``, a payload over :data:`CARRIER_MAX_CHARS` -- is the same
    answer: ``None``. The caller drops the block and counts
    ``history_carrier_malformed``; nothing is ever partially used and nothing
    falls back to "send the raw string as ``encrypted_content``", which is
    exactly the fake-blob failure Bifrost documents.

    This is the upstream project's ``HistoryReplayError`` discipline collapsed to
    "drop + count", because MCC has no retry to spend on it.
    """

    if not is_carrier(value):
        return None
    text = value
    if len(text) > CARRIER_MAX_CHARS:
        return None
    encoded = text[len(CARRIER_PREFIX) :]
    if not encoded:
        return None
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except binascii.Error, ValueError:
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        payload = json.loads(decoded)
    except ValueError, RecursionError:
        return None
    if not isinstance(payload, dict):
        return None
    origin = ReplayOrigin.from_mapping(payload.get("origin"))
    if origin is None:
        return None
    native = payload.get("native")
    if not isinstance(native, dict):
        return None
    if native.get("type") != "reasoning":
        return None
    return CarrierPayload(origin=origin, native=native)


__all__ = [
    "CARRIER_MAX_CHARS",
    "CARRIER_PREFIX",
    "CARRIER_PROTOCOL_RESPONSES",
    "ORIGIN_FIELDS",
    "CarrierPayload",
    "ReplayOrigin",
    "accepts",
    "connection_id",
    "decode_carrier",
    "encode_carrier",
    "is_carrier",
    "normalise_endpoint",
]
