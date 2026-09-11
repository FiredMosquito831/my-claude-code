"""What the official installer needs to know -- and to do -- about ONE server.

From 6.73.0 the installer is allowed to restart the server it just installed
(``install.ps1 -Restart`` / ``install.sh --restart`` / ``install.cmd
--restart``). Two questions stand between "the new version is on disk" and "a
listener is answering on the configured port", and neither of them may be
answered in PowerShell or in ``sh``:

1. **Who holds the configured port, and is it one of ours?** The rule is
   6.59.0's and 6.72.2's, and both live in Python. Re-implementing either in a
   shell script would be a second opinion about which processes MCC is allowed
   to stop, and the one time MCC decided that by image name it stopped the
   user's own application, twice.
2. **Stop exactly that one server, within its own configured budget.** By pid,
   never by name, and never a second server: the user runs several MCC servers
   on several ports with agents waiting on them, and the installer's restart
   means *the server bound to the port of the configuration directory this
   install is for* and nothing else (decision "SCOPE OF RESTART", 2026-09-11).

So the installer asks the product. ``mcc-server --report-holder <port>`` is
read-only and prints one JSON document; ``mcc-server --stop-holder <port>``
stops the MCC server holding that port and prints what it did. Every other MCC
server found on the machine is **listed and never touched**.

Why there is no ``POST /admin/api/.../stop`` in the ladder: there is no such
route. The admin surface can ask a server to *replace* itself
(``/admin/api/version/upgrade``), which is the thing the installer is
replacing, and adding a "stop yourself" route reachable over HTTP is a new
security surface and a separate concern. The stop used here is the escalation
the tray and the 6.72.2 sweep already share -- ask, wait the server's own
``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` budget plus its teardown margin, then
force -- addressed to exact pids (:func:`core.mcc_processes.stop_process`).

Both commands exit 0 whenever they produced a document, including when the
answer is "a stranger holds the port". The document is the result; a non-zero
status is reserved for "this build does not understand the question", which is
how an older ``mcc-server`` (a pinned ``--version`` install) tells a newer
installer to start nothing.
"""

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from my_claude_code.cli.port_takeover import identify_port_holder
from my_claude_code.config.constants import SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT
from my_claude_code.core.mcc_processes import (
    ProcessChain,
    ProcessFacts,
    mcc_server_chains,
    process_is_alive,
    scan_processes,
    stop_chain,
    stop_process,
)
from my_claude_code.core.stop_deadline import (
    HARD_EXIT_GRACE_SECONDS,
    STOP_TEARDOWN_MARGIN_SECONDS,
    clamp_stop_budget,
)

#: How long to wait for the port to actually come free after the holder is
#: gone. A terminated process on Windows can keep a listening socket for a beat
#: after its handle count drops, and starting into that beat is a bind failure
#: with a misleading message.
PORT_RELEASE_SECONDS = 10.0

#: How often to re-ask whether the port is free.
PORT_RELEASE_POLL_SECONDS = 0.5


def default_stop_budget(graceful_seconds: float | None = None) -> float:
    """Seconds between "please stop" and "you are being killed".

    The same sum the tray waits (``cli.desktop.server_stop_wait_seconds``): the
    server's configured graceful budget, its teardown margin, and the beat its
    own watchdog allows itself before it hard-exits. Waiting exactly that long
    means the installer never kills a server that is legitimately mid-drain
    under the budget its operator configured.
    """

    budget = clamp_stop_budget(
        SERVER_GRACEFUL_SHUTDOWN_SECONDS_DEFAULT
        if graceful_seconds is None
        else graceful_seconds
    )
    return budget + STOP_TEARDOWN_MARGIN_SECONDS + HARD_EXIT_GRACE_SECONDS


