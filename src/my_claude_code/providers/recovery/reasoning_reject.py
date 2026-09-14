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
