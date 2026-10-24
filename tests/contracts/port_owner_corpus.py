"""Recorded ``ss -ltnp`` / ``netstat -ano`` outputs for the port-owner equality.

Each case is what ``cli.port_diagnostics.diagnose_port_owner`` sees from its
two commands on one machine shape: Linux (iproute2 ``ss``, net-tools
``netstat``), macOS (BSD ``netstat``, which prints ``host.port`` and has no
``ss``) and Windows (``netstat -ano`` with the PID column, no ``ss``), over IPv4
and IPv6, with the port listening, absent, present only in TIME_WAIT, and held
by some other pid. A command is either an answer (``returncode`` + ``stdout``)
or a failure the ladder swallows (``missing`` -> ``FileNotFoundError``,
``timeout`` -> ``subprocess.TimeoutExpired``, ``error`` -> a bare ``OSError``).

The outputs are typed in the exact column shapes these tools print; the pids,
ports and names are invented. The legacy line parser matches ``:<port>`` as a
SUBSTRING, so every case is asked about several ports (``PORTS``), including
ones that are prefixes of a listening port (808 of 8082) -- the baseline pins
whatever the code before 7.111.3 answered for each, right or wrong.
"""

from typing import Any

#: Every case is asked about each of these ports.
PORTS = (8082, 808, 18082, 53, 135, 631, 51234)

SS_HEADER = "State  Recv-Q Send-Q   Local Address:Port    Peer Address:Port Process"

LINUX_SS_LISTENING_V4 = "\n".join(
    [
        SS_HEADER,
        'LISTEN 0      4096    127.0.0.53%lo:53           0.0.0.0:*     users:(("systemd-resolve",pid=612,fd=13))',
        'LISTEN 0      2048          0.0.0.0:8082         0.0.0.0:*     users:(("python3",pid=4321,fd=7))',
        "LISTEN 0      128         127.0.0.1:631          0.0.0.0:*",
        "",
    ]
)

LINUX_SS_LISTENING_V6 = "\n".join(
    [
        SS_HEADER,
        'LISTEN 0      2048             [::]:8082            [::]:*     users:(("mcc-server",pid=5150,fd=9))',
        "LISTEN 0      128             [::1]:631             [::]:*",
        "",
    ]
)

LINUX_SS_DUAL_STACK = "\n".join(
    [
        SS_HEADER,
        'LISTEN 0      2048          0.0.0.0:8082         0.0.0.0:*     users:(("python3",pid=4321,fd=7))',
        'LISTEN 0      2048             [::]:8082            [::]:*     users:(("python3",pid=4321,fd=8))',
        "",
    ]
)

LINUX_SS_OTHER_PID = "\n".join(
    [
        SS_HEADER,
        'LISTEN 0      511           0.0.0.0:18082        0.0.0.0:*     users:(("nginx",pid=777,fd=6),("nginx",pid=778,fd=6))',
        'LISTEN 0      2048        127.0.0.1:8082         0.0.0.0:*     users:(("node",pid=9001,fd=21))',
        "",
    ]
)

# Another user's socket: ss lists it but cannot name its process without root.
LINUX_SS_NO_PROCESS = "\n".join(
    [
        SS_HEADER,
        "LISTEN 0      2048          0.0.0.0:8082         0.0.0.0:*",
        "",
    ]
)

LINUX_SS_ABSENT = "\n".join(
    [
        SS_HEADER,
        'LISTEN 0      4096    127.0.0.53%lo:53           0.0.0.0:*     users:(("systemd-resolve",pid=612,fd=13))',
        "",
    ]
)

LINUX_SS_EMPTY = SS_HEADER + "\n"

NET_TOOLS_HEADER = "\n".join(
    [
        "Active Internet connections (servers and established)",
        "Proto Recv-Q Send-Q Local Address           Foreign Address         State       Timer",
    ]
)

LINUX_NETSTAT_LISTENING = "\n".join(
    [
        NET_TOOLS_HEADER,
        "tcp        0      0 0.0.0.0:8082            0.0.0.0:*               LISTEN      off (0.00/0/0)",
        "tcp        0      0 127.0.0.53:53           0.0.0.0:*               LISTEN      off (0.00/0/0)",
        "tcp6       0      0 :::8082                 :::*                    LISTEN      off (0.00/0/0)",
        "Active UNIX domain sockets (servers and established)",
        "Proto RefCnt Flags       Type       State         I-Node   Path",
        "unix  2      [ ACC ]     STREAM     LISTENING     23456    /run/systemd/private",
        "",
    ]
)

