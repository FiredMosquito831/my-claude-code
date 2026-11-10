"""``accepts()`` compares ``model`` on its normalised form (8.18.2).

The defect this file pins: ``accepts()`` compared all five origin fields
**byte-exactly**, and ``model`` is one of them. MCC's own configuration can
spell one model two ways between turns -- ``claude-haiku-4-5`` on one and
``claude-haiku-4-5-20251001`` on the next, because a fallback chain's rungs
differ only in the dated suffix or because a route was edited between turns --
and then MCC refuses **its own** carrier. The continuity chain breaks silently;
the only symptom is a ``history_carrier_foreign`` count nobody reads.

The fix, and its whole extent: ``normalised_model`` trims, lower-cases and
removes a trailing ``-YYYYMMDD``. Nothing else. It is applied to **both** sides
at **compare time** and nowhere else -- never at mint, so every carrier already
in a client's transcript still decodes and still matches.

**What this file deliberately does NOT do.** It does not strip a vendor prefix
(measured: 0 joins, ~195 collisions -- it would merge
``bytedance/doubao-seed-2.0-code`` with ``volcengine/…`` on ``zenmux``) and it
does not strip a 4-digit suffix (measured: 11 collisions --
``mistral-medium-2505`` vs ``mistral-medium-2604``). Cases 5 and 6 below assert
those refusals, so a future "helpful" widening turns this file red.

**The mutation controls** (each must turn a named test red; the PR body records
the run):

* **M1** strip the vendor prefix too -> ``test_a_vendor_prefixed_model_...``
* **M2** compare the raw model (drop the suffix strip) -> cases 1, 2, 3
* **M3** strip any trailing digits -> case 5 and the ``seed-1-6`` unit row
* **M4** normalise at mint instead of at accept -> the pre-8.18.2 envelope
* **M5** drop the lower-case step -> case 3
* **M6** normalise the other four fields too -> case 9
* **M7** drop the empty-normalised-form guard -> case 7
"""

import copy
import json
from typing import Any

import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_responses import (
    ReplayOrigin,
    accepts,
    connection_id,
    decode_carrier,
    encode_carrier,
    normalised_model,
)
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_REPLAYED,
    build_responses_request_body,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

#: The host every carrier below was minted for.
ORIGIN = ReplayOrigin(
    provider="chatgpt_oauth",
    protocol="responses",
    endpoint="https://chatgpt.com/backend-api",
    connection=connection_id("account-one"),
    model="claude-sonnet-4-5",
)

#: An opaque payload shaped like the real ``encrypted_content``.
E1 = "gAAAAABpQ2Fycmllck9uZV9FbmNyeXB0ZWRDb250ZW50XzAxMjM0NTY3ODlh"


def _origin(model: str = "claude-sonnet-4-5", **overrides: str) -> ReplayOrigin:
    """``ORIGIN`` with a different ``model`` (and any other field overridden)."""

    return ReplayOrigin(**{**ORIGIN.to_dict(), "model": model, **overrides})


def _reasoning_item(item_id: str = "rs_1", encrypted: str = E1) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "content": [],
        "encrypted_content": encrypted,
    }


def _carrier(model: str, item_id: str = "rs_1") -> str:
    carrier = encode_carrier(_origin(model), _reasoning_item(item_id))
    assert carrier is not None
    return carrier


def _request(messages: list[dict[str, Any]], *, model: str = "claude-sonnet-4-5"):
    return MessagesRequest.model_validate(
        {"model": model, "max_tokens": 4096, "messages": copy.deepcopy(messages)}
    )


