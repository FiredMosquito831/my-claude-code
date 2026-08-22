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