LINUX_NETSTAT_TIME_WAIT_ONLY = "\n".join(
    [
        NET_TOOLS_HEADER,
        "tcp        0      0 127.0.0.1:8082          127.0.0.1:51234         TIME_WAIT   timewait (52.30/0/0)",
        "tcp6       0      0 ::1:8082                ::1:51240               TIME_WAIT   timewait (41.12/0/0)",
        "",
    ]
)

LINUX_NETSTAT_ABSENT = "\n".join(
    [
        NET_TOOLS_HEADER,
        "tcp        0      0 127.0.0.53:53           0.0.0.0:*               LISTEN      off (0.00/0/0)",
        "",
    ]
)

MACOS_NETSTAT_HEADER = "\n".join(
    [
        "Active Internet connections (including servers)",
        "Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)",
    ]
)

MACOS_NETSTAT_LISTENING = "\n".join(
    [
        MACOS_NETSTAT_HEADER,
        "tcp4       0      0  127.0.0.1.8082         *.*                    LISTEN",
        "tcp6       0      0  *.8082                 *.*                    LISTEN",
        "tcp4       0      0  *.631                  *.*                    LISTEN",
        "",
    ]
)

MACOS_NETSTAT_TIME_WAIT_ONLY = "\n".join(
    [
        MACOS_NETSTAT_HEADER,
        "tcp4       0      0  127.0.0.1.8082         127.0.0.1.51234        TIME_WAIT",
        "",
    ]
)

MACOS_NETSTAT_ILLEGAL_OPTION = ""

WINDOWS_NETSTAT_HEADER = "\n".join(
    [
        "",
        "Active Connections",
        "",
        "  Proto  Local Address          Foreign Address        State           PID",
    ]
)

WINDOWS_NETSTAT_LISTENING_V4 = "\n".join(
    [
        WINDOWS_NETSTAT_HEADER,
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1160",
        "  TCP    0.0.0.0:8082           0.0.0.0:0              LISTENING       4321",
        "  TCP    127.0.0.1:8082         127.0.0.1:51234        ESTABLISHED     4321",
        "  TCP    127.0.0.1:51234        127.0.0.1:8082         ESTABLISHED     9876",
        "  UDP    0.0.0.0:5353           *:*                                    2468",
        "",
    ]
)

WINDOWS_NETSTAT_LISTENING_V6 = "\n".join(
    [
        WINDOWS_NETSTAT_HEADER,
        "  TCP    [::]:135               [::]:0                 LISTENING       1160",
        "  TCP    [::1]:8082             [::]:0                 LISTENING       5150",
        "  UDP    [::]:5353              *:*                                    2468",
        "",
    ]
)

WINDOWS_NETSTAT_TIME_WAIT_ONLY = "\n".join(
    [
        WINDOWS_NETSTAT_HEADER,
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1160",
        "  TCP    127.0.0.1:8082         127.0.0.1:51234        TIME_WAIT       0",
        "  TCP    127.0.0.1:8082         127.0.0.1:51240        TIME_WAIT       0",
        "",
    ]
)

WINDOWS_NETSTAT_OTHER_PID = "\n".join(
    [
        WINDOWS_NETSTAT_HEADER,
        "  TCP    0.0.0.0:18082          0.0.0.0:0              LISTENING       777",
        "  TCP    127.0.0.1:8082         0.0.0.0:0              LISTENING       9001",
        "  TCP    [::]:18082             [::]:0                 LISTENING       777",
        "",
    ]
)

WINDOWS_NETSTAT_ABSENT = "\n".join(
    [
        WINDOWS_NETSTAT_HEADER,
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1160",
        "  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4",
        "",
    ]
)

# A German Windows prints the state in German; the legacy parser looks for
# "listen", so this reads as "no owner" before and after.
WINDOWS_NETSTAT_LOCALISED = "\n".join(
    [
        "",
        "Aktive Verbindungen",
        "",
        "  Proto  Lokale Adresse         Remoteadresse          Status           PID",
        "  TCP    0.0.0.0:8082           0.0.0.0:0              ABHÖREN          4321",
        "",
    ]
)


def _answer(stdout: str, returncode: int = 0) -> dict[str, Any]:
    return {"kind": "answer", "returncode": returncode, "stdout": stdout}


