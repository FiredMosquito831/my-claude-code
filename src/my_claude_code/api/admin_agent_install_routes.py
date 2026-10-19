"""The Coding agents page's Install button (7.102.0), loopback-only.

``GET  /admin/api/agent-installs``                    every card's installed
                                                      state, its methods for
                                                      this OS, Node.js, the job
``POST /admin/api/agent-installs/{id}/plan``          ``{method}`` -> the plan
                                                      and its ``plan_token``
``POST /admin/api/agent-installs/{id}/run``           ``?confirm=install-<id>``
                                                      ``{method, plan_token,
                                                      dry_run}`` -> ``job_id``
``GET  /admin/api/agent-installs/jobs/{job_id}``      stage, new lines, result
``POST /admin/api/agent-installs/jobs/{job_id}/cancel`` stop its own PID tree

**The guards on ``run``**, each with its own test and mutation:

* ``require_destructive_admin_confirmation``: 400 without
  ``?confirm=install-<id>``, 403 without a local ``Origin`` -- a bare curl
  sends none, the dashboard always does;
* the ``plan_token`` must be the token of the plan the server writes *now*:
  a plan that changed since the page showed it (Node.js appeared, the agent
  got installed, the method list moved) is 409, and the page re-plans;
* one job at a time: 409 while another runs, here or in another MCC process
  on the same config folder;
* ``dry_run: true`` walks the plan and spawns nothing. There is no
  environment variable for any of this.

**The boundary.** The installer installs MCC and its prerequisites, nothing
else; agents are installed only by the dashboard button you press. Nothing
else in MCC calls these routes.
"""

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from my_claude_code.api.admin_routes import (
    require_destructive_admin_confirmation,
    require_loopback_admin,
)
from my_claude_code.application import agent_installer
from my_claude_code.application.agent_installer import InstallError

router = APIRouter()


class InstallPlanPayload(BaseModel):
    """Which method the user picked on the card."""

    method: str = Field(min_length=1, max_length=64)


class InstallRunPayload(BaseModel):
    """The confirmed plan: its method, its token, and whether to only walk it."""

    method: str = Field(min_length=1, max_length=64)
    plan_token: str = Field(min_length=1, max_length=128)
    dry_run: bool = False


def confirmation_for(target_id: str) -> str:
    """Return the ``?confirm=`` literal one card's run needs."""

    return f"install-{target_id}"


def _http(exc: InstallError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/admin/api/agent-installs")
async def list_agent_installs(request: Request) -> dict[str, Any]:
    """Return every card's install state. Spawns only ``--version`` of found
    programs and Node/npm probes, each bounded at 5 s."""

    require_loopback_admin(request)
    return await asyncio.to_thread(agent_installer.listing, os.environ)


@router.post("/admin/api/agent-installs/{target_id}/plan")
async def plan_agent_install(
    target_id: str, payload: InstallPlanPayload, request: Request
) -> dict[str, Any]:
    """Return exactly what a run would do. Downloads and spawns nothing
    beyond the read-only Node/npm probes."""

    require_loopback_admin(request)

    def run() -> dict[str, Any]:
        plan = agent_installer.plan_for(target_id, payload.method, os.environ)
        return {**plan.payload(), "confirm": confirmation_for(target_id)}

    try:
        return await asyncio.to_thread(run)
    except InstallError as exc:
        raise _http(exc) from exc


@router.post("/admin/api/agent-installs/{target_id}/run")
async def run_agent_install(
    target_id: str,
    payload: InstallRunPayload,
    request: Request,
    confirm: str | None = None,
) -> dict[str, Any]:
    """Start one confirmed install. Returns the job to poll."""

    require_loopback_admin(request)
    require_destructive_admin_confirmation(
        request, confirm, confirmation_for(target_id)
    )

    def run() -> dict[str, Any]:
        plan = agent_installer.plan_for(target_id, payload.method, os.environ)
        if plan.token != payload.plan_token:
            raise InstallError(
                "The plan changed since it was shown (something on this machine "
                "moved). Press Install again to read the new plan.",
                status=409,
            )
        job = agent_installer.start_job(plan, dry_run=payload.dry_run)
        return {"job_id": job.id, "log_path": str(job.log_path)}

    try:
        return await asyncio.to_thread(run)
    except InstallError as exc:
        raise _http(exc) from exc


@router.get("/admin/api/agent-installs/jobs/{job_id}")
async def agent_install_job(
    job_id: str, request: Request, cursor: int = 0
) -> dict[str, Any]:
    """Return a job's stage, the lines since ``cursor`` and, once over, its result."""

    require_loopback_admin(request)
    try:
        return agent_installer.job(job_id).payload(max(cursor, 0))
    except InstallError as exc:
        raise _http(exc) from exc


@router.post("/admin/api/agent-installs/jobs/{job_id}/cancel")
async def cancel_agent_install(job_id: str, request: Request) -> dict[str, Any]:
    """Stop a run: the child process it started and that child's own tree."""

    require_loopback_admin(request)
    try:
        found = await asyncio.to_thread(agent_installer.cancel_job, job_id)
    except InstallError as exc:
        raise _http(exc) from exc
    return found.payload()
