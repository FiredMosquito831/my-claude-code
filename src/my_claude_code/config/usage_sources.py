"""Which providers MCC can show a usage figure for, and how it arrives.

Declared data, in one place, so the read-only admin route and the Usage page
never grow a provider-name branch (IV.6), and so a provider that publishes
nothing is *said* to publish nothing rather than being left blank (IV.16).

This is a **leaf**: nothing first-party is imported here, which is the
``config`` package's own rule (III.5) and is pinned by
``tests/contracts/test_import_boundaries.py``.

TWO MECHANISMS, and the difference between them is the difference between
three facts that must never be collapsed (IV.16):

``response-header``
    The figure arrived on a response MCC already made. No request of its own,
    no credential, nothing to schedule: the cheapest reading there is.
``provider-endpoint``
    The figure needs a read of its own against the host (DeepSeek's
    ``/user/balance``, OpenRouter's ``/api/v1/key``). 8.5.0 builds the seven
    rows that need **no new credential**; a row whose ``endpoint`` is
    ``None`` is one this release does not read, and the page says
    ``Not read yet`` rather than pretending the provider publishes nothing.

THE ENDPOINT IS DECLARED, AND THE CODE THAT READS IT IS NOT A BRANCH. A row's
:class:`UsageEndpoint` names the adapter module under
:mod:`my_claude_code.core.usage` that knows its response shape, the host and
path it goes to, the credential **kind** that carries it, and the provenance of
the claim. ``application/usage_reads.py`` looks the adapter up **by that
declared name**; there is nowhere in the reading code that says
``if provider_id == "deepseek"``. Adding a provider is adding a row and a
module, which is the whole point of the table.

THE PROVENANCE IS ON THE ROW, because §3 of
``specs/RESEARCH-BILLING-ENDPOINT-DISCOVERY.md`` is binding: **only a verified
row may produce a number.** The three words are its own:

``vendor-doc``
    The provider published it. The shape is documented and the numbers are the
    provider's contract.
``vendor-binary``
    The route was read out of the vendor's own installed client. The route and
    the field *names* are confirmed; the nesting is not (no live call was made
    to confirm it -- LIVE CALLS: 0).
``third-party``
    Four independent open-source implementations agree on the route and none of
    them is the vendor (OpenCode Go: `INVESTIGATION-USAGE-AND-CREDITS.md` §8
    Q4's amendment, `RESEARCH-BILLING-ENDPOINT-DISCOVERY.md` §3.5). It is
    declared-verified data, not a guess, and the page says so.

**What is not here, at all**: a management key (OpenRouter's ``/credits``, which
answers an ordinary key with a 403), a browser cookie, an admin key, the
OpenAI/Anthropic organisation APIs, Z.ai, MiniMax, Copilot and Kimi Coding. A
provider MCC has no row for is ``unsupported`` by absence, and the page prints
one sentence -- the majority case, which is why the sentence is not an edge.

A provider may hold **both** mechanisms -- DeepSeek and OpenRouter do: they can
send ``x-ratelimit-*`` on ordinary traffic *and* they publish a balance
endpoint. So a row carries a *tuple* of mechanisms rather than one word, and
the differences never have to be encoded by listing an id twice in two tables
and hoping the caller checks them in the right order.
"""

from dataclasses import dataclass

__all__ = [
    "CREDENTIAL_API_KEY",
    "CREDENTIAL_OAUTH_STORE",
    "MECHANISM_ENDPOINT",
    "MECHANISM_HEADER",
    "PROVENANCE_THIRD_PARTY",
    "PROVENANCE_VENDOR_BINARY",
    "PROVENANCE_VENDOR_DOC",
    "PROVIDER_SOURCES",
    "SOURCE_UNSUPPORTED",
    "UsageEndpoint",
    "UsageSource",
    "endpoint_providers",
    "header_capture_providers",
    "source_for",
]

#: The two mechanism words. Short, stable, and the only values a row's
#: ``mechanisms`` ever takes.
MECHANISM_HEADER = "response-header"
MECHANISM_ENDPOINT = "provider-endpoint"

#: What a provider with no mechanism at all reports.
SOURCE_UNSUPPORTED = "unsupported"

