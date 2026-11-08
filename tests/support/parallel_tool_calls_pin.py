"""Pin the 8.15.0 Responses switch for contracts recorded before it existed.

``RESPONSES_PARALLEL_TOOL_CALLS`` ships **on** from 8.15.0 by the user's
decision (2026-11-08), so a body built through either Responses sender now
carries ``"parallel_tool_calls": true`` where every release up to 8.14.3
carried ``false``.

That is a deliberate change to the shipped default, and it is not a change to
any *conversion*. The contracts that pin historical bytes -- "the tool-name
alias fix changed nothing", "the empty-system-message fix changed nothing",
"the tool-change fold changed nothing", "v7.58.0's Responses body equals this
one with the thinking left out", "7.49.0's bytes from every harness" -- were
all recorded against a sender that sent ``false``. Left alone they would fail
for a reason that has nothing to do with what they guard, and the only ways
out would be to re-record them (which would silently retire the historical
anchor each one exists to be) or to delete the assertion.

So they pin the switch instead. Every frozen baseline stays byte-identical and
keeps pinning every other byte of every body; what moves is only the one cell
this release is allowed to move, and
``tests/providers/test_responses_parallel_tool_calls.py`` proves that cell is
the only one that moves, in both directions, on both doors.

The pin is the real settings path -- an environment variable and the cache the
server itself clears after an admin save -- not a monkeypatched function, so a
contract that passes here would pass against a server started with
``RESPONSES_PARALLEL_TOOL_CALLS=false``.
"""

import pytest

from my_claude_code.config import settings as config_settings

#: The setting's environment name, spelled once.
PARALLEL_TOOL_CALLS_ENV = "RESPONSES_PARALLEL_TOOL_CALLS"


def pin_parallel_tool_calls_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every Responses body built from here on carry ``false``.

    ``get_settings`` is an ``lru_cache``, and the admin save path clears it
    (``runtime/application._commit_admin_update``) -- which is exactly why a
    saved value reaches the next request with no restart. Clearing it here is
    the same mechanism, so the pin follows the production path rather than
    bypassing it.
    """

    monkeypatch.setenv(PARALLEL_TOOL_CALLS_ENV, "false")
    config_settings.get_settings.cache_clear()


def unpin_parallel_tool_calls() -> None:
    """Drop the settings object built under the pin.

    Called at teardown, while ``monkeypatch`` still holds the environment
    variable: the next ``get_settings()`` then rebuilds from the restored
    environment instead of handing a pinned object to the next test.
    """

    config_settings.get_settings.cache_clear()


@pytest.fixture
def parallel_tool_calls_off(monkeypatch: pytest.MonkeyPatch):
    """A named, opt-in pin, for a module that wants only some tests pinned."""

    pin_parallel_tool_calls_off(monkeypatch)
    try:
        yield
    finally:
        unpin_parallel_tool_calls()
