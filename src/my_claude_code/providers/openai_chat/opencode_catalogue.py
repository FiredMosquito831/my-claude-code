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
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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
    "OPENCODE_BUILTIN_TOOL_NAMES",
    "OPENCODE_FREE_TIER_CATALOGUE",
    "OPENCODE_FREE_TIER_TAGS",
    "OPENCODE_TOOL_CATALOGUE",
    "FreeTierToolCatalogue",
    "carries_free_tag",
    "model_is_zero_cost",
]