def _thinking(text: str, signature: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def _redacted(data: str) -> dict[str, Any]:
    return {"type": "redacted_thinking", "data": data}


def _body(
    messages: list[dict[str, Any]],
    *,
    model: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The ChatGPT Responses body for one history, sent to ``model``.

    ``origin`` is built from the **destination** model, exactly as the sender
    does: ``_replay_origin(credentials, request.model)``. That is what makes the
    two sides disagree only when MCC's configuration changed between turns.
    """

    notes: dict[str, str] = {}
    body = build_responses_request_body(
        _request(messages, model=model),
        reasoning=ReasoningPolicy.off(),
        wire_notes=notes,
        origin=_origin(model),
    )
    return body, notes


def _reasoning_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in body["input"] if item.get("type") == "reasoning"]


#: One assistant turn whose ``thinking`` block carries a carrier minted under
#: ``claude-sonnet-4-5``. The block's text is decoration; the envelope decides.
CARRIER_HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "hello"},
    {
        "role": "assistant",
        "content": [
            _thinking("a summary", _carrier("claude-sonnet-4-5")),
            {"type": "text", "text": "hi"},
        ],
    },
    {"role": "user", "content": "again"},
]


# --------------------------------------------------------------------------- #
# §6.2 — the seven rows, asserted directly on ``accepts()``
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "minted, destination, must_match",
    [
        # Unchanged: the same spelling still matches.
        ("model-A", "model-A", True),
        # Unchanged: two genuinely different models still refuse.
        ("model-A", "model-B", False),
        # The fix: an alias and its own dated snapshot.
        ("model-A", "model-A-20260101", True),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5", True),
        # The fix: case only.
        ("GPT-6.1-Sol", "gpt-6.1-sol", True),
        # Deliberate: a 4-digit suffix is NOT stripped.
        ("deepseek-v4.1-flash", "deepseek-v4.1-flash-1017", False),
        # Deliberate: a vendor prefix is NOT stripped.
        (
            "bytedance/doubao-seed-2.0-code",
            "volcengine/doubao-seed-2.0-code",
            False,
        ),
    ],
)
def test_the_seven_rows_of_the_before_after_table(
    minted: str, destination: str, must_match: bool
) -> None:
    """§6.2, row by row. ``source`` is the mint, ``destination`` the next turn.

    Three rows change verdict (the fix) and four do not (two unchanged, two
    deliberate refusals). A row that moves the wrong way is a regression in
    either direction: too strict and MCC still refuses its own carrier, too
    loose and a carrier reaches a model that did not mint it.
    """

    source = _origin(minted)
    target = _origin(destination)
    assert accepts(target, source) is must_match
    # And the predicate is symmetric: a carrier minted under either spelling
    # matches the other, which is what makes the fix work in both directions.
    assert accepts(source, target) is must_match


def test_normalised_model_is_the_whole_transformation() -> None:
    """§7 case 11: the unit table, including the two refusals.

    ``normalised_model`` is exported so a future door can reuse it and so this
    table can be asserted without going through ``accepts()``.
    """

    table = {
        "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
        "claude-haiku-4-5-20251001": "claude-haiku-4-5",
        "gpt-6-luna": "gpt-6-luna",
        # 4 digits: NOT stripped -- the width is measured, not chosen.
        "deepseek-v4.1-flash-1017": "deepseek-v4.1-flash-1017",
        "mistral-medium-2505": "mistral-medium-2505",
        "seed-1-6-250615": "seed-1-6-250615",
        # A vendor prefix is NOT stripped.
        "anthropic/claude-sonnet-4-5": "anthropic/claude-sonnet-4-5",
        # Case and whitespace are.
        "GPT-6.1-Sol": "gpt-6.1-sol",
        "  gpt-6.1-sol  ": "gpt-6.1-sol",
        "gpt-6.1-sol\n": "gpt-6.1-sol",
        # Entirely a suffix: the degenerate case the empty guard exists for.
        "-20250929": "",
    }
    for value, expected in table.items():
        assert normalised_model(value) == expected, value


def test_an_all_suffix_model_never_matches_anything() -> None:
    """§2.5 / case 7: the guard is in ``accepts``, not only in ``is_complete``.

    ``is_complete()`` tests the RAW fields, so ``"-20250929"`` passes it. Its
    normalised form is empty, and two empty forms would otherwise compare equal
    -- which would make every all-suffix model match every other one.
    """

    all_suffix = _origin("-20250929")
    another = _origin("-20260101")

    assert all_suffix.is_complete() is True
    assert normalised_model("-20250929") == ""

    assert accepts(all_suffix, all_suffix) is False
    assert accepts(all_suffix, another) is False
    assert accepts(another, all_suffix) is False


# --------------------------------------------------------------------------- #
# The other four fields are NOT normalised (§7 case 9, mutation M6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field, value",
    [
        ("provider", "opencode"),
        ("protocol", "messages"),
        ("endpoint", "https://opencode.ai/zen/v1"),
        ("connection", connection_id("another-account")),
    ],
)
def test_the_other_four_fields_stay_byte_exact(field: str, value: str) -> None:
    """M6: normalising these too would widen the guard for no measured benefit.

    ``provider`` is a catalogue key, ``protocol`` is one of two values,
    ``endpoint`` is already normalised at mint, ``connection`` is a hash that
    must not drift. The model is spelled identically on both sides here, so the
    **only** reason to refuse is the field under test.
    """

    source = _origin("claude-sonnet-4-5")
    destination = _origin("claude-sonnet-4-5", **{field: value})
    assert accepts(destination, source) is False


@pytest.mark.parametrize(
    "field, value",
    [
        # Each of these differs from the minted value ONLY by the kind of
        # folding a careless "normalise the other fields too" would apply:
        # case, surrounding whitespace, or a trailing slash. Byte-exact
        # comparison refuses every one of them; a normalising comparison would
        # accept all four. This is the mutation control M6 needs -- the case
        # above uses genuinely different values, which no normalisation joins,
        # so it could never catch M6 on its own.
        ("provider", "ChatGPT_OAuth"),
        ("protocol", "Responses"),
        ("endpoint", "https://chatgpt.com/backend-api/"),
        ("connection", connection_id("account-one").upper()),
    ],
)
def test_a_near_miss_on_another_field_is_still_refused(field: str, value: str) -> None:
    """M6's control: the four fields are compared with no folding at all.

    ``endpoint`` looks already normalised because :func:`normalise_endpoint`
    strips a trailing ``/`` **at mint**; the comparison is still byte-exact, and
    this row proves it rather than assuming it.
    """

    source = _origin("claude-sonnet-4-5")
    destination = _origin("claude-sonnet-4-5", **{field: value})
    assert destination.to_dict()[field] != source.to_dict()[field]
    assert accepts(destination, source) is False
    assert accepts(source, destination) is False


def test_an_empty_raw_field_still_fails_before_normalisation() -> None:
    """``is_complete()`` keeps testing the RAW fields -- M7's sibling.

    A normalised comparison must not turn an empty raw field into a wildcard,
    and must not turn it into a match either.
    """

    for field in ("provider", "protocol", "endpoint", "connection", "model"):
        source = _origin()
        destination = ReplayOrigin(**{**source.to_dict(), field: ""})
        assert destination.is_complete() is False
        assert accepts(destination, source) is False
        assert accepts(source, destination) is False


# --------------------------------------------------------------------------- #
# Case 1-3: the carrier IS replayed when the spelling changes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "minted, destination",
    [
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
        ("GPT-6.1-Sol", "gpt-6.1-sol"),
    ],
)
def test_a_carrier_replays_across_two_spellings_of_one_model(
    minted: str, destination: str
) -> None:
    """§7 cases 1-3: the whole body, not a "contains" check.

    The history carries a carrier minted under ``minted``; the turn is routed to
    ``destination``. The ``reasoning`` item must be at the head of its own
    turn's run, ``history_carrier_replayed`` must be 1, and
    ``history_carrier_foreign`` must be **absent** -- not zero, absent, because
    a zero in ``params.wire`` would be a note MCC never wrote.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier(minted)),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _body(history, model=destination)

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "reasoning",
        "message",
        "message",
    ]
    replayed = _reasoning_items(body)
    assert len(replayed) == 1
    # The host's own item, verbatim: id and ciphertext intact.
    assert replayed[0]["id"] == "rs_1"
    assert replayed[0]["encrypted_content"] == E1
    assert notes[HISTORY_CARRIER_REPLAYED] == (
        "replayed 1 reasoning carrier from the client's history"
    )
    assert HISTORY_CARRIER_FOREIGN not in notes


