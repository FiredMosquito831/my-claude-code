"""Every curated document must be inside the built wheel.

This is the test that matters most for the Docs page, and the only one that
can catch its worst failure mode.

`docs_content._bundle_dir()` prefers `my_claude_code/docs_bundle/` -- which
exists only in a built wheel, because hatchling's `force-include` does not
run for an editable install -- and falls back to the repository checkout so
the page is not blank while developing. That fallback means a source checkout
renders all six documents perfectly whether or not `pyproject.toml` ships a
single one of them. Every other test in this suite would pass with the
`force-include` block deleted. Every real install would show an empty page.

So this one builds an actual wheel and looks inside it. It is slow on
purpose; the alternative is a defect that is invisible in development and
total in production.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from my_claude_code.api.docs_content import DOCUMENTS

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_PREFIX = "my_claude_code/docs_bundle/"


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> zipfile.ZipFile:
    """Build a real wheel. Opt-in, because it nests one uv inside another.

    This hung CI for the full 15-minute job limit: pytest already runs under
    ``uv run``, and a second ``uv`` contending for the same lock never
    returned. The log ended in ``Terminate orphan process: (uv)``, which is a
    hang wearing a failure's clothes.

    The drift this file exists to catch -- a document added to ``DOCUMENTS``
    without a matching ``force-include`` line -- is visible in
    ``pyproject.toml`` and does not need a build to see, so the tests below
    check the manifest and always run. This fixture stays for the stronger
    end-to-end check and runs when ``MCC_WHEEL_TESTS=1`` asks for it.
    """

    if os.environ.get("MCC_WHEEL_TESTS") != "1":
        pytest.skip("set MCC_WHEEL_TESTS=1 to build a real wheel (nested uv)")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    if not (REPO_ROOT / "pyproject.toml").is_file():
        pytest.skip("not running from a source checkout")

    out_dir = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(f"uv build --wheel failed:\n{result.stderr[-3000:]}")

    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return zipfile.ZipFile(wheels[0])


def test_the_wheel_contains_a_docs_bundle(built_wheel) -> None:
    """A silent naming change would make the assertion below vacuous."""

    bundled = [n for n in built_wheel.namelist() if n.startswith(BUNDLE_PREFIX)]
    assert bundled, (
        f"the built wheel has nothing under {BUNDLE_PREFIX!r}. The Docs page "
        "will be empty for every installed user while working perfectly from "
        "a source checkout. Check [tool.hatch.build.targets.wheel.force-include] "
        "in pyproject.toml."
    )


def test_every_curated_document_is_inside_the_built_wheel(built_wheel) -> None:
    names = set(built_wheel.namelist())

    missing = sorted(
        document.repo_path
        for document in DOCUMENTS
        if f"{BUNDLE_PREFIX}{document.bundled_name}" not in names
    )
    assert not missing, (
        f"these curated documents are not in the wheel: {missing}. Add a "
        "force-include entry for each in pyproject.toml -- a document that is "
        "not in the wheel is a page that is empty for every installed user."
    )


def test_the_bundled_documents_are_not_empty(built_wheel) -> None:
    """A zero-byte entry satisfies a name check and renders to nothing."""

    for document in DOCUMENTS:
        info = built_wheel.getinfo(f"{BUNDLE_PREFIX}{document.bundled_name}")
        assert info.file_size > 0, document.repo_path


def test_the_bundled_names_are_unique(built_wheel) -> None:
    """The bundle is flat: two documents with the same basename would
    silently overwrite each other and one page would show the other's text.
    """

    names = [document.bundled_name for document in DOCUMENTS]
    assert len(names) == len(set(names)), sorted(names)


def test_developer_only_documents_are_not_shipped(built_wheel) -> None:
    """The list is curated to what someone *running* MCC needs. Agent specs,
    the release checklist and the ADRs are written for whoever builds it.
    """

    bundled = {
        n[len(BUNDLE_PREFIX) :]
        for n in built_wheel.namelist()
        if n.startswith(BUNDLE_PREFIX)
    }
    for unwanted in (
        "RELEASE-CHECKLIST.md",
        "BRAND.md",
        "AGENTS.md",
        "CLAUDE.md",
    ):
        assert unwanted not in bundled, unwanted
    assert not any(name.startswith("AGENT_SPEC_") for name in bundled), bundled


# --------------------------------------------------------------------------
# The always-on half: the same drift, read from the packaging manifest.
# A document added to DOCUMENTS without a force-include line is the realistic
# regression, and it is visible here without building anything.
# --------------------------------------------------------------------------


def _force_include() -> dict[str, str]:
    """The wheel's force-include table, parsed from pyproject.toml."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.hatch.build.targets.wheel.force-include]", 1)
    assert len(section) == 2, "force-include section is missing from pyproject"
    body = section[1].split("\n[", 1)[0]
    mapping: dict[str, str] = {}
    for line in body.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        source, _, destination = line.partition("=")
        mapping[source.strip().strip('"')] = destination.strip().strip('"')
    return mapping


def test_every_curated_document_is_force_included() -> None:
    mapping = _force_include()
    missing = [
        document.repo_path
        for document in DOCUMENTS
        if document.repo_path not in mapping
    ]
    assert not missing, (
        f"curated but not shipped in the wheel: {missing}. Every entry in "
        "DOCUMENTS needs a [tool.hatch.build.targets.wheel.force-include] "
        "line, or the Docs page is empty for every installed user while "
        "working perfectly from a source checkout."
    )


def test_every_force_included_document_exists_on_disk() -> None:
    for source in _force_include():
        if not source.endswith(".md"):
            continue
        assert (REPO_ROOT / source).is_file(), (
            f"{source} is force-included but does not exist; the wheel build "
            "would fail or ship an empty file."
        )


def test_documents_land_under_the_bundle_prefix() -> None:
    mapping = _force_include()
    for document in DOCUMENTS:
        destination = mapping[document.repo_path]
        assert destination.startswith(BUNDLE_PREFIX.rstrip("/")), (
            f"{document.repo_path} maps to {destination}, which the loader will "
            f"not find; it reads {BUNDLE_PREFIX}."
        )


def test_developer_only_documents_are_not_force_included() -> None:
    shipped = set(_force_include())
    for internal in (
        "docs/RELEASE-CHECKLIST.md",
        "docs/BRAND.md",
        "docs/adr/0001-desktop-server-deployment-model.md",
        "smoke/README.md",
    ):
        assert internal not in shipped, (
            f"{internal} is written for whoever builds MCC, not whoever runs "
            "it, and should not be on the Docs page."
        )