@dataclass(frozen=True, slots=True)
class HolderVerdict:
    """Who holds the port, whether it is ours, and why we think so."""

    #: The listening pid, or ``None`` when nothing holds the port.
    pid: int | None
    #: The image name, as the OS reports it.
    image: str | None
    #: The command line, when one could be read.
    command: str | None
    #: Whether this is an MCC **server** -- the only thing that may be stopped.
    is_mcc_server: bool
    #: The sentence the installer prints, and the reason for the verdict.
    reason: str
    #: Every pid of this one launch, innermost last. Empty when not ours.
    chain_pids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "image": self.image,
            "command": self.command,
            "is_mcc_server": self.is_mcc_server,
            "reason": self.reason,
            "chain_pids": list(self.chain_pids),
        }

    def describe(self) -> str:
        if self.pid is None:
            return "nothing is listening on that port"
        name = self.image or "an unidentified process"
        return f"{name} (pid {self.pid})"


def _chain_for(pid: int, chains: list[ProcessChain]) -> ProcessChain | None:
    for chain in chains:
        if pid in chain.pids:
            return chain
    return None


def _facts_for(pid: int, processes: list[ProcessFacts]) -> ProcessFacts | None:
    for item in processes:
        if item.pid == pid:
            return item
    return None


def classify_holder(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
    processes: list[ProcessFacts] | None = None,
) -> HolderVerdict:
    """Who holds ``host:port``, structurally. Never signals anyone.

    Two independent readings have to agree before anything may be stopped:

    * the **structural** one (6.72.2): the holder's own executable basename, or
      one of its argument tokens, is one of the four console scripts that map
      to ``entrypoints:serve``. This is what distinguishes the server from the
      seventeen launchers that live in the same uv tool environment -- a
      directory literally named ``my-claude-code``, which is why no substring
      test may ever decide this;
    * the **port-holder** one (6.59.0): ``ProcessIdentity.is_mcc``, which is
      broader on purpose because a launcher never holds the configured port.

    When the process table can be read, the structural reading decides, in both
    directions: a holder the scan can see and does not call a server is a
    stranger even if its command line mentions us. When the scan produces
    nothing at all -- it timed out, or the platform refused it -- the 6.59.0
    reading is the fallback, because that is the rule that has shipped since
    6.59.0 for exactly this question.

    "Cannot tell" is always **not ours**. A process this module failed to read
    is never stopped.
    """

    holder = identify_port_holder(host, port, timeout=timeout)
    if holder is None or holder.pid is None:
        return HolderVerdict(
            pid=None,
            image=None,
            command=None,
            is_mcc_server=False,
            reason="nothing is listening on that port",
        )
    pid = holder.pid
    facts = scan_processes() if processes is None else processes
    chains = mcc_server_chains(facts)
    if not facts:
        # No process table at all. Fall back to the rule the port takeover has
        # used since 6.59.0 rather than inventing a third one here.
        if holder.is_mcc:
            return HolderVerdict(
                pid=pid,
                image=holder.image,
                command=holder.command,
                is_mcc_server=True,
                reason=(
                    "the process table could not be read; the port holder "
                    "identifies as My Claude Code (the 6.59.0 rule)"
                ),
                chain_pids=(pid,),
            )
        return HolderVerdict(
            pid=pid,
            image=holder.image,
            command=holder.command,
            is_mcc_server=False,
            reason=(
                "the process table could not be read and the port holder does "
                "not identify as My Claude Code"
            ),
        )
    chain = _chain_for(pid, chains)
    if chain is not None:
        return HolderVerdict(
            pid=pid,
            image=holder.image,
            command=holder.command,
            is_mcc_server=True,
            reason=(f"the port holder is a My Claude Code server: {chain.describe()}"),
            chain_pids=chain.pids,
        )
    known = _facts_for(pid, facts)
    if known is None:
        return HolderVerdict(
            pid=pid,
            image=holder.image,
            command=holder.command,
            is_mcc_server=False,
            reason=(
                "the port holder was not in the process scan, so it cannot be "
                "identified as one of ours"
            ),
        )
    return HolderVerdict(
        pid=pid,
        image=holder.image,
        command=holder.command,
        is_mcc_server=False,
        reason=(
            f"the port holder is {known.describe()}, which is not a My Claude "
            "Code server"
        ),
    )


