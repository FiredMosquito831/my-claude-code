"""Which upstream door a response's bytes came through (8.8.0).

One table, read by both ends: the providers that write a ``RAW_UPSTREAM`` line
name the door they are on, and the request detail's viewer reads the same names
back when it explains what a box does or does not hold. Two answers to "which
door is this" would drift, and the sentence the user reads is the one thing
this feature exists to make honest.

A *door* is the transport + protocol pair an attempt's bytes arrive on, not the
provider: twelve OpenAIChatProviders share one door, and the gateway that serves
both a Chat Completions model and a Messages one has two. That is what makes a
reader able to say "this request ran on the OpenAI Chat Completions door" rather
than guessing from the provider's name.

The ``captures`` flag is the one fact the product lacked before this release:
whether a door records the provider's own bytes *at all*. ``metadata`` marks a
door that deliberately records what the host said about an answer instead of
the answer itself (media, by the user's decision of 2026-11-01). A ``False``
capture is named in the box, never blamed on the user's setting.
"""

from typing import Final

#: The provider's own bytes reach the raw log, unchanged, one line per chunk.
CAPTURES_BYTES: Final = "bytes"
#: The door records what the host said about its answer -- status, content
#: type, byte count -- and never the body itself.
CAPTURES_METADATA: Final = "metadata"
#: The door records nothing of the provider's answer.
CAPTURES_NOTHING: Final = "nothing"

DOOR_ANTHROPIC_MESSAGES: Final = "anthropic_messages"
DOOR_OPENAI_RESPONSES: Final = "openai_responses"
DOOR_OPENAI_CHAT: Final = "openai_chat"
DOOR_CHATGPT_OAUTH: Final = "chatgpt_oauth"
DOOR_RESPONSES_TRANSPORT: Final = "openai_chat.responses"
DOOR_REFUSAL: Final = "refusal"
DOOR_MEDIA: Final = "media"
DOOR_WEBSEARCH: Final = "websearch"


class UpstreamDoor:
    """One door: the name a raw line carries and the words the box shows."""

    __slots__ = ("captures", "door", "label")

    def __init__(self, door: str, label: str, captures: str) -> None:
        self.door = door
        self.label = label
        self.captures = captures

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"UpstreamDoor({self.door!r}, {self.captures!r})"


#: Every door, in the order the viewer explains them. The Messages door is
#: named once because ``anthropic``, ``anthropic_oauth``, Command Code's
#: Messages delegate and a gateway's Messages surface all re-point the same
#: reader; likewise the Responses door, which ``chatgpt_oauth`` and the
#: Responses transport both alias.
UPSTREAM_DOORS: dict[str, UpstreamDoor] = {
    door.door: door
    for door in (
        UpstreamDoor(
            DOOR_ANTHROPIC_MESSAGES,
            "Anthropic Messages",
            CAPTURES_BYTES,
        ),
        UpstreamDoor(
            DOOR_OPENAI_RESPONSES,
            "OpenAI Responses",
            CAPTURES_BYTES,
        ),
        UpstreamDoor(
            DOOR_CHATGPT_OAUTH,
            "ChatGPT OAuth (Responses)",
            CAPTURES_BYTES,
        ),
        UpstreamDoor(
            DOOR_RESPONSES_TRANSPORT,
            "OpenAI Responses transport",
            CAPTURES_BYTES,
        ),
        UpstreamDoor(
            DOOR_OPENAI_CHAT,
            "OpenAI Chat Completions",
            CAPTURES_BYTES,
        ),
        UpstreamDoor(
            DOOR_REFUSAL,
            "the provider's refusal body",
            CAPTURES_BYTES,
        ),
        UpstreamDoor(
            DOOR_MEDIA,
            "the media API",
            CAPTURES_METADATA,
        ),
        UpstreamDoor(
            DOOR_WEBSEARCH,
            "the web-search API",
            CAPTURES_NOTHING,
        ),
    )
}

#: The door ids a streaming model answer can arrive on, for the sentences that
#: say what the model doors do.
MODEL_DOORS: tuple[str, ...] = (
    DOOR_ANTHROPIC_MESSAGES,
    DOOR_OPENAI_RESPONSES,
    DOOR_CHATGPT_OAUTH,
    DOOR_RESPONSES_TRANSPORT,
    DOOR_OPENAI_CHAT,
)


def door_label(door: str) -> str:
    """The door's human name, or the id itself for one this file has not met.

    Never raises: a raw line is read long after the release that wrote it, and
    a door renamed or removed later must still be readable in an old file.
    """

    spec = UPSTREAM_DOORS.get(door)
    return spec.label if spec is not None else door


def door_captures(door: str) -> str:
    """What this door records: bytes, metadata, or nothing."""

    spec = UPSTREAM_DOORS.get(door)
    return spec.captures if spec is not None else CAPTURES_NOTHING


def doors_that_capture_nothing() -> tuple[str, ...]:
    """The doors a model request can never find bytes for, in table order."""

    return tuple(
        spec.door
        for spec in UPSTREAM_DOORS.values()
        if spec.captures == CAPTURES_NOTHING
    )
