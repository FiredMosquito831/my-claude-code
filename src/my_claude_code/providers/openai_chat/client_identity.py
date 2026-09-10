"""The client identity one profile puts on every outbound request.

Most hosts do not care who is calling: a bearer token is the whole of the
claim, and the twenty-odd profiles in :mod:`.profiles` declare nothing here.
A few do care, and OpenCode Zen is the first of them this proxy has met -- its
free tier reads a set of identity headers its own client sends and gives a
smaller daily allowance to requests that arrive without them, while its Go
endpoint requires the conversation header outright.

The shape below is deliberately *declarative* rather than a branch. A profile
either declares a :class:`ClientIdentity` or it does not, and
:func:`~my_claude_code.providers.openai_chat.create_openai_chat_provider` reads
the declaration; nothing in the construction path names a provider or a model.
That rule is what keeps this file from becoming the place every host's quirk
accumulates -- the same rule that keeps the reasoning dialects honest.

Two lifetimes, because the values have two lifetimes:

* the **constant** half (which program, which version, which project) is the
  same for every request this provider will ever make, so it is built once and
  handed to the SDK as ``default_headers``;
* the **per-request** half (which conversation, which call) changes, so it
  rides on ``extra_headers`` of the create call.

Both halves are produced by the *same* declaration, in one order, so the
identity cannot drift into two disagreeing halves. The constant half also
carries a fallback value for the per-request names, which is what a request
made outside any client request -- model discovery, a token count -- sends:
those calls have no conversation, and a header that is present with a stable
value is closer to the identified client than a header that vanishes.
"""

import hashlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The alphabet OpenCode's own id generator uses for the random tail of an id.
BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: Length of the random tail on an OpenCode-shaped identifier.
ID_SUFFIX_CHARS = 14

#: Length of the time-ordered hexadecimal head of an OpenCode-shaped identifier.
ID_TIME_CHARS = 12

#: Bound on the text hashed into a derived conversation key. A conversation is
#: identified by how it *starts*, and the opening of a system prompt plus the
#: opening of the first user turn is both stable across turns and far more than
#: enough to separate two conversations. Bounding it also keeps the cost of the
#: derivation flat as a conversation grows.
MAX_CONVERSATION_KEY_CHARS = 4_096


def _base62(value: int, length: int) -> str:
    """Render ``value`` in base 62, left-padded to ``length`` characters."""
    digits: list[str] = []
    remaining = value
    for _ in range(length):
        remaining, index = divmod(remaining, 62)
        digits.append(BASE62_ALPHABET[index])
    return "".join(reversed(digits))


def _random_suffix() -> str:
    """A fresh random base62 tail, the way the identified client mints one."""
    return _base62(int.from_bytes(os.urandom(16), "big"), ID_SUFFIX_CHARS)


def derived_session_id(conversation_key: str, *, prefix: str = "ses") -> str:
    """A stable identifier for one conversation, in the identified client's shape.

    The client this proxy identifies as mints session ids as ``ses_`` plus a
    twelve-character time head plus a fourteen-character random tail, and its
    session ids count *down*, so every one of them begins ``ses_f``. A proxy
    has no conversations of its own, so the head and the tail are derived from
    a digest of whatever identifies the conversation instead of from a clock
    and a random source: the same conversation produces the same id on every
    turn, two conversations produce different ids, and no conversation's id
    can be predicted from another's.

    The leading ``f`` is kept because it is a property of the shape, not an
    accident of one sample.
    """
    digest = hashlib.sha256(conversation_key.encode("utf-8")).digest()
    head = "f" + digest.hex()[: ID_TIME_CHARS - 1]
    tail = _base62(int.from_bytes(digest[16:], "big"), ID_SUFFIX_CHARS)
    return f"{prefix}_{head}{tail}"


def new_message_id(*, prefix: str = "msg") -> str:
    """A fresh per-call identifier, in the identified client's ascending shape.

    Message ids count *up* -- the head is the wall clock in milliseconds -- so
    unlike the session id this one is minted rather than derived: it really is
    a new thing at a new time, and saying so is true.
    """
    head = f"{int(time.time() * 1000) & 0xFFFFFFFFFFFF:012x}"
    return f"{prefix}_{head}{_random_suffix()}"


