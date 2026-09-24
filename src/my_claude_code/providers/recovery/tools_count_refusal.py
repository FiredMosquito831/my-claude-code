"""A host that caps how many tools one request may carry, and the one cut that fits.

OpenAI's platform API answers a catalogue longer than it accepts with

    {"error": {"message": "Invalid 'tools': array too long. Expected an array
     with maximum length 128, but got an array with length 212 instead.",
     "type": "invalid_request_error", "param": "tools",
     "code": "array_above_max_length"}}

and refuses the **whole request** -- reproduced publicly against Claude Code
catalogues in ``musistudio/claude-code-router`` #686, ``zed-industries/zed``
#42393 and ``code-yeongyu/oh-my-openagent`` #2848. The ChatGPT/Codex backend
has served ``chatgpt_oauth`` at 130, 131 and 137 tools, so it does not enforce
128; whether it accepts 212 has never been observed, because every 212-tool
request died on a schema pattern first. 7.36.0-7.39.0 removed that obstacle,
which is exactly why this refusal may now surface.

The number is **stated** by the host, which makes this the output-cap rung's
cousin rather than a guess: read the maximum off the host's own sentence, cut
the catalogue to it once, retry once, and remember the number per provider so
the next request is cut before it leaves.

What makes the cut safe -- and the reason it is a module of its own rather than
``tools[:n]`` -- is what it must never remove:

* **A tool the conversation already used.** A ``function_call`` (and the
  ``function_call_output`` that answers it by ``call_id``) replayed in
  ``input`` names a tool the model has called; removing its definition would
  leave the model looking at a call to a tool that no longer exists.
* **The tool ``tool_choice`` forces.** A forced choice naming a tool the body
  no longer carries is a different 400, and a silent change of instruction.
* **A tool without a name.** A hosted tool (``{"type": "web_search"}``) cannot
  be named in the record, so it is never a candidate.

Everything else is trimmed **from the end**, in the client's own order, and the
kept tools stay in the client's order too. The cut is a pure function of the
catalogue, the history and the cap, so the same request gives the same bytes on
every turn and after every restart. It is also stable *as a conversation
grows*: a kept tool that the model then calls moves from "kept for its slot" to
"protected", and the first ``cap - protected`` unprotected tools are the same
set they were before -- so the vendor's implicit tools prefix changes only when
the history itself forces a different set.

When the protected tools alone exceed the cap there is no cut that keeps the
rules, and the refusal is left exactly as the host sent it: it reaches routing
as the same ``model_rejected`` that falls through to the next model today.
Failure classification is not touched here or anywhere in this change.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .complaint import (
    complaint_evidence_snippet,
    is_bad_request,
    is_echo_key,
    upstream_complaint,
    upstream_error_payload,
)
from .ladder import RecoveryRung, RungResult

#: The rung's name, in the ladder row, the log line and the learned fact's
#: neighbourhood. One spelling for one event on both Responses senders.
RUNG_TOOLS_COUNT = "responses_tools_count"

#: The ``params.wire`` key a cut is recorded under. Names only -- never a
#: schema, never a description -- and every name, because "which tool was the
#: model not offered" is the whole question an operator opens the row with.
TOOLS_TRIMMED = "tools_trimmed"

# --------------------------------------------------------------------------
# The trigger.
# --------------------------------------------------------------------------

#: OpenAI's machine-readable verdict and its prose, either of which is enough
#: to say "an array was too long". Which array is a separate question.
_ARRAY_TOO_LONG = re.compile(r"\barray_above_max_length\b|\barray too long\b")

#: The message naming the ``tools`` array itself. Needed beside ``param``
#: because a host that omits ``param`` still says which array it means.
_TOOLS_NAMED = re.compile(r"invalid\s+['\"`]?tools['\"`]?\s*:|\btoo many tools\b")

#: The phrasings of the stated maximum. The number is what the host said; a
#: complaint that states none is not answered, because a guessed cap would
#: remove tools on no evidence at all.
_STATED_MAXIMUM: tuple[re.Pattern[str], ...] = (
    re.compile(r"maximum length (?:of )?(\d{1,5})"),
    re.compile(r"at most (\d{1,5}) (?:tools|items|elements)"),
    re.compile(r"maximum of (\d{1,5}) (?:tools|items|elements)"),
    re.compile(r"(?:max(?:imum)?|limit)(?: number of)? tools?(?: is| of)? (\d{1,5})"),
)


def _param_names_tools(payload: Any) -> bool:
    """Whether the structured error's own ``param`` is ``tools``.

    Walked by key rather than read off the flattened complaint, which mixes
    ``param`` with the message: ``Invalid 'input': array too long`` is the
    same code about a different array, and answering it by removing tools
    would spend the retry on a rewrite that cannot help.
    """

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key).lower()
            if is_echo_key(name):
                continue
            if name == "param" and isinstance(value, str):
                if value.strip().lower() == "tools":
                    return True
                continue
            if _param_names_tools(value):
                return True
        return False
    if isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
        return any(_param_names_tools(item) for item in payload)
    return False


def rejected_tools_max_count(error: Exception) -> int | None:
    """The tools-count maximum this 400 states, or ``None`` for another 400.

    ``None`` means "not my kind of rejection" and the caller must leave the
    error to the next rung or raise it. Three things have to hold together:
    the host said an array was too long, the array it named is ``tools``, and
    it stated the number. A maximum below 1 is not a count this rung can cut
    to -- "no tools at all" is a different fact with a different owner.
    """

    if not is_bad_request(error):
        return None
    complaint = upstream_complaint(error)
    if not _ARRAY_TOO_LONG.search(complaint):
        return None
    if not (
        _param_names_tools(upstream_error_payload(error))
        or _TOOLS_NAMED.search(complaint)
    ):
        return None
    for pattern in _STATED_MAXIMUM:
        match = pattern.search(complaint)
        if match is None:
            continue
        stated = int(match.group(1))
        return stated if stated >= 1 else None
    return None


# --------------------------------------------------------------------------
# The cut.
# --------------------------------------------------------------------------


def _tool_name(tool: Any) -> str | None:
    """A tool's wire name, or ``None`` for a tool that has none."""

    if not isinstance(tool, Mapping):
        return None
    name = tool.get("name")
    return name if isinstance(name, str) and name else None


