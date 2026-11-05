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
    "GATEWAY_FAMILIES",
    "MECHANISM_ENDPOINT",
    "MECHANISM_HEADER",
    "PROVENANCE_THIRD_PARTY",
    "PROVENANCE_VENDOR_BINARY",
    "PROVENANCE_VENDOR_DOC",
    "PROVIDER_SOURCES",
    "SOURCE_UNSUPPORTED",
    "STAGE_DISCOVERY",
    "STAGE_READ",
    "UsageEndpoint",
    "UsageFamily",
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
    #: 8.7.0: the route this provider publishes but MCC cannot read, and WHY.
    #: Empty for a provider that publishes nothing at all (`unsupported`) and
    #: for one whose read is built (`readable`). The value is the credential
    #: the route needs and the user does not have, in a reader's words --
    #: "a management key", "a browser session cookie", "an admin key". It is
    #: the SEVENTH of §2's eight states: *needs a credential MCC does not
    #: hold*. Named on the row rather than branched on in the renderer, so a
    #: later release that holds one of these credentials changes one string
    #: and a row moves from that state to `readable`.
    needs_credential: str = ""
    #: **This provider's subscription windows are drawn as percentages, and
    #: nothing else.**
    #:
    #: `specs/PR-USAGE-LAYOUTS-SPEC.md` §9.1 is explicit that this is a
    #: *provider-scoped presentation rule in declared data* — a flag on the
    #: provider's usage row — **not** an ``if provider_id == …`` in a renderer
    #: and **not** a heuristic on the shape of the numbers. So it lives here,
    #: beside ``needs_credential`` and ``mechanisms``, and travels to the page on
    #: the row it already reads.
    #:
    #: The user's reason for the one row that sets it (2026-11-03): Command
    #: Code's window is denominated in model-equivalent credits that move under
    #: the same percentage whenever prices change, so a dollar figure beside it
    #: invites a comparison with real money elsewhere on the page.
    #:
    #: **It covers that provider's windows and nothing else.** A balance is
    #: money and stays money — Command Code's own ``monthlyCredits``,
    #: ``purchased`` and ``free`` rows are outside the rule — and every other
    #: provider's amounts render exactly as they render today (§9.1,
    #: "do not generalise this rule"). A used/limit pair is not evidence of
    #: anything, which is why this is a word in a table rather than a branch.
    percent_only_windows: bool = False
    #: One sentence the card prints beside a provider whose windows are
    #: percentage-only, saying what that rule leaves out and why.
    #:
    #: §9.3: when the monthly grant is not stated by the API there is **no
    #: honest percentage**, so there is **no monthly row at all** — and the PR
    #: "says so in one sentence on the card rather than inventing a cap". This
    #: is that sentence, in declared data, so no renderer invents its own.
    percent_only_note: str = ""

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

    @property
    def credential_bound(self) -> bool:
        """Whether the provider publishes a route MCC cannot use without a new secret.

        §2's seventh state. It is checked BEFORE `unsupported`, so a provider
        whose route exists and needs a management key reads as *needs a
        credential MCC does not hold* rather than as "publishes nothing" --
        which is the difference between a fact about the user's keyring and a
        fact about the provider (IV.16).
        """

        return bool(self.needs_credential) and not self.readable


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
        # SPEC §9.1, the ONE row this applies to. The user's words: "the % thing
        # only applies for the cmdc sub the other providers remain as is". Its
        # BALANCES are outside the rule and render as money, unchanged.
        percent_only_windows=True,
        # SPEC §9.3, and the honest half of option (B): the monthly percentage
        # exists only when the API states the monthly grant. Command Code's
        # ``/alpha/billing/credits`` states ``windowLimits.fiveHour`` and
        # ``windowLimits.weekly`` as used/cap pairs, and states the monthly
        # figure as a REMAINING POOL (``credits.monthlyCredits``) with no
        # denominator anywhere in the record — see the adapter's docstring for
        # the vendor-binary evidence. So there is no honest monthly percentage,
        # the row is omitted entirely, and this sentence says why rather than
        # leaving a reader to wonder where the third figure went.
        percent_only_note=(
            "5-hour and weekly windows are percentages of the caps Command Code "
            "reports. There is no monthly percentage: the API states what is "
            "left of the monthly pool but never the monthly grant, and a "
            "percentage with an invented denominator would be worse than no row."
        ),
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
    # ── §2's seventh state, declared rather than branched on ──────────────
    # Three routes exist and MCC must NOT use them: each needs a credential the
    # user does not have and must never be asked for
    # (`INVESTIGATION-USAGE-AND-CREDITS.md` §6.1.2, §6.4). Naming them here is
    # what turns "we show nothing" into "this provider publishes a route that
    # needs a management key MCC does not hold" -- a fact about the keyring
    # rather than about the provider.
    #
    # `needs_credential` is the reason in a reader's words. It is never rendered
    # as an error, never retried, and never carries a figure.
    UsageSource(
        provider_id="open_router_credits",
        display_name="OpenRouter account credits",
        needs_credential="a management key",
        detail="the account-credits route answers an ordinary key with 403",
    ),
    UsageSource(
        provider_id="opencode_zen",
        display_name="OpenCode Zen balance",
        needs_credential="a browser session cookie",
        detail="the balance sits behind the console's own browser session",
    ),
    UsageSource(
        provider_id="copilot",
        display_name="GitHub Copilot",
        needs_credential="a Copilot OAuth token MCC does not hold",
        detail="the usage route needs a different credential from the one MCC routes",
    ),
    UsageSource(
        provider_id="zai",
        display_name="Z.ai",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="the route is third-party evidence only; MCC does not read it yet",
    ),
    # Named because they are in the user's own list and are the reference cases
    # for the state-3 sentence. Absence from this table would give the same
    # answer; naming them makes the answer checkable in one place.
    UsageSource(provider_id="novita", display_name="Novita"),
    UsageSource(provider_id="cline", display_name="Cline"),
    # ── Mistral's two credentials ─────────────────────────────────────────
    # Neither pool has a key-readable remaining balance, and the two rows say
    # two different things because they are two different facts (IV.16).
    #
    # Studio: no mechanism at all. There is no documented Bearer-authenticated
    # endpoint that returns the API allowance, so this is the eighth state --
    # "publishes no usage endpoint MCC can read" -- exactly as Novita and Cline
    # read. Named rather than left absent so the answer is checkable here.
    UsageSource(provider_id="mistral", display_name="Mistral Studio (API)"),
    # Vibe: the percentage does exist, but only behind Mistral's own dashboard
    # tRPC route, which needs an authenticated browser session (and sometimes
    # CSRF) and does not accept the plan key as a replacement. MCC does not ask
    # for a cookie, so this is the seventh state -- a route that exists and
    # needs a credential this machine does not hold -- the same fact
    # ``opencode_zen`` states about a different vendor.
    #
    # No figure, no percentage, no reset date and no dollar conversion: the
    # percentage does not establish the allowance in dollars, and the $300/$150
    # Vibe budgets are subscriber-reported, not published.
    UsageSource(
        provider_id="mistral_vibe",
        display_name="Mistral Vibe (subscription)",
        needs_credential="a browser session cookie",
        detail="the remaining allowance sits behind Mistral's own dashboard session",
    ),
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


