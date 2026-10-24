"""The marketplace page's keyed reads: the account's own state (7.110.0).

``config/marketplace_reads.py`` declares what the InferHub page may read with
the account's key -- balance and spend, the key's kind, the budgets and alias
groups set on InferHub's own dashboard, and the per-request usage logs. This
is the transport, in ``providers/runtime`` because network belongs here
(``marketplace_market.py`` is its sibling), reached by the admin API only
through the runtime port.

Discipline, the market snapshot's:

* **Only with a key, only on the page.** Every read here is made because the
  operator opened the page, pressed Refresh, or pressed "fetch the charged
  ask" -- never on a request path, never on a timer.
* **Masked.** Every read leaves through the provider's own proxy chain, the
  one exit :func:`~my_claude_code.providers.runtime.config.masked_exit_for`
  picks; nothing is sent where the chain says no. TLS is never weakened.
* **Never raises, never invents.** A read that fails says how (its HTTP
  status, or the exception's class name); a refused read keeps the
  marketplace's own error code, and a key-scope refusal the key kind it
  names. Nothing failed is turned into a zero.
* **The key never leaves this module.** It goes in the ``Authorization``
  header and nowhere else: not in an answer, not in a log line. Which kind of
  key it is comes from the key list's non-secret prefix, compared here and
  never returned.
"""

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.marketplace_reads import (
    BASE_INFERENCE,
    READ_KEYS,
    READ_USAGE_LOGS,
    MarketplaceAccountReads,
    MarketplaceRead,
    account_reads_for,
)
from my_claude_code.config.marketplaces import marketplace_for_provider
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.providers.socks_deadline import bound_socks_handshake

from .config import masked_exit_for, string_setting

STATE_OFF = "off"
STATE_NO_KEY = "no_key"
STATE_NOT_SENT = "not_sent"
STATE_FAILED = "failed"
STATE_READ = "read"


async def fetch_marketplace_json(
    url: str,
    *,
    proxy: str | None,
    timeout: float,
    key: str,
    params: Mapping[str, str] | None = None,
) -> tuple[int, Any]:
    """One keyed GET: ``(status, parsed body or None)``; raises only on transport."""

    headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
    client = bound_socks_handshake(
        httpx.AsyncClient(proxy=proxy or None, timeout=timeout)
    )
    try:
        response = await client.get(
            url, headers=headers, params=dict(params) if params else None
        )
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        return response.status_code, body
    finally:
        await client.aclose()


def _error_field(body: Any, field: str) -> str | None:
    """``error.<field>`` or a top-level ``<field>``, when it is a string."""

    if not isinstance(body, Mapping):
        return None
    error = body.get("error")
    if isinstance(error, Mapping) and isinstance(error.get(field), str):
        return str(error[field])
    value = body.get(field)
    return value if isinstance(value, str) else None


def _key_kind_from_list(body: Any, key: str, prefix_chars: int) -> str | None:
    """The kind the key list gives the key in use, matched by its prefix."""

    items = body.get("data") if isinstance(body, Mapping) else body
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        prefix = item.get("keyPrefix")
        kind = item.get("kind")
        if (
            isinstance(prefix, str)
            and isinstance(kind, str)
            and len(prefix) >= min(prefix_chars, len(key))
            and key.startswith(prefix)
        ):
            return kind
    return None


async def _read_one(
    read: MarketplaceRead,
    table: MarketplaceAccountReads,
    *,
    base: str,
    proxy: str | None,
    timeout: float,
    key: str,
    params: Mapping[str, str] | None,
) -> dict[str, Any]:
    url = f"{base}{read.path}"
    pages = table.usage_logs_max_pages if read.name == READ_USAGE_LOGS else 1
    rows: list[Any] = []
    first_body: Any = None
    pages_read = 0
    truncated = False
    try:
        for page in range(1, pages + 1):
            query = dict(params or {})
            if read.name == READ_USAGE_LOGS:
                query["page"] = str(page)
                query["pageSize"] = str(table.usage_logs_page_size)
            status, body = await fetch_marketplace_json(
                url, proxy=proxy, timeout=timeout, key=key, params=query
            )
            if status < 200 or status >= 300:
                answer: dict[str, Any] = {"status": f"http_{status}"}
                code = _error_field(body, "code")
                if code is not None:
                    answer["code"] = code
                if code == table.scope_error_code:
                    kind = _error_field(body, table.scope_error_kind_field)
                    if kind is not None:
                        answer["key_kind"] = kind
                if pages_read:
                    # A later page failed: what was read stands, and says so.
                    break
                return answer
            pages_read += 1
            if read.name != READ_USAGE_LOGS:
                return {"status": "ok", "body": body}
            if first_body is None:
                first_body = body
            page_rows = body.get("rows") if isinstance(body, Mapping) else None
            if not isinstance(page_rows, list):
                break
            rows.extend(page_rows)
            total = body.get("total") if isinstance(body, Mapping) else None
            if len(page_rows) < table.usage_logs_page_size or (
                isinstance(total, int) and len(rows) >= total
            ):
                break
            if page == pages:
                truncated = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"status": f"failed:{type(exc).__name__}"}
    merged = dict(first_body) if isinstance(first_body, Mapping) else {}
    merged["rows"] = rows
    return {
        "status": "ok",
        "body": merged,
        "pages_read": pages_read,
        "truncated": truncated,
    }