#: The two credential **kinds** a declared read may use. There is no third:
#: a read uses a credential MCC already holds, from the source it already
#: reads (§6.1.1). A provider needing anything else is not on this table.
CREDENTIAL_API_KEY = "api-key-env"
CREDENTIAL_OAUTH_STORE = "oauth-store"

#: §3's provenance ladder, in the words the page prints.
PROVENANCE_VENDOR_DOC = "vendor-doc"
PROVENANCE_VENDOR_BINARY = "vendor-binary"
PROVENANCE_THIRD_PARTY = "third-party"


@dataclass(frozen=True, slots=True)
class UsageEndpoint:
    """One declared read: where it goes, what carries it, how it is read.

    ``adapter`` is a module name under :mod:`my_claude_code.core.usage` -- the
    only loose coupling in the whole feature, and deliberately a *name in
    data* rather than an import statement here, so ``config`` stays a leaf
    (III.5) and the reader has one lookup instead of a chain of ``if``s.
    """

    #: The adapter module that knows this row's response shape.
    adapter: str
    #: Absolute origin. Never a third party: the credential goes to the host
    #: the inference request already goes to (§6.1.3).
    host: str
    #: The path on that host, leading slash included.
    path: str
    #: ``api-key-env`` or ``oauth-store`` -- the *kind*, never a value.
    credential: str
    #: For ``api-key-env``: the variable the existing accessor reads. Empty for
    #: an OAuth row, whose account store is named by ``oauth_provider``.
    credential_env: str = ""
    #: For ``oauth-store``: which account store supplies the token.
    oauth_provider: str = ""
    #: Extra request headers this route needs, as ``(name, value)``. A beta
    #: header, an account id, a client-version claim -- declared, dated, and
    #: visible in one place, because §3.9.8 is right that private routes rot
    #: and a literal buried in a call site is how nobody notices.
    headers: tuple[tuple[str, str], ...] = ()
    provenance: str = PROVENANCE_VENDOR_DOC
    #: What this row actually yields, in a reader's words, for the card.
    reads: str = ""
    #: Where the account id travels, when a row needs one: ``(header name)``.
    #: The *value* comes from the store, never from this table.
    account_header: str = ""

    @property
    def url(self) -> str:
        return f"{self.host.rstrip('/')}{self.path}"

    @property
    def label(self) -> str:
        """The provenance line's "read from …" clause."""

        return f"{self.host.split('//')[-1].split('/')[0]}{self.path}"


@dataclass(frozen=True, slots=True)
class UsageSource:
    """One provider's mechanisms, and the words the page needs to explain them.

    ``detail`` is what a card prints on its provenance line for the mechanism
    that is actually installed -- the source of the figure, in a reader's own
    words. Empty for a provider with no mechanism, which has no provenance to
    give.
    """

    provider_id: str
    display_name: str
    #: Which mechanisms this provider has. Empty means ``unsupported``.
    #: Ordered least-cost-first, which is also the order the page prefers.
    mechanisms: tuple[str, ...] = ()
    #: Where a header-derived figure comes from, for the provenance line.
    detail: str = ""
    #: The declared read, when this provider has one MCC builds.
    endpoint: UsageEndpoint | None = None

    @property
    def supported(self) -> bool:
        """Whether any figure for this provider is reachable at all."""
        return bool(self.mechanisms)

    @property
    def source(self) -> str:
        """The mechanism the page should describe: the cheapest one present."""
        return self.mechanisms[0] if self.mechanisms else SOURCE_UNSUPPORTED

    @property
    def readable(self) -> bool:
        """Whether this row's read exists in this release (an adapter, not a plan)."""

        return self.endpoint is not None


