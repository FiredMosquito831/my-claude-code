"""What the InferHub page reads with the account's key, declared per marketplace (7.110.0).

The market snapshot (``config/marketplaces.py`` ``market_sources``) is what a
*request* may lean on: it is read in the background and stored. The reads
here are the other kind -- the account's own state, shown on the
marketplace's admin page and nowhere else: the balance and spend, which kind
of key it is, the budgets and alias groups set on the marketplace's own
dashboard, and, on one button press, the price each request was actually
charged at. None of it is ever read on a request path, never stored, and
never sent anywhere but the marketplace itself.

Declared as data, in the ``config/marketplaces.py`` pattern, so a second
marketplace is one more table entry rather than a branch: every read names
the base it is relative to (the provider's inference base, or the
management base), its path, what the page shows from it, and where its shape
was read. Every keyed shape is UNVERIFIED -- the user has no InferHub key, so
nothing here has been observed on a real account; the page says so.

``config`` is a leaf: this module imports nothing first-party.
"""

from collections.abc import Mapping
from dataclasses import dataclass

#: The two bases a read can be relative to.
BASE_INFERENCE = "inference"
BASE_MANAGEMENT = "management"

READ_USAGE = "usage"
READ_KEYS = "keys"
READ_BUDGETS = "budgets"
READ_BUDGET_ALIASES = "budget_aliases"
READ_BUDGET_DEFAULT = "budget_default"
READ_USAGE_LOGS = "usage_logs"

#: The reads the page's Account block makes on open and on Refresh. The
#: usage logs are not among them: they are read only on the "fetch the
#: charged ask" button (the user's Q17).
ACCOUNT_BLOCK_READS: tuple[str, ...] = (
    READ_USAGE,
    READ_KEYS,
    READ_BUDGETS,
    READ_BUDGET_ALIASES,
    READ_BUDGET_DEFAULT,
)


@dataclass(frozen=True, slots=True)
class MarketplaceRead:
    """One keyed GET the marketplace page makes."""

    name: str
    #: :data:`BASE_INFERENCE` or :data:`BASE_MANAGEMENT`.
    base: str
    path: str
    #: What the page shows from it, in the page's words.
    purpose: str
    #: Where its shape was read.
    source: str


@dataclass(frozen=True, slots=True)
class MarketplaceAccountReads:
    """Everything the page may read with one marketplace's key."""

    dialect_id: str
    #: The settings attribute holding the management base, beside the
    #: dialect's own ``default_market_base``.
    management_base_attr: str
    reads: tuple[MarketplaceRead, ...]
    #: How many leading characters of a key the key list names it by
    #: ("12-char non-secret prefix"); the page never shows them.
    key_prefix_chars: int
    #: The error code a key gets on a read its kind may not make, and the
    #: field naming that kind (``{"error": {"code": ..., "keyKind": ...}}``).
    scope_error_code: str
    scope_error_kind_field: str
    #: How long the marketplace keeps per-request logs, in days.
    usage_logs_retention_days: int
    #: The largest page the usage-log read accepts (documented 1..100).
    usage_logs_page_size: int
    #: MCC's own bound on one press of "fetch the charged ask": at most this
    #: many pages are read. A bound on MCC's work, not on the marketplace.
    usage_logs_max_pages: int
    #: The smallest charge of any billed request, USD ("at least $0.00001 for
    #: any billed request"). The page's charged-vs-ceiling check never calls a
    #: tiny request over its ceiling for paying this floor.
    minimum_charge_usd: float
    source: str

    def read(self, name: str) -> MarketplaceRead | None:
        for entry in self.reads:
            if entry.name == name:
                return entry
        return None


_OPENAPI = "https://inferhub.dev/api/openapi.json (read 2026-10-24, RES §4.2, §7)"

INFERHUB_ACCOUNT_READS = MarketplaceAccountReads(
    dialect_id="inferhub_bid_headers",
    management_base_attr="inferhub_market_base_url",
    reads=(
        MarketplaceRead(
            READ_USAGE,
            BASE_INFERENCE,
            "/me/usage",
            "balance and spend (any inference key, inference_only included)",
            f"GET /v1/me/usage, {_OPENAPI}",
        ),
        MarketplaceRead(
            READ_KEYS,
            BASE_MANAGEMENT,
            "/keys",
            "which kind of key this is (matched by its non-secret prefix)",
            f"GET /keys ApiKey.keyPrefix + kind, {_OPENAPI}",
        ),
        MarketplaceRead(
            READ_BUDGETS,
            BASE_MANAGEMENT,
            "/budgets",
            "the per-model budgets set on InferHub's own dashboard (read-only)",
            f"GET /budgets, {_OPENAPI}",
        ),
        MarketplaceRead(
            READ_BUDGET_ALIASES,
            BASE_MANAGEMENT,
            "/budgets/aliases",
            "the alias groups and their members (read-only)",
            f"GET /budgets/aliases, {_OPENAPI}",
        ),
        MarketplaceRead(
            READ_BUDGET_DEFAULT,
            BASE_MANAGEMENT,
            "/budgets/default",
            "the account-wide default budget (read-only)",
            f"GET /budgets/default, {_OPENAPI}",
        ),
        MarketplaceRead(
            READ_USAGE_LOGS,
            BASE_MANAGEMENT,
            "/usage/logs",
            "the ask each request was charged at (on the button only)",
            f"GET /usage/logs, {_OPENAPI}",
        ),
    ),
    key_prefix_chars=12,
    scope_error_code="insufficient_key_scope",
    scope_error_kind_field="keyKind",
    usage_logs_retention_days=7,
    usage_logs_page_size=100,
    usage_logs_max_pages=5,
    # CONFIRMED: GET /usage/logs cost_consumer_usdc, and docs line 143 (RES §6.1).
    minimum_charge_usd=0.00001,
    source=_OPENAPI,
)

#: The page's reads per dialect id.
MARKETPLACE_ACCOUNT_READS: Mapping[str, MarketplaceAccountReads] = {
    INFERHUB_ACCOUNT_READS.dialect_id: INFERHUB_ACCOUNT_READS,
}


def account_reads_for(dialect_id: str) -> MarketplaceAccountReads | None:
    """The page's reads for one marketplace, or ``None`` for one that declares none."""

    return MARKETPLACE_ACCOUNT_READS.get(dialect_id)


__all__ = [
    "ACCOUNT_BLOCK_READS",
    "BASE_INFERENCE",
    "BASE_MANAGEMENT",
    "INFERHUB_ACCOUNT_READS",
    "MARKETPLACE_ACCOUNT_READS",
    "READ_BUDGETS",
    "READ_BUDGET_ALIASES",
    "READ_BUDGET_DEFAULT",
    "READ_KEYS",
    "READ_USAGE",
    "READ_USAGE_LOGS",
    "MarketplaceAccountReads",
    "MarketplaceRead",
    "account_reads_for",
]