# --------------------------------------------------------------------------- #
# Case 4-6: the carrier is REFUSED where the transformation does not reach
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "minted, destination",
    [
        # Two genuinely different models.
        ("gpt-6-luna", "gpt-6-sol"),
        # A 4-digit suffix (M3 would join these).
        ("deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"),
        # A vendor prefix (M1 would join these).
        ("anthropic/claude-sonnet-4-5", "claude-sonnet-4-5"),
        # The vendor-prefix collision the measurement found: two DIFFERENT
        # models that a prefix strip would merge.
        (
            "bytedance/doubao-seed-2.0-code",
            "volcengine/doubao-seed-2.0-code",
        ),
    ],
)
def test_a_carrier_is_refused_where_the_transformation_does_not_reach(
    minted: str, destination: str
) -> None:
    """§7 cases 4-6, plus the measured collision, at the body level.

    No ``reasoning`` item, ``history_carrier_foreign`` == 1, and the envelope's
    own bytes are nowhere in the body -- the fake-blob failure Bifrost
    documents is that a refused carrier is sent as a raw string.
    """

    history = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier(minted)),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    body, notes = _body(history, model=destination)

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "message",
        "message",
    ]
    assert _reasoning_items(body) == []
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
    assert HISTORY_CARRIER_REPLAYED not in notes
    assert _carrier(minted) not in json.dumps(body)


