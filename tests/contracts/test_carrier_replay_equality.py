"""The carrier replay's equality proof: off is byte-identical, on adds items.

IV.7 is the contract: *"Everything new is opt-in, and the prompt cache must not
break. With no new preference set, request bodies are byte-identical."*

Two claims, and this file is where both are proven rather than asserted-to:

**Off (§7.1).** With ``RESPONSES_CARRIER_REPLAY`` unset -- which is what
``origin=None`` means at the builder, and what the shipped default produces at
the sender -- every body is byte-identical to v8.15.1 across all four doors and
all eight corpus shapes. The corpus is the one the Responses goldens already
use, extended to every carrier-bearing shape the spec names.

**On (§7.2).** With it on, the Responses body differs from the setting-off body
in exactly one key, ``input``, and inside it in exactly the added ``reasoning``
items. Every other key is byte-identical, on both Responses senders, and the
Anthropic and Chat doors are unchanged.

**The cache prefix (§7.2, IV.7's second clause).** The added items are *inside*
``input``, at the head of each turn's run, and a turn's items are computed from
that turn's blocks -- which the client already sent. So the digest of
``input[:k]`` for the last turn's start index is identical between the
setting-off and setting-on bodies of the *same* history. That is the assertion
below: not an argument, a measurement.

**One deliberate exception, and it is in the wire NOTE, not the body.** The
merged PR-1 test ``test_the_omission_note_counts_the_thinking_block_and_the_
carrier`` changed on purpose: ``history_thinking_omitted`` now counts a
``redacted_thinking`` block too, because that block is reasoning the surface
leaves out and the note was under-reporting its own omission. With the setting
off MCC never mints a carrier, so no real transcript can contain one and the
change is fixture-level only. It is named in the PR body.
"""

import copy
import hashlib
import itertools
import json
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import (
    ReplayOrigin,
    connection_id,
    encode_carrier,
)
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.openai_chat import create_openai_chat_provider
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_responses.conversion import (
    build_responses_request_body,
)
from tests.providers.support import passthrough_rate_limiter
from tests.support.parallel_tool_calls_pin import pin_parallel_tool_calls_off

#: The host every "on" body is built for.
ORIGIN = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="gpt-5.6-terra",
)

#: A second host, for the OpenCode-shaped bodies.
OPENCODE_ORIGIN = ReplayOrigin(
    provider="opencode",
    protocol="responses",
    endpoint="https://opencode.ai/zen/v1",
    connection=connection_id("sk-test"),
    model="muse-spark-1.3-contributor-free",
)

E1 = "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"
E2 = "gAAAAABpQ2FycmllclR3b19FbmNyeXB0ZWRDb250ZW50X2FiY2RlZmdoaWo"


@pytest.fixture(autouse=True)
def _pre_8_15_0_responses_bodies(monkeypatch: pytest.MonkeyPatch):
    """Both sides of every comparison on the same ``parallel_tool_calls``.

    8.15.0 ships that setting on, which moves one field in every Responses
    body. Pinning it off keeps the comparison about the carrier and nothing
    else; ``test_responses_parallel_tool_calls.py`` proves that cell separately.
    """

    pin_parallel_tool_calls_off(monkeypatch)
    try:
        yield
    finally:
        pass


def _carrier(encrypted: str, item_id: str | None = "rs_1") -> str:
    item: dict[str, Any] = {
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "content": [],
        "encrypted_content": encrypted,
    }
    if item_id is not None:
        item = {"id": item_id, **item}
    carrier = encode_carrier(ORIGIN, item)
    assert carrier is not None
    return carrier


