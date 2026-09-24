"""A host that refuses one JSON-Schema keyword, and the one sweep that answers it.

The refusal this module reads is the narrowest possible statement a validator
can make and the widest possible consequence: OpenAI's Responses surface
answers a 212-tool catalogue with

    {"error": {"message": "Invalid JSON schema: regex lookaround is not
     supported. Found at $.properties.videoScale.pattern.",
     "type": "invalid_request_error", "param": "tools",
     "code": "invalid_json_schema"}}

and refuses **the whole request** -- 216 times on 2026-09-20 -- because one
MCP tool out of 212 spells "end of input" the way V8 does, ``(?![\\s\\S])``.
The path is rooted at *one* tool's ``parameters`` and never names which tool,
so the path alone cannot be patched; and the rung contract in
:mod:`~my_claude_code.providers.recovery.ladder` forbids a rung firing twice,
so N offences must not cost N retries. Both facts have the same answer: the
rewrite is a **sweep of the whole catalogue for the keyword class the host
named**, fired exactly once.

Four rules this module keeps, each of them a lesson something already paid for:

* **Read the structured complaint, never a flat string.** The request being
  refused *contains* the offending regex and the word ``tools``, so a
  substring matcher would fire on its own payload.
  :func:`~my_claude_code.providers.recovery.complaint.upstream_complaint`
  prunes the echoed request first, and nothing here reads anything else.
* **Copy on write.** The converted tool objects are the client's own dicts by
  reference and are shared across attempts and across models in one route, so
  a sweep that mutated them would corrupt the next model's request. Every node
  that loses nothing is returned **by identity**, which is also what keeps the
  emitted bytes -- and therefore the vendor's prompt-cache prefix -- unchanged
  for a catalogue that does not offend.
* **Drop, never rewrite.** A lookaround-free equivalent of an arbitrary
  lookaround is not generally constructible, and a wrong rewrite silently
  changes what the model is allowed to emit. Dropping ``pattern`` costs the
  client-side format check on one field; the ``description`` beside it already
  states the format, and MCC does not validate tool input on the way out.
* **Never widen beyond the keyword class.** The catalogue keeps every tool,
  every property, every ``type`` and every ``description``. A Codex-style
  allow-list -- which would also drop ``minimum``, ``title`` and ``format`` --
  is strictly more destructive than what this host asked for, and the same
  catalogue's other 134 ``$schema`` keys and 33 ``minimum`` keys were accepted.

The keyword the host named decides the blast radius, in three widening steps:

1. ``pattern`` + a **named construct** (``regex lookaround``) -- only the
   patterns that actually contain that construct lose their key. On the
   measured catalogue that is 1 of 3 patterns in 1 of 205 tools.
2. a **different keyword** at the path tail (``format``, ``patternProperties``)
   -- every occurrence of that keyword goes, because the host named the
   keyword and not the value.
3. **no path and no keyword** -- every ``pattern`` goes and nothing else. The
   older Chat-surface wording, ``Invalid schema for function 'Artifact': '…'
   is not a 'regex'``, names the function and quotes the regex but states no
   path, and "a regex keyword is wrong" is the whole of what it proves.

What is learned from it is keyed ``(provider, keyword, construct)`` and never
``(tool, path)``: the catalogue changes every session, the host's validator
does not.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .complaint import complaint_evidence_snippet, is_bad_request, upstream_complaint
from .ladder import RecoveryRung, RungResult

#: The rung's name, in the ladder row, the log line and the learned fact. One
#: spelling so an operator reading "retry: responses_tool_schema" in the modal
#: and the row on the Models page can tell they are the same event.
RUNG_TOOL_SCHEMA = "responses_tool_schema"

#: How many ``(tool, path)`` pairs the wire marker names before it summarises.
#: The marker is a breadcrumb in a request row, not an inventory: it exists so
#: the next incident is one query rather than a decompression script, and a
#: 212-tool catalogue whose every pattern offended must not write 212 strings
#: into every attempt's params.
MAX_RECORDED_REMOVALS = 8

# --------------------------------------------------------------------------
# The trigger.
# --------------------------------------------------------------------------

#: The Responses surface's machine-readable verdict. Measured 216 times on
#: 2026-09-20 against ``chatgpt_oauth/gpt-5.6-sol``.
_SCHEMA_CODE = re.compile(r"\binvalid_json_schema\b")

#: The older, path-free wording the Chat surface uses, quoted in
#: ``raine/claude-code-proxy`` #142 for Claude Code's own Artifact tool:
#: ``Invalid schema for function 'Artifact': '…' is not a 'regex'.``
_SCHEMA_FUNCTION = re.compile(r"invalid schema for function\b")

#: ``Found at $.properties.videoScale.pattern.`` The path is relative to one
#: tool's ``parameters`` object and does **not** name the tool, which is why
#: only its *tail* -- the keyword -- is read off it.
_FOUND_AT = re.compile(r"found at (\$[^\s]*)")

#: A refusal that names ``tools`` but states a *count* is a different fault
#: with a different answer (a declared ``tools_max_count``), and answering it
#: by deleting schema keywords would spend the request's one retry on a
#: rewrite that cannot possibly help.
_ARRAY_TOO_LONG = re.compile(r"\barray_above_max_length\b|\barray too long\b")


# --------------------------------------------------------------------------
# The construct table -- data, not branches.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegexConstruct:
    """One regex feature a validator may refuse, and how both ends spell it.

    ``host_words`` is how the *refusal* names it; ``markers`` is how a
    *pattern* carries it. A construct MCC can recognise in the host's words
    but not find in a pattern would remove every pattern in the catalogue, so
    the two halves are declared together and tested together.
    """

    #: The stable identifier. Half of the learned fact's key, so it is a word
    #: an operator reads on the Models page and never a number.
    name: str
    #: Lowercased phrases the host uses for it. Read against the pruned
    #: complaint, so a phrase that also appears in a tool description is
    #: invisible here.
    host_words: tuple[str, ...]
    #: Literal substrings that prove a pattern carries the construct. Literal
    #: rather than regex on purpose: these are needles in a *pattern string*,
    #: and a needle that was itself a regex would need escaping nobody would
    #: get right twice.
    markers: tuple[str, ...] = ()
    #: An extra test, for a construct whose markers overlap another's.
    #: ``(?<`` opens a lookbehind *and* a named group, and only the character
    #: after it says which.
    refine: str = ""


def _has_named_group(pattern: str) -> bool:
    """``(?<name>`` / ``(?'name'`` / ``(?P<name>``, but never a lookbehind.

    ``(?<=`` and ``(?<!`` share the first three characters with a named group
    and are the *other* entry in this table, so the discriminator is the
    character that follows.
    """

    for index in range(len(pattern) - 3):
        if pattern[index : index + 3] != "(?<":
            continue
        if pattern[index + 3] not in "=!":
            return True
    return "(?P<" in pattern or "(?'" in pattern


def _has_backreference(pattern: str) -> bool:
    """``\\1``--``\\9``, ``\\k<name>`` or ``(?P=name)``.

    ``\\1`` is read only outside a character class, where it would be an octal
    escape rather than a backreference -- but the distinction costs a parser
    and buys nothing here: both readings make the pattern one the Rust engine
    may refuse, and the consequence either way is that one ``pattern`` key is
    dropped.
    """

    return bool(_BACKREFERENCE.search(pattern))


_BACKREFERENCE = re.compile(r"\\[1-9]|\\k<|\(\?P=")

#: Every construct a JavaScript-authored MCP schema routinely carries and a
#: Rust ``regex``-backed validator has no support for, with the words the
#: hosts measured so far use for each. Adding a row is the whole of adding a
#: construct: nothing below branches on a name.
#:
#: Ordered widest-evidence-first only for readability; the matcher takes the
#: first row whose words the host used, and no two rows share a phrase.
REGEX_CONSTRUCTS: tuple[RegexConstruct, ...] = (
    RegexConstruct(
        name="lookaround",
        host_words=("lookaround", "lookahead", "lookbehind", "look-around"),
        markers=("(?=", "(?!", "(?<=", "(?<!"),
    ),
    RegexConstruct(
        name="backreference",
        host_words=("backreference", "back reference", "back-reference"),
        refine="backreference",
    ),
    RegexConstruct(
        name="unicode_property",
        # Prose only, and deliberately. The Chat-surface wording *quotes the
        # offending regex back*, so a needle like ``\p{`` here would read the
        # client's own pattern as the host's diagnosis and narrow a refusal
        # the host never narrowed -- turning "every pattern is suspect" into
        # "only the ones with this escape", on no evidence at all.
        host_words=("unicode property", "property escape", "unicode escape"),
        markers=("\\p{", "\\P{"),
    ),
    RegexConstruct(
        name="possessive",
        host_words=("possessive", "atomic group", "atomic grouping"),
        markers=("(?>", "*+", "++", "?+", "}+"),
    ),
    RegexConstruct(
        name="named_group",
        host_words=("named group", "named capture", "named capturing"),
        refine="named_group",
    ),
    RegexConstruct(
        name="inline_flags",
        host_words=("inline flag", "inline modifier", "flag expression"),
        markers=("(?i", "(?m", "(?s", "(?x", "(?u"),
    ),
    RegexConstruct(
        name="text_anchor",
        host_words=("\\z anchor", "end of text anchor", "absolute anchor"),
        markers=("\\Z", "\\z", "\\A"),
    ),
)

_REFINERS = {
    "backreference": _has_backreference,
    "named_group": _has_named_group,
}


def construct_named_by(complaint: str) -> RegexConstruct | None:
    """The construct this complaint names, or ``None`` when it names none."""

    for construct in REGEX_CONSTRUCTS:
        if any(word in complaint for word in construct.host_words):
            return construct
    return None


def pattern_uses(pattern: str, construct: RegexConstruct) -> bool:
    """Whether one ``pattern`` value actually carries this construct."""

    refiner = _REFINERS.get(construct.refine)
    if refiner is not None:
        return refiner(pattern)
    return any(marker in pattern for marker in construct.markers)


# --------------------------------------------------------------------------
# Which keyword a path names, and whether it may be dropped.
# --------------------------------------------------------------------------

#: JSON-Schema keywords whose removal costs a *constraint* and never a
#: *shape*. A tool keeps every property, every ``type``, every ``description``
#: and every ``required`` entry whatever the host refuses, because those are
#: what tell the model how to call the tool; the rest are validation the host
#: was going to ignore anyway once it had refused the request.
#:
#: Keyed lowercase because the complaint arrives lowercased, and mapped back
#: to the canonical spelling because the schema is not.
DROPPABLE_KEYWORDS: Mapping[str, str] = {
    "pattern": "pattern",
    "patternproperties": "patternProperties",
    "format": "format",
    "contentencoding": "contentEncoding",
    "contentmediatype": "contentMediaType",
    "contentschema": "contentSchema",
    "propertynames": "propertyNames",
    "multipleof": "multipleOf",
    "minimum": "minimum",
    "maximum": "maximum",
    "exclusiveminimum": "exclusiveMinimum",
    "exclusivemaximum": "exclusiveMaximum",
    "minlength": "minLength",
    "maxlength": "maxLength",
    "minitems": "minItems",
    "maxitems": "maxItems",
    "uniqueitems": "uniqueItems",
    "minproperties": "minProperties",
    "maxproperties": "maxProperties",
    "examples": "examples",
    "title": "title",
    "default": "default",
    "$schema": "$schema",
}

#: The keyword a path-free refusal is about. Every measured wording of this
#: refusal -- both of them -- is a statement about a regex, and ``pattern`` is
#: the only keyword in the dialect whose value is one.
DEFAULT_KEYWORD = "pattern"

_ARRAY_INDEX = re.compile(r"\[\d+\]$")


def _keyword_at(path: str) -> str | None:
    """The droppable keyword a ``$.…`` path ends in, if it ends in one.

    A path whose tail is a *property name* (``$.properties.videoScale``) or a
    structural keyword (``$.properties``) yields ``None``, and the caller
    falls back to the path-free reading. Guessing that a tail is a keyword
    because it sits where one would is how a sweep starts deleting a client's
    own property called ``format``.
    """

    for raw in reversed(path.rstrip(".").split(".")):
        segment = _ARRAY_INDEX.sub("", raw).strip("$ ")
        if not segment:
            continue
        return DROPPABLE_KEYWORDS.get(segment)
    return None


# --------------------------------------------------------------------------
# The refusal, and the sweep it calls for.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaKeywordRefusal:
    """One host's stated objection to one JSON-Schema keyword."""

    #: The canonical keyword to remove, e.g. ``pattern``.
    keyword: str
    #: The construct that made it offend, or ``None`` when the host named the
    #: keyword without naming what was wrong with the value -- in which case
    #: every occurrence of the keyword goes.
    construct: RegexConstruct | None = None

    @property
    def detail(self) -> str:
        """The learned fact's ``detail``: the second half of its key."""

        return f"{self.keyword}:{self.construct.name if self.construct else '*'}"

    @property
    def words(self) -> str:
        """How the rung names itself in a log line and in the wire marker."""

        if self.construct is None:
            return f"every {self.keyword}"
        return f"{self.keyword} using {self.construct.name}"


def refusal_from_detail(detail: str) -> SchemaKeywordRefusal | None:
    """Rebuild a refusal from a stored fact's ``detail``, or ``None``.

    Deliberately strict: a detail this cannot read is a row nothing should
    act on, and acting on half of it would sweep a keyword the host never
    named.
    """

    keyword, separator, construct_name = detail.partition(":")
    if not separator or keyword not in DROPPABLE_KEYWORDS.values():
        return None
    if construct_name == "*":
        return SchemaKeywordRefusal(keyword=keyword)
    for construct in REGEX_CONSTRUCTS:
        if construct.name == construct_name:
            return SchemaKeywordRefusal(keyword=keyword, construct=construct)
    return None


def rejected_tool_schema_keyword(error: Exception) -> SchemaKeywordRefusal | None:
    """The keyword class this 400 refuses, or ``None`` for another rejection.

    ``None`` means "not my kind of rejection" and the caller must leave the
    error to the next rung or raise it -- the rule every matcher in this
    package keeps, because a rung that answers an unrelated 400 by deleting
    schema keywords spends the request's one retry on a rewrite that cannot
    help and hides the real refusal behind a second failure.
    """

    if not is_bad_request(error):
        return None
    complaint = upstream_complaint(error)
    if not (_SCHEMA_CODE.search(complaint) or _SCHEMA_FUNCTION.search(complaint)):
        return None
    if _ARRAY_TOO_LONG.search(complaint):
        # The host named ``tools`` for a reason that is about the array's
        # length, not its contents. A declared count ceiling answers that one.
        return None
    construct = construct_named_by(complaint)
    found = _FOUND_AT.search(complaint)
    keyword = _keyword_at(found.group(1)) if found is not None else None
    if keyword is None:
        # Path-free fallback: every regex-valued keyword, and nothing else.
        # Never an allow-list -- the catalogue's other keywords were accepted
        # by this same validator on this same request.
        return SchemaKeywordRefusal(keyword=DEFAULT_KEYWORD, construct=construct)
    if keyword != DEFAULT_KEYWORD:
        # The host named a keyword whose value is not a regex, so the
        # construct it may also have named is about something else. Every
        # occurrence of the keyword goes.
        return SchemaKeywordRefusal(keyword=keyword)
    return SchemaKeywordRefusal(keyword=keyword, construct=construct)


@dataclass(frozen=True, slots=True)
class SchemaRemoval:
    """One keyword this sweep took out, and where it was."""

    tool: str
    path: str
    keyword: str


def _offends(value: Any, refusal: SchemaKeywordRefusal) -> bool:
    """Whether one keyword's value is the one the host objected to."""

    if refusal.construct is None:
        return True
    if not isinstance(value, str):
        # A ``pattern`` that is not a string is malformed rather than
        # offending; leaving it is the conservative reading and the host will
        # say so again if it disagrees.
        return False
    return pattern_uses(value, refusal.construct)


#: Keys whose value is one schema.
_SCHEMA_VALUE_KEYS = frozenset(
    {
        "additionalProperties",
        "additionalItems",
        "unevaluatedProperties",
        "unevaluatedItems",
        "items",
        "contains",
        "propertyNames",
        "if",
        "then",
        "else",
        "not",
    }
)
#: Keys whose value is a list of schemas.
_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
#: Keys whose value is a mapping of name to schema. The *names* are never
#: touched -- a property called ``pattern`` is a property, not a keyword --
#: which is the whole reason the walk descends by key rather than by shape.
_SCHEMA_MAP_KEYS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)


#: What a keyword rewrite returns to take the keyword out altogether.
DROP_KEYWORD: Any = object()


def rewrite_schema_keyword(
    node: Any,
    keyword: str,
    rewrite: Callable[[Any], Any],
    path: str = "$",
    visit: Callable[[str, Any], None] | None = None,
) -> Any:
    """Return ``node`` with every ``keyword`` value rewritten, or ``node`` itself.

    The one schema walker the rung, the learned sweep and the declared dialect
    share -- whether a keyword is *dropped* (``rewrite`` returns
    :data:`DROP_KEYWORD`) or *repaired* (it returns a new value). The walk
    descends by key, never by shape, through exactly the vocabulary above, so
    a property *called* ``pattern`` is a property and instance data
    (``default``, ``const``, ``enum``, ``examples``) is never read as schema.

    ``visit(path, replacement)`` is told about every keyword that changed,
    with :data:`DROP_KEYWORD` for a removal.

    Copy-on-write, and identity on the way out whenever nothing changed, all
    the way up: a catalogue with no offence is the very same list of the very
    same dicts the converter produced, so the serialised bytes cannot have
    moved.
    """

    if isinstance(node, list):
        rewritten = [
            rewrite_schema_keyword(item, keyword, rewrite, f"{path}[{index}]", visit)
            for index, item in enumerate(node)
        ]
        if all(new is old for new, old in zip(rewritten, node, strict=True)):
            return node
        return rewritten
    if not isinstance(node, Mapping):
        return node

    changed: dict[str, Any] = {}
    touched = False
    for key, value in node.items():
        if key == keyword:
            replacement = rewrite(value)
            if replacement is DROP_KEYWORD:
                if visit is not None:
                    visit(f"{path}.{key}", DROP_KEYWORD)
                touched = True
                continue
            if replacement is not value:
                if visit is not None:
                    visit(f"{path}.{key}", replacement)
                changed[str(key)] = replacement
                touched = True
                continue
        if key in _SCHEMA_VALUE_KEYS or key in _SCHEMA_LIST_KEYS:
            walked = rewrite_schema_keyword(
                value, keyword, rewrite, f"{path}.{key}", visit
            )
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, Mapping):
            inner: dict[str, Any] = {}
            inner_changed = False
            for name, schema in value.items():
                walked_schema = rewrite_schema_keyword(
                    schema, keyword, rewrite, f"{path}.{key}.{name}", visit
                )
                inner_changed = inner_changed or walked_schema is not schema
                inner[str(name)] = walked_schema
            walked = inner if inner_changed else value
        else:
            changed[str(key)] = value
            continue
        changed[str(key)] = walked
        touched = touched or walked is not value

    return changed if touched else node


def _prune(
    node: Any,
    refusal: SchemaKeywordRefusal,
    tool: str,
    path: str,
    removals: list[SchemaRemoval],
) -> Any:
    """Return ``node`` without the refused keyword, or ``node`` itself."""

    def _drop(value: Any) -> Any:
        return DROP_KEYWORD if _offends(value, refusal) else value

    def _record(where: str, _replacement: Any) -> None:
        removals.append(SchemaRemoval(tool=tool, path=where, keyword=refusal.keyword))

    return rewrite_schema_keyword(node, refusal.keyword, _drop, path, _record)


def prune_tool_catalogue(
    tools: Sequence[Any], refusal: SchemaKeywordRefusal
) -> tuple[Sequence[Any], tuple[SchemaRemoval, ...]]:
    """Sweep every tool for the refused keyword class, copy-on-write.

    A sweep rather than a patch of the one path the host named, because the
    host names one offence at a time and the ladder gives this rung one firing
    -- so a catalogue with three offending patterns has to come back clean on
    the first retry or the request fails with nothing learned.
    """

    removals: list[SchemaRemoval] = []
    pruned_tools: list[Any] = []
    changed = False
    for tool in tools:
        if not isinstance(tool, Mapping):
            pruned_tools.append(tool)
            continue
        parameters = tool.get("parameters")
        if parameters is None:
            pruned_tools.append(tool)
            continue
        name = str(tool.get("name") or "unknown")
        pruned = _prune(parameters, refusal, name, "$", removals)
        if pruned is parameters:
            pruned_tools.append(tool)
            continue
        changed = True
        pruned_tools.append({**tool, "parameters": pruned})
    if not changed:
        return tools, ()
    return pruned_tools, tuple(removals)


def wire_marker(
    refusal: SchemaKeywordRefusal, removals: Sequence[SchemaRemoval]
) -> str:
    """One line naming what left the catalogue, for ``params.wire``.

    Names and paths only. A schema body in a request row would be the one
    place a client's own prompt text could be archived without anybody
    choosing to archive it, and the names are what makes the next incident a
    query rather than an investigation.
    """

    return describe_removals((refusal.words,), removals)


def describe_removals(
    words: Sequence[str], removals: Sequence[SchemaRemoval], provenance: str = ""
) -> str:
    """The one sentence every ``tool_schema_pruned`` marker is written in.

    Shared by the rung, the learned sweep and the declared dialect so an
    operator reads the same shape whichever of the three took a keyword out;
    ``provenance`` is the only part that says which one it was.
    """

    tools = sorted({removal.tool for removal in removals})
    where = ", ".join(
        f"{removal.tool} {removal.path}" for removal in removals[:MAX_RECORDED_REMOVALS]
    )
    if len(removals) > MAX_RECORDED_REMOVALS:
        where += f", +{len(removals) - MAX_RECORDED_REMOVALS} more"
    noun = "tool" if len(tools) == 1 else "tools"
    return (
        f"dropped {' and '.join(words)} from {len(tools)} {noun}{provenance}: {where}"
    )


#: The ``params.wire`` key every schema removal is recorded under.
TOOL_SCHEMA_PRUNED = "tool_schema_pruned"
#: The ``params.wire`` key a *repaired* (rather than removed) keyword is
#: recorded under -- the declared dialect's Unicode-property translation.
TOOL_SCHEMA_TRANSLATED = "tool_schema_translated"
#: Every key a schema sweep writes, in the order a record lists them.
_TOOL_SCHEMA_MARKER_KEYS = (TOOL_SCHEMA_TRANSLATED, TOOL_SCHEMA_PRUNED)


def merge_tool_schema_markers(*markers: Mapping[str, str]) -> dict[str, str]:
    """One schema record out of several sweeps' records.

    The declared dialect, the learned facts and the rung can each take a
    keyword out of the same body, and they all write the same key -- so a
    plain ``{**a, **b}`` would let the last one erase what the others did.
    Joined per key in the order given, which is the order the sweeps ran in.
    A key no sweep wrote is absent, so a body nothing touched records nothing.
    """

    merged: dict[str, str] = {}
    for key in _TOOL_SCHEMA_MARKER_KEYS:
        lines = [marker[key] for marker in markers if marker.get(key)]
        if lines:
            merged[key] = "; ".join(lines)
    return merged


@dataclass(frozen=True, slots=True)
class ToolSchemaRecovery:
    """The whole of one firing: what to send, what was lost, what to learn."""

    refusal: SchemaKeywordRefusal
    body: dict[str, Any]
    removals: tuple[SchemaRemoval, ...]
    evidence: str

    @property
    def marker(self) -> dict[str, str]:
        """The ``params.wire`` record, shaped to merge into the wire capture."""

        return {TOOL_SCHEMA_PRUNED: wire_marker(self.refusal, self.removals)}

    @property
    def log_line(self) -> str:
        """What the provider's own log says it did and why."""

        return (
            f"host refuses {self.refusal.words} in tool schemas -- "
            f"{wire_marker(self.refusal, self.removals)}"
        )


def _swept_body(
    body: Mapping[str, Any], refusal: SchemaKeywordRefusal
) -> tuple[dict[str, Any], tuple[SchemaRemoval, ...]] | None:
    """A shallow clone whose ``tools`` lost the keyword, or ``None``.

    ``None`` when nothing offended, which is the same rule
    :func:`clone_body_without_tool_choice` keeps: removing nothing cannot be
    what fixes a 400, so a body this leaves alone is never retried.
    """

    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    pruned, removals = prune_tool_catalogue(tools, refusal)
    if pruned is tools or not removals:
        return None
    cloned = dict(body)
    cloned["tools"] = list(pruned)
    return cloned, removals


def tool_schema_recovery(
    error: Exception, body: Mapping[str, Any]
) -> ToolSchemaRecovery | None:
    """The rewrite this schema refusal calls for, or ``None`` to pass it on.

    Pure, and deliberately so: the transport reads it once inside its rung
    table and ``chatgpt_oauth`` reads it a second time to recover the marker
    and the refusal its ladder's :class:`RecoveryDecision` has no slot for.
    Widening that decision would change the arity every rung in the fleet
    returns to carry a value one rung can produce -- the same reason
    ``rejected_effort_values`` is read at the ladder rather than returned by
    the rung that proves it.
    """

    refusal = rejected_tool_schema_keyword(error)
    if refusal is None:
        return None
    swept = _swept_body(body, refusal)
    if swept is None:
        return None
    retry_body, removals = swept
    return ToolSchemaRecovery(
        refusal=refusal,
        body=retry_body,
        removals=removals,
        evidence=complaint_evidence_snippet(upstream_complaint(error)),
    )


@dataclass(slots=True)
class ToolSchemaRefusalRecovery:
    """The rung a provider whose own ladder is not the transport's registers.

    ``chatgpt_oauth`` has never run through
    ``ResponsesTransport._send_with_recovery``: it owns its client and its own
    one-rung :class:`~my_claude_code.providers.recovery.ladder.RecoveryLadder`.
    Joining the two ladders is a larger change than this recovery is, so the
    seam taken here is the smallest one that serves both senders -- one more
    entry in that tuple -- and the ladder's own contract does the rest:
    ``used`` is never cleared, so the sweep fires exactly once per request on
    either path.

    Nothing is remembered here, the rule
    :class:`~my_claude_code.providers.recovery.ladder.ReasoningStripRecovery`
    states: the host named the keyword, but what gets written down is "the
    swept catalogue was accepted", and a request that would have failed anyway
    must not teach the process to strip a keyword from every later catalogue.
    """

    log_tag: str
    kind: str = RUNG_TOOL_SCHEMA

    def rung(self) -> RecoveryRung:
        return RecoveryRung(kind=self.kind, apply=self)

    def __call__(self, error: Exception, body: dict[str, Any]) -> RungResult:
        recovery = tool_schema_recovery(error, body)
        if recovery is None:
            return None
        logger.warning(
            "{}: {} -- retrying once ({})",
            self.log_tag,
            recovery.log_line,
            recovery.evidence,
        )
        return recovery.body, None


def apply_learned_tool_schema_refusals(
    body: Mapping[str, Any], refusals: Sequence[SchemaKeywordRefusal]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Sweep what this host has already refused, before the first send.

    The point of learning the fact at all: after the first 400 the host is
    never asked the same question again, so the retry is paid **once per
    provider** rather than once per request. Deterministic -- the same
    catalogue and the same facts give the same bytes on every turn and after
    every restart -- which is what lets the vendor's implicit tools prefix
    stay cached from the second request onward.

    Returns the body unchanged, **by identity**, when there is nothing to
    apply or nothing offends.
    """

    current: Mapping[str, Any] = body
    applied: list[SchemaRemoval] = []
    words: list[str] = []
    for refusal in refusals:
        swept = _swept_body(current, refusal)
        if swept is None:
            continue
        current, removals = swept
        applied.extend(removals)
        words.append(refusal.words)
    if current is body:
        # Content-identical *and* sharing the very same ``tools`` list and the
        # very same schema dicts inside it, which is the half of the identity
        # contract that decides the bytes. The shallow clone is only so the
        # caller always owns its own top level.
        return dict(body), {}
    return dict(current), {
        TOOL_SCHEMA_PRUNED: describe_removals(
            words, applied, " (learned from this host)"
        )
    }
