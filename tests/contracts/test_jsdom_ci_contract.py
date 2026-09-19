"""CI actually runs the dashboard's jsdom suite, and cannot skip it quietly.

``tests/api/test_admin_static_jsdom.py`` is the only thing in this repository
that executes ``admin.js``. It runs the real script in jsdom through a node
subprocess, and it skips itself when node or jsdom is missing -- which on a
GitHub runner meant node was found, jsdom was not, and all ~360 of its tests
reported SKIPPED inside a green ``pytest`` job. A suite that skips proves
nothing, and 7.16.1 shipped a dashboard page that lied to the server because
the jsdom fixture was missing an array.

So the workflow is asserted here rather than trusted:

* a dedicated job exists, and it installs Node and jsdom from a committed
  lockfile,
* it runs that file, and
* it sets ``MCC_CI=1``, which is what turns the file's two ``pytest.skip``
  calls into failures.

The last one is the load-bearing assertion. Everything else could be present
and the job could still go green having run nothing.
"""

import json
import pathlib

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "tests.yml"
_SUITE = "tests/api/test_admin_static_jsdom.py"
_JOB_ID = "jsdom"


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


def test_the_workflow_has_a_job_that_runs_the_jsdom_suite() -> None:
    runs = [str(step.get("run", "")) for step in _steps()]
    assert any(_SUITE in line for line in runs), (
        f"the `{_JOB_ID}` job never names {_SUITE}"
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

    running = [step for step in _steps() if _SUITE in str(step.get("run", ""))]
    assert running, f"no step runs {_SUITE}"
    for step in running:
        env = step.get("env") or {}
        assert str(env.get("MCC_CI", "")) == "1", (
            "the step that runs the jsdom suite must set MCC_CI=1, or a "
            "runner without jsdom skips every test in it and the job is green"
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
    assert f"--ignore={_SUITE}" in pytest_entry["run"]
