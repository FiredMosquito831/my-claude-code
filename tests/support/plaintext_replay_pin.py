"""Pin the 8.17.0 Responses switch for contracts recorded before it existed.

``RESPONSES_PLAINTEXT_REPLAY`` ships **on** from 8.17.0 by the user's ROUND 9
decision ("nothing is lost", "all toggleable"), so a body built through either
Responses **sender** now replays an earlier turn's readable reasoning as a
summary-only ``reasoning`` item where every release up to 8.16.3 replayed
nothing.

That is a deliberate change to the shipped default, and it is not a change to
any *conversion*. The contracts that assert what a real sender records -- "the
omission note reaches ``params.wire`` on both senders" -- were recorded against
a sender that replayed no plaintext at all. Left alone they would fail for a
reason that has nothing to do with what they guard, and the only ways out would
be to re-record them (which would silently retire the anchor each one exists to
be) or to delete the assertion.

So they pin the switch instead. Every existing assertion stays byte-identical;
what moves is only the one behaviour this release is allowed to move, and
``tests/contracts/test_responses_plaintext_replay.py`` proves the new default's
own behaviour separately, on the same two senders.

The pin is the real settings path -- an environment variable and the cache the
server itself clears after an admin save -- not a monkeypatched function, so a
contract that passes here would pass against a server started with
``RESPONSES_PLAINTEXT_REPLAY=false``. It is the same shape, and the same
argument, as ``tests/support/parallel_tool_calls_pin.py``.
"""

import pytest

from my_claude_code.config import settings as config_settings

#: The setting's environment name, spelled once.
PLAINTEXT_REPLAY_ENV = "RESPONSES_PLAINTEXT_REPLAY"


def pin_plaintext_replay_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every Responses body built from here on replay no plaintext.

    ``get_settings`` is an ``lru_cache``, and the admin save path clears it
    (``runtime/application._commit_admin_update``) -- which is exactly why a
    saved value reaches the next request with no restart. Clearing it here is
    the same mechanism, so the pin follows the production path rather than
    bypassing it.
    """

    monkeypatch.setenv(PLAINTEXT_REPLAY_ENV, "false")
    config_settings.get_settings.cache_clear()


def unpin_plaintext_replay() -> None:
    """Drop the settings object built under the pin.

    Called at teardown, while ``monkeypatch`` still holds the environment
    variable: the next ``get_settings()`` then rebuilds from the restored
    environment instead of handing a pinned object to the next test.
    """

    config_settings.get_settings.cache_clear()


@pytest.fixture
def plaintext_replay_off(monkeypatch: pytest.MonkeyPatch):
    """A named, opt-in pin, for a module that wants only some tests pinned."""

    pin_plaintext_replay_off(monkeypatch)
    try:
        yield
    finally:
        unpin_plaintext_replay()