#: Every provider MCC has an opinion about. A provider that is not here is
#: ``unsupported`` by absence -- see :func:`source_for` -- which is what keeps
#: this table from having to enumerate the catalogue's endpoint-less majority.
#:
#: ``commandcode`` is the one row whose header mechanism spans two protocol
#: families: the half of its catalogue that speaks Anthropic Messages is
#: covered by the generic observer installed on that family's *existing*
#: ``set_response_observer`` seam, and its Chat Completions half by the mirror
#: seam. Both are this one provider's response headers, so it is one row.
#:
#: **None of the header rows is confirmed to send a usage header to an MCC
#: caller.** The Nous names are third-party evidence (see
#: ``core/usage_capture.py`` for the exact provenance); ``x-ratelimit-*`` is a
#: de-facto convention rather than a promise. A host that sends nothing stays
#: ``Not read yet`` for ever, which is the honest answer -- and the reason
#: ``detail`` says where a figure WOULD arrive rather than claiming one does.
PROVIDER_SOURCES: tuple[UsageSource, ...] = (
    UsageSource(
        provider_id="nous_portal",
        display_name="Nous Portal",
        mechanisms=(MECHANISM_HEADER,),
        detail="Nous Portal's credits response headers",
    ),
    UsageSource(
        provider_id="open_router",
        display_name="OpenRouter",
        mechanisms=(MECHANISM_HEADER, MECHANISM_ENDPOINT),
        detail="the x-ratelimit response headers",
        endpoint=UsageEndpoint(
            adapter="openrouter",
            host="https://openrouter.ai",
            # ``/api/v1/key`` -- NOT ``/api/v1/credits``, which needs a
            # management key the user does not hold and MCC must never ask
            # for (§3.2, USER DECISIONS). A key's own spend and its remaining
            # ceiling are the question this row can answer honestly.
            path="/api/v1/key",
            credential=CREDENTIAL_API_KEY,
            credential_env="OPENROUTER_API_KEY",
            provenance=PROVENANCE_VENDOR_DOC,
            reads="this key's spend and its remaining limit",
        ),
    ),
    UsageSource(
        provider_id="deepseek",
        display_name="DeepSeek",
        mechanisms=(MECHANISM_HEADER, MECHANISM_ENDPOINT),
        detail="the x-ratelimit response headers",
        endpoint=UsageEndpoint(
            adapter="deepseek",
            host="https://api.deepseek.com",
            path="/user/balance",
            credential=CREDENTIAL_API_KEY,
            credential_env="DEEPSEEK_API_KEY",
            provenance=PROVENANCE_VENDOR_DOC,
            reads="the account's balance, per currency",
        ),
    ),
    UsageSource(
        provider_id="kimi",
        display_name="Kimi (Moonshot)",
        mechanisms=(MECHANISM_HEADER, MECHANISM_ENDPOINT),
        detail="the x-ratelimit response headers",
        endpoint=UsageEndpoint(
            adapter="kimi",
            host="https://api.moonshot.ai",
            path="/v1/users/me/balance",
            credential=CREDENTIAL_API_KEY,
            credential_env="KIMI_API_KEY",
            provenance=PROVENANCE_VENDOR_DOC,
            reads="the account's available, voucher and cash balances",
        ),
    ),
    UsageSource(
        provider_id="commandcode",
        display_name="Command Code",
        mechanisms=(MECHANISM_HEADER, MECHANISM_ENDPOINT),
        detail="the x-ratelimit response headers",
        endpoint=UsageEndpoint(
            adapter="commandcode",
            host="https://api.commandcode.ai",
            path="/alpha/billing/credits",
            credential=CREDENTIAL_API_KEY,
            credential_env="COMMANDCODE_API_KEY",
            # Read out of the installed ``command-code`` 1.65.0 CLI, which the
            # vendor's own usage view is built on (§3.1). The route and the
            # field names are the vendor's; nothing live was called.
            provenance=PROVENANCE_VENDOR_BINARY,
            reads="the credit pool this account has left",
        ),
    ),
    UsageSource(
        provider_id="opencode_go",
        display_name="OpenCode Go",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="",
        endpoint=UsageEndpoint(
            adapter="opencode_go",
            host="https://opencode.ai",
            # Four independent implementations call exactly this path with the
            # Go key as a Bearer token (§3.6, §3.5 of the research dossier;
            # Poracode `openCodeGoApi.ts:13`, CodexBar
            # `OpenCodeGoUsageFetcher.swift:34`, Pulse
            # `OpenCodeGoUsageService.swift:32`, TokenBar
            # `local_auth_b_providers.dart:914`). The user's 2-call probe was
            # WITHDRAWN as unnecessary, so this ships third-party-verified --
            # and the card says so, because it is not the vendor's own word.
            path="/zen/go/v1/usage",
            credential=CREDENTIAL_API_KEY,
            credential_env="OPENCODE_API_KEY",
            provenance=PROVENANCE_THIRD_PARTY,
            reads="the rolling, weekly and monthly Go windows",
        ),
    ),
    UsageSource(
        provider_id="anthropic_oauth",
        display_name="Claude subscription",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="",
        endpoint=UsageEndpoint(
            adapter="claude",
            host="https://api.anthropic.com",
            path="/api/oauth/usage",
            credential=CREDENTIAL_OAUTH_STORE,
            oauth_provider="anthropic_oauth",
            # The beta header is not decoration: §3.1 records that Claude
            # Code's own call carries it, and "a bare call is not the CLI's
            # call".
            headers=(("anthropic-beta", "oauth-2025-04-20"),),
            provenance=PROVENANCE_VENDOR_BINARY,
            reads="the 5-hour and weekly windows, per-model weekly windows, "
            "and extra-usage dollars",
        ),
    ),
    UsageSource(
        provider_id="chatgpt_oauth",
        display_name="ChatGPT / Codex subscription",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="",
        endpoint=UsageEndpoint(
            adapter="codex",
            host="https://chatgpt.com",
            path="/backend-api/wham/usage",
            credential=CREDENTIAL_OAUTH_STORE,
            oauth_provider="chatgpt_oauth",
            account_header="ChatGPT-Account-ID",
            provenance=PROVENANCE_VENDOR_BINARY,
            reads="the plan's primary and secondary windows and its credits",
        ),
    ),
    # Declared, and deliberately NOT read. Kimi *Coding* is a different product
    # from the Moonshot platform row above: its route is undocumented, its only
    # evidence is third-party, and the user's decision is that it ships only if
    # a probe settles it -- which no release has run. So the row keeps its
    # mechanism and has no endpoint: the page says "not read yet", never "this
    # provider publishes nothing" (IV.16), and never a number.
    UsageSource(
        provider_id="kimi_coding",
        display_name="Kimi Coding",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="the route is third-party evidence only; MCC does not read it yet",
    ),
    # Named because they are in the user's own list and are the reference cases
    # for the state-3 sentence. Absence from this table would give the same
    # answer; naming them makes the answer checkable in one place.
    UsageSource(provider_id="novita", display_name="Novita"),
    UsageSource(provider_id="cline", display_name="Cline"),
)

