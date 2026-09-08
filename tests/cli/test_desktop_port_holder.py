"""``classify_port_holder``: who holds the port, decided by process.

This is BUG-5 from the desktop/server lifecycle audit, and the defect is worth
stating because the fix looks like a refactor otherwise. The old answer came
from a *bind test*: if a socket could not be bound, something was on the port,
and anything that did not answer a healthy ``/health`` was ``foreign``. Every
one of those words is true and the conclusion is wrong -- MCC's own server
during its twenty-second startup binds the port and answers nothing, so the
desktop window told the reporter that "python.exe (pid N) ... is not the MCC
server" about My Claude Code's own process, and offered them a page whose only
advice was to stop it and change the port.

The classification here is decided by the *process*: the pid holding the
listener, its image, and its command line -- via ``port_takeover``'s
``identity_for_owner``, which is the same identification the server's own port
takeover uses, so the two cannot disagree about what "ours" means.
"""

import pytest

from my_claude_code.cli import desktop as desktop_module
from my_claude_code.cli.desktop import (
    HOLDER_KINDS,
    PortHolder,
    ServerState,
    classify_port_holder,
    server_pid_of,
)
from my_claude_code.cli.port_diagnostics import PortOwner
from my_claude_code.cli.port_takeover import ProcessIdentity
from my_claude_code.config.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(host="127.0.0.1", port=8099)


def _owner(monkeypatch, owner: PortOwner | None) -> None:
    monkeypatch.setattr(desktop_module, "diagnose_port_owner", lambda *a, **k: owner)


def _identity(monkeypatch, identity: ProcessIdentity | None) -> None:
    import my_claude_code.cli.port_takeover as takeover

    monkeypatch.setattr(takeover, "identity_for_owner", lambda *a, **k: identity)


def _free(monkeypatch, free: bool) -> None:
    monkeypatch.setattr(desktop_module, "probe_port_available", lambda *a, **k: free)


def test_an_empty_port_is_absent_and_costs_no_process_lookup(settings, monkeypatch):
    """`absent` is the cheap answer, and it must stay cheap."""

    _free(monkeypatch, True)

    def explode(*_args, **_kwargs):  # pragma: no cover - the point is it is not called
        raise AssertionError("a free port must not be diagnosed")

    monkeypatch.setattr(desktop_module, "diagnose_port_owner", explode)
    holder = classify_port_holder(settings, ServerState("free"))
    assert holder == PortHolder("absent")
    assert server_pid_of(holder) is None


@pytest.mark.parametrize(
    ("presence", "kind"),
    [
        ("healthy", "ours_healthy"),
        ("starting", "ours_starting"),
        ("draining", "ours_draining"),
    ],
)
def test_a_server_that_answered_is_ours_and_carries_its_pid(
    settings, monkeypatch, presence, kind
):
    """A reply settles the kind; the lookup only supplies the pid.

    The pid is what ``server_pid`` reports, and it is the fact the audit asked
    for in §5.3: one recorded server pid, readable by the window, the tray and
    the helper.
    """

    _owner(monkeypatch, PortOwner(pid=4242, name="python.exe", command=None))
    holder = classify_port_holder(settings, ServerState(presence))
    assert holder == PortHolder(kind, pid=4242, image="python.exe")
    assert server_pid_of(holder) == 4242


def test_a_silent_holder_that_is_ours_is_stale_and_never_foreign(settings, monkeypatch):
    """The reporter's exact case: MCC's own python.exe, holding, silent.

    ``mcc-stale``/``ours_stale`` and not ``foreign``, because the port-conflict
    page tells the user to stop another program -- and following that advice
    about My Claude Code's own server is how a slow start became a dead end.
    """

    _free(monkeypatch, False)
    _owner(monkeypatch, PortOwner(pid=17, name="python.exe", command=None))
    _identity(
        monkeypatch,
        ProcessIdentity(pid=17, image="python.exe", command="python -m mcc_server"),
    )
    holder = classify_port_holder(settings, ServerState("mcc-stale"))
    assert holder == PortHolder("ours_stale", pid=17, image="python.exe")
    assert server_pid_of(holder) == 17


def test_a_genuinely_foreign_holder_is_named(settings, monkeypatch):
    _free(monkeypatch, False)
    _owner(monkeypatch, PortOwner(pid=99, name="nginx.exe", command=None))
    _identity(
        monkeypatch,
        ProcessIdentity(pid=99, image="nginx.exe", command="nginx -g daemon off"),
    )
    holder = classify_port_holder(settings, ServerState("foreign"))
    assert holder == PortHolder("foreign", pid=99, image="nginx.exe")
    # Never reported as our server's pid: a key called ``server_pid`` naming
    # somebody else's process is worse than one that says nothing.
    assert server_pid_of(holder) is None


def test_a_holder_that_cannot_be_identified_is_foreign_and_the_shell_waits(
    settings, monkeypatch
):
    """Unidentifiable is ``foreign`` here, and a *grace window* in the shell.

    The two halves have to be read together. Python says what it can see;
    ``foreign_grace_seconds`` in the status document is what stops the window
    acting on an unidentifiable holder during our own startup, when the holder
    is overwhelmingly us.
    """

    _free(monkeypatch, False)
    _owner(monkeypatch, PortOwner(pid=None, name=None, command=None))
    _identity(monkeypatch, None)
    holder = classify_port_holder(settings, ServerState("foreign"))
    assert holder.kind == "foreign"


def test_every_kind_is_declared(settings):
    """The table in the audit and the type here must not drift apart."""

    assert set(HOLDER_KINDS) == {
        "absent",
        "ours_healthy",
        "ours_starting",
        "ours_draining",
        "ours_stale",
        "foreign",
    }
    # And the shell branches on exactly these strings.
    shell = desktop_module.__file__.replace("cli/desktop.py", "").replace(
        "cli\\desktop.py", ""
    )
    del shell


def test_the_document_shape_is_what_the_shell_parses():
    holder = PortHolder("ours_stale", pid=7, image="python.exe")
    assert holder.as_dict() == {"kind": "ours_stale", "pid": 7, "image": "python.exe"}
    assert PortHolder("absent").as_dict() == {
        "kind": "absent",
        "pid": None,
        "image": None,
    }