def _thinking(text: str, signature: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def _tool_use(tool_id: str, name: str = "Read") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _tool_result(tool_id: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
    }


#: The eight corpus shapes the spec names, plus the plain turn the goldens use.
CORPUS: dict[str, list[dict[str, Any]]] = {
    "1_plain_user_turn": [
        {"role": "user", "content": "hello"},
    ],
    "2_real_anthropic_signature": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a thought", "sig-from-anthropic"),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "3_thinking_carrying_a_carrier": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier(E1)),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "4_redacted_thinking_carrying_a_carrier": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": _carrier(E1)},
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "5_redacted_thinking_json_container": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": json.dumps({"carrier": E1})},
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "6_redacted_thinking_bare_base64": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": "cGxhaW4tYmFzZTY0LW5vdC1vdXJz"},
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "7_multi_turn_tool_loop_with_carriers": [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": _carrier(E1, "rs_1")},
                _tool_use("call_1"),
            ],
        },
        _tool_result("call_1"),
        {
            "role": "assistant",
            "content": [
                _thinking("two", _carrier(E2, "rs_2")),
                _tool_use("call_2", "Grep"),
            ],
        },
        _tool_result("call_2"),
    ],
    "8_two_carriers_in_one_turn": [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "redacted_thinking", "data": _carrier(E1, "rs_1")},
                _thinking("two", _carrier(E2, "rs_2")),
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
}


def _request(fixture: str, *, model: str) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 4096,
            "system": "You are a coding agent.",
            "messages": copy.deepcopy(CORPUS[fixture]),
        }
    )


POLICIES: dict[str, ReasoningPolicy] = {
    "off": ReasoningPolicy.off(),
    "on_high": ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
}


OMIT = object()
"""A sentinel meaning "call the builder without the ``origin`` keyword at all".

The distinction matters: ``origin=None`` is the shipped default, and *omitting
the keyword* is the v8.15.1 call shape. The equality proof compares the two, so
"the new keyword changes no byte when it is absent" is measured rather than
argued.
"""


def _opencode_body(
    provider_id: str, request: MessagesRequest, policy: ReasoningPolicy, origin: Any
) -> dict[str, Any]:
    provider = create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[provider_id],
    )
    kwargs: dict[str, Any] = {}
    if origin is not OMIT:
        kwargs["origin"] = origin
    body = build_responses_request_body(
        request,
        reasoning=policy,
        store=False,
        stream=True,
        parallel_tool_calls=False,
        max_output_tokens=request.max_tokens,
        extra_body=provider._responses_extra_body(request),
        tool_name_max_length=provider._responses.tool_name_max_length,
        tool_catalogue=provider._responses.tool_catalogue(request),
        **kwargs,
    )
    body.pop("prompt_cache_key", None)
    return body


def _bodies(fixture: str, policy_name: str, origin: Any) -> dict[str, Any]:
    """Every door's body for one corpus shape, at one setting."""

    policy = POLICIES[policy_name]
    out: dict[str, Any] = {}
    chatgpt_kwargs: dict[str, Any] = {}
    if origin is not OMIT:
        chatgpt_kwargs["origin"] = origin
    out["responses/chatgpt_oauth"] = build_chatgpt_oauth_request_body(
        _request(fixture, model="gpt-5.6-terra"),
        reasoning=policy,
        **chatgpt_kwargs,
    )
    for provider_id, model in (
        ("opencode", "muse-spark-1.3-contributor-free"),
        ("opencode_go", "a-go-model"),
    ):
        out[f"responses/{provider_id}"] = _opencode_body(
            provider_id,
            _request(fixture, model=model),
            policy,
            # The OpenCode origin only matches the OpenCode bodies; the
            # ChatGPT origin is passed for the other door above.
            OMIT if origin is OMIT else (OPENCODE_ORIGIN if origin else None),
        )
    out["anthropic_messages"] = build_anthropic_messages_body(
        _request(fixture, model="claude-sonnet-5-5"), reasoning=policy
    )
    for provider_id in ("opencode", "opencode_go"):
        out[f"openai_chat/{provider_id}"] = build_openai_chat_request_body(
            _request(fixture, model="a-chat-model"),
            reasoning=policy,
            policy=OPENAI_CHAT_PROFILES[provider_id].request_policy,
            postprocessors=OPENAI_CHAT_PROFILES[provider_id].request_postprocessors,
            provider_id=provider_id,
        )
    return out


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=1, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# OFF: every body byte-identical across 8 shapes x 4 doors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fixture", sorted(CORPUS))
@pytest.mark.parametrize("policy_name", sorted(POLICIES))
def test_with_the_setting_off_every_body_is_byte_identical(
    fixture: str, policy_name: str
) -> None:
    """``origin=None`` is the shipped default, and it moves nothing.

    The comparison is against the same builder called **without the keyword at
    all** -- which is the v8.15.1 call shape -- so the claim is "the new keyword
    changes no byte when it is absent", proven rather than argued.
    """

    off = _bodies(fixture, policy_name, origin=None)
    legacy = _bodies(fixture, policy_name, origin=OMIT)
    for door, body in off.items():
        assert _digest(body) == _digest(legacy[door]), door


