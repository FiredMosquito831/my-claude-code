"""The tool catalogue OpenCode's own client ships, and who has to look like it.

**Measured, 2026-09-18/19.** Some time in the twelve silent hours before
2026-09-18 00:52 UTC the OpenCode Zen free tier stopped reading only the five
identity headers and started classifying the request *body*. Since then every
request whose tool catalogue is not OpenCode's own is answered HTTP 403
``FreeTierError`` -- "OpenCode's free tier can only be used from within
OpenCode". MCC's headers did not change (byte-identical 6.74.0 -> 7.26.1) and
no MCC release falls on the boundary; the gate moved, server-side, and the
maintainer said so on the record the same day. The live probe matrix that
found it is in ``specs/INVESTIGATION-ZEN-403-FREETIER.md``; the two rows that
decide this module:

===== ==================================================== ==============
probe tools on the request                                 answer
===== ==================================================== ==============
E     ``bash, edit, glob, grep, read`` (lowercase)         **200**, SSE
F     ``Bash, Edit, Glob, Grep, Read`` (Claude Code's)     403
A     no ``tools`` key at all                              403
H     both casings at once                                 500 server_error
===== ==================================================== ==============

so the difference between working and refused is *spelling*, and probe H is
the design constraint: two tool names that differ only in case are not a
catalogue, they are a collision the model chokes on.

**Where the spellings come from.** Not a blog post and not this investigation's
own guess -- the shipped ``opencode-ai@1.18.31`` bundle
(``bin/opencode.exe``), the same method
:mod:`~my_claude_code.providers.openai_chat.opencode_identity` used for the
headers. The five tool modules each export a name constant::

    PH(HD,{node:()=>...,name:()=>IE,BashTool:()=>HD});  var IE="bash"
    PH(AD,{...,name:()=>m_,EditTool:()=>AD});           var m_="edit"
    PH(ID,{...,name:()=>uE,GlobTool:()=>ID});           var uE="glob"
    PH(DD,{...,name:()=>BE,GrepTool:()=>DD});           var BE="grep"
    PH(_D,{node:()=>vK,name:()=>KE,ReadTool:()=>_D});   var KE="read"

and the roster the agent builder validates against is the literal
``["bash","read","edit","glob","grep","webfetch","task","todowrite",
"websearch","lsp","skill"]``. A capture of the real client's wire on
2026-09-19 (a scratch ``HOME``, ``provider.opencode.options.baseURL`` pointed
at a local recorder, ``opencode run`` on
``muse-spark-1.3-contributor-free``) sent eleven tools, all lowercase:
``bash, edit, glob, grep, read, skill, task, todowrite, webfetch, websearch,
write``.

**Only five are mapped, and that is deliberate.** A mapping is a promise that
the two tools *are the same tool*: Claude Code's ``Bash`` and OpenCode's
``bash`` both run a shell command, ``Read`` and ``read`` both read a file.
Nothing else in either catalogue is close enough to claim -- Claude Code's
``Write`` is not OpenCode's ``write`` in argument shape, and inventing the
correspondence would hand the model a schema under a name whose behaviour it
has been prompted about differently. Everything unmapped goes through the
7.18.1 alias codec, which is what that codec is for.

**And nothing is ever appended.** The obvious cheat -- keep Claude Code's
names and add five lowercase decoys so the classifier sees its catalogue -- is
exactly probe H, and probe H is a 500. It also lies twice over: it tells the
host about tools the model may call and MCC cannot run.

**Scope: the free tier only.** This is a workaround for one vendor's
anti-abuse gate on one tier. A paid Zen model, a Go model with credit, and
every other provider MCC speaks to are entitled to the request MCC has always
sent, byte for byte, and the goldens say so. Scope is therefore *data* -- a
declaration on the profile, read through :class:`FreeTierToolCatalogue` --
and never a model name in a branch.

**Half-life.** The vendor tightened this gate three times in 48 hours and says
plainly that it is policy. Everything here is written to be re-measured: the
spellings are one mapping, the scope is one predicate plus one operator
setting, and ``OPENCODE_CLIENT_IDENTITY=mcc`` turns the whole thing off.

**Every client, not only Claude Code (7.49.1).** Claude Code is one of many
coding agents that reach Zen through MCC, and the five spellings above are
only *its* spellings. OpenCode itself, Pi and the other clients that already
say ``bash``/``read``/``edit`` had the opposite problem: the collision rule
that keeps a stray ``bash`` from shadowing Claude Code's ``Bash`` treated their
own, correct spelling as the stray and hashed it (``bash_37d2b12d5d9abc2a``),
so OpenCode's own catalogue reached OpenCode's free tier with none of its
names. :class:`ToolFamily` declares each client's spellings as data, with
where they were read from, and one family is chosen per request by the tool
names that request carries -- never by a header saying which client sent it,
because the names are what the gate reads and they are there whoever
launched the client (``specs/PR-ZEN-FREE-TIER-ALL-HARNESSES-SPEC.md``).
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from my_claude_code.config.settings import configured_opencode_free_tier_models

from .opencode_identity import MCC_CLIENT_VALUE, opencode_identity_mode

#: Claude Code's spelling -> the spelling OpenCode's own client sends, for the
#: five tools that exist on both sides. Cited above, from the 1.18.31 bundle.
OPENCODE_TOOL_CATALOGUE: Mapping[str, str] = MappingProxyType(
    {
        "Bash": "bash",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Read": "read",
    }
)

#: The whole roster the client validates agent configuration against, kept
#: only so a reader can see what was *not* mapped and why the choice was five.
#: Nothing reads this at runtime.
OPENCODE_BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "bash",
    "read",
    "edit",
    "glob",
    "grep",
    "webfetch",
    "task",
    "todowrite",
    "websearch",
    "lsp",
    "skill",
)

#: Where one family's spellings were read from. ``captured``: the client's own
#: request, recorded on the wire (a local recorder or MCC's request log).
#: ``source``: the client's published source or installed bundle at a pinned
#: version, read, never run against a real home. A row that is neither is not
#: shipped -- a guessed spelling fails silently, as a 403 nobody can trace.
ToolFamilyProvenance = Literal["captured", "source"]


@dataclass(frozen=True, slots=True)
class ToolFamily:
    """One client's spellings for the tools OpenCode's free tier looks for.

    ``spellings`` is ``{client spelling: OpenCode spelling}`` and lists only
    tools that do the same job on both sides -- the rule the five above were
    chosen by. A client with no tool for a role simply has no row for it;
    nothing is invented to fill the gap.
    """

    #: A label for docs, tests and logs; never compared with a harness id.
    name: str
    provenance: ToolFamilyProvenance
    #: The exact place the spellings were read, version included.
    cited: str
    spellings: Mapping[str, str]

    def covers(self, names: frozenset[str]) -> int:
        """How many distinct OpenCode roles this family finds in ``names``."""

        return len({host for client, host in self.spellings.items() if client in names})

    def catalogue(self, names: frozenset[str]) -> Mapping[str, str]:
        """This family's rows that ``names`` carries, one client per role.

        A client may declare two tools for one role (Codex has spelled its
        shell three ways across releases); the first row in declaration order
        claims the OpenCode spelling and the others keep their own names,
        because one host name cannot decode back to two client names.
        """

        chosen: dict[str, str] = {}
        taken: set[str] = set()
        for client, host in self.spellings.items():
            if client in names and host not in taken:
                chosen[client] = host
                taken.add(host)
        return MappingProxyType(chosen)


#: The family whose mapping was the whole of this module from 7.28.0 to 7.49.0.
#: Choosing it returns :attr:`FreeTierToolCatalogue.names` whole -- all five
#: keys, present or not -- because that is the mapping every Claude Code and
#: Agent SDK request has been encoded with, and a body that moved would move
#: the prompt-cache prefix with it.
CLAUDE_CODE_FAMILY = "claude_code"

#: Every client whose spellings MCC translates, in tie-break order: when two
#: families find the same number of roles in one request, the earlier wins.
#: Claude Code is first so that a request carrying both ``Bash`` and ``bash``
#: is encoded exactly as 7.49.0 encoded it.
OPENCODE_TOOL_FAMILIES: tuple[ToolFamily, ...] = (
    ToolFamily(
        name=CLAUDE_CODE_FAMILY,
        provenance="captured",
        cited=(
            "Claude Code and the Claude Agent SDK, request log "
            "(tool_catalogues, harness claude / claude_agent_sdk, 2026-09-19..25); "
            "OpenCode's side from opencode-ai@1.18.31 bin/opencode.exe"
        ),
        spellings=OPENCODE_TOOL_CATALOGUE,
    ),
    ToolFamily(
        name="opencode_native",
        provenance="captured",
        cited=(
            "opencode-ai 1.18.32 `opencode run` wire capture 2026-09-19 (scratch "
            "HOME, local recorder: bash, edit, glob, grep, read, skill, task, "
            "todowrite, webfetch, websearch, write); request log wire_body._names "
            "for harness opencode. Pi sends the same spellings "
            "(@earendil-works/pi-coding-agent 0.82.1 dist/core/sdk.js:132, "
            "defaults read, bash, edit, write; dist/core/tools/index.js:17 all)"
        ),
        spellings=MappingProxyType(
            {name: name for name in ("bash", "read", "edit", "glob", "grep")}
        ),
    ),
    ToolFamily(
        name="codex",
        provenance="captured",
        cited=(
            "@openai/codex 0.155.1 (codex_exec), its request captured on a local "
            "recorder 2026-09-25 under a scratch CODEX_HOME and MCC's own catalogue "
            "entry: exec_command (function, cmd), apply_patch (custom, lark), "
            "write_stdin, request_user_input, view_image, get_goal, create_goal, "
            "update_goal. No read, glob or grep tool: Codex reads and searches "
            "through the shell, so those roles have no row"
        ),
        spellings=MappingProxyType({"exec_command": "bash", "apply_patch": "edit"}),
    ),
    ToolFamily(
        name="gemini_cli",
        provenance="captured",
        cited=(
            "@google/gemini-cli 0.58.0, its request captured 2026-09-25 through a "
            "scratch MCC onto a local recorder (headless: read_file, grep_search, "
            "glob; --yolo adds replace, run_shell_command); the same constants in "
            "its installed bundle/chunk-FQCNOBUR.js:279968-279990"
        ),
        spellings=MappingProxyType(
            {
                "run_shell_command": "bash",
                "read_file": "read",
                "replace": "edit",
                "glob": "glob",
                "grep_search": "grep",
            }
        ),
    ),
    ToolFamily(
        name="qwen_code",
        provenance="captured",
        cited=(
            "@qwen-code/qwen-code 0.15.11, its request captured 2026-09-25 through "
            "a scratch MCC onto a local recorder (headless: read_file, grep_search, "
            "glob; --yolo adds edit, run_shell_command); the same ToolNames in its "
            "installed cli.js:75434-75444"
        ),
        spellings=MappingProxyType(
            {
                "run_shell_command": "bash",
                "read_file": "read",
                "edit": "edit",
                "glob": "glob",
                "grep_search": "grep",
            }
        ),
    ),
    ToolFamily(
        name="commandcode",
        provenance="captured",
        cited=(
            "Command Code's request as MCC's request log stored it "
            "(request_attempts.wire_body._names, harness commandcode_cli: read_file, "
            "write_file, edit_file, read_directory, glob, grep, shell_command, "
            "powershell, ...); the same names in command-code 1.65.0 "
            "dist/bundled/command-code-knowledge/reference/tools.md:26,109,127,140,164"
        ),
        spellings=MappingProxyType(
            {
                "shell_command": "bash",
                "read_file": "read",
                "edit_file": "edit",
                "glob": "glob",
                "grep": "grep",
            }
        ),
    ),
)


def select_tool_family(
    names: frozenset[str], families: tuple[ToolFamily, ...]
) -> ToolFamily | None:
    """The family that finds the most OpenCode roles in one request's names.

    ``None`` when no family finds any. Ties go to declaration order. A pure
    function of the name *set*: a client's catalogue is stable within a
    session, so every turn chooses the same family and encodes identically.
    """

    best: ToolFamily | None = None
    best_roles = 0
    for family in families:
        roles = family.covers(names)
        if roles > best_roles:
            best, best_roles = family, roles
    return best


#: How a Zen or Go model id says out loud that it is on the free tier. Zen
#: spells it with a dash (``muse-spark-1.3-contributor-free``); the colon form
#: is the OpenRouter-style spelling other hosts in the registry use, declared
#: here because the two OpenCode profiles front a registry that carries both.
OPENCODE_FREE_TIER_TAGS: tuple[str, ...] = ("-free", ":free")

#: Characters that may follow a free tag and still leave it a tag rather than
#: the first syllable of a longer word. ``…-free`` and ``…-free-preview`` are
#: the free tier; ``…-freeform`` is a different model.
_TAG_BOUNDARY = frozenset("-:/@._")


def _model_identifiers(model_id: str) -> tuple[str, ...]:
    """The spellings of one model id a scope test may legitimately match.

    Both the id as routed (``opencode/muse-spark-1.3-contributor-free``) and
    the bare model name, because an operator typing a setting writes the name
    and the router carries the prefix, and neither of them is wrong.
    """

    ident = model_id.strip().lower()
    if not ident:
        return ()
    bare = ident.rsplit("/", 1)[-1]
    return (ident,) if bare == ident else (ident, bare)


def carries_free_tag(model_id: str) -> bool:
    """Whether the model id itself says it is a free-tier model."""

    for ident in _model_identifiers(model_id):
        for tag in OPENCODE_FREE_TIER_TAGS:
            start = 0
            while True:
                at = ident.find(tag, start)
                if at < 0:
                    break
                after = at + len(tag)
                if after == len(ident) or ident[after] in _TAG_BOUNDARY:
                    return True
                start = at + 1
    return False


@dataclass(frozen=True, slots=True)
class FreeTierToolCatalogue:
    """One host's declaration: "my free tier wants my own tool names".

    A profile field rather than a check somewhere in the transport, for the
    same reason ``responses_tool_name_max_length`` is a profile field: "this
    host classifies the body" is a property of a deployment. The day the
    vendor stops doing it, this declaration comes off one profile and no
    transport changes.
    """

    #: Client name -> host name, for the tools that exist on both sides.
    names: Mapping[str, str] = field(default=OPENCODE_TOOL_CATALOGUE)
    #: An extra, operator-editable roster of model ids on the free tier, read
    #: per request so a dashboard save takes effect without a release. Held as
    #: a callable for exactly that reason -- a value captured at import could
    #: not follow the save.
    extra_models: Callable[[], tuple[str, ...]] = field(
        default=configured_opencode_free_tier_models
    )
    #: Every client's spellings, chosen between per request by
    #: :meth:`catalogue_for_request`.
    families: tuple[ToolFamily, ...] = field(default=OPENCODE_TOOL_FAMILIES)

    def applies_to(self, model_id: str, *, zero_cost: bool = False) -> bool:
        """Whether one model on this host is inside the free-tier scope.

        Three ways in, in the order they cost anything to answer: the id
        carries the vendor's own free tag; the operator listed it in
        ``OPENCODE_FREE_TIER_MODELS`` (``big-pickle`` is free and untagged,
        which is why the setting exists at all); or the published catalogue
        prices it at zero on this host, which the caller resolves because only
        the caller knows which host it is talking to.
        """

        if not model_id.strip():
            return False
        if carries_free_tag(model_id):
            return True
        listed = {name.strip().lower() for name in self.extra_models()}
        if listed & set(_model_identifiers(model_id)):
            return True
        return zero_cost

    def catalogue_for(
        self, model_id: str, *, zero_cost: bool = False
    ) -> Mapping[str, str]:
        """The mapping one request should encode with; empty means "none".

        Empty for a model outside the scope, and empty whenever the operator
        asked for the truthful identity: ``OPENCODE_CLIENT_IDENTITY=mcc`` says
        "tell this host it is MCC calling", and a request that says it is MCC
        while wearing OpenCode's tool names would be the worse of both claims.
        The opt-out turns off the whole impersonation, not half of it.
        """

        if opencode_identity_mode() == MCC_CLIENT_VALUE:
            return MappingProxyType({})
        if not self.applies_to(model_id, zero_cost=zero_cost):
            return MappingProxyType({})
        return self.names

    def catalogue_for_request(
        self, model_id: str, names: Iterable[str], *, zero_cost: bool = False
    ) -> Mapping[str, str]:
        """The mapping one request should encode with, chosen by its tool names.

        The scope and the opt-out are :meth:`catalogue_for`'s, unchanged: a
        request that one answers "none" for is answered "none" here too, so a
        paid model, a Go model with credit and ``OPENCODE_CLIENT_IDENTITY=mcc``
        send what they always sent. Inside the scope, the family that finds the
        most roles in ``names`` supplies the mapping. Claude Code's family, or
        no family at all, returns exactly what :meth:`catalogue_for` returns;
        any other returns only its rows this request carries.

        ``names`` is every tool name the request carries -- its tools, a forced
        ``tool_choice`` and the ``tool_use`` blocks it replays -- the same set
        the codec is built from, so encode and decode choose alike.
        """

        static = self.catalogue_for(model_id, zero_cost=zero_cost)
        if not static:
            return static
        present = frozenset(names)
        family = select_tool_family(present, self.families)
        if family is None or family.name == CLAUDE_CODE_FAMILY:
            return static
        return family.catalogue(present)


#: The one instance both OpenCode profiles carry, so Zen and Go cannot drift
#: apart -- the same argument :data:`OPENCODE_CLIENT_IDENTITY` is written for.
OPENCODE_FREE_TIER_CATALOGUE = FreeTierToolCatalogue()


def model_is_zero_cost(provider_id: str, model_id: str) -> bool:
    """Whether the published catalogue prices this model at zero on this host.

    The third door into the scope. Imported where it is used rather than at
    module scope because the registry reader is a heavy leaf of the provider
    tree and this module is imported by ``profiles.py``, which is a module-level
    dict literal every provider build walks.

    A model nobody has published a price for is **not** free: ``None`` means
    "not stated", and treating silence as zero would pull every unlisted model
    into the scope.
    """

    from my_claude_code.providers.runtime.models_dev import model_prices_tiered

    try:
        prices = model_prices_tiered(provider_id, model_id)
    except OSError, ValueError, KeyError:
        # A price is never worth a failed request.
        return False
    stated = [prices[field_name][0] for field_name in ("input_price", "output_price")]
    if any(value is None for value in stated):
        return False
    return all(value == 0.0 for value in stated)


__all__ = [
    "CLAUDE_CODE_FAMILY",
    "OPENCODE_BUILTIN_TOOL_NAMES",
    "OPENCODE_FREE_TIER_CATALOGUE",
    "OPENCODE_FREE_TIER_TAGS",
    "OPENCODE_TOOL_CATALOGUE",
    "OPENCODE_TOOL_FAMILIES",
    "FreeTierToolCatalogue",
    "ToolFamily",
    "ToolFamilyProvenance",
    "carries_free_tag",
    "model_is_zero_cost",
    "select_tool_family",
]