def other_server_reports(
    holder: HolderVerdict, *, processes: list[ProcessFacts] | None = None
) -> list[dict[str, Any]]:
    """Every OTHER My Claude Code server on this machine, for the transcript.

    Listed, never stopped. The user runs several servers on several ports with
    agents waiting on them; the installer's restart is one server, the one
    bound to the port of the configuration directory this install is for.

    THIS process is excluded, and that is not a detail. ``mcc-server
    --report-holder`` runs out of the same console script the server does, so
    the structural scan quite correctly calls it a My Claude Code server -- and
    without this, the first real run of the command reported *itself* to the
    installer as another server running on the machine. Measured on
    2026-09-11: ``mcc-server.exe (pid 2260) -> pid 58100``, where 58100 was the
    process writing the report.
    """

    facts = scan_processes() if processes is None else processes
    own_pid = os.getpid()
    reports: list[dict[str, Any]] = []
    for chain in mcc_server_chains(facts):
        if holder.pid is not None and holder.pid in chain.pids:
            continue
        if own_pid in chain.pids:
            continue
        reports.append(
            {
                "pids": list(chain.pids),
                "describe": chain.describe(),
                "holds": list(chain.holds()),
            }
        )
    return reports


def report_document(host: str, port: int, *, timeout: float = 5.0) -> dict[str, Any]:
    """The read-only answer to "what is on my port, and what else is running?"."""

    facts = scan_processes()
    holder = classify_holder(host, port, timeout=timeout, processes=facts)
    return {
        "host": host,
        "port": port,
        "holder": holder.as_dict(),
        "holder_description": holder.describe(),
        "other_servers": other_server_reports(holder, processes=facts),
    }