def history_tool_names(items: Any) -> frozenset[str]:
    """Every tool name the conversation in ``input`` has already called.

    A replayed call is any ``input`` item whose type is a call and that names
    a tool -- ``function_call`` today, and ``custom_tool_call`` for a host
    that sends one. A ``function_call_output`` carries only a ``call_id``; the
    Anthropic protocol MCC translates from requires every ``tool_result`` to
    answer a ``tool_use`` in the turn before it, so the call it answers is in
    the same ``input`` and its name is already collected here.
    """

    names: set[str] = set()
    if not isinstance(items, Sequence) or isinstance(items, str | bytes):
        return frozenset()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        name = item.get("name")
        if isinstance(kind, str) and kind.endswith("_call") and isinstance(name, str):
            names.add(name)
    return frozenset(names)


def forced_tool_names(choice: Any) -> frozenset[str]:
    """The tool names a ``tool_choice`` pins, in any spelling it arrives in.

    ``{"type": "function", "name": X}`` (Responses), the Chat nesting
    ``{"function": {"name": X}}``, and an ``allowed_tools`` list of either.
    ``auto`` / ``required`` / ``none`` pin nothing: ``required`` asks for *a*
    tool, which any kept tool satisfies.
    """

    names: set[str] = set()
    if isinstance(choice, Mapping):
        name = choice.get("name")
        if isinstance(name, str) and name:
            names.add(name)
        names.update(forced_tool_names(choice.get("function")))
        tools = choice.get("tools")
        if isinstance(tools, Sequence) and not isinstance(tools, str | bytes):
            for tool in tools:
                names.update(forced_tool_names(tool))
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class ToolsTrim:
    """One catalogue cut to a cap, and exactly what left it."""

    tools: list[Any]
    #: Every removed tool's name, in the client's order.
    dropped: tuple[str, ...]
    #: The cap the catalogue was cut to.
    max_count: int
    #: How many tools the catalogue carried before the cut.
    original_count: int
    #: How many of the kept tools could not have been removed.
    protected_count: int

    def describe(self, provenance: str) -> str:
        """The ``tools_trimmed`` sentence: counts, then every dropped name."""

        return (
            f"dropped {len(self.dropped)} of {self.original_count} tools from "
            f"the end to fit a maximum of {self.max_count}{provenance}; "
            f"{self.protected_count} protected (used in this conversation, "
            f"forced by tool_choice, or unnamed); dropped: "
            f"{', '.join(self.dropped)}"
        )


def trim_tool_catalogue(
    tools: Any, max_count: int, body: Mapping[str, Any]
) -> ToolsTrim | None:
    """Cut ``tools`` to ``max_count``, or ``None`` when no cut is right.

    ``None`` in three cases, each of which leaves the body exactly as it was:
    the catalogue already fits, the cap is not a positive number, or the tools
    the rules protect already exceed the cap on their own -- in which case the
    host's refusal stands and falls through unchanged.

    The kept list is the client's list with the removed entries taken out:
    the same dicts, in the same order, so no kept definition's bytes move.
    """

    if not isinstance(tools, list) or max_count < 1 or len(tools) <= max_count:
        return None
    protected_names = history_tool_names(body.get("input")) | forced_tool_names(
        body.get("tool_choice")
    )

    def _protected(tool: Any) -> bool:
        name = _tool_name(tool)
        return name is None or name in protected_names

    protected_count = sum(1 for tool in tools if _protected(tool))
    if protected_count > max_count:
        return None
    room = max_count - protected_count
    kept: list[Any] = []
    dropped: list[str] = []
    for tool in tools:
        if _protected(tool):
            kept.append(tool)
        elif room > 0:
            kept.append(tool)
            room -= 1
        else:
            # ``_protected`` is True for every nameless tool, so a tool that
            # reaches this branch always has one.
            dropped.append(_tool_name(tool) or "")
    return ToolsTrim(
        tools=kept,
        dropped=tuple(dropped),
        max_count=max_count,
        original_count=len(tools),
        protected_count=protected_count,
    )