_BY_ID: dict[str, UsageSource] = {row.provider_id: row for row in PROVIDER_SOURCES}


def source_for(provider_id: str) -> UsageSource:
    """What MCC knows about one provider's usage mechanism.

    An id in :data:`PROVIDER_SOURCES` gets its own row; every other id --
    every custom provider, and the catalogue's endpoint-less majority -- is
    ``unsupported``. Never raises and never returns ``None``: the page asks
    this once per configured provider, and a missing entry must degrade to the
    honest sentence rather than to a blank row.
    """

    row = _BY_ID.get(provider_id)
    if row is not None:
        return row
    return UsageSource(provider_id=provider_id, display_name=provider_id)


def header_capture_providers() -> tuple[str, ...]:
    """The catalogue ids whose usage figure rides on ordinary responses.

    Read by the installer sites and by the contract test that every provider
    claiming header capture actually has a provider class that installs the
    observer -- so the table and the code cannot drift into disagreeing.
    """

    return tuple(
        row.provider_id
        for row in PROVIDER_SOURCES
        if MECHANISM_HEADER in row.mechanisms
    )


def endpoint_providers() -> tuple[str, ...]:
    """The ids with a read this release actually performs (an adapter exists).

    A row may declare ``MECHANISM_ENDPOINT`` and have no adapter -- that is
    how "this provider publishes something MCC cannot read yet" is expressed
    without lying in either direction. This function returns only the rows
    whose read is built, and the contract test pairs it with the modules on
    disk so the table and the package cannot drift.
    """

    return tuple(row.provider_id for row in PROVIDER_SOURCES if row.readable)