def port_is_free(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """Whether nothing is listening on ``host:port`` right now."""

    return identify_port_holder(host, port, timeout=timeout) is None


def stop_document(
    host: str,
    port: int,
    *,
    graceful_seconds: float | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Stop the MCC server holding ``host:port``, and say what happened.

    Refuses in every case but one: the holder is, structurally, one of our own
    servers. A stranger, an unreadable process, or an empty port all come back
    with ``stopped: false`` and a sentence, and nothing is signalled.
    """

    facts = scan_processes()
    holder = classify_holder(host, port, timeout=timeout, processes=facts)
    document: dict[str, Any] = {
        "host": host,
        "port": port,
        "holder": holder.as_dict(),
        "holder_description": holder.describe(),
        "other_servers": other_server_reports(holder, processes=facts),
        "stopped": False,
        "port_free": holder.pid is None,
        "budget_seconds": default_stop_budget(graceful_seconds),
    }
    if holder.pid is None:
        document["message"] = "Nothing was listening on that port."
        return document
    if not holder.is_mcc_server:
        document["message"] = (
            f"{holder.describe()} holds the port and is not a My Claude Code "
            "server, so it was left alone."
        )
        return document

    budget = float(document["budget_seconds"])
    chain = _chain_for(holder.pid, mcc_server_chains(facts))
    if chain is None:
        gone = not process_is_alive(holder.pid)
        if not gone:
            gone = stop_process(holder.pid, grace=budget)
    else:
        gone = stop_chain(chain, grace=budget)
    document["stopped"] = bool(gone)
    document["port_free"] = _wait_for_release(host, port, timeout=timeout)
    if document["stopped"] and document["port_free"]:
        document["message"] = f"Stopped {holder.describe()} and the port is free."
    elif document["stopped"]:
        document["message"] = (
            f"Stopped {holder.describe()}, but the port is still held."
        )
    else:
        document["message"] = f"Could not stop {holder.describe()}."
    return document


def _wait_for_release(host: str, port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + PORT_RELEASE_SECONDS
    while time.monotonic() < deadline:
        if port_is_free(host, port, timeout=timeout):
            return True
        time.sleep(PORT_RELEASE_POLL_SECONDS)
    return port_is_free(host, port, timeout=timeout)


def shell_lines(document: dict[str, Any]) -> list[str]:
    """The same document as ``KEY=value`` lines, for ``install.sh``.

    POSIX ``sh`` has no JSON parser and the machines this runs on are not
    guaranteed to have ``jq``; the alternatives are a ``sed`` expression that
    pretends to parse JSON or an ``eval`` of text a process wrote, and both of
    those are how a shell script comes to execute something it read. These
    lines are consumed by a ``case`` over the key and a plain assignment, so
    nothing in the value is ever interpreted.

    Every value is one line: newlines inside a message become spaces, because a
    reader that has to reassemble a value across lines is a parser again.
    """

    def one_line(value: Any) -> str:
        return " ".join(str(value).split())

    holder = document.get("holder") or {}
    lines = [
        f"MCC_PORT={document.get('port', 0)}",
        f"MCC_HOST={one_line(document.get('host', ''))}",
        f"MCC_HOLDER_PID={holder.get('pid') or 0}",
        f"MCC_HOLDER_IS_SERVER={'1' if holder.get('is_mcc_server') else '0'}",
        f"MCC_HOLDER_DESCRIPTION={one_line(document.get('holder_description', ''))}",
        f"MCC_HOLDER_REASON={one_line(holder.get('reason', ''))}",
    ]
    if "stopped" in document:
        lines.append(f"MCC_STOPPED={'1' if document.get('stopped') else '0'}")
    if "port_free" in document:
        lines.append(f"MCC_PORT_FREE={'1' if document.get('port_free') else '0'}")
    if "message" in document:
        lines.append(f"MCC_MESSAGE={one_line(document.get('message', ''))}")
    others = document.get("other_servers") or []
    lines.append(f"MCC_OTHER_SERVERS={len(others)}")
    for index, item in enumerate(others, start=1):
        lines.append(f"MCC_OTHER_SERVER_{index}={one_line(item.get('describe', ''))}")
    return lines


def _value_after(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag:
            return argv[index + 1] if index + 1 < len(argv) else None
        if item.startswith(f"{flag}="):
            return item.split("=", 1)[1]
    return None


def run_installer_support(argv: list[str]) -> int | None:
    """Answer ``--report-holder``/``--stop-holder``; ``None`` when neither was asked.

    ``None`` rather than a status so ``cli.entrypoints.serve`` can tell "this
    was not for me" from "this was for me and it is done", without the
    entrypoint having to know either flag's shape.
    """

    for flag, handler in (
        ("--report-holder", "report"),
        ("--stop-holder", "stop"),
    ):
        raw = _value_after(argv, flag)
        if raw is None and flag not in argv:
            continue
        if raw is None:
            print(f"{flag} needs a port number.", file=sys.stderr)
            return 2
        try:
            port = int(raw)
        except ValueError:
            print(f"{flag} needs a port number, not {raw!r}.", file=sys.stderr)
            return 2
        host = _value_after(argv, "--host") or "127.0.0.1"
        grace_raw = _value_after(argv, "--graceful-seconds")
        graceful = None
        if grace_raw is not None:
            try:
                graceful = float(grace_raw)
            except ValueError:
                graceful = None
        if handler == "report":
            document = report_document(host, port)
        else:
            document = stop_document(host, port, graceful_seconds=graceful)
        if (_value_after(argv, "--format") or "json") == "shell":
            for line in shell_lines(document):
                print(line)
        else:
            print(json.dumps(document))
        return 0
    return None


__all__ = [
    "HolderVerdict",
    "classify_holder",
    "default_stop_budget",
    "other_server_reports",
    "port_is_free",
    "report_document",
    "run_installer_support",
    "shell_lines",
    "stop_document",
]