def trim_body_tools(
    body: Mapping[str, Any], max_count: int
) -> tuple[dict[str, Any], ToolsTrim] | None:
    """A shallow clone whose ``tools`` fit ``max_count``, or ``None``."""

    trim = trim_tool_catalogue(body.get("tools"), max_count, body)
    if trim is None:
        return None
    cloned = dict(body)
    cloned["tools"] = trim.tools
    return cloned, trim


# --------------------------------------------------------------------------
# The rung, and the cap applied before the first send.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolsCountRecovery:
    """The whole of one firing: the stated cap, the cut body, what left."""

    max_count: int
    body: dict[str, Any]
    trim: ToolsTrim
    evidence: str

    @property
    def marker(self) -> dict[str, str]:
        """The ``params.wire`` record, shaped to merge into the wire capture."""

        return {TOOLS_TRIMMED: self.trim.describe(" (stated by this host)")}

    @property
    def log_line(self) -> str:
        """What the provider's own log says it did and why."""

        return (
            f"host states at most {self.max_count} tools -- dropped "
            f"{len(self.trim.dropped)} of {self.trim.original_count} from the end"
        )


def tools_count_recovery(
    error: Exception, body: Mapping[str, Any]
) -> ToolsCountRecovery | None:
    """The cut this count refusal calls for, or ``None`` to pass it on.

    Pure, for the reason :func:`tool_schema_recovery` is: the transport reads
    it inside its rung table, and ``chatgpt_oauth`` reads it a second time to
    recover the marker and the number its ladder's decision has no slot for.
    """

    stated = rejected_tools_max_count(error)
    if stated is None:
        return None
    cut = trim_body_tools(body, stated)
    if cut is None:
        return None
    retry_body, trim = cut
    return ToolsCountRecovery(
        max_count=stated,
        body=retry_body,
        trim=trim,
        evidence=complaint_evidence_snippet(upstream_complaint(error)),
    )


@dataclass(slots=True)
class ToolsCountRefusalRecovery:
    """The rung ``chatgpt_oauth`` registers on its own one-provider ladder.

    Nothing is remembered here: the number is written down only once the cut
    catalogue has been accepted, the rule the schema rung keeps, so a request
    that would have failed anyway never teaches the process to cut every
    later catalogue.
    """

    log_tag: str
    kind: str = RUNG_TOOLS_COUNT

    def rung(self) -> RecoveryRung:
        return RecoveryRung(kind=self.kind, apply=self)

    def __call__(self, error: Exception, body: dict[str, Any]) -> RungResult:
        recovery = tools_count_recovery(error, body)
        if recovery is None:
            return None
        logger.warning(
            "{}: {} -- retrying once ({})",
            self.log_tag,
            recovery.log_line,
            recovery.evidence,
        )
        return recovery.body, None


def effective_tools_max_count(
    declared: int | None, learned: int | None
) -> tuple[int | None, str]:
    """The cap to apply before the first send, and where it came from.

    The smaller of what the host's dialect declares and what the host has
    stated, because both are ceilings and the lower one is the one a request
    must fit: a declared 128 that the host has since stated as 100 would
    otherwise re-pay the 400 on every request. ``(None, "")`` -- every host
    today -- means no cap, which is what *unknown* has to mean.
    """

    if declared is None and learned is None:
        return None, ""
    if learned is None or (declared is not None and declared <= learned):
        return declared, "declared"
    return learned, "learned"


def apply_tools_max_count(
    body: Mapping[str, Any], cap: int | None, provenance: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Cut the catalogue to a known cap before the first send.

    Returns a shallow clone of ``body`` sharing the very same ``tools`` list
    when there is no cap, the catalogue fits, or the protected tools alone
    exceed the cap (the host then answers, and the rung finds no cut either,
    so the refusal falls through as it does today).
    """

    if cap is None:
        return dict(body), {}
    cut = trim_body_tools(body, cap)
    if cut is None:
        return dict(body), {}
    trimmed, trim = cut
    return trimmed, {TOOLS_TRIMMED: trim.describe(f" ({provenance})")}


def merge_tools_trimmed_markers(*markers: Mapping[str, str]) -> dict[str, str]:
    """One ``tools_trimmed`` record out of a pre-send cut and a rung's cut."""

    lines = [marker[TOOLS_TRIMMED] for marker in markers if marker.get(TOOLS_TRIMMED)]
    return {TOOLS_TRIMMED: "; ".join(lines)} if lines else {}
