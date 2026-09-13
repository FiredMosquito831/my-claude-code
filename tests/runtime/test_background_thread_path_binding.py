"""The server's two post-readiness threads bind no paths of their own.

The defect this guards (found as a CI flake, one pytest worker at a time):
``start_request_path_warmup()`` spawned a daemon thread with no handle, no join
and no cancel path, whose second step resolved the models.dev cache path -- and
therefore ``config/paths.config_dir_path()`` -- *at tick time*. The hermetic
fixture that had redirected ``HOME`` for the test which started the thread had
already restored the runner's real ``HOME`` by the time the thread ticked, so
the thread resolved the runner's real ``~/.mcc`` and cached it in the
process-wide ``paths._resolution``. Every later test in that worker then saw a
config directory outside its sandbox and the hermeticity guard fired on some
unrelated test, thousands of errors deep, in one worker only.

The invariant: a background worker binds every path on the thread that started
it, never on its own. ``core/request_log.py`` has always done this
(``self._db_path`` in ``__init__``); 6.72.2 did the same for the survey thread.
"""

import threading
from pathlib import Path

import pytest

from my_claude_code.config import paths
from my_claude_code.providers.runtime import models_dev
from my_claude_code.runtime import asgi, warmup


@pytest.fixture(autouse=True)
def _forget_the_warmup():
    warmup.reset_request_path_warmup_for_tests()
    yield
    warmup.reset_request_path_warmup_for_tests()


def test_the_cache_path_is_resolved_before_the_thread_starts(monkeypatch):
    """``start_request_path_warmup`` hands the thread a concrete path."""

    handed: list[object] = []
    monkeypatch.setattr(warmup, "_spawn", handed.append)

    warmup.start_request_path_warmup()

    assert len(handed) == 1
    assert handed[0] == models_dev.models_dev_cache_path()


def test_the_resolution_happens_on_the_calling_thread(monkeypatch):
    """And it happens on the caller's thread, not on the worker's."""

    resolving_threads: list[int] = []
    real = models_dev.models_dev_cache_path

    def _record():
        resolving_threads.append(threading.get_ident())
        return real()

    monkeypatch.setattr(models_dev, "models_dev_cache_path", _record)
    monkeypatch.setattr(warmup, "_spawn", lambda cache_path: None)

    warmup.start_request_path_warmup()

    assert resolving_threads == [threading.get_ident()]


def test_the_thread_body_never_resolves_the_config_dir(monkeypatch, tmp_path):
    """The thread's own work touches ``config_dir_path`` zero times.

    This is the assertion that fails on the old code: ``_warm`` called
    ``prewarm_models_dev_indexes()`` with no path, which resolved the config
    directory from whatever the environment said at that moment.
    """

    # Recorded, not raised: ``_warm_models_dev_indexes`` catches ``Exception``
    # on purpose, so an assertion raised inside the thread's work would be
    # swallowed and the test could not fail.
    resolutions: list[str] = []

    def _record_config_dir():
        resolutions.append("config_dir_path")
        return tmp_path / "real-home"

    def _record_cache_path():
        resolutions.append("models_dev_cache_path")
        return tmp_path / "real-home" / "cache" / "models-dev.json"

    monkeypatch.setattr(paths, "config_dir_path", _record_config_dir)
    monkeypatch.setattr(models_dev, "config_dir_path", _record_config_dir)
    monkeypatch.setattr(models_dev, "models_dev_cache_path", _record_cache_path)
    monkeypatch.setattr(warmup, "_warm_openai_sdk", lambda: None)

    cache_path = tmp_path / "cache" / "models-dev.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{}", encoding="utf-8")

    warmup._warm(cache_path)

    assert resolutions == [], (
        f"the warm-up thread resolved paths of its own: {resolutions}"
    )


def test_a_restored_home_cannot_reach_the_thread(monkeypatch, tmp_path):
    """The cascade pattern, reproduced: HOME moves while the thread runs.

    The thread is handed the path the sandbox resolved; the environment is then
    restored, exactly as the hermetic fixture does at teardown; the thread runs
    afterwards and must still build from the sandbox path.
    """

    sandbox = tmp_path / "sandbox"
    (sandbox / "cache").mkdir(parents=True)
    (sandbox / "cache" / "models-dev.json").write_text("{}", encoding="utf-8")

    handed: list[Path | None] = []
    monkeypatch.setattr(warmup, "_spawn", handed.append)
    monkeypatch.setattr(
        models_dev,
        "models_dev_cache_path",
        lambda: sandbox / "cache" / "models-dev.json",
    )

    warmup.start_request_path_warmup()

    # The fixture restores the real HOME here -- after the path was bound.
    monkeypatch.setattr(
        models_dev,
        "models_dev_cache_path",
        lambda: tmp_path / "real-home" / "cache" / "models-dev.json",
    )

    built: list[object] = []
    monkeypatch.setattr(
        models_dev, "prewarm_models_dev_indexes", lambda path=None: built.append(path)
    )
    monkeypatch.setattr(warmup, "_warm_openai_sdk", lambda: None)

    warmup._warm(handed[0])

    assert built == [sandbox / "cache" / "models-dev.json"]


def test_the_housekeeping_thread_is_handed_its_answers(monkeypatch):
    """The second thread with the same defect, found by the same cascade.

    ``_post_readiness_housekeeping`` asked ``get_settings()`` and
    ``active_update()`` for itself, and both go through ``config_dir_path()``.
    Its guard flag is a process global that one test resets
    (``test_the_hook_runs_once_per_server_start``), so a later successful
    lifespan in the same worker spawns a real, un-patched one -- which is how a
    thread came to resolve the runner's real home during a test that had
    nothing to do with it.
    """

    asgi.reset_desktop_shell_auto_update_for_tests()
    reading_threads: list[int] = []

    def _inputs():
        reading_threads.append(threading.get_ident())
        return (False, False)

    handed: list[object] = []
    monkeypatch.setattr(asgi, "_housekeeping_inputs", _inputs)
    monkeypatch.setattr(asgi, "_desktop_shell_auto_update", handed.append)
    monkeypatch.setattr(asgi, "_sweep_superseded_environments", lambda: None)

    asgi.start_desktop_shell_auto_update()
    for thread in threading.enumerate():
        if thread.name == "mcc-desktop-shell-auto-update":
            thread.join(timeout=5)
    asgi.reset_desktop_shell_auto_update_for_tests()

    assert reading_threads == [threading.get_ident()]
    assert handed == [(False, False)]


def test_the_housekeeping_thread_body_resolves_nothing(monkeypatch):
    """And its own work leaves the process-wide resolution exactly as it found it.

    This is the guard's own question, asked of the thread's body directly:
    ``tests/support/hermetic.py:909-935`` fails a test whose teardown finds
    ``paths._resolution`` pointing inside the real home, and a thread that
    resolves nothing can never put one there.
    """

    from my_claude_code.config import desktop_shell

    monkeypatch.setattr(
        desktop_shell,
        "auto_update_desktop_shells",
        lambda *, enabled, helper_is_installing: desktop_shell.ShellAutoUpdate(
            skipped="test"
        ),
    )
    monkeypatch.setattr(asgi, "_sweep_superseded_environments", lambda: None)

    paths.reset_config_dir_cache()
    asgi._post_readiness_housekeeping((True, False))

    assert paths._resolution is None, (
        "the post-readiness thread resolved the config directory at tick time"
    )
