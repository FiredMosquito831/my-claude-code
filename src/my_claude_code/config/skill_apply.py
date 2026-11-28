"""Safe, hash-tracked skill lifecycle manager for the bundled ``mcc-media`` skill.

Pure ``config`` leaf module (stdlib + ``config`` only): resolves the bundled
``mcc-media`` skill files (wheel ``skills_bundle/`` or source ``skills/``),
maps every supported coding-agent global skill directory in ``SKILL_TARGETS``,
computes ``SkillState`` from SHA-256 digests + ``SkillRecord``, previews plans
without touching disk, and applies/updates/removes/reverts atomically with
foreign-directory adoption and drift protection.
"""

import contextlib
import hashlib
import importlib.metadata
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from my_claude_code.config.restore_record import (
    SkillRecord,
    forget_skill_record,
    read_all_skill_records,
    read_skill_record,
    write_skill_record,
)

MCC_SKILL_NAME = "mcc-media"

#: Curated files inside ``skills/mcc-media/`` and ``skills_bundle/mcc-media/``.
#: Tests compare this list against ``pyproject.toml`` ``force-include``.
SKILL_BUNDLE_FILES: tuple[str, ...] = (
    "SKILL.md",
    "scripts/_mcc_env.py",
    "scripts/mcc_media.py",
    "references/models-and-capabilities.md",
    "references/endpoints-and-payloads.md",
    "references/troubleshooting.md",
)

_BACKUP_DIR_PREFIX = f".mcc-backup-{MCC_SKILL_NAME}-"
_STAGING_DIR_PREFIX = f".mcc-tmp-{MCC_SKILL_NAME}-"
_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git"})
_SKIP_SUFFIXES = (".pyc", ".pyo")


class SkillApplyError(RuntimeError):
    """Refusal with an actionable message for plan/apply/remove/revert."""


class SkillState(StrEnum):
    """Live state of one skill target directory."""

    ABSENT = "absent"
    INSTALLED = "installed"
    UPDATE_AVAILABLE = "update_available"
    DRIFTED = "drifted"
    FOREIGN = "foreign"
    REVERTABLE = "revertable"


class SkillAction(StrEnum):
    """Supported lifecycle operations."""

    INSTALL = "install"
    UPDATE = "update"
    REMOVE = "remove"
    REVERT = "revert"


@dataclass(frozen=True, slots=True)
class SkillPathRung:
    """One candidate path rule for resolving a harness's global skills folder."""

    env_var: str | None = None
    parts: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    #: When ``env_var`` points to a file (e.g. ``CRUSH_GLOBAL_CONFIG=~/x/crush.json``),
    #: resolve relative to its parent if it has a file suffix.
    allow_file_parent: bool = False

    def applies_to(self, platform: str) -> bool:
        return not self.platforms or platform in self.platforms


@dataclass(frozen=True, slots=True)
class SkillTargetSpec:
    """One global skill installation target."""

    id: str
    display_name: str
    summary: str
    display_path: str
    rungs: tuple[SkillPathRung, ...]
    harness_ids: tuple[str, ...]
    shared_with: tuple[str, ...] = ()
    aliases_shared_agents: bool = False
    also_reads_shared_agents: bool = False


_SHARED_AGENTS_HARNESSES: tuple[str, ...] = (
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
    "antigravity",
)


