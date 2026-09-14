"""The other My Claude Code servers on this machine, and stopping the dead ones.

The engine was finished before this file existed: ``core/server_inventory``
observes every MCC server launch from three read-only sources (the process
table, the socket table, and the session rows in the request log) and
``stop_stale_servers`` re-derives each chain from a *fresh* process table and
refuses to signal anything whose pid set no longer matches exactly. What was
missing was a route and a page -- ``grep other_servers src/my_claude_code/api/``
returned nothing, so the only way to see the survey was the server's own start
log or the desktop status document.

Three rules shape what is here, and all three are the product of a near-miss.
On 2026-09-10 an investigation called two of the user's servers "abandoned"
because they owned no listening socket, and the plan that followed would have
stopped them; they were live agent chains with hours of CPU and open upstream
connections.

**Nothing is ever automatic.** This route stops a server only when a person
asked for that server, by pid, after being shown what it is.
``SERVER_STALE_SERVER_ACTION`` keeps its ``report`` default and is not read
here: it governs what the *server* does to itself at start, which is a
different question from what an operator may ask for at a keyboard.

**"Stale" stays the provable definition.** It is not "looks idle" and not
"owns no socket": it is a heartbeat that has gone quiet *and* the recorded port
now served by a different MCC server, or a launcher whose whole descendant tree
is gone. ``serving`` and ``live`` chains are never offered and are refused when
asked for by pid anyway.

**The observation is re-made before anything is signalled.** The client sends
pids; this re-runs the survey and acts only on a chain that is still stale and
whose pid set is still exactly what the client named. A pid is a reusable
number, and the gap between rendering a page and clicking a button is long
enough for one to be handed to somebody else.
"""

import asyncio
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from my_claude_code.config.paths import request_log_path
from my_claude_code.config.settings import Settings
from my_claude_code.core.server_inventory import (
    ACTIONABLE_STATUSES,
    ServerObservation,
    observe_servers,
    stop_stale_servers,
)

from .admin_routes import require_loopback_admin
from .dependencies import get_settings

router = APIRouter()

#: A pid tuple the client is asking about, as JSON gives it: a list of ints.
PidList = list[int]


class StopServersPayload(BaseModel):
    """The chains the operator selected, each named by its exact pid set.

    Named by pid rather than by an index or an id on purpose: an index into a
    list the client rendered is meaningless by the time the server re-observes,
    and there is no durable identifier for a process chain. The pid set is the
    only thing that both sides can check.
    """

    pids: list[PidList] = Field(default_factory=list)


def _survey(settings: Settings) -> list[ServerObservation]:
    """Observe every other MCC server, now.

    Fresh rather than read from ``other-servers.json``: that file is written
    once, by the server's own start (``cli/commands.py:261``), and may be an
    hour old. A page offering to stop a process must not be looking at an
    hour-old list.
    """

    return observe_servers(
        request_log_path=request_log_path(),
        self_pid=os.getpid(),
        stale_after_seconds=settings.server_stale_session_seconds,
    )


def _entry(observation: ServerObservation) -> dict[str, Any]:
    """One server, with every field the confirmation dialog has to show."""

    payload = observation.as_status_entry()
    payload["actionable"] = observation.is_actionable
    payload["heartbeat_age_seconds"] = observation.heartbeat_age_seconds
    payload["describe"] = observation.describe()
    return payload


def _payload(observations: list[ServerObservation]) -> dict[str, Any]:
    return {
        "servers": [_entry(item) for item in observations],
        "stale_count": sum(1 for item in observations if item.is_actionable),
        # Named so the page can say what the *server* would do on its own,
        # which is "nothing" unless the operator changed it.
        "actionable_statuses": sorted(ACTIONABLE_STATUSES),
    }


@router.get("/admin/api/servers")
async def list_servers(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Return every other MCC server launch this machine can see, right now."""

    require_loopback_admin(request)
    observations = await asyncio.to_thread(_survey, settings)
    return _payload(observations)


@router.post("/admin/api/servers/stop")
async def stop_servers(
    payload: StopServersPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Stop exactly the stale chains the operator named, by pid.

    Refuses in both directions: a pid set that is no longer stale is not
    stopped even when the client asks for it by pid, and a pid set that no
    longer matches any chain is reported as gone rather than signalled.
    """

    require_loopback_admin(request)
    if not payload.pids:
        raise HTTPException(status_code=400, detail="No servers were named.")

    wanted = {tuple(pids) for pids in payload.pids if pids}
    if not wanted:
        raise HTTPException(status_code=400, detail="No servers were named.")

    def run() -> dict[str, Any]:
        observations = _survey(settings)
        by_pids = {item.pids: item for item in observations}
        selected: list[ServerObservation] = []
        refused: list[dict[str, Any]] = []
        for pids in sorted(wanted):
            observation = by_pids.get(pids)
            if observation is None:
                refused.append(
                    {
                        "pids": list(pids),
                        "reason": "no MCC server with exactly these pids is running",
                    }
                )
                continue
            if not observation.is_actionable:
                refused.append(
                    {
                        "pids": list(pids),
                        "status": observation.status,
                        "reason": (
                            f"status is {observation.status}, not stale: "
                            f"{observation.reason}"
                        ),
                    }
                )
                continue
            selected.append(observation)

        # ``stop_stale_servers`` filters by ``is_actionable`` again and
        # re-derives each chain from another fresh process table. Handing it
        # only the selection is what makes this an operator's decision rather
        # than a sweep.
        stopped = stop_stale_servers(selected)
        return {
            "stopped": [_entry(item) for item in stopped],
            "refused": refused,
            **_payload(_survey(settings)),
        }

    return await asyncio.to_thread(run)
