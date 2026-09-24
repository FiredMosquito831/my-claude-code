"""What a Responses host's validator refuses in a tool schema, declared as data.

7.36.0 taught both Responses senders to *answer* a schema refusal: one 400, one
swept retry, and a learned fact so the next request is swept before it leaves.
That is the right tool for the construct nobody has met yet, and the wrong one
for the construct everybody has. OpenAI's Responses validator checks every
tool's ``pattern`` with the Rust ``regex`` crate, which has no lookaround at
all, and JavaScript-authored MCP schemas carry lookaround routinely because V8
accepts it -- the appium-mcp screen recorder spells "end of input"
``(?![\\s\\S])``. On a fresh process, a new provider, or after *Forget*, the
learned route still pays that 400 once. This module is what makes it zero.

It is the learned facts of the rung, promoted into source:

* **The vocabulary is data.** A :class:`ToolSchemaDialect` is a tuple of the
  rung's own :class:`SchemaKeywordRefusal` values -- the same ``(keyword,
  construct)`` pair a 400 teaches, drawn from the same
  :data:`REGEX_CONSTRUCTS` table -- so a construct the rung can learn is a
  construct a host can declare, with one table and no second matcher.
* **It runs for every Responses host.** The sweep sits inside
  ``_convert_tools``, the one place a Responses tool definition is built, and
  a host that declares nothing inherits :data:`RESPONSES_TOOL_SCHEMA_DIALECT`.
  ``anomalyco/opencode`` #49434 is what a per-host opt-in costs: their
  sanitiser existed, one adapter bypassed it, and the same lookaround 400 came
  back. What a host declares is *what* its validator refuses, never *whether*
  the seam runs.
* **Identity when nothing offends.** The sweep is the rung's own
  :func:`prune_tool_catalogue`, which returns the very list it was handed when
  nothing was removed -- so a catalogue without an offending construct leaves
  as the same objects, the serialised bytes cannot have moved, and neither has
  the vendor's implicit tools prefix.
* **Deterministic.** The same catalogue and the same dialect give the same
  bytes on every turn and after every restart; the prompt cache sees the
  removal at most once, on the first request that carries the tool.

What it removes is recorded in ``params.wire`` under the same
``tool_schema_pruned`` key the rung and the learned sweep write, names and
paths only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.providers.recovery import (
    REGEX_CONSTRUCTS,
    TOOL_SCHEMA_PRUNED,
    RegexConstruct,
    SchemaKeywordRefusal,
    SchemaRemoval,
    describe_removals,
    prune_tool_catalogue,
)


@dataclass(frozen=True, slots=True)
class ToolSchemaDialect:
    """The schema constructs one family of Responses validators refuses.

    ``refused`` is swept in order, each entry exactly as the rung would sweep
    it had the host just refused it: a ``pattern`` refusal with a construct
    drops only the patterns that carry the construct, and every other keyword,
    property, ``type`` and ``description`` stays. An empty tuple is a real
    declaration -- "this validator refuses nothing" -- and sends every schema
    exactly as the client wrote it.
    """

    #: How the wire record names where a removal came from. A word an
    #: operator reads in the request modal, never a code path.
    name: str
    refused: tuple[SchemaKeywordRefusal, ...] = ()


def _construct(name: str) -> RegexConstruct:
    """One row of the rung's construct table, by its stable name."""

    for construct in REGEX_CONSTRUCTS:
        if construct.name == name:
            return construct
    raise LookupError(f"no regex construct named {name!r}")


#: What every Responses host is assumed to refuse unless it declares
#: otherwise. Lookaround in a ``pattern`` and nothing more: it is the one
#: construct measured on this surface (216 refusals of
#: ``$.properties.videoScale.pattern`` on 2026-09-20), OpenAI documents the
#: engine as the Rust ``regex`` crate, and Codex CLI -- which sends the same MCP
#: tools to the same endpoint -- cannot express ``pattern`` at all, so dropping
#: one offending ``pattern`` is strictly gentler than what that host already
#: receives every day. Anything else a host refuses is taught by the 400
#: through the rung, remembered, and swept before the next send.
RESPONSES_TOOL_SCHEMA_DIALECT = ToolSchemaDialect(
    name="responses",
    refused=(
        SchemaKeywordRefusal(keyword="pattern", construct=_construct("lookaround")),
    ),
)

#: A host whose validator is declared to refuse nothing. Not the default for
#: anybody; it exists so a host proven to accept every construct can say so as
#: data rather than by bypassing the seam.
PERMISSIVE_TOOL_SCHEMA_DIALECT = ToolSchemaDialect(name="permissive")


@dataclass(frozen=True, slots=True)
class DialectSweep:
    """One catalogue after the dialect, and what left it."""

    tools: list[Any]
    removals: tuple[SchemaRemoval, ...] = ()
    #: The refusals that actually removed something, in sweep order.
    applied: tuple[SchemaKeywordRefusal, ...] = ()

    def wire_marker(self, dialect: ToolSchemaDialect) -> dict[str, str]:
        """The ``params.wire`` record, or ``{}`` when nothing was removed."""

        if not self.removals:
            return {}
        return {
            TOOL_SCHEMA_PRUNED: describe_removals(
                [refusal.words for refusal in self.applied],
                self.removals,
                f" (declared {dialect.name} dialect)",
            )
        }


def sweep_tool_catalogue(tools: list[Any], dialect: ToolSchemaDialect) -> DialectSweep:
    """Drop what ``dialect`` refuses from a converted tool list, copy-on-write.

    Returns ``tools`` itself -- the same list object, holding the same dicts --
    when nothing offends. A tool that loses a keyword is a shallow copy with a
    copied ``parameters`` path down to the removal; the client's own dicts are
    never mutated, because they are shared across attempts and across models
    in one route.
    """

    current: Sequence[Any] = tools
    removals: list[SchemaRemoval] = []
    applied: list[SchemaKeywordRefusal] = []
    for refusal in dialect.refused:
        pruned, removed = prune_tool_catalogue(current, refusal)
        if not removed:
            continue
        current = pruned
        removals.extend(removed)
        applied.append(refusal)
    if current is tools:
        return DialectSweep(tools=tools)
    return DialectSweep(
        tools=list(current), removals=tuple(removals), applied=tuple(applied)
    )
