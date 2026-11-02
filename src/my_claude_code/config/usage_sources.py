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
    no credential, nothing to schedule: the cheapest reading there is. This
    release installs observers for these.
``provider-endpoint``
    The figure needs a read of its own against the host (DeepSeek's
    ``/user/balance``, OpenRouter's ``/api/v1/key``). **Listed, not built** --
    that is USAGE PR 3. A provider with this mechanism and no reading is
    ``Not read yet``, exactly like a header provider waiting for traffic:
    a point of capture exists, and the missing thing is the read, not the
    provider's willingness to publish one.

A provider with **neither** is ``unsupported``: one plain sentence,
permanently, no retry and no spinner (the user's Q7). That is the majority of
the catalogue, which is why the sentence is not an edge case.

A provider may hold **both** mechanisms -- DeepSeek and OpenRouter do: they can
send ``x-ratelimit-*`` on ordinary traffic *and* they publish a balance
endpoint. So a row carries a *tuple* of mechanisms rather than one word, and
the differences never have to be encoded by listing an id twice in two tables
and hoping the caller checks them in the right order.
"""

from dataclasses import dataclass

__all__ = [
    "MECHANISM_ENDPOINT",
    "MECHANISM_HEADER",
    "PROVIDER_SOURCES",
    "SOURCE_UNSUPPORTED",
    "UsageSource",
    "header_capture_providers",
    "source_for",
]

#: The two mechanism words. Short, stable, and the only values a row's
#: ``mechanisms`` ever takes.
MECHANISM_HEADER = "response-header"
MECHANISM_ENDPOINT = "provider-endpoint"

#: What a provider with no mechanism at all reports.
SOURCE_UNSUPPORTED = "unsupported"


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

    @property
    def supported(self) -> bool:
        """Whether any figure for this provider is reachable at all."""
        return bool(self.mechanisms)

    @property
    def source(self) -> str:
        """The mechanism the page should describe: the cheapest one present."""
        return self.mechanisms[0] if self.mechanisms else SOURCE_UNSUPPORTED


#: Every provider MCC has an opinion about. A provider that is not here is
#: ``unsupported`` by absence -- see :func:`source_for` -- which is what keeps
#: this table from having to enumerate the catalogue's endpoint-less majority.
#:
#: ``commandcode`` is the one row whose mechanism spans two protocol families:
#: the half of its catalogue that speaks Anthropic Messages is covered by the
#: generic observer installed on that family's *existing* ``set_response_observer``
#: seam, and its Chat Completions half by the mirror seam. Both are this one
#: provider's response headers, so it is one row.
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
    ),
    UsageSource(
        provider_id="deepseek",
        display_name="DeepSeek",
        mechanisms=(MECHANISM_HEADER, MECHANISM_ENDPOINT),
        detail="the x-ratelimit response headers",
    ),
    UsageSource(
        provider_id="kimi",
        display_name="Kimi (Moonshot)",
        mechanisms=(MECHANISM_HEADER,),
        detail="the x-ratelimit response headers",
    ),
    UsageSource(
        provider_id="commandcode",
        display_name="Command Code",
        mechanisms=(MECHANISM_HEADER,),
        detail="the x-ratelimit response headers",
    ),
    # Listed so the page can tell "not built yet" from "nothing to read". Both
    # are state 3's opposite, and the page prints ``Not read yet`` for them.
    UsageSource(
        provider_id="kimi_coding",
        display_name="Kimi Coding",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="Kimi's binary is the only evidence for this route; it is not built",
    ),
    UsageSource(
        provider_id="opencode_go",
        display_name="OpenCode Go",
        mechanisms=(MECHANISM_ENDPOINT,),
        detail="the third-party-verified /zen/go/v1/usage route; not built",
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
