"""CI actually runs the dashboard's jsdom suites, and cannot skip them quietly.

``tests/api/test_admin_static_jsdom.py`` and
``tests/contracts/test_admin_static_xss.py`` are the only things in this
repository that execute ``admin.js``. Each runs the real script in jsdom
through a node subprocess, and each skips itself when node or jsdom is
missing -- which on a GitHub runner meant node was found, jsdom was not, and
all ~360 dashboard tests reported SKIPPED inside a green ``pytest`` job. The
five XSS tests did the same there until 2026-10-09, running only on a Windows
machine with jsdom installed. A suite that skips proves nothing, and 7.16.1
shipped a dashboard page that lied to the server because the jsdom fixture was
missing an array.

So the workflow is asserted here rather than trusted:

* a dedicated job exists, and it installs Node and jsdom from a committed
  lockfile,
* it runs both files, and every test module that drives a jsdom harness is
  one of them,
* it sets ``MCC_CI=1``, which is what turns each file's missing-node and
  missing-jsdom skips into failures, and each file reads it.

The last one is the load-bearing assertion. Everything else could be present
and the job could still go green having run nothing.
"""

import json
import os
import pathlib
import re

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "tests.yml"
_SUITES = (
    "tests/api/test_admin_static_jsdom.py",
    "tests/contracts/test_admin_static_xss.py",
)
_JOB_ID = "jsdom"
_IMPORTS_JSDOM = re.compile(r"""from\s+["']jsdom["']""")
_NOT_SOURCE = {"node_modules", ".venv", "__pycache__"}


def _workflow() -> dict:
    loaded = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jsdom_job() -> dict:
    jobs = _workflow()["jobs"]
    assert _JOB_ID in jobs, (
        f"{_WORKFLOW.name} has no `{_JOB_ID}` job. The dashboard's JavaScript "
        "is then executed by nothing on CI."
    )
    job = jobs[_JOB_ID]
    assert isinstance(job, dict)
    return job


def _steps() -> list[dict]:
    return [step for step in _jsdom_job()["steps"] if isinstance(step, dict)]


def _source_files(suffix: str) -> list[pathlib.Path]:
    found = []
    for root, dirs, files in os.walk(_REPO_ROOT / "tests"):
        dirs[:] = [name for name in dirs if name not in _NOT_SOURCE]
        found.extend(
            pathlib.Path(root, name) for name in files if name.endswith(suffix)
        )
    return found


def test_the_workflow_has_a_job_that_runs_the_jsdom_suite() -> None:
    runs = [str(step.get("run", "")) for step in _steps()]
    for suite in _SUITES:
        assert any(suite in line for line in runs), (
            f"the `{_JOB_ID}` job never names {suite}"
        )


def test_every_module_that_drives_a_jsdom_harness_runs_in_the_job() -> None:
    """A jsdom-driven module outside the job skips on Linux CI, unseen.

    That is how the five XSS tests ran only on a Windows machine for months:
    the dashboard suite had its own job, the XSS file did not. A module counts
    when it names a ``.mjs`` harness under tests/ that imports jsdom.
    """

    harnesses = {
        path.name
        for path in _source_files(".mjs")
        if _IMPORTS_JSDOM.search(path.read_text(encoding="utf-8"))
    }
    assert harnesses, "found no jsdom harness under tests/; the scan is vacuous"
    driving = {
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _source_files(".py")
        if path.name.startswith("test_")
        and any(name in path.read_text(encoding="utf-8") for name in harnesses)
    }
    assert driving == set(_SUITES), (
        f"test modules driving a jsdom harness: {sorted(driving)}; the "
        f"`{_JOB_ID}` job runs {sorted(_SUITES)}. Add the new module to the "
        "job's pytest line and to _SUITES, with an MCC_CI=1 skip-or-fail."
    )


def test_each_suite_reads_the_switch_that_fails_a_skip() -> None:
    """A file that never reads MCC_CI still skips with MCC_CI=1 set."""

    for suite in _SUITES:
        source = (_REPO_ROOT / suite).read_text(encoding="utf-8")
        assert "MCC_CI" in source, (
            f"{suite} never reads MCC_CI, so the job's MCC_CI=1 cannot turn "
            "its missing-node / missing-jsdom skips into failures"
        )


def test_the_job_runs_on_every_pull_request_and_on_main() -> None:
    # `on` is the YAML 1.1 boolean True once parsed, which is why this reads
    # both spellings rather than assuming one.
    triggers = _workflow().get("on", _workflow().get(True))
    assert isinstance(triggers, dict)
    assert "pull_request" in triggers
    assert "main" in triggers["push"]["branches"]
    # The job is unconditional: an `if:` here would be a way to skip it.
    assert "if" not in _jsdom_job()


def test_a_missing_node_or_jsdom_fails_the_job_instead_of_skipping_it() -> None:
    """MCC_CI=1 is the switch; without it the job can pass having run zero."""

    for suite in _SUITES:
        running = [step for step in _steps() if suite in str(step.get("run", ""))]
        assert running, f"no step runs {suite}"
        for step in running:
            env = step.get("env") or {}
            assert str(env.get("MCC_CI", "")) == "1", (
                f"the step that runs {suite} must set MCC_CI=1, or a runner "
                "without jsdom skips every jsdom test in it and the job is green"
            )


def test_the_job_installs_node_and_jsdom_from_the_committed_lockfile() -> None:
    steps = _steps()
    assert any("actions/setup-node@" in str(step.get("uses", "")) for step in steps), (
        "nothing installs Node, so the harness's `node` is whatever the image "
        "happens to ship"
    )
    assert any("npm ci" in str(step.get("run", "")) for step in steps), (
        "`npm install` would resolve jsdom afresh; `npm ci` installs the pin"
    )


def test_the_pin_is_exact_and_the_lockfile_agrees_with_it() -> None:
    manifest = json.loads(
        (_REPO_ROOT / "tests" / "package.json").read_text(encoding="utf-8")
    )
    pinned = manifest["dependencies"]["jsdom"]
    assert pinned[0].isdigit(), (
        f"jsdom is pinned as {pinned!r}; a range makes the one suite that "
        "runs admin.js reproduce differently on two machines"
    )

    lock = json.loads(
        (_REPO_ROOT / "tests" / "package-lock.json").read_text(encoding="utf-8")
    )
    assert lock["packages"]["node_modules/jsdom"]["version"] == pinned


def test_the_ordinary_pytest_job_does_not_pretend_to_run_it() -> None:
    """Excluded there precisely because it would skip, silently, forever."""

    matrix = _workflow()["jobs"]["quality"]["strategy"]["matrix"]["include"]
    pytest_entry = next(entry for entry in matrix if entry["id"] == "pytest")
    for suite in _SUITES:
        assert f"--ignore={suite}" in pytest_entry["run"]
