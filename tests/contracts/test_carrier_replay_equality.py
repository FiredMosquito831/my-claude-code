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

#: A third host, matching neither of the two above. A carrier minted for it is
#: ``CARRIER_FOREIGN`` at both, which is what §7.3's three-hop fixture needs:
#: the envelope is refused by A *and* by B, so both replay the text.
THIRD_ORIGIN = ReplayOrigin(
    provider="inferhub",
    protocol="responses",
    endpoint="https://api.inferhub.dev/v1",
    connection=connection_id("a-third-account"),
    model="a-third-model",
)

E1 = "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"
E2 = "gAAAAABpQ2FycmllclR3b19FbmNyeXB0ZWRDb250ZW50X2FiY2RlZmdoaWo"

#: The readable reasoning every plaintext shape carries. A string no other
#: fixture uses, so "the text is in ``input``" and "the text is nowhere" are
#: both unambiguous.
PLAINTEXT = "the previous model's readable plan"


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


def _carrier(
    encrypted: str,
    item_id: str | None = "rs_1",
    origin: ReplayOrigin = ORIGIN,
) -> str:
    item: dict[str, Any] = {
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "content": [],
        "encrypted_content": encrypted,
    }
    if item_id is not None:
        item = {"id": item_id, **item}
    carrier = encode_carrier(origin, item)
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
    # ------------------------------------------------------------------ #
    # 8.17.0's shapes. The eight above carry no plaintext-reasoning case
    # that is not already a carrier case, so the plaintext pass needs its
    # own: every branch of the dynamic rule, plus position and ordering.
    # ------------------------------------------------------------------ #
    "9_plain_thinking_no_carrier": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking(PLAINTEXT, "sig-from-anthropic"),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "10_plain_thinking_blank_text": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("   ", "sig-from-anthropic"),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "11_foreign_carrier_with_text": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                # Minted for OPENCODE_ORIGIN, so the ChatGPT door refuses the
                # envelope and the text must take the plaintext path.
                _thinking(PLAINTEXT, _carrier(E1, "rs_1", OPENCODE_ORIGIN)),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "12_malformed_carrier_with_text": [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking(PLAINTEXT, "mcc:reasoning:v1:" + "not-base64!!!"),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "13_string_turn_reasoning_content": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done", "reasoning_content": PLAINTEXT},
        {"role": "user", "content": "again"},
    ],
    "14_carrier_and_plain_in_one_turn": [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier(E1, "rs_1")),
                _thinking(PLAINTEXT, "sig-from-anthropic"),
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "user", "content": "again"},
    ],
    "15_multi_turn_tool_loop_with_plaintext": [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                _thinking("first thought", "sig-1"),
                _tool_use("call_1"),
            ],
        },
        _tool_result("call_1"),
        {
            "role": "assistant",
            "content": [
                _thinking("second thought", "sig-2"),
                _tool_use("call_2", "Grep"),
            ],
        },
        _tool_result("call_2"),
        {
            "role": "assistant",
            "content": [
                _thinking("third thought", "sig-3"),
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
    provider_id: str,
    request: MessagesRequest,
    policy: ReasoningPolicy,
    origin: Any,
    plaintext_replay: Any = OMIT,
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
    if plaintext_replay is not OMIT:
        kwargs["plaintext_replay"] = plaintext_replay
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


def _bodies(
    fixture: str,
    policy_name: str,
    origin: Any,
    plaintext_replay: Any = OMIT,
) -> dict[str, Any]:
    """Every door's body for one corpus shape, at one setting.

    ``plaintext_replay`` (8.17.0) is passed through to the two Responses
    senders only. ``OMIT`` keeps the v8.16.3 call shape, which is what the
    equality proof compares against: *"the new keyword changes no byte when it
    is absent"*, measured rather than argued.
    """

    policy = POLICIES[policy_name]
    out: dict[str, Any] = {}
    chatgpt_kwargs: dict[str, Any] = {}
    if origin is not OMIT:
        chatgpt_kwargs["origin"] = origin
    if plaintext_replay is not OMIT:
        chatgpt_kwargs["plaintext_replay"] = plaintext_replay
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
            plaintext_replay,
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

    8.17.0 extends the same claim to ``plaintext_replay``: with it explicitly
    ``False`` every body must equal the body built by a caller that never heard
    of the keyword, on all 15 shapes x 6 doors x 2 policies.
    """

    off = _bodies(fixture, policy_name, origin=None, plaintext_replay=False)
    legacy = _bodies(fixture, policy_name, origin=OMIT, plaintext_replay=OMIT)
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
        plaintext_replay=False,
    )
    assert not [key for key in notes if key.startswith("history_carrier_")]
    assert not [key for key in notes if key.startswith("history_plaintext_")]


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

    8.17.0's second producer is held to the same claim, and to one more: every
    item it adds has exactly ``{"summary", "type"}`` as its key set -- no
    ``id``, no ``encrypted_content``, no ``status``, no ``content``, no
    ``role``. That is LiteLLM's shape (an item without an id is always
    accepted; one with a foreign id 404s), asserted on the whole key set rather
    than a "contains" check.
    """

    off = _bodies(fixture, policy_name, origin=None)
    on = _bodies(fixture, policy_name, origin=ORIGIN, plaintext_replay=True)
    on_carrier_only = _bodies(fixture, policy_name, origin=ORIGIN)

    for door in off:
        if not door.startswith("responses/"):
            # The plaintext pass is a property of the Responses door. The
            # Messages and Chat bodies are byte-identical with the setting on
            # and off -- §8 case 9.
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
        # The plaintext pass adds items only where the carrier pass did not
        # already replay that reasoning: the carrier-only body is a subset of
        # the plaintext-on body, item for item.
        carrier_items = [
            item
            for item in on_carrier_only[door]["input"]
            if item.get("type") == "reasoning"
        ]
        for item in carrier_items:
            assert item in after["input"], (door, item)
        # Every item the plaintext pass itself added carries exactly two keys.
        for item in added:
            if "encrypted_content" in item:
                continue
            assert sorted(item) == ["summary", "type"], (door, item)
            assert item["summary"] and all(
                sorted(part) == ["text", "type"] and part["type"] == "summary_text"
                for part in item["summary"]
            ), (door, item)


def test_the_anthropic_and_chat_doors_never_move() -> None:
    """This PR adds no code to either door, so neither body can differ."""

    for fixture in CORPUS:
        for policy_name in POLICIES:
            off = _bodies(fixture, policy_name, origin=None)
            on = _bodies(fixture, policy_name, origin=ORIGIN, plaintext_replay=True)
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

    Run for both producers independently and for both together, because each
    inserts at the same place by the same rule.
    """

    for origin, plaintext_replay in (
        (ORIGIN, False),
        (None, True),
        (ORIGIN, True),
    ):
        off = _bodies(fixture, "off", origin=None)["responses/chatgpt_oauth"]
        on = _bodies(fixture, "off", origin=origin, plaintext_replay=plaintext_replay)[
            "responses/chatgpt_oauth"
        ]

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


def _body_for(
    messages: list[dict[str, Any]],
    *,
    origin: ReplayOrigin | None = ORIGIN,
    plaintext_replay: bool = False,
) -> dict[str, Any]:
    return build_chatgpt_oauth_request_body(
        MessagesRequest.model_validate(
            {"model": "gpt-5.6-terra", "max_tokens": 4096, "messages": messages}
        ),
        reasoning=ReasoningPolicy.off(),
        origin=origin,
        plaintext_replay=plaintext_replay,
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


# --------------------------------------------------------------------------- #
# 8.17.0's cache proofs: the plaintext pass is stable across turns and hops
# --------------------------------------------------------------------------- #

#: The same three-turn shape, with a plaintext block on every assistant turn and
#: no carrier anywhere -- the conversation the plaintext pass is alone in.
PLAINTEXT_CONVERSATION: list[dict[str, Any]] = [
    {"role": "user", "content": "one"},
    {
        "role": "assistant",
        "content": [
            _thinking("first thought", "sig-1"),
            _tool_use("call_1"),
        ],
    },
    _tool_result("call_1"),
    {
        "role": "assistant",
        "content": [
            _thinking("second thought", "sig-2"),
            {"type": "text", "text": "done"},
        ],
    },
    {"role": "user", "content": "two"},
]


def test_each_turns_body_extends_the_previous_turns_body_with_plaintext() -> None:
    """§7.4, second run: the same property with the plaintext pass alone.

    The new items appear at the same place on every turn -- at the head of
    their own turn's run, computed from that turn's blocks -- so
    ``later["input"][:len(earlier["input"])] == earlier["input"]`` exactly.
    Nothing an earlier turn sent moves when a later turn is added.
    """

    cuts = [1, 3, 5]
    bodies = [
        _body_for(PLAINTEXT_CONVERSATION[:cut], origin=None, plaintext_replay=True)
        for cut in cuts
    ]

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
    # And the items are the text items, not carriers.
    assert bodies[1]["input"][1] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "first thought"}],
    }
    assert bodies[2]["input"][4] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "second thought"}],
    }

    for earlier, later in itertools.pairwise(bodies):
        assert _canonical(later["input"][: len(earlier["input"])]) == _canonical(
            earlier["input"]
        ), "turn N's body must start with turn N-1's body"


def test_a_hop_through_another_host_changes_nothing_for_the_original_host() -> None:
    """§7.3, the three-hop A -> B -> A, in the user's own words.

    *"if we strip thinking for encrypted models do it only for that request
    without affecting the requests for non encrypted models ... so if we return
    to the original model we don't break prefixes."*

    The history's one block carries a carrier minted for **another** host, so
    both A and B refuse the envelope and both replay the **text**. The plaintext
    item is portable by definition, so unlike a carrier it is *identical* for A
    and B -- which is stronger than the strip case, where the two destinations
    legitimately differ.

    The property: A's body after the hop is byte-identical to what it would
    have been if the hop had never happened, and the history itself was never
    mutated.
    """

    history = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                # Minted for a THIRD host, so both A and B refuse the envelope
                # and both replay the text.
                _thinking(PLAINTEXT, _carrier(E1, "rs_1", THIRD_ORIGIN)),
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "two"},
    ]
    control_history = copy.deepcopy(history)
    before = json.dumps(history)

    control = _body_for(control_history, origin=ORIGIN, plaintext_replay=True)
    b_body = _body_for(history, origin=OPENCODE_ORIGIN, plaintext_replay=True)
    a_body = _body_for(history, origin=ORIGIN, plaintext_replay=True)

    # A is unaffected by the hop.
    assert json.dumps(a_body) == json.dumps(control)
    # And the history was never mutated by any of the three builds.
    assert json.dumps(history) == before
    assert json.dumps(control_history) == before

    # The three assertions that make it a *hop* test rather than a purity test.
    expected = {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": PLAINTEXT}],
    }
    assert b_body["input"][1] == expected
    assert a_body["input"][1] == expected
    assert a_body["input"][1] == b_body["input"][1]


def test_a_history_whose_carrier_matches_one_host_gives_two_correct_bodies() -> None:
    """§7.3's other half: where A and B legitimately differ.

    A history whose block carries **A's** carrier. A replays the host's own
    item; B refuses it and replays the **text** instead. Two different, correct
    bodies from one history -- and neither is a mutation of the other.
    """

    history = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                _thinking(PLAINTEXT, _carrier(E1, "rs_1", ORIGIN)),
                {"type": "text", "text": "ok"},
            ],
        },
        {"role": "user", "content": "two"},
    ]
    a_body = _body_for(history, origin=ORIGIN, plaintext_replay=True)
    b_body = _body_for(history, origin=OPENCODE_ORIGIN, plaintext_replay=True)

    # A: the host's own item, verbatim, and no second summary item.
    a_reasoning = [item for item in a_body["input"] if item.get("type") == "reasoning"]
    assert len(a_reasoning) == 1
    assert "encrypted_content" in a_reasoning[0]
    assert "summary" not in a_reasoning[0] or a_reasoning[0]["summary"] == []

    # B: the envelope refused, the text kept.
    b_reasoning = [item for item in b_body["input"] if item.get("type") == "reasoning"]
    assert b_reasoning == [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": PLAINTEXT}],
        }
    ]
    # The envelope's own bytes never reach B.
    assert E1 not in json.dumps(b_body)