# --------------------------------------------------------------------------- #
# Case 8: a carrier minted BEFORE 8.18.2 still decodes and still matches
# --------------------------------------------------------------------------- #


def test_a_pre_8_18_2_envelope_still_decodes_and_still_matches() -> None:
    """M4: normalising at MINT would break every carrier already in a transcript.

    The envelope below is built by hand, byte for byte, the way v8.18.1's
    ``encode_carrier`` built it -- ``origin.to_dict()`` verbatim, the raw model
    spelling, no normalisation anywhere. It is not produced by this tree's
    encoder, so it cannot silently follow a mint-side change.

    Three claims: it decodes, its ``origin.model`` is still the exact string the
    old release wrote (the mint did not move), and it is accepted by a
    destination that spells the model the other way.
    """

    # The v8.18.1 envelope, hand-built: base64url(compact JSON), padding off.
    import base64

    payload = {
        "origin": {
            "provider": "chatgpt_oauth",
            "protocol": "responses",
            "endpoint": "https://chatgpt.com/backend-api",
            "connection": connection_id("account-one"),
            "model": "claude-sonnet-4-5-20250929",
        },
        "native": _reasoning_item(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    legacy = "mcc:reasoning:v1:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip(
        "="
    )

    decoded = decode_carrier(legacy)
    assert decoded is not None
    # The mint's bytes did not move: the envelope still carries the raw spelling.
    assert decoded.origin.model == "claude-sonnet-4-5-20250929"
    assert decoded.origin.to_dict() == payload["origin"]

    # And it matches a destination that spells the model as the alias.
    assert accepts(_origin("claude-sonnet-4-5"), decoded.origin) is True

    # Through the real builder, the item is replayed verbatim.
    body, notes = _body(
        [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    _thinking("a summary", legacy),
                    {"type": "text", "text": "hi"},
                ],
            },
            {"role": "user", "content": "again"},
        ],
        model="claude-sonnet-4-5",
    )
    assert [item.get("type") for item in body["input"]] == [
        "message",
        "reasoning",
        "message",
        "message",
    ]
    assert _reasoning_items(body)[0]["id"] == "rs_1"
    assert notes[HISTORY_CARRIER_REPLAYED] == (
        "replayed 1 reasoning carrier from the client's history"
    )


