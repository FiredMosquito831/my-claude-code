"""Contract & wheel packaging tests for the bundled ``mcc-media`` skill."""

import ast
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from my_claude_code.config.skill_apply import (
    MCC_SKILL_NAME,
    SKILL_BUNDLE_FILES,
    load_bundled_skill,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / MCC_SKILL_NAME
BUNDLE_PREFIX = f"my_claude_code/skills_bundle/{MCC_SKILL_NAME}/"


def _force_include() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"])


def test_skill_md_line_limit_and_yaml_frontmatter() -> None:
    skill_md = SKILL_DIR / "SKILL.md"
    raw = skill_md.read_bytes()
    assert b"\r\n" not in raw, "SKILL.md must use LF line endings"
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert len(lines) < 250, f"SKILL.md has {len(lines)} lines; limit is < 250"
    assert lines[0] == "---", "SKILL.md must begin with YAML frontmatter"
    closing = lines.index("---", 1)
    frontmatter = yaml.safe_load("\n".join(lines[1:closing]))
    assert isinstance(frontmatter, dict)
    assert frontmatter["name"] == MCC_SKILL_NAME
    desc = frontmatter.get("description", "")
    assert isinstance(desc, str) and 1 <= len(desc) <= 1024
    assert frontmatter.get("license")
    assert isinstance(frontmatter.get("metadata"), dict)
    assert frontmatter["metadata"].get("version")


def test_skill_files_have_no_crlf_and_references_exist() -> None:
    for rel in SKILL_BUNDLE_FILES:
        path = REPO_ROOT / "skills" / MCC_SKILL_NAME / rel
        assert path.is_file(), f"Missing skill file: {path}"
        raw = path.read_bytes()
        assert len(raw) > 0, f"Empty skill file: {path}"
        assert b"\r\n" not in raw, f"CRLF found in {path}"

    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for ref in (
        "references/models-and-capabilities.md",
        "references/endpoints-and-payloads.md",
        "references/troubleshooting.md",
    ):
        assert ref in skill_text, f"SKILL.md does not link {ref}"


def test_skill_scripts_use_pure_python_stdlib_only() -> None:
    scripts_dir = SKILL_DIR / "scripts"
    sibling_modules = {p.stem for p in scripts_dir.glob("*.py")}
    allowed = set(sys.stdlib_module_names) | sibling_modules | {"__future__"}

    for script in sorted(scripts_dir.glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    assert top in allowed, (
                        f"{script.name}:{node.lineno} imports non-stdlib module {top!r}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".", 1)[0]
                assert top in allowed, (
                    f"{script.name}:{node.lineno} imports non-stdlib module {top!r}"
                )


def test_every_skill_file_is_force_included_in_pyproject() -> None:
    mapping = _force_include()
    for rel in SKILL_BUNDLE_FILES:
        src = f"skills/{MCC_SKILL_NAME}/{rel}"
        dst = f"{BUNDLE_PREFIX}{rel}"
        assert mapping.get(src) == dst, (
            f"{src} must be mapped to {dst} in pyproject.toml force-include"
        )


def test_load_bundled_skill_matches_disk() -> None:
    bundle = load_bundled_skill()
    assert bundle.name == MCC_SKILL_NAME
    assert set(bundle.files) == set(SKILL_BUNDLE_FILES)
    assert bundle.total_bytes > 0


@pytest.mark.spawns_process
@pytest.mark.local_serial
def test_real_built_wheel_contains_all_mcc_media_skill_files(tmp_path: Path) -> None:
    if os.environ.get("MCC_WHEEL_TESTS") != "1":
        pytest.skip("set MCC_WHEEL_TESTS=1 to build a real wheel")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")

    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(f"uv build --wheel failed:\n{result.stderr[-3000:]}")
    wheels = sorted(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
        for rel in SKILL_BUNDLE_FILES:
            entry = f"{BUNDLE_PREFIX}{rel}"
            assert entry in names, f"{entry} missing from built wheel"
            assert zf.read(entry) == (SKILL_DIR / rel).read_bytes()
        assert not any("__pycache__" in n for n in names if n.startswith(BUNDLE_PREFIX))