@pytest.mark.parametrize("fixture", sorted(CORPUS))
def test_with_the_setting_off_no_carrier_note_is_written(fixture: str) -> None:
    """A note that appears when the setting is off would move every stored body."""

    notes: dict[str, str] = {}
    build_responses_request_body(
        _request(fixture, model="gpt-5.6-terra"),
        reasoning=ReasoningPolicy.off(),
        wire_notes=notes,
        origin=None,
    )
    assert not [key for key in notes if key.startswith("history_carrier_")]


# --------------------------------------------------------------------------- #
# ON: only ``input`` gains items, and only on the Responses door
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fixture", sorted(CORPUS))
@pytest.mark.parametrize("policy_name", sorted(POLICIES))
def test_with_the_setting_on_only_input_gains_items(
    fixture: str, policy_name: str
) -> None:
    """Exactly one key differs, and inside it exactly the added items.

    ``model``, ``store``, ``stream``, ``parallel_tool_calls``,
    ``max_output_tokens``, ``prompt_cache_key``, ``instructions``, ``tools``,
    ``tool_choice``, ``reasoning`` and ``include`` are byte-identical.
    """

    off = _bodies(fixture, policy_name, origin=None)
    on = _bodies(fixture, policy_name, origin=ORIGIN)

    for door in off:
        if not door.startswith("responses/"):
            assert _digest(on[door]) == _digest(off[door]), door
            continue
        before = off[door]
        after = on[door]
        assert set(before) == set(after), door
        for key in before:
            if key == "input":
                continue
            assert _canonical(before[key]) == _canonical(after[key]), (door, key)
        added = [item for item in after["input"] if item.get("type") == "reasoning"]
        if added:
            # Removing exactly the added items restores the setting-off body.
            without = [
                item for item in after["input"] if item.get("type") != "reasoning"
            ]
            assert _canonical(without) == _canonical(before["input"]), door


def test_the_anthropic_and_chat_doors_never_move() -> None:
    """This PR adds no code to either door, so neither body can differ."""

    for fixture in CORPUS:
        for policy_name in POLICIES:
            off = _bodies(fixture, policy_name, origin=None)
            on = _bodies(fixture, policy_name, origin=ORIGIN)
            for door in off:
                if door.startswith("responses/"):
                    continue
                assert _digest(on[door]) == _digest(off[door]), (fixture, door)