MISSING: dict[str, Any] = {"kind": "missing"}
TIMEOUT: dict[str, Any] = {"kind": "timeout"}
ERROR: dict[str, Any] = {"kind": "error"}

#: name -> {"ss": behaviour, "netstat": behaviour}
CASES: dict[str, dict[str, dict[str, Any]]] = {
    # Linux, iproute2 present.
    "linux_ss_listening_v4": {
        "ss": _answer(LINUX_SS_LISTENING_V4),
        "netstat": MISSING,
    },
    "linux_ss_listening_v6": {
        "ss": _answer(LINUX_SS_LISTENING_V6),
        "netstat": MISSING,
    },
    "linux_ss_dual_stack": {"ss": _answer(LINUX_SS_DUAL_STACK), "netstat": MISSING},
    "linux_ss_other_pid": {"ss": _answer(LINUX_SS_OTHER_PID), "netstat": MISSING},
    "linux_ss_no_process_column": {
        "ss": _answer(LINUX_SS_NO_PROCESS),
        "netstat": MISSING,
    },
    "linux_ss_absent": {"ss": _answer(LINUX_SS_ABSENT), "netstat": MISSING},
    "linux_ss_empty": {"ss": _answer(LINUX_SS_EMPTY), "netstat": MISSING},
    "linux_ss_and_net_tools": {
        "ss": _answer(LINUX_SS_ABSENT),
        "netstat": _answer(LINUX_NETSTAT_LISTENING),
    },
    "linux_ss_failed_net_tools_listening": {
        "ss": _answer("", returncode=1),
        "netstat": _answer(LINUX_NETSTAT_LISTENING),
    },
    "linux_net_tools_only_listening": {
        "ss": MISSING,
        "netstat": _answer(LINUX_NETSTAT_LISTENING),
    },
    "linux_net_tools_only_time_wait": {
        "ss": MISSING,
        "netstat": _answer(LINUX_NETSTAT_TIME_WAIT_ONLY),
    },
    "linux_net_tools_only_absent": {
        "ss": MISSING,
        "netstat": _answer(LINUX_NETSTAT_ABSENT),
    },
    "linux_ss_timeout_net_tools_listening": {
        "ss": TIMEOUT,
        "netstat": _answer(LINUX_NETSTAT_LISTENING),
    },
    "linux_neither": {"ss": MISSING, "netstat": MISSING},
    "linux_both_timeout": {"ss": TIMEOUT, "netstat": TIMEOUT},
    # macOS: no ss; BSD netstat prints host.port and no pid.
    "macos_listening": {"ss": MISSING, "netstat": _answer(MACOS_NETSTAT_LISTENING)},
    "macos_time_wait_only": {
        "ss": MISSING,
        "netstat": _answer(MACOS_NETSTAT_TIME_WAIT_ONLY),
    },
    "macos_illegal_option": {
        "ss": MISSING,
        "netstat": _answer(MACOS_NETSTAT_ILLEGAL_OPTION, returncode=1),
    },
    "macos_netstat_timeout": {"ss": MISSING, "netstat": TIMEOUT},
    # Windows: no ss; netstat -ano carries the PID.
    "windows_listening_v4": {
        "ss": MISSING,
        "netstat": _answer(WINDOWS_NETSTAT_LISTENING_V4),
    },
    "windows_listening_v6": {
        "ss": MISSING,
        "netstat": _answer(WINDOWS_NETSTAT_LISTENING_V6),
    },
    "windows_time_wait_only": {
        "ss": MISSING,
        "netstat": _answer(WINDOWS_NETSTAT_TIME_WAIT_ONLY),
    },
    "windows_other_pid": {
        "ss": MISSING,
        "netstat": _answer(WINDOWS_NETSTAT_OTHER_PID),
    },
    "windows_absent": {"ss": MISSING, "netstat": _answer(WINDOWS_NETSTAT_ABSENT)},
    "windows_localised": {
        "ss": MISSING,
        "netstat": _answer(WINDOWS_NETSTAT_LOCALISED),
    },
    "windows_netstat_timeout": {"ss": MISSING, "netstat": TIMEOUT},
    "windows_netstat_os_error": {"ss": ERROR, "netstat": ERROR},
    "windows_netstat_nonzero": {
        "ss": MISSING,
        "netstat": _answer(WINDOWS_NETSTAT_LISTENING_V4, returncode=1),
    },
}