def test_the_mint_writes_the_raw_model_unchanged() -> None:
    """The other half of M4: ``encode_carrier`` is untouched.

    A carrier minted for a dated model must contain that dated model, not its
    normalised form. If this ever fails, every carrier's bytes moved.
    """

    carrier = _carrier("claude-sonnet-4-5-20250929")
    decoded = decode_carrier(carrier)
    assert decoded is not None
    assert decoded.origin.model == "claude-sonnet-4-5-20250929"
    assert normalised_model(decoded.origin.model) == "claude-sonnet-4-5"


# --------------------------------------------------------------------------- #
# Case 10: a history with no carrier is byte-identical
# --------------------------------------------------------------------------- #


def test_a_history_with_no_carrier_is_byte_identical() -> None:
    """The pass is reached only through a ``mcc:reasoning:v1:`` value.

    A body whose history carries none never calls ``accepts()``, so the fix
    cannot move a byte of it. Asserted by building the same history twice, once
    with a plain Anthropic signature (which is not a carrier) and once with no
    reasoning block at all, and comparing the canonical JSON.
    """

    plain = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                _thinking("a thought", "sig-from-anthropic"),
                {"type": "text", "text": "hi"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    # The same history, but the model spelling changes between the two builds:
    # nothing in the body may move, because no carrier is involved.
    alias_body, alias_notes = _body(plain, model="claude-sonnet-4-5")
    dated_body, dated_notes = _body(plain, model="claude-sonnet-4-5-20250929")

    # ``model`` is the request's own routed name, so it legitimately differs;
    # everything else must be identical.
    alias_body.pop("model")
    dated_body.pop("model")
    assert json.dumps(alias_body, sort_keys=True) == json.dumps(
        dated_body, sort_keys=True
    )
    assert not [key for key in alias_notes if key.startswith("history_carrier_")]
    assert not [key for key in dated_notes if key.startswith("history_carrier_")]


# --------------------------------------------------------------------------- #
# The end-to-end three-turn conversation, and its negative
# --------------------------------------------------------------------------- #


def test_a_mid_conversation_spelling_change_still_replays() -> None:
    """§7.3: turn 1 mints under the alias, turn 3 routes the dated snapshot.

    The conversation is cut at each user turn, which is what a real session
    sends. Turn 3's history carries turn 2's carrier, minted under
    ``claude-sonnet-4-5``, and the turn is routed to
    ``claude-sonnet-4-5-20250929``. Before 8.18.2 that carrier was refused and
    the reasoning was lost; now it replays.
    """

    conversation = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier("claude-sonnet-4-5")),
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "user", "content": "two"},
    ]

    body, notes = _body(conversation, model="claude-sonnet-4-5-20250929")

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "reasoning",
        "message",
        "message",
    ]
    assert notes[HISTORY_CARRIER_REPLAYED] == (
        "replayed 1 reasoning carrier from the client's history"
    )
    assert HISTORY_CARRIER_FOREIGN not in notes


def test_the_same_conversation_routed_to_another_model_does_not_replay() -> None:
    """§7.4, the negative: the guard still refuses a genuinely different model.

    Without this, "the fix replays more" could be satisfied by replaying
    everything -- which is the cross-model 400 OmniRoute documents.
    """

    conversation = [
        {"role": "user", "content": "one"},
        {
            "role": "assistant",
            "content": [
                _thinking("a summary", _carrier("claude-sonnet-4-5")),
                {"type": "text", "text": "done"},
            ],
        },
        {"role": "user", "content": "two"},
    ]

    body, notes = _body(conversation, model="gpt-6-luna")

    assert [item.get("type") for item in body["input"]] == [
        "message",
        "message",
        "message",
    ]
    assert _reasoning_items(body) == []
    assert notes[HISTORY_CARRIER_FOREIGN] == (
        "dropped 1 reasoning carrier minted by another host"
    )
