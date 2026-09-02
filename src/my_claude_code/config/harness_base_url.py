"""Put the real proxy root into a generated catalogue that cannot reference it.

Three delivery styles exist across the harnesses and each one solves the same
problem differently. OpenCode expands ``{env:VARIABLE}`` inside a trusted
config, so its generated document names a variable and the launcher sets it.
Command Code refuses a non-parseable ``baseURL`` and applies no substitution at
all, so ``config/harness_config_merge.with_base_url`` writes the literal into
the single key MCC owns. Qwen Code and Crush are the third case: their base-URL
fields are literals like Command Code's, but the document is MCC's own file
rather than one key inside the user's, so the substitution has to reach
wherever in that document the sentinel happens to sit.

Hence this module. A serialiser stays a pure function of the model records --
it does not know which port this install listens on -- and writes a sentinel;
the launcher and the runtime fan-out publisher both call
:func:`with_root_base_url` on their way to disk.

**The value written is the proxy root, with no ``/v1``.** Both CLIs reach MCC
through an official Anthropic SDK -- ``@anthropic-ai/sdk`` for Qwen Code,
``anthropic-sdk-go`` for Crush -- and both append ``/v1/messages`` themselves.
Appending ``/v1`` here, the way Command Code's helper must, would produce
``POST /v1/v1/messages``. Verified on the wire for both.
"""

from collections.abc import Mapping, Sequence
from typing import Any


def root_base_url(proxy_root_url: str) -> str:
    """Return the proxy root in the form an Anthropic SDK expects.

    A trailing ``/v1`` is removed rather than tolerated: a caller that already
    holds a ``…/v1`` URL is far more likely to have built it for a CLI that
    wanted one than to be naming a proxy actually mounted one level down.
    """

    stripped = proxy_root_url.rstrip("/")
    return stripped.removesuffix("/v1").rstrip("/")


def with_root_base_url(
    document: Mapping[str, Any], sentinel: str, proxy_root_url: str
) -> dict[str, Any]:
    """Return the document with every occurrence of ``sentinel`` resolved.

    Every string equal to the sentinel is replaced, at any depth. Matching the
    whole value rather than a substring is deliberate: a sentinel that has
    somehow reached a prose field must not be silently rewritten, and a
    partially substituted URL is a worse failure than an untouched one.
    """

    resolved = _walk(document, sentinel, root_base_url(proxy_root_url))
    if not isinstance(resolved, dict):  # pragma: no cover - documents are mappings
        raise TypeError("harness catalogue document must be a mapping")
    return resolved


def _walk(node: Any, sentinel: str, value: str) -> Any:
    if isinstance(node, str):
        return value if node == sentinel else node
    if isinstance(node, Mapping):
        return {key: _walk(item, sentinel, value) for key, item in node.items()}
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [_walk(item, sentinel, value) for item in node]
    return node


def v1_base_url(proxy_root_url: str) -> str:
    """Return the proxy root in the form an OpenAI SDK expects.

    The OpenAI SDKs -- and every client built on them -- append
    ``chat/completions`` to whatever ``baseURL`` they were given and insert no
    ``/v1`` of their own. So the value here carries it: Cline's
    ``openai-compatible`` provider, LiteLLM's ``OPENAI_BASE_URL`` and Goose's
    ``OPENAI_HOST`` + ``OPENAI_BASE_PATH`` pair all resolve to
    ``<root>/v1/chat/completions`` and never ``<root>/v1/v1/...``.

    Idempotent: a root that already ends in ``/v1`` is returned unchanged, so a
    caller cannot double the segment by resolving twice.
    """

    return f"{root_base_url(proxy_root_url)}/v1"


def with_v1_base_url(
    document: Mapping[str, Any], sentinel: str, proxy_root_url: str
) -> dict[str, Any]:
    """Return the document with every occurrence of ``sentinel`` resolved to ``<root>/v1``."""

    resolved = _walk(document, sentinel, v1_base_url(proxy_root_url))
    if not isinstance(resolved, dict):  # pragma: no cover - documents are mappings
        raise TypeError("harness catalogue document must be a mapping")
    return resolved