# --------------------------------------------------------------------------- #
# The prompt-cache prefix: the digest of ``input[:k]`` never moves
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fixture", sorted(CORPUS))
def test_the_prompt_cache_prefix_before_every_insertion_is_byte_stable(
    fixture: str,
) -> None:
    """IV.7's second clause, first half: nothing that was there moves.

    The added items sit at the head of *their own turn's* run, and a turn's
    items are computed from that turn's blocks -- which the client already
    sent. So every item that the setting-off body carried keeps its bytes and
    its relative order: walking the setting-on ``input`` and skipping the
    ``reasoning`` items reproduces the setting-off ``input`` item for item.
    """

    off = _bodies(fixture, "off", origin=None)["responses/chatgpt_oauth"]
    on = _bodies(fixture, "off", origin=ORIGIN)["responses/chatgpt_oauth"]

    off_items = off["input"]
    on_items = on["input"]
    i = 0
    j = 0
    while i < len(on_items):
        if on_items[i].get("type") == "reasoning":
            i += 1
            continue
        assert _canonical(on_items[i]) == _canonical(off_items[j]), (i, j)
        i += 1
        j += 1
    assert j == len(off_items)


#: A conversation whose assistant turns each carry a carrier, cut at every
#: user turn -- the three request bodies a real session would send.
CACHE_CONVERSATION: list[dict[str, Any]] = [
    {"role": "user", "content": "one"},
    {
        "role": "assistant",
        "content": [
            {"type": "redacted_thinking", "data": _carrier(E1, "rs_1")},
            _tool_use("call_1"),
        ],
    },
    _tool_result("call_1"),
    {
        "role": "assistant",
        "content": [
            _thinking("two", _carrier(E2, "rs_2")),
            {"type": "text", "text": "done"},
        ],
    },
    {"role": "user", "content": "two"},
]


def _body_for(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return build_chatgpt_oauth_request_body(
        MessagesRequest.model_validate(
            {"model": "gpt-5.6-terra", "max_tokens": 4096, "messages": messages}
        ),
        reasoning=ReasoningPolicy.off(),
        origin=ORIGIN,
    )


def test_each_turns_body_extends_the_previous_turns_body() -> None:
    """IV.7's second clause, second half -- the property the cache needs.

    A prompt cache is computed over the request prefix, so what matters is that
    turn N's body *starts with* turn N-1's body. It does, and for the reason the
    design is built on: a carrier is derived from the client's transcript, which
    is stable, so the same item is inserted at the same place on every later
    turn.

    Turn 1's body is ``[message(user)]``. Turn 2's is that plus
    ``[reasoning(rs_1), function_call, function_call_output]``. Turn 3's is turn
    2's whole body plus ``[reasoning(rs_2), message(assistant), message(user)]``.
    Each is a strict prefix of the next, so every earlier turn's bytes stay
    cached.
    """

    cuts = [1, 3, 5]
    bodies = [_body_for(CACHE_CONVERSATION[:cut]) for cut in cuts]

    assert [item.get("type") for item in bodies[0]["input"]] == ["message"]
    assert [item.get("type") for item in bodies[1]["input"]] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert [item.get("type") for item in bodies[2]["input"]] == [
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "message",
        "message",
    ]

    for earlier, later in itertools.pairwise(bodies):
        assert _canonical(later["input"][: len(earlier["input"])]) == _canonical(
            earlier["input"]
        ), "turn N's body must start with turn N-1's body"


def test_a_history_with_one_carrier_in_its_last_turn_adds_nothing_earlier() -> None:
    """The strongest form: the whole earlier conversation is byte-identical.

    The carrier is in the only assistant turn, so the first item of the
    setting-on body's ``input`` is the first item of the setting-off body's
    assistant run -- every byte before it is the same prefix.
    """

    fixture = "4_redacted_thinking_carrying_a_carrier"
    off = _bodies(fixture, "off", origin=None)["responses/chatgpt_oauth"]
    on = _bodies(fixture, "off", origin=ORIGIN)["responses/chatgpt_oauth"]
    first_assistant = next(
        index
        for index, item in enumerate(off["input"])
        if item.get("type") == "message" and item.get("role") == "assistant"
    )
    assert on["input"][first_assistant]["type"] == "reasoning"
    assert _digest(on["input"][:first_assistant]) == _digest(
        off["input"][:first_assistant]
    )
