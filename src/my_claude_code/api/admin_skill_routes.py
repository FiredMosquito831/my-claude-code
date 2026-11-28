"""Admin API for managing the bundled ``mcc-media`` agent skill.

Three loopback-only routes matching the Desktop apps preview-then-apply pattern:

``GET  /admin/api/skills``                 bundled metadata & all target states
``POST /admin/api/skills/{target_id}/plan``   non-mutating change preview
``POST /admin/api/skills/{target_id}/apply``  execute install/update/remove/revert
"""

import asyncio
import os
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from my_claude_code.config.skill_apply import (
    SkillApplyError,
    SkillApplyResult,
    SkillPlan,
    SkillProbe,
    SkillState,
    SkillTargetSpec,
    apply_skill,
    load_bundled_skill,
    plan_skill,
    probe_skill,
    skill_target,
    skill_targets,
)

from .admin_routes import require_loopback_admin

router = APIRouter()


class SkillActionPayload(BaseModel):
    """Lifecycle action requested for one skill target."""

    action: Literal["install", "update", "remove", "revert"]
    force: bool = False
    adopt: bool = False


def _resolve_target(target_id: str) -> SkillTargetSpec:
    try:
        return skill_target(target_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _target_payload(spec: SkillTargetSpec, probe: SkillProbe) -> dict[str, Any]:
    return {
        "id": spec.id,
        "display_name": spec.display_name,
        "summary": spec.summary,
        "display_path": spec.display_path,
        "harness_ids": list(spec.harness_ids),
        "shared_with": list(spec.shared_with),
        "aliases_shared_agents": spec.aliases_shared_agents,
        "also_reads_shared_agents": spec.also_reads_shared_agents,
        "install_dir": probe.install_dir,
        "state": probe.state.value,
        "installed": probe.state
        in {
            SkillState.INSTALLED,
            SkillState.UPDATE_AVAILABLE,
            SkillState.DRIFTED,
        },
        "update_available": probe.update_available,
        "drifted": probe.state is SkillState.DRIFTED,
        "foreign": probe.state is SkillState.FOREIGN,
        "revertable": probe.revertable,
        "backup_dir": probe.backup_dir,
        "installed_version": probe.installed_version,
        "warning": probe.warning,
    }


def _plan_payload(plan: SkillPlan) -> dict[str, Any]:
    return {
        "target_id": plan.target_id,
        "action": plan.action,
        "install_dir": plan.install_dir,
        "state": plan.state.value,
        "files_to_create": list(plan.files_to_create),
        "files_to_update": list(plan.files_to_update),
        "files_to_delete": list(plan.files_to_delete),
        "backup_dir": plan.backup_dir,
        "no_op": plan.no_op,
        "warning": plan.warning,
    }


def _result_payload(
    spec: SkillTargetSpec,
    result: SkillApplyResult,
    probe: SkillProbe,
) -> dict[str, Any]:
    return {
        "target_id": result.target_id,
        "action": result.action,
        "changed": result.changed,
        "install_dir": result.install_dir,
        "backup_dir": result.backup_dir,
        "created_files": list(result.created_files),
        "updated_files": list(result.updated_files),
        "deleted_files": list(result.deleted_files),
        "restored_from_backup": result.restored_from_backup,
        "target": _target_payload(spec, probe),
    }


def _list_skills_payload() -> dict[str, Any]:
    bundle = load_bundled_skill()
    targets: list[dict[str, Any]] = []
    for spec in skill_targets():
        probe = probe_skill(spec, env=os.environ)
        targets.append(_target_payload(spec, probe))
    return {
        "skill": {
            "name": bundle.name,
            "version": bundle.version,
            "files": sorted(bundle.files),
            "total_bytes": bundle.total_bytes,
        },
        "targets": targets,
    }


@router.get("/admin/api/skills")
async def list_skills(request: Request):
    """Return bundled skill metadata and live state across all skill targets."""

    require_loopback_admin(request)
    try:
        return await asyncio.to_thread(_list_skills_payload)
    except SkillApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/api/skills/{target_id}/plan")
async def plan_skill_action(
    target_id: str,
    payload: SkillActionPayload,
    request: Request,
):
    """Preview what installing, updating, removing, or reverting a skill would do."""

    require_loopback_admin(request)
    spec = _resolve_target(target_id)

    def run() -> dict[str, Any]:
        plan = plan_skill(
            spec,
            payload.action,
            force=payload.force,
            adopt=payload.adopt,
            env=os.environ,
        )
        return _plan_payload(plan)

    try:
        return await asyncio.to_thread(run)
    except SkillApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/api/skills/{target_id}/apply")
async def apply_skill_action(
    target_id: str,
    payload: SkillActionPayload,
    request: Request,
):
    """Install, update, remove, or revert the bundled skill for one target."""

    require_loopback_admin(request)
    spec = _resolve_target(target_id)

    def run() -> dict[str, Any]:
        result = apply_skill(
            spec,
            payload.action,
            force=payload.force,
            adopt=payload.adopt,
            env=os.environ,
        )
        probe = probe_skill(spec, env=os.environ)
        return _result_payload(spec, result, probe)

    try:
        return await asyncio.to_thread(run)
    except SkillApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
