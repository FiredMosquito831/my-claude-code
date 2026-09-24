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

**Repair before removal (7.39.0).** A dialect may also declare that a
``pattern``'s Unicode property escapes are *translated* rather than left for
the host to refuse: :mod:`my_claude_code.core.tool_schema_patterns` (ported
from upstream 39b9c272) expands ``\\p{Cc}``, ``\\p{Cf}``, ``\\p{Zl}`` and
``\\p{Zp}`` inside a negated class into the explicit ranges they stand for.
It runs *first*, so a pattern whose only problem is ``\\p{}`` keeps its
restriction, and one that also carries lookaround -- Claude Code's own
``Artifact`` pattern, ``^(?!__.*__$)[^\\p{Cc}...]{1,200}$`` -- is translated
and then dropped by the lookaround refusal exactly as 7.38.0 dropped it: the
translation does **not** rescue that tool's pattern. A translation that
survived is recorded under ``tool_schema_translated``; one whose pattern was
then dropped is not, because it never reached the wire.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.core.tool_schema_patterns import translate_pattern
from my_claude_code.providers.recovery import (
    REGEX_CONSTRUCTS,
    TOOL_SCHEMA_PRUNED,
    TOOL_SCHEMA_TRANSLATED,
    RegexConstruct,
    SchemaKeywordRefusal,
    SchemaRemoval,
    describe_removals,
    prune_tool_catalogue,
    rewrite_schema_keyword,
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

    ``translate_unicode_properties`` repairs before anything is removed: a
    ``pattern`` whose ``\\p{Cc}``/``\\p{Cf}``/``\\p{Zl}``/``\\p{Zp}`` atoms can
    be expanded into explicit ranges is rewritten, and one that cannot is
    left exactly as written (the rung answers it if the host refuses it).
    """

    #: How the wire record names where a removal came from. A word an
    #: operator reads in the request modal, never a code path.
    name: str
    refused: tuple[SchemaKeywordRefusal, ...] = ()
    translate_unicode_properties: bool = False
    #: The most tools this family of validators accepts in one request, or
    #: ``None`` when nobody knows -- which means **no cap**, and is what every
    #: declared dialect says today. OpenAI's platform API documents 128, but
    #: no MCC host is that API: the ChatGPT/Codex backend has served 137, and
    #: OpenCode's Responses door has never been measured. A host that is shown
    #: to cap states the number in its 400, and the rung learns it; a dialect
    #: declares one only once the number is known for that whole family.
    #: Applied by the sender before the first send (the smaller of this and a
    #: learned number wins), never inside ``_convert_tools``: the cut must see
    #: the history and ``tool_choice``, which the converter does not.
    tools_max_count: int | None = None


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
#:
#: And, since 7.39.0, the upstream Unicode-property translation (decision 3 of
#: the second-pass review: Responses only, run before the refusal, knowing it
#: does not rescue the ``Artifact`` pattern, whose lookahead still goes).
RESPONSES_TOOL_SCHEMA_DIALECT = ToolSchemaDialect(
    name="responses",
    refused=(
        SchemaKeywordRefusal(keyword="pattern", construct=_construct("lookaround")),
    ),
    translate_unicode_properties=True,
)

#: A host whose validator is declared to refuse nothing. Not the default for
#: anybody; it exists so a host proven to accept every construct can say so as
#: data rather than by bypassing the seam.
PERMISSIVE_TOOL_SCHEMA_DIALECT = ToolSchemaDialect(name="permissive")


@dataclass(frozen=True, slots=True)
class SchemaTranslation:
    """One ``pattern`` the dialect repaired in place, and where it was."""

    tool: str
    path: str


#: How many translated paths one wire record names before it summarises.
MAX_RECORDED_TRANSLATIONS = 8


@dataclass(frozen=True, slots=True)
class DialectSweep:
    """One catalogue after the dialect, and what left it."""

    tools: list[Any]
    removals: tuple[SchemaRemoval, ...] = ()
    #: The refusals that actually removed something, in sweep order.
    applied: tuple[SchemaKeywordRefusal, ...] = ()
    #: Patterns translated and still on the wire (a translated pattern the
    #: refusal then dropped is a removal, not a translation).
    translations: tuple[SchemaTranslation, ...] = ()

    def wire_marker(self, dialect: ToolSchemaDialect) -> dict[str, str]:
        """The ``params.wire`` record, or ``{}`` when nothing changed."""

        marker: dict[str, str] = {}
        provenance = f" (declared {dialect.name} dialect)"
        if self.translations:
            marker[TOOL_SCHEMA_TRANSLATED] = describe_translations(
                self.translations, provenance
            )
        if self.removals:
            marker[TOOL_SCHEMA_PRUNED] = describe_removals(
                [refusal.words for refusal in self.applied],
                self.removals,
                provenance,
            )
        return marker


def describe_translations(
    translations: Sequence[SchemaTranslation], provenance: str = ""
) -> str:
    """The ``tool_schema_translated`` sentence: names and paths, never a regex."""

    tools = sorted({translation.tool for translation in translations})
    where = ", ".join(
        f"{translation.tool} {translation.path}"
        for translation in translations[:MAX_RECORDED_TRANSLATIONS]
    )
    if len(translations) > MAX_RECORDED_TRANSLATIONS:
        where += f", +{len(translations) - MAX_RECORDED_TRANSLATIONS} more"
    noun = "tool" if len(tools) == 1 else "tools"
    return (
        "translated Unicode property escapes in pattern of "
        f"{len(tools)} {noun}{provenance}: {where}"
    )


def translate_schema_patterns(
    schema: Any, visit: Callable[[str, Any], None] | None = None, path: str = "$"
) -> Any:
    """``schema`` with every translatable ``pattern`` repaired, copy-on-write.

    The shared schema walker (the one the rung prunes with) with the upstream
    translator as its rewrite: instance data is never read as schema, a
    property *called* ``pattern`` is a property, and ``schema`` itself comes
    back by identity when nothing was translated.
    """

    def _translate(value: Any) -> Any:
        return translate_pattern(value) if isinstance(value, str) else value

    return rewrite_schema_keyword(schema, "pattern", _translate, path, visit)


def _translate_catalogue(
    tools: Sequence[Any],
) -> tuple[Sequence[Any], list[SchemaTranslation]]:
    """Translate every tool's parameters; ``tools`` by identity if none moved."""

    translations: list[SchemaTranslation] = []
    result: list[Any] = []
    changed = False
    for tool in tools:
        parameters = tool.get("parameters") if isinstance(tool, Mapping) else None
        if parameters is None:
            result.append(tool)
            continue
        name = str(tool.get("name") or "unknown")

        def _record(where: str, _value: Any, name: str = name) -> None:
            translations.append(SchemaTranslation(tool=name, path=where))

        translated = translate_schema_patterns(parameters, _record)
        if translated is parameters:
            result.append(tool)
            continue
        changed = True
        result.append({**tool, "parameters": translated})
    if not changed:
        return tools, []
    return result, translations


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
    translations: list[SchemaTranslation] = []
    if dialect.translate_unicode_properties:
        # Repair first, so a pattern whose only problem is ``\p{}`` keeps its
        # restriction; the refusals below then drop whatever still offends.
        current, translations = _translate_catalogue(current)
    for refusal in dialect.refused:
        pruned, removed = prune_tool_catalogue(current, refusal)
        if not removed:
            continue
        current = pruned
        removals.extend(removed)
        applied.append(refusal)
    if current is tools:
        return DialectSweep(tools=tools)
    dropped = {(removal.tool, removal.path) for removal in removals}
    return DialectSweep(
        tools=list(current),
        removals=tuple(removals),
        applied=tuple(applied),
        translations=tuple(
            translation
            for translation in translations
            if (translation.tool, translation.path) not in dropped
        ),
    )