SKILL_TARGETS: tuple[SkillTargetSpec, ...] = (
    SkillTargetSpec(
        id="shared_agents",
        display_name="Shared Agents (~/.agents/skills)",
        summary="Universal agentskills.io user directory scanned by multiple coding agents.",
        display_path=f"~/.agents/skills/{MCC_SKILL_NAME}",
        rungs=(SkillPathRung(parts=(".agents", "skills", MCC_SKILL_NAME)),),
        harness_ids=_SHARED_AGENTS_HARNESSES,
        shared_with=_SHARED_AGENTS_HARNESSES,
        aliases_shared_agents=False,
        also_reads_shared_agents=False,
    ),
    SkillTargetSpec(
        id="claude",
        display_name="Claude Code",
        summary="Claude Code's native user skills directory.",
        display_path=f"~/.claude/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="CLAUDE_CONFIG_DIR", parts=("skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(parts=(".claude", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("claude",),
        aliases_shared_agents=False,
        also_reads_shared_agents=False,
    ),
    SkillTargetSpec(
        id="codex",
        display_name="Codex CLI",
        summary="Codex CLI user skills directory (aliases shared ~/.agents/skills/ unless CODEX_HOME is set).",
        display_path=f"~/.agents/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(env_var="CODEX_HOME", parts=("skills", MCC_SKILL_NAME)),
            SkillPathRung(parts=(".agents", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("codex",),
        shared_with=_SHARED_AGENTS_HARNESSES,
        aliases_shared_agents=True,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="pi",
        display_name="Pi",
        summary="Pi's native user skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.pi/agent/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="PI_CODING_AGENT_DIR", parts=("skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(parts=(".pi", "agent", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("pi",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="opencode",
        display_name="OpenCode",
        summary="OpenCode's global skills directory (shared with OpenCode 2; also scans ~/.agents/skills/).",
        display_path=f"~/.config/opencode/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="OPENCODE_CONFIG_DIR", parts=("skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(
                env_var="XDG_CONFIG_HOME",
                parts=("opencode", "skills", MCC_SKILL_NAME),
            ),
            SkillPathRung(parts=(".config", "opencode", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("opencode", "opencode2"),
        shared_with=("opencode", "opencode2"),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="opencode2",
        display_name="OpenCode 2",
        summary="OpenCode 2 shares OpenCode's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.config/opencode/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="OPENCODE_CONFIG_DIR", parts=("skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(
                env_var="XDG_CONFIG_HOME",
                parts=("opencode", "skills", MCC_SKILL_NAME),
            ),
            SkillPathRung(parts=(".config", "opencode", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("opencode2",),
        shared_with=("opencode", "opencode2"),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="kilo",
        display_name="Kilo CLI",
        summary="Kilo CLI's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.config/kilo/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="XDG_CONFIG_HOME", parts=("kilo", "skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(parts=(".config", "kilo", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("kilo",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="crush",
        display_name="Crush",
        summary="Crush's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.config/crush/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="CRUSH_GLOBAL_CONFIG",
                parts=("skills", MCC_SKILL_NAME),
                allow_file_parent=True,
            ),
            SkillPathRung(
                env_var="LOCALAPPDATA",
                parts=("crush", "skills", MCC_SKILL_NAME),
                platforms=("win32",),
            ),
            SkillPathRung(
                env_var="XDG_CONFIG_HOME", parts=("crush", "skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(parts=(".config", "crush", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("crush",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="qwen_code",
        display_name="Qwen Code",
        summary="Qwen Code's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.qwen/skills/{MCC_SKILL_NAME}",
        rungs=(SkillPathRung(parts=(".qwen", "skills", MCC_SKILL_NAME)),),
        harness_ids=("qwen_code",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="gemini_cli",
        display_name="Gemini CLI",
        summary="Gemini CLI's user skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.gemini/skills/{MCC_SKILL_NAME}",
        rungs=(SkillPathRung(parts=(".gemini", "skills", MCC_SKILL_NAME)),),
        harness_ids=("gemini_cli",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="kimi_code",
        display_name="Kimi Code",
        summary="Kimi Code's user skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.kimi/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(env_var="KIMI_CODE_HOME", parts=("skills", MCC_SKILL_NAME)),
            SkillPathRung(parts=(".kimi", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("kimi_code",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="goose",
        display_name="Goose",
        summary="Goose's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.config/goose/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="APPDATA",
                parts=("Block", "goose", "config", "skills", MCC_SKILL_NAME),
                platforms=("win32",),
            ),
            SkillPathRung(
                env_var="XDG_CONFIG_HOME", parts=("goose", "skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(parts=(".config", "goose", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("goose",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="cline_cli",
        display_name="Cline CLI",
        summary="Cline's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.cline/skills/{MCC_SKILL_NAME}",
        rungs=(SkillPathRung(parts=(".cline", "skills", MCC_SKILL_NAME)),),
        harness_ids=("cline_cli",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="droid",
        display_name="Droid",
        summary="Factory Droid's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.factory/skills/{MCC_SKILL_NAME}",
        rungs=(SkillPathRung(parts=(".factory", "skills", MCC_SKILL_NAME)),),
        harness_ids=("droid",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="commandcode_cli",
        display_name="Command Code",
        summary="Command Code's global skills directory.",
        display_path=f"~/.commandcode/skills/{MCC_SKILL_NAME}",
        rungs=(SkillPathRung(parts=(".commandcode", "skills", MCC_SKILL_NAME)),),
        harness_ids=("commandcode_cli",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
    SkillTargetSpec(
        id="hermes",
        display_name="Hermes Agent",
        summary="Hermes Agent's $HERMES_HOME/skills directory.",
        display_path=f"~/.hermes/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(env_var="HERMES_HOME", parts=("skills", MCC_SKILL_NAME)),
            SkillPathRung(
                env_var="LOCALAPPDATA",
                parts=("hermes", "skills", MCC_SKILL_NAME),
                platforms=("win32",),
            ),
            SkillPathRung(
                parts=("AppData", "Local", "hermes", "skills", MCC_SKILL_NAME),
                platforms=("win32",),
            ),
            SkillPathRung(parts=(".hermes", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("hermes",),
        aliases_shared_agents=False,
        also_reads_shared_agents=False,
    ),
    SkillTargetSpec(
        id="openclaw",
        display_name="OpenClaw",
        summary="OpenClaw's state skills directory.",
        display_path=f"~/.openclaw/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(
                env_var="OPENCLAW_STATE_DIR", parts=("skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(
                env_var="OPENCLAW_HOME", parts=(".openclaw", "skills", MCC_SKILL_NAME)
            ),
            SkillPathRung(parts=(".openclaw", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("openclaw",),
        aliases_shared_agents=False,
        also_reads_shared_agents=False,
    ),
    SkillTargetSpec(
        id="antigravity",
        display_name="Antigravity",
        summary="Antigravity's global skills directory (also scans ~/.agents/skills/).",
        display_path=f"~/.gemini/antigravity/skills/{MCC_SKILL_NAME}",
        rungs=(
            SkillPathRung(parts=(".gemini", "antigravity", "skills", MCC_SKILL_NAME)),
        ),
        harness_ids=("antigravity",),
        aliases_shared_agents=False,
        also_reads_shared_agents=True,
    ),
)

_TARGETS_BY_ID: dict[str, SkillTargetSpec] = {t.id: t for t in SKILL_TARGETS}


def skill_targets() -> tuple[SkillTargetSpec, ...]:
    """Return every supported skill target in presentation order."""

    return SKILL_TARGETS


def skill_target(target_id: str) -> SkillTargetSpec:
    """Return one skill target by id, raising KeyError when unknown."""

    if target_id not in _TARGETS_BY_ID:
        known = ", ".join(t.id for t in SKILL_TARGETS)
        raise KeyError(f"unknown skill target: {target_id}. Known: {known}")
    return _TARGETS_BY_ID[target_id]


def _env_home(env: Mapping[str, str]) -> Path:
    for key in ("HOME", "USERPROFILE"):
        val = env.get(key, "").strip()
        if val:
            return Path(val).expanduser()
    return Path.home()


def resolve_skill_dir(
    spec: SkillTargetSpec,
    env: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path:
    """Resolve the destination ``mcc-media`` directory for ``spec``."""

    source_env = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    for rung in spec.rungs:
        if not rung.applies_to(plat):
            continue
        if rung.env_var is None:
            return _env_home(source_env).joinpath(*rung.parts).resolve()
        raw = source_env.get(rung.env_var, "").strip()
        if not raw:
            continue
        base = Path(raw).expanduser()
        if rung.allow_file_parent and base.suffix:
            base = base.parent
        return base.joinpath(*rung.parts).resolve()
    return _env_home(source_env).joinpath(*spec.rungs[-1].parts).resolve()


def bundled_skill_dir() -> Path:
    """Resolve the bundled ``mcc-media`` directory (wheel bundle first, then checkout)."""

    packaged = Path(__file__).resolve().parent.parent / "skills_bundle" / MCC_SKILL_NAME
    if (packaged / "SKILL.md").is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[3] / "skills" / MCC_SKILL_NAME
    if (checkout / "SKILL.md").is_file():
        return checkout
    raise SkillApplyError(
        f"Bundled {MCC_SKILL_NAME} skill files were not found in wheel or checkout."
    )


def bundled_skill_version() -> str:
    """Return the package version without importing ``core``."""

    try:
        return importlib.metadata.version("my-claude-code")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """Resolved skill source files and their SHA-256 digests."""

    name: str
    version: str
    root: Path
    files: Mapping[str, bytes]
    hashes: Mapping[str, str]

    @property
    def total_bytes(self) -> int:
        return sum(len(content) for content in self.files.values())


def _collect_dir_files(directory: Path) -> dict[str, bytes]:
    collected: dict[str, bytes] = {}
    if not directory.is_dir():
        return collected
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(directory)
        if any(part in _SKIP_DIR_NAMES for part in rel.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        collected[rel.as_posix()] = path.read_bytes()
    return collected


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_map(files: Mapping[str, bytes]) -> dict[str, str]:
    return {rel: _sha256_bytes(content) for rel, content in sorted(files.items())}


def load_bundled_skill(
    *, source_dir: Path | None = None, version: str | None = None
) -> SkillBundle:
    """Load the bundled ``mcc-media`` skill bytes and SHA-256 digests."""

    root = source_dir if source_dir is not None else bundled_skill_dir()
    files = _collect_dir_files(root)
    if "SKILL.md" not in files:
        raise SkillApplyError(f"Skill bundle at {root} is missing SKILL.md.")
    return SkillBundle(
        name=MCC_SKILL_NAME,
        version=version if version is not None else bundled_skill_version(),
        root=root,
        files=files,
        hashes=_hash_map(files),
    )


def _matching_record(
    target_id: str,
    install_dir: Path,
    *,
    record_path: Path | None = None,
) -> SkillRecord | None:
    direct = read_skill_record(target_id, path=record_path)
    if direct is not None:
        return direct
    normalized = str(install_dir.resolve())
    for rec in read_all_skill_records(path=record_path).values():
        if not rec.install_dir:
            continue
        try:
            if str(Path(rec.install_dir).resolve()) == normalized:
                return rec
        except OSError:
            continue
    return None


def _backup_exists(record: SkillRecord | None) -> bool:
    return bool(record and record.backup_dir and Path(record.backup_dir).exists())


@dataclass(frozen=True, slots=True)
class SkillProbe:
    """Live state of one skill target on disk."""

    target_id: str
    state: SkillState
    install_dir: str
    installed_version: str = ""
    backup_dir: str = ""
    revertable: bool = False
    update_available: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class SkillPlan:
    """Non-mutating preview of a skill action."""

    target_id: str
    action: str
    install_dir: str
    state: SkillState
    files_to_create: tuple[str, ...] = ()
    files_to_update: tuple[str, ...] = ()
    files_to_delete: tuple[str, ...] = ()
    backup_dir: str = ""
    no_op: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class SkillApplyResult:
    """Outcome of executing a skill lifecycle action."""

    target_id: str
    action: str
    changed: bool
    install_dir: str
    backup_dir: str = ""
    created_files: tuple[str, ...] = ()
    updated_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    restored_from_backup: bool = False


def probe_skill(
    spec: SkillTargetSpec,
    *,
    env: Mapping[str, str] | None = None,
    record_path: Path | None = None,
    source_dir: Path | None = None,
    bundle_version: str | None = None,
    platform: str | None = None,
) -> SkillProbe:
    """Inspect ``spec`` on disk and compare against ``SkillRecord`` & bundle."""

    install_dir = resolve_skill_dir(spec, env, platform=platform)
    record = _matching_record(spec.id, install_dir, record_path=record_path)
    has_backup = _backup_exists(record)
    backup_dir = record.backup_dir if has_backup and record is not None else ""
    bundle = load_bundled_skill(source_dir=source_dir, version=bundle_version)

    if not install_dir.exists():
        if has_backup:
            return SkillProbe(
                target_id=spec.id,
                state=SkillState.REVERTABLE,
                install_dir=str(install_dir),
                backup_dir=backup_dir,
                revertable=True,
                warning=(
                    "A pre-adoption backup exists and can be restored with revert."
                ),
            )
        return SkillProbe(
            target_id=spec.id,
            state=SkillState.ABSENT,
            install_dir=str(install_dir),
        )

    if not install_dir.is_dir():
        return SkillProbe(
            target_id=spec.id,
            state=SkillState.FOREIGN,
            install_dir=str(install_dir),
            warning=f"{install_dir} exists as a file, not a skill directory.",
        )

    if record is None:
        return SkillProbe(
            target_id=spec.id,
            state=SkillState.FOREIGN,
            install_dir=str(install_dir),
            warning=(
                f"{install_dir} already exists and was not installed by MCC "
                "(pass adopt=True / --adopt to back it up and adopt)."
            ),
        )

    disk_hashes = _hash_map(_collect_dir_files(install_dir))
    if disk_hashes != dict(record.file_hashes):
        return SkillProbe(
            target_id=spec.id,
            state=SkillState.DRIFTED,
            install_dir=str(install_dir),
            installed_version=record.version,
            backup_dir=backup_dir,
            revertable=has_backup,
            warning=(
                f"Files in {install_dir} were modified since MCC installed "
                f"v{record.version} (pass force=True / --force to overwrite or remove)."
            ),
        )

    if disk_hashes != dict(bundle.hashes) or record.version != bundle.version:
        return SkillProbe(
            target_id=spec.id,
            state=SkillState.UPDATE_AVAILABLE,
            install_dir=str(install_dir),
            installed_version=record.version,
            backup_dir=backup_dir,
            revertable=has_backup,
            update_available=True,
        )

    return SkillProbe(
        target_id=spec.id,
        state=SkillState.INSTALLED,
        install_dir=str(install_dir),
        installed_version=record.version,
        backup_dir=backup_dir,
        revertable=has_backup,
    )


def _new_backup_path(install_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = install_dir.parent / f"{_BACKUP_DIR_PREFIX}{stamp}"
    index = 1
    while candidate.exists():
        candidate = install_dir.parent / f"{_BACKUP_DIR_PREFIX}{stamp}-{index}"
        index += 1
    return candidate


def _validate_action(action: str) -> SkillAction:
    try:
        return SkillAction(action)
    except ValueError as exc:
        raise SkillApplyError(
            f"Unsupported skill action {action!r}; expected install, update, remove, or revert."
        ) from exc


def plan_skill(
    spec: SkillTargetSpec,
    action: str,
    *,
    force: bool = False,
    adopt: bool = False,
    env: Mapping[str, str] | None = None,
    record_path: Path | None = None,
    source_dir: Path | None = None,
    bundle_version: str | None = None,
    platform: str | None = None,
) -> SkillPlan:
    """Compute what ``apply_skill`` would change without touching disk."""

    act = _validate_action(action)
    probe = probe_skill(
        spec,
        env=env,
        record_path=record_path,
        source_dir=source_dir,
        bundle_version=bundle_version,
        platform=platform,
    )
    install_dir = Path(probe.install_dir)
    bundle = load_bundled_skill(source_dir=source_dir, version=bundle_version)
    record = _matching_record(spec.id, install_dir, record_path=record_path)

    if act in (SkillAction.INSTALL, SkillAction.UPDATE):
        if act is SkillAction.UPDATE and probe.state in (
            SkillState.ABSENT,
            SkillState.REVERTABLE,
        ):
            raise SkillApplyError(
                f"{MCC_SKILL_NAME} is not installed at {install_dir}; run install first."
            )
        if probe.state is SkillState.FOREIGN and not adopt:
            raise SkillApplyError(
                f"{install_dir} already exists and was not installed by MCC. "
                "Re-run with adopt=True (--adopt) to back it up before installing."
            )
        if probe.state is SkillState.DRIFTED and not force:
            raise SkillApplyError(
                f"{install_dir} has local modifications since MCC installed it. "
                "Re-run with force=True (--force) to back up and overwrite."
            )
        if probe.state is SkillState.INSTALLED and act in (
            SkillAction.INSTALL,
            SkillAction.UPDATE,
        ):
            return SkillPlan(
                target_id=spec.id,
                action=act.value,
                install_dir=probe.install_dir,
                state=probe.state,
                backup_dir=probe.backup_dir,
                no_op=True,
            )

        existing = (
            _collect_dir_files(install_dir)
            if install_dir.is_dir() and probe.state is not SkillState.FOREIGN
            else {}
        )
        to_create = tuple(sorted(k for k in bundle.files if k not in existing))
        to_update = tuple(
            sorted(
                k
                for k in bundle.files
                if k in existing and existing[k] != bundle.files[k]
            )
        )
        to_delete = tuple(sorted(k for k in existing if k not in bundle.files))
        planned_backup = probe.backup_dir
        warning = ""
        if probe.state is SkillState.FOREIGN and adopt:
            planned_backup = str(_new_backup_path(install_dir))
            warning = f"Existing folder will be backed up to {planned_backup}."
        elif probe.state is SkillState.DRIFTED and force and not planned_backup:
            planned_backup = str(_new_backup_path(install_dir))
            warning = f"Modified folder will be backed up to {planned_backup}."
        return SkillPlan(
            target_id=spec.id,
            action=act.value,
            install_dir=probe.install_dir,
            state=probe.state,
            files_to_create=to_create,
            files_to_update=to_update,
            files_to_delete=to_delete,
            backup_dir=planned_backup,
            no_op=not (to_create or to_update or to_delete)
            and probe.state is not SkillState.UPDATE_AVAILABLE,
            warning=warning,
        )

    if act is SkillAction.REMOVE:
        if probe.state in (SkillState.ABSENT, SkillState.REVERTABLE):
            return SkillPlan(
                target_id=spec.id,
                action=act.value,
                install_dir=probe.install_dir,
                state=probe.state,
                backup_dir=probe.backup_dir,
                no_op=True,
                warning="Nothing installed by MCC to remove.",
            )
        if probe.state is SkillState.FOREIGN:
            raise SkillApplyError(
                f"{install_dir} was not installed by MCC; refusing to remove."
            )
        if probe.state is SkillState.DRIFTED and not force:
            raise SkillApplyError(
                f"{install_dir} has local modifications. Re-run with force=True (--force) to remove."
            )
        existing = _collect_dir_files(install_dir)
        planned_backup = probe.backup_dir
        warning = ""
        if probe.state is SkillState.DRIFTED and force and not planned_backup:
            planned_backup = str(_new_backup_path(install_dir))
            warning = f"Modified folder will be backed up to {planned_backup}."
        return SkillPlan(
            target_id=spec.id,
            action=act.value,
            install_dir=probe.install_dir,
            state=probe.state,
            files_to_delete=tuple(sorted(existing)),
            backup_dir=planned_backup,
            no_op=False,
            warning=warning,
        )

    # SkillAction.REVERT
    if probe.state is SkillState.ABSENT:
        return SkillPlan(
            target_id=spec.id,
            action=act.value,
            install_dir=probe.install_dir,
            state=probe.state,
            no_op=True,
            warning="Nothing to revert.",
        )
    if probe.state is SkillState.FOREIGN:
        raise SkillApplyError(
            f"{install_dir} was not installed by MCC; refusing to revert."
        )
    if probe.state is SkillState.DRIFTED and not force:
        raise SkillApplyError(
            f"{install_dir} has local modifications. Re-run with force=True (--force) to revert."
        )
    existing = _collect_dir_files(install_dir) if install_dir.is_dir() else {}
    has_backup = _backup_exists(record)
    backup_files = (
        _collect_dir_files(Path(record.backup_dir))
        if has_backup and record is not None
        else {}
    )
    return SkillPlan(
        target_id=spec.id,
        action=act.value,
        install_dir=probe.install_dir,
        state=probe.state,
        files_to_create=tuple(sorted(backup_files)) if has_backup else (),
        files_to_delete=tuple(sorted(existing)),
        backup_dir=probe.backup_dir,
        no_op=False,
        warning=(
            f"Will restore original folder from {probe.backup_dir}."
            if has_backup
            else "No adoption backup; will remove MCC-installed skill files cleanly."
        ),
    )


def _missing_parent_dirs(install_dir: Path) -> tuple[str, ...]:
    """Return parent directories up to existing ancestor, deepest first."""

    created: list[Path] = []
    current = install_dir.parent
    while current != current.parent and not current.exists():
        created.append(current)
        current = current.parent
    return tuple(str(p) for p in created)


def _cleanup_empty_dirs(dirs: tuple[str, ...]) -> None:
    for raw in dirs:
        path = Path(raw)
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue


def _write_bundle_atomically(install_dir: Path, bundle: SkillBundle) -> None:
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ%f")
    staging = install_dir.parent / f"{_STAGING_DIR_PREFIX}{stamp}"
    old_swap = install_dir.parent / f"{_STAGING_DIR_PREFIX}old-{stamp}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
        for rel, data in bundle.files.items():
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        if install_dir.exists():
            os.replace(install_dir, old_swap)
        os.replace(staging, install_dir)
        if old_swap.exists():
            shutil.rmtree(old_swap, ignore_errors=True)
    except OSError:
        if not install_dir.exists() and old_swap.exists():
            with contextlib.suppress(OSError):
                os.replace(old_swap, install_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise


def apply_skill(
    spec: SkillTargetSpec,
    action: str,
    *,
    force: bool = False,
    adopt: bool = False,
    env: Mapping[str, str] | None = None,
    record_path: Path | None = None,
    source_dir: Path | None = None,
    bundle_version: str | None = None,
    platform: str | None = None,
) -> SkillApplyResult:
    """Execute ``install``, ``update``, ``remove``, or ``revert`` safely."""

    plan = plan_skill(
        spec,
        action,
        force=force,
        adopt=adopt,
        env=env,
        record_path=record_path,
        source_dir=source_dir,
        bundle_version=bundle_version,
        platform=platform,
    )
    if plan.no_op:
        return SkillApplyResult(
            target_id=spec.id,
            action=plan.action,
            changed=False,
            install_dir=plan.install_dir,
            backup_dir=plan.backup_dir,
        )

    install_dir = Path(plan.install_dir)
    record = _matching_record(spec.id, install_dir, record_path=record_path)
    record_owner_id = record.target_id if record is not None else spec.id
    act = SkillAction(plan.action)

    if act in (SkillAction.INSTALL, SkillAction.UPDATE):
        bundle = load_bundled_skill(source_dir=source_dir, version=bundle_version)
        created_dirs = (
            record.created_dirs
            if record is not None and record.created_dirs
            else _missing_parent_dirs(install_dir)
        )
        backup_dir = record.backup_dir if record is not None else ""
        if plan.backup_dir and plan.backup_dir != backup_dir and install_dir.exists():
            backup_path = Path(plan.backup_dir)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if plan.state is SkillState.FOREIGN:
                os.replace(install_dir, backup_path)
            else:
                shutil.copytree(install_dir, backup_path)
            backup_dir = str(backup_path)

        _write_bundle_atomically(install_dir, bundle)
        write_skill_record(
            SkillRecord(
                target_id=record_owner_id,
                install_dir=str(install_dir),
                skill_name=bundle.name,
                version=bundle.version,
                file_hashes=bundle.hashes,
                backup_dir=backup_dir,
                created_dirs=created_dirs,
            ),
            path=record_path,
        )
        return SkillApplyResult(
            target_id=spec.id,
            action=act.value,
            changed=True,
            install_dir=str(install_dir),
            backup_dir=backup_dir,
            created_files=plan.files_to_create,
            updated_files=plan.files_to_update,
            deleted_files=plan.files_to_delete,
        )

    if act is SkillAction.REMOVE:
        backup_dir = record.backup_dir if record is not None else ""
        if plan.backup_dir and plan.backup_dir != backup_dir and install_dir.exists():
            backup_path = Path(plan.backup_dir)
            shutil.copytree(install_dir, backup_path)
            backup_dir = str(backup_path)
        if install_dir.exists():
            shutil.rmtree(install_dir)
        if backup_dir and Path(backup_dir).is_dir() and record is not None:
            # Keep record with backup_dir so revert remains available.
            write_skill_record(
                SkillRecord(
                    target_id=record_owner_id,
                    install_dir=str(install_dir),
                    skill_name=record.skill_name,
                    version=record.version,
                    file_hashes=record.file_hashes,
                    backup_dir=backup_dir,
                    created_dirs=record.created_dirs,
                    installed_at=record.installed_at,
                ),
                path=record_path,
            )
        else:
            if record is not None and record.created_dirs:
                _cleanup_empty_dirs(record.created_dirs)
            forget_skill_record(record_owner_id, path=record_path)
        return SkillApplyResult(
            target_id=spec.id,
            action=act.value,
            changed=True,
            install_dir=str(install_dir),
            backup_dir=backup_dir,
            deleted_files=plan.files_to_delete,
        )

    # SkillAction.REVERT
    restored = False
    backup_path = Path(record.backup_dir) if record and record.backup_dir else None
    if install_dir.exists():
        shutil.rmtree(install_dir)
    if backup_path is not None and backup_path.is_dir():
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup_path, install_dir)
        restored = True
    elif record is not None and record.created_dirs:
        _cleanup_empty_dirs(record.created_dirs)
    forget_skill_record(record_owner_id, path=record_path)
    return SkillApplyResult(
        target_id=spec.id,
        action=act.value,
        changed=True,
        install_dir=str(install_dir),
        backup_dir=str(backup_path) if restored and backup_path is not None else "",
        created_files=plan.files_to_create,
        deleted_files=plan.files_to_delete,
        restored_from_backup=restored,
    )
