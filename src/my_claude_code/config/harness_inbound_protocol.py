"""Pure post-serialisation inbound-protocol transformer for multi-protocol harnesses.

Keeps ``application/catalogues.serialise()`` 100% untouched (so equality
baselines stay 0-diff) while letting multi-protocol harnesses target any of
MCC's verified inbound endpoints:

- ``opencode`` / ``opencode2`` / ``kilo``: ``provider.mcc.npm``
  (``"@ai-sdk/anthropic"`` | ``"@ai-sdk/openai"`` | ``"@ai-sdk/openai-compatible"``)
  with ``<root>/v1`` across all three.
- ``commandcode_cli`` (``format_id="commandcode"``): ``provider.mcc.api``
  (``"anthropic-messages"`` | ``"openai-completions"``) with ``<root>/v1``.
- ``openclaw``: ``models.providers.mcc.api``
  (``"openai-completions"`` with ``<root>/v1`` | ``"anthropic-messages"`` with ``<root>``).
- ``hermes``: ``providers.mcc.transport``
  (``"openai_chat"`` with ``<root>/v1`` | ``"anthropic_messages"`` with ``<root>``).
- ``crush``: ``providers.mcc.type``
  (``"anthropic"`` with ``<root>`` | ``"openai-compat"`` with ``<root>/v1``).
- ``droid``: ``customModels[].provider``
  (``"anthropic"`` with ``<root>`` | ``"generic-chat-completion-api"`` with ``<root>/v1``).
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.config.harness_base_url import root_base_url, v1_base_url
from my_claude_code.config.harnesses import (
    COMMANDCODE_BASE_URL_SENTINEL,
    CRUSH_BASE_URL_SENTINEL,
    DROID_BASE_URL_SENTINEL,
    HERMES_BASE_URL_SENTINEL,
    OPENCLAW_BASE_URL_SENTINEL,
    HarnessProtocol,
)

#: ``(format_id, HarnessProtocol) -> provider protocol field value``
_PROVIDER_VALUE_BY_FORMAT: dict[tuple[str, HarnessProtocol], str] = {
    ("opencode", HarnessProtocol.ANTHROPIC_MESSAGES): "@ai-sdk/anthropic",
    ("opencode", HarnessProtocol.OPENAI_RESPONSES): "@ai-sdk/openai",
    ("opencode", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "@ai-sdk/openai-compatible",
    ("kilo", HarnessProtocol.ANTHROPIC_MESSAGES): "@ai-sdk/anthropic",
    ("kilo", HarnessProtocol.OPENAI_RESPONSES): "@ai-sdk/openai",
    ("kilo", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "@ai-sdk/openai-compatible",
    ("commandcode", HarnessProtocol.ANTHROPIC_MESSAGES): "anthropic-messages",
    ("commandcode", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "openai-completions",
    ("openclaw", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "openai-completions",
    ("openclaw", HarnessProtocol.ANTHROPIC_MESSAGES): "anthropic-messages",
    ("hermes", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "openai_chat",
    ("hermes", HarnessProtocol.ANTHROPIC_MESSAGES): "anthropic_messages",
    ("crush", HarnessProtocol.ANTHROPIC_MESSAGES): "anthropic",
    ("crush", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "openai-compat",
    ("droid", HarnessProtocol.ANTHROPIC_MESSAGES): "anthropic",
    ("droid", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "generic-chat-completion-api",
}

#: Reverse lookup for on-disk detection: ``(format_id, raw_value) -> HarnessProtocol``.
_PROTOCOL_BY_FORMAT_AND_VALUE: dict[tuple[str, str], HarnessProtocol] = {
    (format_id, raw_value): protocol
    for (format_id, protocol), raw_value in _PROVIDER_VALUE_BY_FORMAT.items()
}

#: Base-URL shape required by each ``(format_id, HarnessProtocol)`` pair.
_BASE_URL_SHAPE_BY_FORMAT: dict[tuple[str, HarnessProtocol], str] = {
    ("opencode", HarnessProtocol.ANTHROPIC_MESSAGES): "v1",
    ("opencode", HarnessProtocol.OPENAI_RESPONSES): "v1",
    ("opencode", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
    ("kilo", HarnessProtocol.ANTHROPIC_MESSAGES): "v1",
    ("kilo", HarnessProtocol.OPENAI_RESPONSES): "v1",
    ("kilo", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
    ("commandcode", HarnessProtocol.ANTHROPIC_MESSAGES): "v1",
    ("commandcode", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
    ("openclaw", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
    ("openclaw", HarnessProtocol.ANTHROPIC_MESSAGES): "root",
    ("hermes", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
    ("hermes", HarnessProtocol.ANTHROPIC_MESSAGES): "root",
    ("crush", HarnessProtocol.ANTHROPIC_MESSAGES): "root",
    ("crush", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
    ("droid", HarnessProtocol.ANTHROPIC_MESSAGES): "root",
    ("droid", HarnessProtocol.OPENAI_CHAT_COMPLETIONS): "v1",
}


def inbound_protocol_base_url_shape(
    format_id: str, protocol: HarnessProtocol | None
) -> str:
    """Return ``"root"`` or ``"v1"`` for ``(format_id, protocol)``."""

    if protocol is None:
        return (
            "root"
            if format_id in {"crush", "droid", "qwen", "vscode", "goose"}
            else "v1"
        )
    return _BASE_URL_SHAPE_BY_FORMAT.get((format_id, protocol), "v1")


def _resolved_url_for_shape(
    shape: str, sentinel: str, proxy_root_url: str | None
) -> str:
    target = proxy_root_url if proxy_root_url is not None else sentinel
    return v1_base_url(target) if shape == "v1" else root_base_url(target)


def apply_inbound_protocol(
    format_id: str,
    document: Mapping[str, Any],
    protocol: HarnessProtocol | None,
    *,
    proxy_root_url: str | None = None,
) -> dict[str, Any]:
    """Return ``document`` updated for ``protocol`` (or unchanged when ``None``).

    Never mutates ``document`` in place. Leaves every model entry and unrelated
    section intact. When ``proxy_root_url`` is supplied, updates embedded base
    URLs to the exact ``root`` or ``<root>/v1`` form required by ``protocol``.
    """

    if protocol is None:
        return dict(document)
    provider_value = _PROVIDER_VALUE_BY_FORMAT.get((format_id, protocol))
    if provider_value is None:
        return dict(document)

    updated = dict(document)
    shape = inbound_protocol_base_url_shape(format_id, protocol)

    if format_id in {"opencode", "kilo"}:
        provider_map = updated.get("provider")
        if isinstance(provider_map, Mapping) and isinstance(
            provider_map.get("mcc"), Mapping
        ):
            mcc_block = dict(provider_map["mcc"])
            mcc_block["npm"] = provider_value
            updated["provider"] = {**provider_map, "mcc": mcc_block}
        return updated

    if format_id == "commandcode":
        provider_map = updated.get("provider")
        if isinstance(provider_map, Mapping) and isinstance(
            provider_map.get("mcc"), Mapping
        ):
            mcc_block = dict(provider_map["mcc"])
            mcc_block["api"] = provider_value
            if proxy_root_url is not None or "baseURL" in mcc_block:
                mcc_block["baseURL"] = _resolved_url_for_shape(
                    shape, COMMANDCODE_BASE_URL_SENTINEL, proxy_root_url
                )
            updated["provider"] = {**provider_map, "mcc": mcc_block}
        return updated

    if format_id == "openclaw":
        models = updated.get("models")
        if isinstance(models, Mapping):
            providers = models.get("providers")
            if isinstance(providers, Mapping) and isinstance(
                providers.get("mcc"), Mapping
            ):
                mcc_block = dict(providers["mcc"])
                mcc_block["api"] = provider_value
                mcc_block["baseUrl"] = _resolved_url_for_shape(
                    shape, OPENCLAW_BASE_URL_SENTINEL, proxy_root_url
                )
                updated["models"] = {
                    **models,
                    "providers": {**providers, "mcc": mcc_block},
                }
        return updated

    if format_id == "hermes":
        providers = updated.get("providers")
        if isinstance(providers, Mapping) and isinstance(providers.get("mcc"), Mapping):
            mcc_block = dict(providers["mcc"])
            mcc_block["transport"] = provider_value
            mcc_block["api"] = _resolved_url_for_shape(
                shape, HERMES_BASE_URL_SENTINEL, proxy_root_url
            )
            updated["providers"] = {**providers, "mcc": mcc_block}
        return updated

    if format_id == "crush":
        providers = updated.get("providers")
        if isinstance(providers, Mapping) and isinstance(providers.get("mcc"), Mapping):
            mcc_block = dict(providers["mcc"])
            mcc_block["type"] = provider_value
            if proxy_root_url is not None:
                mcc_block["base_url"] = _resolved_url_for_shape(
                    shape, CRUSH_BASE_URL_SENTINEL, proxy_root_url
                )
            updated["providers"] = {**providers, "mcc": mcc_block}
        return updated

    if format_id == "droid":
        custom_models = updated.get("customModels")
        if isinstance(custom_models, list):
            new_models: list[Any] = []
            for item in custom_models:
                if isinstance(item, Mapping):
                    entry = dict(item)
                    entry["provider"] = provider_value
                    if (
                        proxy_root_url is not None
                        or protocol != HarnessProtocol.ANTHROPIC_MESSAGES
                    ):
                        entry["baseUrl"] = _resolved_url_for_shape(
                            shape, DROID_BASE_URL_SENTINEL, proxy_root_url
                        )
                    new_models.append(entry)
                else:
                    new_models.append(item)
            updated["customModels"] = new_models
        return updated

    return updated


def document_inbound_protocol(
    format_id: str, document: Mapping[str, Any]
) -> HarnessProtocol | None:
    """Return the :class:`HarnessProtocol` encoded in ``document``, or ``None``."""

    raw_value: object = None
    if format_id in {"opencode", "kilo"}:
        provider = document.get("provider")
        mcc = provider.get("mcc") if isinstance(provider, Mapping) else None
        raw_value = mcc.get("npm") if isinstance(mcc, Mapping) else None
    elif format_id == "commandcode":
        provider = document.get("provider")
        mcc = provider.get("mcc") if isinstance(provider, Mapping) else None
        raw_value = mcc.get("api") if isinstance(mcc, Mapping) else None
    elif format_id == "openclaw":
        models = document.get("models")
        providers = models.get("providers") if isinstance(models, Mapping) else None
        mcc = providers.get("mcc") if isinstance(providers, Mapping) else None
        raw_value = mcc.get("api") if isinstance(mcc, Mapping) else None
    elif format_id == "hermes":
        providers = document.get("providers")
        mcc = providers.get("mcc") if isinstance(providers, Mapping) else None
        raw_value = mcc.get("transport") if isinstance(mcc, Mapping) else None
    elif format_id == "crush":
        providers = document.get("providers")
        mcc = providers.get("mcc") if isinstance(providers, Mapping) else None
        raw_value = mcc.get("type") if isinstance(mcc, Mapping) else None
    elif format_id == "droid":
        custom_models = document.get("customModels")
        if isinstance(custom_models, list) and custom_models:
            first = custom_models[0]
            raw_value = first.get("provider") if isinstance(first, Mapping) else None
    if not isinstance(raw_value, str):
        return None
    return _PROTOCOL_BY_FORMAT_AND_VALUE.get((format_id, raw_value.strip()))


def document_matches_inbound_protocol(
    format_id: str, document: Mapping[str, Any], protocol: HarnessProtocol
) -> bool:
    """Return whether ``document`` already targets ``protocol`` with the right URL shape."""

    if document_inbound_protocol(format_id, document) != protocol:
        return False
    expected_shape = inbound_protocol_base_url_shape(format_id, protocol)
    if format_id == "crush":
        providers = document.get("providers")
        mcc = providers.get("mcc") if isinstance(providers, Mapping) else None
        base_url = mcc.get("base_url") if isinstance(mcc, Mapping) else None
        if isinstance(base_url, str):
            has_v1 = base_url.rstrip("/").endswith("/v1")
            return has_v1 if expected_shape == "v1" else not has_v1
    if format_id == "droid":
        custom_models = document.get("customModels")
        if isinstance(custom_models, list) and custom_models:
            first = custom_models[0]
            base_url = first.get("baseUrl") if isinstance(first, Mapping) else None
            if isinstance(base_url, str):
                has_v1 = base_url.rstrip("/").endswith("/v1")
                return has_v1 if expected_shape == "v1" else not has_v1
    return True
