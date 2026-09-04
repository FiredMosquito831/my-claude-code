"""Submodules for Anthropic web server tool handling (search/fetch, egress, streaming).

The convenience re-exports below resolve on first attribute access rather than
at import. ``streaming`` reaches ``outbound``, which imports ``aiohttp``, and
``admin_routes`` imports ``web_tools.search_providers`` -- so an eager
``__init__`` put the whole outbound HTTP stack on the server's startup path for
the sake of names that only a web-tool request ever reads.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .egress import (
        WebFetchEgressPolicy,
        WebFetchEgressViolation,
        enforce_web_fetch_egress,
    )
    from .request import is_web_server_tool_request
    from .streaming import stream_web_server_tool_response

_EXPORTS: dict[str, str] = {
    "WebFetchEgressPolicy": "egress",
    "WebFetchEgressViolation": "egress",
    "enforce_web_fetch_egress": "egress",
    "is_web_server_tool_request": "request",
    "stream_web_server_tool_response": "streaming",
}

__all__ = [
    "WebFetchEgressPolicy",
    "WebFetchEgressViolation",
    "enforce_web_fetch_egress",
    "is_web_server_tool_request",
    "stream_web_server_tool_response",
]


def __getattr__(name: str) -> object:
    """Resolve a re-exported name by importing only the submodule that owns it."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