def _text_of(content: object) -> str:
    """Flatten one message's content to text, whatever shape it arrived in."""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def conversation_key_from_messages(messages: object) -> str:
    """Derive a conversation's identity from how it opens.

    Used only when the inbound client supplied no conversation id of its own.
    The system prompt and the first user turn are the two things that do not
    change as a conversation grows, which is exactly the property the header
    needs: the host uses it for provider stickiness and prompt caching, and an
    id that changed every turn would be worse than no id at all.
    """
    if not isinstance(messages, Sequence) or isinstance(messages, str | bytes):
        return ""
    system_text = ""
    first_user_text = ""
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "system" and not system_text:
            system_text = _text_of(message.get("content"))
        elif role == "user" and not first_user_text:
            first_user_text = _text_of(message.get("content"))
        if system_text and first_user_text:
            break
    combined = f"{system_text}\x00{first_user_text}"
    return combined[:MAX_CONVERSATION_KEY_CHARS]


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """One profile's outbound identity, declared once and built twice.

    ``constant`` names the headers whose values never change for the life of a
    provider and produces them; ``session_header`` and ``request_header`` name
    the two that vary and are filled per request. ``order`` is the sequence the
    identified client emits them in, and every header set this class builds is
    emitted in it, so the two halves cannot disagree about shape.
    """

    #: Header names in the order the identified client sends them.
    order: tuple[str, ...]
    #: Produces the never-changing half. Called per construction, and again by
    #: the per-request path, so an operator's choice can follow a config apply.
    constant: Callable[[], Mapping[str, str]]
    #: The header carrying the conversation id, or ``None``.
    session_header: str | None = None
    #: The header carrying the per-call id, or ``None``.
    request_header: str | None = None
    #: The header a probe deliberately leaves off, to ask whether this host
    #: acts on the identity at all. ``None`` -- the default -- means the
    #: question cannot be asked of this host and no probe traffic is spent
    #: trying.
    #:
    #: It names the header the host was *measured* refusing without, not
    #: the one that looks most like an identity. Those turned out to be
    #: different headers: on 2026-09-10 OpenCode Zen answered a request
    #: carrying a deliberately invalid ``x-opencode-client`` with 200 and a
    #: request carrying no ``x-opencode-session`` with 400. A probe aimed
    #: at the first would have recorded *no difference observed* on a host
    #: that was refusing us outright.
    probe_omit_header: str | None = None
    #: What a call made outside any client request uses as its conversation
    #: key. Not a conversation -- a name for "no conversation" -- and stated as
    #: such rather than left to look like one.
    context_free_key: str = "mcc:no-conversation"

    def _ordered(self, values: Mapping[str, str]) -> dict[str, str]:
        """Return ``values`` in this identity's declared header order."""
        ordered = {name: values[name] for name in self.order if name in values}
        # A value produced for a name the order does not mention is a bug in
        # the declaration, not something to drop silently.
        for name, value in values.items():
            if name not in ordered:
                ordered[name] = value
        return ordered

    def headers_for(self, conversation_key: str) -> dict[str, str]:
        """The whole identity, for one conversation and one call.

        Built as one thing rather than two halves stitched together at the
        call site: the constant half is re-read every time, so an operator
        who changes which identity MCC presents sees it on the next request
        rather than on the next provider rebuild.
        """
        values = dict(self.constant())
        values.update(self.request_headers(conversation_key))
        return self._ordered(values)

    def default_headers(self) -> dict[str, str]:
        """The set as it stands with no request in flight.

        Handed to the SDK once, which is what fixes the order every later
        request is emitted in: replacing a header the client already knows
        keeps its position, while a new name is appended. It is also what a
        call made outside any client request -- model discovery, a token
        count -- actually sends.
        """
        return self.headers_for(self.context_free_key)

    def request_headers(self, conversation_key: str) -> dict[str, str]:
        """The half that varies, for one conversation and one call."""
        values: dict[str, str] = {}
        if self.session_header is not None:
            values[self.session_header] = derived_session_id(
                conversation_key or self.context_free_key
            )
        if self.request_header is not None:
            values[self.request_header] = new_message_id()
        return self._ordered(values)

    def header_names(self) -> tuple[str, ...]:
        """Every header name this identity can put on the wire."""
        return self.order


def identity_headers_for_body(
    identity: ClientIdentity | None,
    body: Mapping[str, Any],
    conversation_id: str | None,
) -> dict[str, str]:
    """The per-request half for one outbound body, or an empty mapping.

    ``conversation_id`` is what the inbound client called this conversation,
    when it named one. Nothing here reads the model, the provider or the
    credential: the only inputs are the declaration and the conversation.
    """
    if identity is None:
        return {}
    key = conversation_id or conversation_key_from_messages(body.get("messages"))
    return identity.headers_for(key)


__all__ = [
    "BASE62_ALPHABET",
    "ID_SUFFIX_CHARS",
    "ID_TIME_CHARS",
    "MAX_CONVERSATION_KEY_CHARS",
    "ClientIdentity",
    "conversation_key_from_messages",
    "derived_session_id",
    "identity_headers_for_body",
    "new_message_id",
]
