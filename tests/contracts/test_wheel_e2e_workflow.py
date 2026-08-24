"""The wheel e2e workflow must stay armed and stay out of the merge path.

`tests/api/test_docs_bundle_wheel.py` builds a real wheel only behind
``MCC_WHEEL_TESTS=1``, and ordinary CI must never set that flag: a nested uv
build inside ``uv run pytest`` hung CI until the fixture was gated. These
tests pin the one workflow allowed to set the flag to exactly that contract
-- weekly, dispatch-only (never push/pull_request, so it cannot gate
merges), on Windows, SHA-pinned to the same actions the main CI uses, and
pointed at nothing but the wheel test file. Without them the workflow can
silently rot back into decoration: a drifted trigger, a dropped env var or a
desynced action pin all look green from every other test in the suite.
"""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/wheel-e2e.yml")
CI_WORKFLOW = Path(".github/workflows/tests.yml")
WHEEL_TEST_FILE = "tests/api/test_docs_bundle_wheel.py"
EXPECTED_NAME = "Wheel E2E"


def _raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _parsed() -> dict:
    """safe_load the workflow; note YAML resolves the bare ``on:`` key to True."""
    document = yaml.safe_load(_raw())
    assert isinstance(document, dict), "wheel-e2e.yml must be a YAML mapping"
    return document


def _triggers() -> dict:
    document = _parsed()
    assert True in document, (
        "no `on:` block found (bare `on:` parses as the boolean True key); "
        "a workflow without triggers never runs"
    )
    return document[True]


def _job() -> dict:
    jobs = _parsed()["jobs"]
    assert isinstance(jobs, dict), f"expected a jobs mapping, got {type(jobs)}"
    assert len(jobs) == 1, f"expected one job, got {sorted(map(str, jobs))}"
    job = next(iter(jobs.values()))
    assert isinstance(job, dict), f"expected a job mapping, got {type(job)}"
    return job


def _pinned_sha(text: str, action: str, source: str) -> str:
    match = re.search(rf"uses:\s*{re.escape(action)}@([0-9a-f]{{40}})", text)
    assert match is not None, f"{action} in {source} must be SHA-pinned (40 hex)"
    return match.group(1)


def test_workflow_file_parses_as_yaml_and_pins_its_name() -> None:
    document = _parsed()
    assert document["name"] == EXPECTED_NAME, document.get("name")


def test_triggers_are_exactly_workflow_dispatch_and_schedule() -> None:
    triggers = _triggers()
    # Exact set: adding push/pull_request/merge_group here would put a slow
    # nested-uv build on the merge path, the exact regression the gate exists
    # to prevent.
    assert set(triggers) == {"workflow_dispatch", "schedule"}, sorted(triggers)


def test_schedule_is_weekly_at_an_off_minute() -> None:
    schedule = _triggers()["schedule"]
    assert isinstance(schedule, list) and len(schedule) == 1, schedule
    cron = schedule[0]["cron"]
    fields = cron.split()
    assert len(fields) == 5, f"cron must have five fields, got {cron!r}"
    minute, _hour, dom, month, weekday = fields
    assert dom == month == "*", f"cron must leave the date unconstrained: {cron!r}"
    assert weekday != "*", f"cron must pin one weekday to fire weekly: {cron!r}"
    assert minute not in {"0", "30"}, (
        f"cron must use an off-minute (got :{minute}); scheduled jobs pile up "
        "on :00/:30"
    )


def test_actions_are_sha_pinned_identically_to_tests_yml() -> None:
    ci_text = CI_WORKFLOW.read_text(encoding="utf-8")
    ours = _raw()
    for action in ("actions/checkout", "astral-sh/setup-uv"):
        expected = _pinned_sha(ci_text, action, "tests.yml")
        actual = _pinned_sha(ours, action, "wheel-e2e.yml")
        assert actual == expected, (
            f"{action} pin drifted from tests.yml ({actual} != {expected})"
        )


def test_single_job_runs_on_windows_with_a_build_sized_timeout() -> None:
    job = _job()
    assert job["runs-on"] == "windows-latest", job["runs-on"]
    timeout = job["timeout-minutes"]
    assert timeout == 30, (
        f"timeout-minutes must stay 30: the fixture caps each uv build at 15 "
        f"minutes and a cold cache needs headroom (got {timeout})"
    )


def test_sync_precedes_the_gated_pytest_run() -> None:
    runs = [step.get("run", "") for step in _job()["steps"]]
    sync_positions = [i for i, run in enumerate(runs) if run.strip() == "uv sync"]
    assert sync_positions, "a `uv sync` step must prepare the environment"
    pytest_positions = [
        i for i, run in enumerate(runs) if "uv run --no-sync pytest" in run
    ]
    assert len(pytest_positions) == 1, pytest_positions
    assert min(sync_positions) < pytest_positions[0], (
        "`uv run --no-sync` trusts a fully synced environment; `uv sync` must run first"
    )


def test_gated_step_sets_the_flag_and_targets_only_the_wheel_tests() -> None:
    gated = [step for step in _job()["steps"] if "pytest" in step.get("run", "")]
    assert len(gated) == 1, "exactly one step may invoke pytest"
    assert gated[0].get("env") == {"MCC_WHEEL_TESTS": "1"}, gated[0].get("env")
    run = gated[0]["run"]
    assert WHEEL_TEST_FILE in run, f"the gated run must target {WHEEL_TEST_FILE}: {run}"


def test_permissions_are_contents_read_only() -> None:
    assert _parsed()["permissions"] == {"contents": "read"}, (
        "the workflow needs nothing beyond reading the checkout"
    )


def test_overlapping_runs_cancel_via_concurrency_group() -> None:
    concurrency = _parsed()["concurrency"]
    assert concurrency["cancel-in-progress"] is True, concurrency
    group = concurrency["group"]
    assert "${{ github.workflow }}" in group and "${{ github.ref }}" in group, group
