"""One small module per declared read: what the host answered, as a reading.

The table lives in :mod:`my_claude_code.config.usage_sources`; the transport
lives in :mod:`my_claude_code.application.usage_reads`; this package is the part
in between -- **decoding**, and nothing else. That split is what makes the
adapters provable without a socket: every module here is a pure function from
``bytes`` to :class:`~my_claude_code.core.usage.reading.Reading`, driven against
a fixture in ``tests/core/test_usage_adapters.py`` and against a fake host
in-process in ``tests/api/test_admin_usage_reads.py``.

**No module here imports ``httpx``, opens a connection, reads a credential or
knows a provider id.** ``core/`` may not import a transport SDK (III.5, pinned
by ``test_core_does_not_import_provider_transport_sdks``), and that constraint
turns out to be exactly the right shape: the adapter receives the bytes the
transport fetched and returns the provider's figures, so "the read path cannot
charge a key" is not a promise about a code path -- it is a promise about a
package that contains no key handling at all.

THE ONE CONTRACT every ``parse(body: bytes) -> Reading`` honours:

* a figure is a **string the provider wrote** (``fields.decode_json`` decodes
  numbers as text, so nothing is re-derived);
* a field the host did not send is **absent**, never a zero and never an empty
  string standing in for a number;
* a body that is not the declared shape raises
  :class:`~my_claude_code.core.usage.reading.UsageUnreadable` with a sentence a
  reader can act on, which the route turns into state 4;
* the host's own answer that there is nothing available -- an expired plan, an
  account not enabled -- is a **successful** reading with ``available=False``
  and the host's words in ``unavailable_reason``. It is not an error, because
  the call worked and the provider said so (the user's decision on Kimi).
"""

import importlib
from collections.abc import Callable

from .reading import Reading, UsageUnreadable

__all__ = ["MODULES", "ParseFn", "adapter_for", "parse_with"]

#: What every adapter module exposes: the host's bytes in, the reading out.
ParseFn = Callable[[bytes], Reading]

#: Every adapter module in this package, by its declared name. The declared
#: table names one of these strings; nothing here names a provider.
MODULES: tuple[str, ...] = (
    "claude",
    "codex",
    "commandcode",
    "deepseek",
    "kimi",
    "openrouter",
    "opencode_go",
)


def adapter_for(name: str) -> ParseFn:
    """The declared adapter, by the name a table row gives.

    Raises :class:`LookupError` for a name this release does not ship rather
    than returning a stub: a table row naming an adapter that is not there is a
    programming error, and the contract test catches it before a user does.

    The import is inside the function on purpose -- it is a *lookup by data*,
    the one indirection that keeps the reader free of a branch per provider,
    and it costs one ``importlib`` call on a press rather than on start-up.
    """

    if name not in MODULES:
        raise LookupError(f"no usage adapter named {name!r}")
    module = importlib.import_module(f"{__name__}.{name}")
    parse = getattr(module, "parse", None)
    if not callable(parse):
        raise LookupError(f"usage adapter {name!r} has no parse()")
    return parse


def parse_with(name: str, body: bytes) -> Reading:
    """Decode one declared read's body. The one call a transport makes."""

    result = adapter_for(name)(body)
    if not isinstance(result, Reading):  # pragma: no cover - a broken adapter
        raise UsageUnreadable(f"adapter {name!r} did not return a reading")
    return result
