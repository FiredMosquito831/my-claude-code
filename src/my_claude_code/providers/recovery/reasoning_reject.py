"""Learn, from a host's own 400, that it will not take a reasoning field.

The counterpart to :mod:`~my_claude_code.providers.recovery.output_cap`. That
module learns a number a provider named; this one learns a *field* a provider
refused. Both exist because the alternative -- a hand-written per-provider
verdict -- cannot be right for a gateway that fronts many models, which is
precisely what the 5.70.0 ``NO_REASONING`` audit discovered the hard way.

Evidence, not shape. A 400 is only a reasoning rejection when the provider's
own words name a reasoning field that this request actually sent. A rung that
fires on the shape of a 400 turns every unrelated rejection -- a sampling
complaint such as ``top_p is immutable for this model`` -- into a silent,
invisible loss of thinking.

The candidate set comes from :func:`~my_claude_code.core.wire_capture.is_reasoning_key`,
the fleet's single definition of "this key is a reasoning instruction". Reusing
it is what makes this net universal rather than another hardcoded list: an
encoder that starts emitting a new field is covered the day that key is added
there, and it is why the same function reads an OpenAI-chat
``reasoning_effort``, an Anthropic ``thinking`` object and a Responses
``reasoning`` block without knowing which dialect it is looking at.
"""

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from my_claude_code.config.reasoning_enum import parse_effort_enum
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.core.wire_capture import is_reasoning_key

from .complaint import is_bad_request, sampling_parameter_evidence, upstream_complaint


def rejected_effort_values(
    error: Exception, body: Mapping[str, Any], field: str
) -> set[str]:
    """The effort words this 400 proves the model will not take, if any.

    An empty set means "nothing about a *value* was proven", and the caller
    remembers the field rejection it always did. That is the answer for the
    common ``Unsupported parameter: reasoning_effort`` -- a host that names
    only the field is saying it has no such knob, and narrowing a vocabulary
    would leave MCC sending a field this host will refuse every time.

    Two readings do prove something finer, strongest first.

    **The host named its own vocabulary.** ``parse_effort_enum`` is the parser
    the *Probe capabilities* path has used since 6.52.0, and a 400 that lists
    the words it does accept answers the whole question at once: everything
    MCC can spell that is not in that list is refused. The value that was sent
    is passed as ``sent`` so that a host echoing the invalid value back inside
    its own list is not read as offering it as a rung.

    **The host named the value it was sent.** Then exactly one word is proven
    wrong, and one word is what is returned -- the smallest honest answer, and
    the whole point of this function: before 7.12.0 the caller had to blame
    the entire channel for it.
    """

    sent = effort_value_sent(body, field)
    complaint = upstream_complaint(error)
    named = parse_effort_enum(complaint, sent=sent)
    if named:
        accepted = {word.lower() for word in named}
        refused = {
            effort.value for effort in ReasoningEffort if effort.value not in accepted
        }
        if sent:
            refused.add(sent)
        return refused
    if sent and re.search(rf"\b{re.escape(sent)}\b", complaint):
        return {sent}
    return set()


def effort_value_sent(body: Mapping[str, Any], field: str) -> str:
    """The effort word this body carried for ``field``, or ``""``.

    Three shapes, all of them real: a bare string (``reasoning_effort``), a
    mapping with an ``effort`` key (``reasoning``), and either of those inside
    ``extra_body`` -- the same three places
    :func:`rejected_reasoning_field` looks for the field itself.
    """

    for container in (body, body.get("extra_body")):
        if not isinstance(container, Mapping):
            continue
        if field not in container:
            continue
        value = container[field]
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, Mapping):
            effort = value.get("effort")
            if isinstance(effort, str):
                return effort.strip().lower()
    return ""