async def read_marketplace(
    settings: Settings,
    provider_id: str,
    reads: Sequence[str],
    params: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Make the named keyed reads for one marketplace provider. Never raises.

    Returns ``{"state", "reads": {name: {"status", "body"?, "code"?,
    "key_kind"?}}, "key_kind": {"kind", "how"} | None, "proxy_exit"}``.
    ``state`` is ``read``, or why nothing was read: ``off`` (not a
    marketplace), ``no_key``, ``not_sent`` (the proxy chain refused, with
    ``words``) or ``failed``.
    """

    descriptor = PROVIDER_CATALOG.get(provider_id)
    dialect = marketplace_for_provider(provider_id)
    table = account_reads_for(dialect.dialect_id) if dialect is not None else None
    if descriptor is None or dialect is None or table is None:
        return {"state": STATE_OFF, "reads": {}, "key_kind": None}
    keys = parse_credential_keys(string_setting(settings, descriptor.credential_attr))
    if not keys:
        return {"state": STATE_NO_KEY, "reads": {}, "key_kind": None}
    key = keys[0]
    try:
        exit_ = masked_exit_for(
            provider_id,
            string_setting(settings, descriptor.proxy_attr) or None,
            settings,
            name=descriptor.display_name,
        )
    except Exception as exc:
        logger.debug("Marketplace page read: no exit resolved: {}", type(exc).__name__)
        return {"state": STATE_FAILED, "reads": {}, "key_kind": None}
    if exit_.refused:
        return {
            "state": STATE_NOT_SENT,
            "words": exit_.refused,
            "reads": {},
            "key_kind": None,
        }
    inference_base = (
        string_setting(settings, descriptor.base_url_attr)
        or descriptor.default_base_url
        or ""
    ).rstrip("/")
    management_base = (
        string_setting(settings, table.management_base_attr)
        or dialect.default_market_base
    ).rstrip("/")
    timeout = float(settings.models_dev_fetch_timeout_seconds)
    wanted = [entry for name in reads if (entry := table.read(name)) is not None]
    answers = await asyncio.gather(
        *(
            _read_one(
                entry,
                table,
                base=inference_base
                if entry.base == BASE_INFERENCE
                else management_base,
                proxy=exit_.proxy,
                timeout=timeout,
                key=key,
                params=(params or {}).get(entry.name),
            )
            for entry in wanted
        )
    )
    results = {
        entry.name: answer for entry, answer in zip(wanted, answers, strict=True)
    }
    key_kind: dict[str, str] | None = None
    listed = results.get(READ_KEYS)
    if listed is not None and listed.get("status") == "ok":
        kind = _key_kind_from_list(listed.get("body"), key, table.key_prefix_chars)
        if kind is not None:
            key_kind = {"kind": kind, "how": "key_list"}
        # The list itself never leaves: only the kind matched in it.
        listed.pop("body", None)
    if key_kind is None:
        for answer in results.values():
            kind = answer.get("key_kind")
            if isinstance(kind, str):
                key_kind = {"kind": kind, "how": "refusal"}
                break
    logger.info(
        "{} page read: {}",
        dialect.label,
        ", ".join(f"{name} {answer['status']}" for name, answer in results.items()),
    )
    return {
        "state": STATE_READ,
        "reads": results,
        "key_kind": key_kind,
        "proxy_exit": exit_.label,
    }


__all__ = [
    "STATE_FAILED",
    "STATE_NOT_SENT",
    "STATE_NO_KEY",
    "STATE_OFF",
    "STATE_READ",
    "fetch_marketplace_json",
    "read_marketplace",
]
