"""The official installers must be inside the built wheel.

6.82.0 made the update path BE the install command: the dashboard's Update
button writes a short helper that waits for the server to exit and then runs
``scripts/install.ps1 -Restart`` (or ``scripts/install.sh --restart``) from the
copy that shipped inside the wheel. If that copy is not there, the helper has
nothing to run and every update on every installed machine fails with "the
installer that ships with this version was not found".

``_bundled_installer`` falls back to nothing rather than to the repository, so
a source checkout cannot hide the mistake -- but the *manifest* can, and the
manifest is what this file reads. The wheel build itself is opt-in for the same
reason as ``tests/api/test_docs_bundle_wheel.py``: nesting one ``uv`` inside
another hung CI for a full job limit once already.
"""

import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from my_claude_code.application import release_updates

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_PREFIX = "my_claude_code/installers/"
INSTALLERS = ("install.ps1", "install.sh")


def _force_include() -> dict[str, str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def test_both_installers_are_force_included_under_the_package() -> None:
    """The manifest, which is where the drift would be."""

    mapping = _force_include()
    for name in INSTALLERS:
        assert mapping.get(f"scripts/{name}") == f"{BUNDLE_PREFIX}{name}", (
            f"scripts/{name} is not force-included into the wheel. Without it "
            "the dashboard's Update button has no installer to run."
        )


def test_the_helper_looks_for_them_where_the_manifest_puts_them() -> None:
    """The two halves have to agree on one directory name.

    ``_bundled_installer`` resolves ``<package>/installers/<name>``; the
    manifest writes ``my_claude_code/installers/<name>``. A rename on either
    side is invisible until an update fails on a user's machine.
    """

    package_root = Path(release_updates.__file__).resolve().parent.parent
    for name in INSTALLERS:
        expected = package_root / "installers" / name
        found = release_updates._bundled_installer(name)
        # In a source checkout nothing is there and the answer is None; in an
        # installed one it is exactly this path.
        assert found in (None, expected)
        if found is not None:
            assert found.is_file()


def test_an_installer_that_is_not_there_is_reported_not_guessed_at() -> None:
    assert release_updates._bundled_installer("install.bat") is None


@pytest.mark.spawns_process
def test_both_installers_are_inside_a_really_built_wheel(tmp_path) -> None:
    """The end-to-end check. Opt-in: it nests one uv inside another."""

    if os.environ.get("MCC_WHEEL_TESTS") != "1":
        pytest.skip("set MCC_WHEEL_TESTS=1 to build a real wheel (nested uv)")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    if not (REPO_ROOT / "pyproject.toml").is_file():
        pytest.skip("not running from a source checkout")

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
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    for name in INSTALLERS:
        assert f"{BUNDLE_PREFIX}{name}" in names, (
            f"{name} is missing from the built wheel; every update on every "
            "installed machine would fail."
        )