# ─────────────────────────── 8.7.0 (USAGE-LAYOUTS): the eighth state ─────────
#
# `specs/PR-USAGE-LAYOUTS-SPEC.md` §2 gives the page EIGHT honest states. Six of
# them describe what MCC has already learnt; the seventh is `unsupported`
# ("Not published") and the eighth is:
#
#     possible endpoint, unverified -- discovery found a candidate that has
#     not been verified. WORDS ONLY, NEVER A NUMBER.
#
# That state exists because a CUSTOM provider is a host somebody typed. There is
# no catalogue row to read a mechanism from, so MCC asks the two offline
# recognisers `specs/RESEARCH-BILLING-ENDPOINT-DISCOVERY.md`'s USER DECISIONS Q7
# approved:
#
#   * models.dev's `api` host match (208 distinct hosts in the local cache, 184
#     of 235 providers resolved unambiguously when the dossier measured);
#   * the three GATEWAY FAMILIES below, recognised from the inference base URL.
#
# BOTH ARE HYPOTHESES, NOT READINGS. A recognised host says only "this looks
# like provider X, which publishes a route MCC could read", and the page prints
# exactly that, with no figure, no bar, no percentage and no retry. It is kept
# apart from `unsupported` because "we believe this one publishes something" and
# "this one publishes nothing" are two different facts (IV.16), and it is kept
# apart from every measured state because recognising a host is not having read
# it.
STAGE_READ = "read"
STAGE_DISCOVERY = "discovery"


@dataclass(frozen=True, slots=True)
class UsageFamily:
    """One gateway family a custom provider's base URL can identify.

    Recognising the family is the only claim made: the family's own route is a
    DECLARED fact about the family (both implementations the dossier surveyed
    treat it that way), and nothing here reads it. ``routes`` is for the
    sentence a reader sees, so a possible endpoint names what it might be
    rather than landing behind an unnamed shrug.
    """

    id: str
    name: str
    #: Lower-case fragments in the base URL that identify the family anywhere in
    #: the path or host. Order inside a family does not matter; order between
    #: families is the tie-break, so the most specific family is declared first.
    markers: tuple[str, ...]
    #: The route the family serves its own usage on, for the sentence only.
    routes: tuple[str, ...]
    #: One clause: what this family answers with when it answers.
    reads: str


#: §4.3 of the dossier, as three rows. Each is the family's own name for
#: itself -- never a provider's -- which is why one row serves every deployment
#: of that family.
#:
#: `key/info` is LiteLLM's own information route; the two "families" that
#: precede it are whole deployments (LiteLLM self-hosted, New API / one-api and
#: its fork lineage), recognised by the fragments their own containers and
#: documentation use. A user's own deployment is frequently at a bare host, so
#: matching is by FRAGMENT anywhere in the base URL -- and an ambiguity (two
#: families claiming one URL) resolves to nothing, the same refusal as a shared
#: models.dev host.
GATEWAY_FAMILIES: tuple[UsageFamily, ...] = (
    UsageFamily(
        id="new-api",
        name="New API",
        markers=("/new-api", "newapi", "one-api", "oneapi"),
        routes=("/api/user/self",),
        reads="the signed-in account's quota",
    ),
    UsageFamily(
        id="litellm",
        name="LiteLLM",
        markers=("/litellm", "litellm"),
        routes=("/key/info",),
        reads="the key's spend and its remaining budget",
    ),
    UsageFamily(
        id="key-info",
        name="a key/info gateway",
        markers=("key/info",),
        routes=("/key/info",),
        reads="the key's spend and its remaining budget",
    ),
)


def families_for_base_url(base_url: str) -> tuple[UsageFamily, ...]:
    """Every gateway family whose markers this base URL carries.

    An empty tuple means no family claimed it -- which the caller renders as
    "not published" rather than as a guess. TWO OR MORE is also an empty-ish
    answer to the caller (it refuses), so this returns all of them and lets the
    caller say why: a URL that names two families is one MCC should not guess
    about, and silence there would lose the reason.
    """

    text = (base_url or "").strip().lower()
    if not text:
        return ()
    return tuple(
        family for family in GATEWAY_FAMILIES if any(m in text for m in family.markers)
    )
