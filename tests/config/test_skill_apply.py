"""Lifecycle tests for ``config.skill_apply`` and ``SkillRecord`` persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from my_claude_code.config.restore_record import (
    McpServerRecord,
    read_mcp_record,
    read_skill_record,
    write_mcp_record,
)
from my_claude_code.config.skill_apply import (
    MCC_SKILL_NAME,
    SKILL_TARGETS,
    SkillApplyError,
    SkillState,
    apply_skill,
    plan_skill,
    probe_skill,
    resolve_skill_dir,
    skill_target,
)


def _env(home: Path, **extra: str) -> dict[str, str]:
    base = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    base.update(extra)
    return base


def _make_bundle(
    root: Path, skill_text: str = "---\nname: mcc-media\n---\nv1\n"
) -> Path:
    bundle = root / "mcc-media"
    (bundle / "scripts").mkdir(parents=True, exist_ok=True)
    (bundle / "references").mkdir(parents=True, exist_ok=True)
    (bundle / "SKILL.md").write_text(skill_text, encoding="utf-8", newline="\n")
    (bundle / "scripts" / "_mcc_env.py").write_text("X = 1\n", encoding="utf-8")
    (bundle / "scripts" / "mcc_media.py").write_text("Y = 2\n", encoding="utf-8")
    (bundle / "references" / "troubleshooting.md").write_text(
        "# Help\n", encoding="utf-8"
    )
    return bundle


def test_skill_targets_cover_shared_and_all_supported_harnesses(tmp_path: Path) -> None:
    ids = {t.id for t in SKILL_TARGETS}
    expected = {
        "shared_agents",
        "claude",
        "codex",
        "pi",
        "opencode",
        "opencode2",
        "kilo",
        "crush",
        "qwen_code",
        "gemini_cli",
        "kimi_code",
        "goose",
        "cline_cli",
        "droid",
        "commandcode_cli",
        "hermes",
        "openclaw",
        "antigravity",
    }
    assert expected <= ids
    assert skill_target("codex").aliases_shared_agents is True
    assert skill_target("claude").aliases_shared_agents is False
    assert "codex" in skill_target("shared_agents").shared_with

    home = tmp_path / "home"
    env = _env(home)
    assert (
        resolve_skill_dir(skill_target("shared_agents"), env)
        == (home / ".agents" / "skills" / MCC_SKILL_NAME).resolve()
    )
    assert (
        resolve_skill_dir(skill_target("codex"), env)
        == (home / ".agents" / "skills" / MCC_SKILL_NAME).resolve()
    )
    codex_env = _env(home, CODEX_HOME=str(tmp_path / "custom-codex"))
    assert (
        resolve_skill_dir(skill_target("codex"), codex_env)
        == (tmp_path / "custom-codex" / "skills" / MCC_SKILL_NAME).resolve()
    )
    hermes_win = _env(home)
    assert (
        resolve_skill_dir(skill_target("hermes"), hermes_win, platform="win32")
        == (home / "AppData" / "Local" / "hermes" / "skills" / MCC_SKILL_NAME).resolve()
    )
    assert (
        resolve_skill_dir(skill_target("hermes"), hermes_win, platform="linux")
        == (home / ".hermes" / "skills" / MCC_SKILL_NAME).resolve()
    )
    openclaw_env = _env(home, OPENCLAW_STATE_DIR=str(tmp_path / "claw-state"))
    assert (
        resolve_skill_dir(skill_target("openclaw"), openclaw_env)
        == (tmp_path / "claw-state" / "skills" / MCC_SKILL_NAME).resolve()
    )


def test_install_update_remove_and_record_persistence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _env(home)
    record = tmp_path / "mcc" / "restore-record.json"
    # Pre-seed an McpServerRecord to verify SkillRecord coexists cleanly.
    write_mcp_record(
        McpServerRecord(
            subject="codex_desktop",
            document_path="/tmp/config.toml",
            created=((("mcp_servers", "mcc_media"), {"command": "mcc-mcp"}),),
            splices=(),
        ),
        path=record,
    )
    src_v1 = _make_bundle(tmp_path / "src1", "---\nname: mcc-media\n---\nv1\n")
    spec = skill_target("claude")

    assert (
        probe_skill(
            spec,
            env=env,
            record_path=record,
            source_dir=src_v1,
            bundle_version="8.73.0",
        ).state
        is SkillState.ABSENT
    )
    plan = plan_skill(
        spec,
        "install",
        env=env,
        record_path=record,
        source_dir=src_v1,
        bundle_version="8.73.0",
    )
    assert plan.no_op is False
    assert "SKILL.md" in plan.files_to_create
    assert not resolve_skill_dir(spec, env).exists()

    res = apply_skill(
        spec,
        "install",
        env=env,
        record_path=record,
        source_dir=src_v1,
        bundle_version="8.73.0",
    )
    assert res.changed is True
    install_dir = Path(res.install_dir)
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8").endswith("v1\n")

    saved_rec = read_skill_record("claude", path=record)
    assert saved_rec is not None
    assert saved_rec.version == "8.73.0"
    assert len(saved_rec.file_hashes["SKILL.md"]) == 64
    assert read_mcp_record("codex_desktop", path=record) is not None

    # Idempotent re-install is a no-op.
    assert (
        plan_skill(
            spec,
            "install",
            env=env,
            record_path=record,
            source_dir=src_v1,
            bundle_version="8.73.0",
        ).no_op
        is True
    )

    # Update when bundle changes.
    src_v2 = _make_bundle(tmp_path / "src2", "---\nname: mcc-media\n---\nv2\n")
    probe_v2 = probe_skill(
        spec, env=env, record_path=record, source_dir=src_v2, bundle_version="8.74.0"
    )
    assert probe_v2.state is SkillState.UPDATE_AVAILABLE
    assert probe_v2.update_available is True

    upd_plan = plan_skill(
        spec,
        "update",
        env=env,
        record_path=record,
        source_dir=src_v2,
        bundle_version="8.74.0",
    )
    assert upd_plan.files_to_update == ("SKILL.md",)
    upd_res = apply_skill(
        spec,
        "update",
        env=env,
        record_path=record,
        source_dir=src_v2,
        bundle_version="8.74.0",
    )
    assert upd_res.changed is True
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8").endswith("v2\n")

    # Clean remove deletes files, empty created dirs, and forgets SkillRecord.
    rem_res = apply_skill(
        spec,
        "remove",
        env=env,
        record_path=record,
        source_dir=src_v2,
        bundle_version="8.74.0",
    )
    assert rem_res.changed is True
    assert not install_dir.exists()
    assert not install_dir.parent.exists()
    assert read_skill_record("claude", path=record) is None
    assert read_mcp_record("codex_desktop", path=record) is not None


def test_drifted_guard_and_force_backup(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _env(home)
    record = tmp_path / "mcc" / "restore-record.json"
    src = _make_bundle(tmp_path / "src")
    spec = skill_target("gemini_cli")

    apply_skill(
        spec,
        "install",
        env=env,
        record_path=record,
        source_dir=src,
        bundle_version="8.73.0",
    )
    install_dir = resolve_skill_dir(spec, env)
    (install_dir / "SKILL.md").write_text("user edited", encoding="utf-8")

    probe = probe_skill(
        spec, env=env, record_path=record, source_dir=src, bundle_version="8.73.0"
    )
    assert probe.state is SkillState.DRIFTED

    for action in ("install", "update", "remove", "revert"):
        with pytest.raises(SkillApplyError, match="force=True"):
            apply_skill(
                spec,
                action,
                env=env,
                record_path=record,
                source_dir=src,
                bundle_version="8.73.0",
            )

    forced = apply_skill(
        spec,
        "install",
        force=True,
        env=env,
        record_path=record,
        source_dir=src,
        bundle_version="8.73.0",
    )
    assert forced.changed is True
    assert (
        forced.backup_dir
        and (Path(forced.backup_dir) / "SKILL.md").read_text() == "user edited"
    )
    assert (install_dir / "SKILL.md").read_text(encoding="utf-8").endswith("v1\n")


def test_foreign_guard_adopt_and_byte_identical_revert(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = _env(home)
    record = tmp_path / "mcc" / "restore-record.json"
    src = _make_bundle(tmp_path / "src")
    spec = skill_target("shared_agents")
    install_dir = resolve_skill_dir(spec, env)
    install_dir.mkdir(parents=True)
    original_bytes = b"# my custom skill\nkeep every byte\n"
    (install_dir / "SKILL.md").write_bytes(original_bytes)
    (install_dir / "notes.txt").write_bytes(b"personal notes\n")

    assert (
        probe_skill(
            spec, env=env, record_path=record, source_dir=src, bundle_version="8.73.0"
        ).state
        is SkillState.FOREIGN
    )
    with pytest.raises(SkillApplyError, match="adopt=True"):
        apply_skill(
            spec,
            "install",
            env=env,
            record_path=record,
            source_dir=src,
            bundle_version="8.73.0",
        )
    with pytest.raises(SkillApplyError, match="not installed by MCC"):
        apply_skill(
            spec,
            "remove",
            env=env,
            record_path=record,
            source_dir=src,
            bundle_version="8.73.0",
        )

    adopted = apply_skill(
        spec,
        "install",
        adopt=True,
        env=env,
        record_path=record,
        source_dir=src,
        bundle_version="8.73.0",
    )
    assert adopted.changed is True
    assert adopted.backup_dir and Path(adopted.backup_dir).is_dir()

    probe_after = probe_skill(
        spec, env=env, record_path=record, source_dir=src, bundle_version="8.73.0"
    )
    assert probe_after.state is SkillState.INSTALLED
    assert probe_after.revertable is True
    # Alias target `codex` shares the same directory and sees it as INSTALLED, not FOREIGN.
    codex_probe = probe_skill(
        skill_target("codex"),
        env=env,
        record_path=record,
        source_dir=src,
        bundle_version="8.73.0",
    )
    assert codex_probe.state is SkillState.INSTALLED

    reverted = apply_skill(
        spec,
        "revert",
        env=env,
        record_path=record,
        source_dir=src,
        bundle_version="8.73.0",
    )
    assert reverted.changed is True
    assert reverted.restored_from_backup is True
    assert (install_dir / "SKILL.md").read_bytes() == original_bytes
    assert (install_dir / "notes.txt").read_bytes() == b"personal notes\n"
    assert not (install_dir / "scripts").exists()
    assert read_skill_record("shared_agents", path=record) is None
    assert json.loads(record.read_text(encoding="utf-8"))["skills"] == {}
