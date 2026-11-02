"""Install the generic usage observer on the providers that opt in (8.4.0).

The seam is not new. ``AnthropicMessagesProvider`` has carried
``set_response_observer`` since the Claude subscription's own headers needed
reading, and 8.4.0 adds its mirror to the OpenAI-chat / Responses family
(``providers/openai_chat/provider.py`` and ``.responses_transport``). What is
new is who installs one: before this module the only installer was the
Anthropic-OAuth provider, so a host on either protocol could send its credits
state on every response MCC already made and MCC kept none of it.

**Where it is installed, and why there.** The composition root's
:func:`~.factory._create_leaf_provider` builds every provider exactly once --
the plain one, each proxied chain leg, and a chain's Direct rung all go through
it -- and it is the only place that knows a provider is being built *for
inference in this process*. Installing inside a provider class instead would
put the feature on the path of every construction, including the model-list
calls that never see a response a quota header would ride on, and wrapping the
``_create_*`` builders would miss every chain leg. One call at the end of the
leaf builder covers all three shapes.

**What "opt in" means.** :func:`config.usage_sources.header_capture_providers`
-- declared data, one row per provider. A provider that is not in it never
reaches :func:`install_usage_observer` and is returned from the builder
untouched: the very same object, with no attribute written on it, so "an
uninstalled seam changes nothing" is true by construction rather than by
inspection.

**What one installed observer does.** A dictionary write in
:mod:`my_claude_code.core.usage_capture`: allow-listed header names and their
verbatim values, stamped with when they arrived. The callback cannot raise (and
is additionally guarded at both call sites), it reads no credential, it opens
no connection, it sends nothing and it stores nothing off the allow-list.

**No provider's request changes.** Not one byte: the callback is passed
response *headers* after a send has completed, on all three surfaces, and
nothing here touches a body, a header set, a timeout, a retry or the ladder.
"""

from collections.abc import Callable
from typing import Any

from my_claude_code.config.usage_sources import header_capture_providers
from my_claude_code.core.usage_capture import OBSERVER, UsageHeaderObserver

__all__ = ["install_usage_observer", "opted_in"]


def install_usage_observer(
    provider: Any,
    provider_id: str,
    *,
    observer: UsageHeaderObserver | None = None,
) -> bool:
    """Install the generic observer on one provider, if it has a seam.

    Returns whether it was installed. ``False`` for a provider that did not opt
    in, and for one whose class has no ``set_response_observer`` -- which is
    every provider in the catalogue that sends no usage header. Never raises: a
    provider that cannot hold an observer is not a failure, it is a provider
    with nothing to report.
    """

    if provider_id not in _OPTED_IN:
        return False
    setter = getattr(provider, "set_response_observer", None)
    if not callable(setter):
        return False
    board = OBSERVER if observer is None else observer
    try:
        setter(_observer_for(board, provider_id))
    except Exception:
        # A seam that refused its callback leaves the provider exactly as it
        # was built. Nothing about this feature may fail a construction.
        return False
    return True


def opted_in(provider_id: str) -> bool:
    """Whether this provider id declared header capture."""

    return provider_id in _OPTED_IN


def _observer_for(
    board: UsageHeaderObserver, provider_id: str
) -> Callable[[Any, int], None]:
    """One provider's callback: record the allow-listed headers, nothing else.

    ``account_id`` is left empty on purpose. A provider in this catalogue takes
    one credential per instance and the reading is about the subscription
    behind it, so the provider id is already the whole identity here. The
    ``account_id`` argument exists on the observer for the OAuth pools, which
    have several accounts behind one provider id, and a family that later
    needs it has it available without changing this module's shape.
    """

    def observe(headers: Any, status_code: int) -> None:
        board.observe(headers, status_code=status_code, provider_id=provider_id)

    return observe


#: The ids :mod:`my_claude_code.config.usage_sources` declares as header
#: capture, read once at import. A provider whose only mechanism is a read of
#: its own -- or none at all -- never gets an observer.
_OPTED_IN: frozenset[str] = frozenset(header_capture_providers())