def rejected_reasoning_field(error: Exception, body: Mapping[str, Any]) -> str | None:
    """Return the reasoning field this 400 names, if it names one we sent.

    Naming the field wins even when a sampling parameter is named alongside:
    naming the field is direct evidence, a sampling name is only a negative
    signal. When nothing was named, ``None`` -- the caller fails visibly rather
    than guessing, because retrying a degraded body trades a visible failure
    for an invisible loss of reasoning.
    """
    if not is_bad_request(error):
        return None
    complaint = upstream_complaint(error)

    candidates: list[str] = [str(key) for key in body if is_reasoning_key(str(key))]
    extra_body = body.get("extra_body")
    if isinstance(extra_body, Mapping):
        candidates.extend(str(key) for key in extra_body if is_reasoning_key(str(key)))

    for key in candidates:
        if re.search(rf"\b{re.escape(key)}\b", complaint):
            return key

    # No candidate named. A sampling complaint is called out separately by the
    # caller's log line; the answer here is the same ``None`` either way.
    if sampling_parameter_evidence(complaint) is not None:
        return None
    return None


#: A location inside the conversation history, spelled the way Anthropic's
#: validator names one (``messages.3.content.0``).
_HISTORY_LOCATION = re.compile(r"\bmessages\.\d+\.")


def complaint_is_about_the_body(complaint: str, field: str) -> bool:
    """Whether a 400 that names ``field`` faults the body, not the model.

    :func:`rejected_reasoning_field` matches the field's *word*, which is the
    right test for whether stripping the field may help this one request --
    and the wrong one for whether the model refuses the field. Two shapes of
    400 name the word and say nothing about the model at all:

    * the fault is located in the history -- ``messages.1.content.0: Invalid
      `signature` in `thinking` block``, or ``messages.1.content.0.type:
      Expected `thinking` ... When `thinking` is enabled, a final `assistant`
      message must start with a thinking block``. A block of the conversation
      is wrong; the field was accepted;
    * an extra key *inside* the field -- ``thinking.adaptive.enabled: Extra
      inputs are not permitted``. The object carried a key it should not
      have; the field itself was accepted.

    The answer decides only what is *remembered*. The retry without the field
    goes out exactly as before either way. Reads lowercased text, so it serves
    a stored fact's evidence excerpt as well as a live complaint.
    """

    text = complaint.lower()
    if _HISTORY_LOCATION.search(text):
        return True
    nested_extra = rf"\b{re.escape(field.lower())}\.[\w.\[\]]+:\s*extra\b"
    return re.search(nested_extra, text) is not None


#: A complaint naming a thinking block. ``\b`` on both sides on purpose: in
#: ``redacted_thinking`` the underscore is a word character, so the boundary
#: before ``thinking`` is absent and that block is NOT matched. It must not be:
#: a ``redacted_thinking`` block carries opaque ``data`` and no signature, so
#: dropping the host's unsigned ``thinking`` blocks would not answer a refusal
#: of one.
_THINKING_BLOCK_NAMED = re.compile(r"\bthinking\b")


def complaint_refuses_a_history_thinking_block(complaint: str) -> bool:
    """Whether a 400 is the host refusing a thinking block in the history.

    The one piece of evidence 8.12.3 acts on, and it is deliberately two
    conditions rather than one:

    * the fault is **located in the conversation history** -- ``messages.N.``,
      the spelling Anthropic's validator uses (``messages.1.content.0: Invalid
      `signature` in `thinking` block``). A 400 that names ``thinking`` at the
      top level is a refusal of the *field*, which the strip ladder already
      answers; only a history location says the block itself is the problem;
    * that complaint **names a thinking block**.

    Reads lowercased text, so it serves a stored fact's evidence excerpt as
    well as a live complaint -- the same contract
    :func:`complaint_is_about_the_body` keeps.
    """

    text = complaint.lower()
    if not _HISTORY_LOCATION.search(text):
        return False
    return _THINKING_BLOCK_NAMED.search(text) is not None


