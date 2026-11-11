"""The carrier-surface drop count: one note, every dropping cell (8.20.0).

**What this is.** Q1's answer was *count instead of build*: MCC does not invent a
carrier for the Chat or Gemini door, because neither door has an opaque field in
either direction and the measured population is 17 Chat requests and 2 Gemini
requests out of 757,322. What replaces the build is a **measurement** -- and a
measurement that says only "something was dropped" is a shrug, not an addition.
So this module names **every cell** of the matrix that drops on those two doors,
per door, per direction, per mode, and gives each one its own key.

**Where the count lives, and why.** It is written once per request, on the
request record's ``params``, under :data:`CARRIER_SURFACE_DROPPED`. Two of the
six cells (the egress half) are only knowable *after* the attempt record has
been committed -- the Chat assembler and the Gemini assembler run downstream of
the executor -- so the attempt record cannot hold the whole breakdown. One key
on the record that outlives the whole request can, and it keeps every cell in
one place so an operator reads the cell, not a guess. **Door and mode are not
repeated in the key names**: they are the row's own ``protocol`` and ``stream``
columns, so the same sub-key on two rows is two different cells and the query
that separates them is a ``WHERE``, not a second vocabulary.

**The rules the note keeps**, all of them the shape ``foreign_thinking_dropped``
and ``history_thinking_omitted`` already keep:

* **counts only, never content** -- no substring of a carrier, no reasoning
  text, no id;
* **absent, not ``0``** -- a request that dropped nothing carries no key at all,
  so every existing ``params`` is exactly what it was;
* **no wire byte** -- nothing here reaches a body;
* **not a reasoning key** -- the name deliberately avoids the
  ``reasoning_*``/``thinking_*`` prefixes :func:`core.wire_capture.is_reasoning_key`
  reads as a reasoning *instruction*. A note about a drop must never count as
  reasoning sent.

**The six cells, and what each one is.** The matrix's cells 9, 10, 13 and 14,
which are the ones Q1 left dropping. **Every key names its door, its direction
and its cell**, so a reader of the raw ``params`` JSON needs no table:

========================  ==============================  ===================
cell                      sub-key                         what was dropped
========================  ==============================  ===================
Chat IN                   ``chat_in_details_carrier``
                          a client's ``reasoning_details`` entry whose ``data``
                          is an ``mcc:reasoning:v1:`` carrier -- the opaque
                          half, the one a carrier-replay build would replay
Chat IN                   ``chat_in_details_other``
                          a client's ``reasoning_details`` entry that is
                          **not** a carrier -- another dialect's text detail,
                          which this door has never forwarded either
Chat OUT                  ``chat_out_redacted_thinking``
                          a ``redacted_thinking`` block the upstream produced
                          that the Chat assembler has no frame for, so the
                          client is never handed the opaque blob it would echo
Gemini IN                 ``gemini_in_thought_part``
                          a replayed ``thought: true`` part, dropped because an
                          Anthropic ``thinking`` block needs a signature
Gemini IN                 ``gemini_in_thought_signature``
                          a ``thoughtSignature`` on a replayed part, ignored
                          entirely -- the field this door has no reader for
Gemini OUT                ``gemini_out_thought_unsigned``
                          a thought emitted with no ``thoughtSignature``: the
                          carrier that would make the round trip possible
========================  ==============================  ===================

**Mode is the row's own ``stream`` column**, not a seventh spelling in the key:
one request has one mode, and repeating it in every sub-key would let the two
disagree.

The sub-keys are **not** mutually exclusive. A replayed thought part that also
carries a ``thoughtSignature`` counts once in each, because two different things
were dropped and an operator needs both facts.
"""

from collections.abc import Mapping

#: The ``params`` key on the request record. Deliberately not spelled
#: ``reasoning_*``/``thinking_*``: :func:`core.wire_capture.is_reasoning_key`
#: reads any key with those prefixes as a reasoning *instruction*, and a note
#: about a drop would then count as reasoning sent.
CARRIER_SURFACE_DROPPED = "carrier_surface_dropped"

#: Chat IN: a client's ``reasoning_details`` entry carrying a carrier.
CHAT_DETAILS_CARRIER = "chat_in_details_carrier"

#: Chat IN: a client's ``reasoning_details`` entry that is not a carrier.
CHAT_DETAILS_OTHER = "chat_in_details_other"

#: Chat OUT: a ``redacted_thinking`` block the Chat door cannot hand the client.
CHAT_REDACTED_THINKING = "chat_out_redacted_thinking"

#: Gemini IN: a replayed ``thought: true`` part.
GEMINI_THOUGHT_PART = "gemini_in_thought_part"

#: Gemini IN: a ``thoughtSignature`` on a replayed part.
GEMINI_THOUGHT_SIGNATURE = "gemini_in_thought_signature"

#: Gemini OUT: a thought emitted with no ``thoughtSignature``.
GEMINI_THOUGHT_UNSIGNED = "gemini_out_thought_unsigned"


def note(counts: dict[str, int], cell: str, amount: int = 1) -> None:
    """Add ``amount`` to one cell of a per-request count.

    A zero or negative amount writes nothing, so a caller that counted nothing
    leaves the mapping empty and the note absent -- the rule
    ``carrier_foreign_note`` states for a count of zero.
    """

    if amount <= 0:
        return
    counts[cell] = counts.get(cell, 0) + amount


def merge_carrier_surface_note(
    params: dict[str, object], counts: Mapping[str, int]
) -> None:
    """Write the breakdown into ``params``, or nothing when it is empty.

    Called once, at finalize, on the request record. ``{}`` leaves ``params``
    untouched, which is what keeps every request that dropped nothing byte for
    byte the row it always was.
    """

    if not counts:
        return
    params[CARRIER_SURFACE_DROPPED] = {
        cell: counts[cell] for cell in sorted(counts) if counts[cell] > 0
    }