def history_carries_unsigned_thinking(messages: Any) -> bool:
    """Whether this body's history replays a thinking block with no signature.

    The third piece of evidence, and the one that keeps the fact honest: a host
    can name ``thinking`` at a history location for the opposite complaint
    (``messages.1.content.0.type: Expected `thinking` but found `text```), and
    dropping blocks is not the answer to that. Requiring the block to actually
    be there makes the fact mean "this deployment refused a block MCC sent it",
    never "this deployment said the word".

    ``True`` for an absent, non-string or blank ``signature`` -- the same test
    :func:`~my_claude_code.providers.anthropic_messages.request.drop_foreign_thinking`
    applies, read here on the body that was actually sent. ``redacted_thinking``
    is not looked at: it is dropped for every host regardless of signature.
    """

    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "thinking":
                continue
            signature = block.get("signature")
            if not isinstance(signature, str) or not signature.strip():
                return True
    return False


def clone_body_without_reasoning_field(
    body: dict[str, Any], field: str
) -> dict[str, Any] | None:
    """Return a deep clone with one reasoning field removed, or None if absent.

    ``None`` when nothing was removed, so an unchanged body is never retried.
    """
    cloned = deepcopy(body)
    removed = cloned.pop(field, None) is not None
    extra_body = cloned.get("extra_body")
    if isinstance(extra_body, dict):
        if extra_body.pop(field, None) is not None:
            removed = True
        if not extra_body:
            cloned.pop("extra_body", None)
    return cloned if removed else None


#: A location inside a Responses ``input`` array, spelled the way a validator
#: names one -- ``input[3]``, ``input.3``, ``input/3``. The three separators are
#: all in use: OpenAI's own validator writes ``Invalid 'input[3].name'``, some
#: gateways write ``input.3``, and a JSON-pointer style error writes
#: ``input/3``.
_INPUT_LOCATION = re.compile(r"\binput\s*[\[./]\s*\d+")

#: A complaint naming a reasoning item. ``reasoning`` on its own is enough --
#: no other item type is called that -- and the two phrases a host uses when it
#: knows the item is a reasoning item are matched explicitly.
_REASONING_ITEM_NAMED = re.compile(
    r"\breasoning\b|\bencrypted_content\b|\bencrypted content\b", re.IGNORECASE
)

#: The field only a replayed reasoning item carries. A complaint that names it
#: is evidence about that item wherever the host says the fault is -- Bifrost's
#: documented binding failure ("Encrypted content item_id did not match the
#: target item id") names no index at all.
_ENCRYPTED_CONTENT_NAMED = re.compile(
    r"\bencrypted_content\b|\bencrypted content\b", re.IGNORECASE
)


def complaint_refuses_a_replayed_reasoning_item(complaint: str) -> bool:
    """Whether a 400 is the host refusing a ``reasoning`` item in ``input[]``.

    The one piece of evidence 8.16.0's carrier net acts on. Two shapes, and
    both are needed:

    * the fault is **located in the input array** -- ``input[3]``, ``input.3``
      or ``input/3`` -- **and** the complaint names a reasoning item. A 400
      that names ``reasoning`` at the top level is a refusal of the *field*,
      which the strip ladder already answers; only an input location says the
      replayed *item* is the problem;
    * or the complaint names **``encrypted_content``**, the field only a
      replayed reasoning item carries. Bifrost's documented failure --
      ``Encrypted content item_id did not match the target item id`` -- names
      no index, and it is precisely the failure this net exists for.

    Reads lowercased text, so it serves a stored fact's evidence excerpt as well
    as a live complaint -- the same contract
    :func:`complaint_refuses_a_history_thinking_block` keeps.
    """

    text = complaint.lower()
    if _ENCRYPTED_CONTENT_NAMED.search(text):
        return True
    if not _INPUT_LOCATION.search(text):
        return False
    return _REASONING_ITEM_NAMED.search(text) is not None


def input_carries_reasoning_items(input_items: Any) -> bool:
    """Whether this body's ``input`` actually replays a reasoning item.

    The third piece of evidence, and the one that keeps the fact honest: a host
    can fault an input index for anything, and a complaint that names
    ``reasoning`` about a body MCC never sent a reasoning item in is not
    evidence that replaying one is what broke it.

    ``True`` for any input item whose ``type`` is ``"reasoning"``.
    """

    if not isinstance(input_items, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("type") == "reasoning"
        for item in input_items
    )
