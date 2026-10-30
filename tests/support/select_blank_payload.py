"""The scratch ``.env`` and the config payload the select_blank scenario renders.

7.121.1 fixes a display-and-diff defect: a saved value that is not among a
control's options rendered blank, a number box could not hold ``7_200``, the
dirty count treated the untouched controls as changed, and Apply posted ``""``
for them -- which the server reads as "delete the line". The scenario
(``tests/api/admin_jsdom_select_blank.mjs``) must therefore render what the
server really sends for a file that holds those values, not a hand-written
fixture that could drift from the manifests.

``FIXTURE_ENV`` is that file. Every value is fake. ``build_payload`` writes the
server's own ``/admin/api/config`` answer for it -- ``_config_response``, the
same function the route calls -- to a JSON file the harness reads, with a
scratch config directory and a scratch working directory, so the developer's
real ``~/.mcc`` and a repository ``.env`` are never read.

The fixture's shape is deliberate, one line per class of value:

* five values no option carries -- the measured wipe sources -- plus a sixth,
  ``TAVILY_API_KEY_ROTATION``, whose control does not exist until the coverage
  release and which therefore has to stay out of every Apply body;
* two listed selects and one number a number box can hold, which must render
  exactly as they always did;
* one plain text value;
* one key that is not a manifest field at all (``MCC_SMOKE_MARKER``), to prove
  an unmanaged line survives a save untouched.
"""

import json
import os
from pathlib import Path

from my_claude_code.api.admin_routes import _config_response
from my_claude_code.config import paths
from my_claude_code.config.admin.manifest import env_keys
from my_claude_code.config.provider_registry import reset_provider_registry

#: The scratch managed ``.env``. Fake values, and every one of them chosen to
#: exercise a class of value the page has to handle. The sha256 of this text is
#: recorded in the committed baseline, so a fixture edit that forgets to
#: regenerate it is a failing test rather than a silent re-baseline.
FIXTURE_ENV = """\
# fake values, written by a scratch test fixture
NVIDIA_NIM_API_KEY_ROTATION=on_error
TAVILY_API_KEY_ROTATION=on_error
DDGS_BACKEND=brave,mojeek
LOG_LEVEL=debug
PROXY_CHECK_ENABLED=True
MODEL_DISCOVERY_REFRESH_SECONDS=7_200
DDGS_SAFESEARCH=strict
WEB_FETCH_EXTRACTOR=basic
REQUEST_LOG_MAX_ROWS=20000
BRAVE_SEARCH_LANG=en
MCC_SMOKE_MARKER=keep-me
"""

#: Environment names that decide where the payload is read from, on top of the
#: manifest keys themselves. ``load_value_state`` treats the process
#: environment as its highest layer, so a developer shell that exports any of
#: them would change the payload; all of them are cleared while it is built.
_DECIDING_ENVIRONMENT = ("MCC_CONFIG_DIR", "MCC_ENV_FILE")


def build_payload(destination: Path) -> dict:
    """Write ``/admin/api/config`` for ``FIXTURE_ENV`` and return it.

    ``destination`` gets the JSON the scenario reads. The managed ``.env``
    lives beside it under ``<stem>-root/``; the payload is built from there and
    from a scratch working directory, and the process environment is restored
    exactly as it was found -- including the config-directory resolution cache,
    which would otherwise keep answering with the scratch path for every later
    test in this process.
    """

    root = destination.parent / f"{destination.stem}-root"
    root.mkdir(parents=True, exist_ok=True)
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / ".env").write_text(FIXTURE_ENV, encoding="utf-8")
    work = root / "cwd"
    work.mkdir(exist_ok=True)

    names = (*env_keys(), *_DECIDING_ENVIRONMENT)
    saved_cwd = os.getcwd()
    saved_environment = {name: os.environ[name] for name in names if name in os.environ}
    for name in saved_environment:
        del os.environ[name]
    os.environ["MCC_CONFIG_DIR"] = str(config_dir)
    paths.reset_config_dir_cache()
    # The custom-provider registry is a process-wide singleton that reads
    # ``custom_providers.json`` once, from whatever config directory was in
    # force when it was first used. Reset on both sides, so the payload's
    # provider list comes from the scratch directory (which has none) and the
    # next caller does not inherit it.
    reset_provider_registry()
    try:
        os.chdir(work)
        payload = _config_response()
    finally:
        os.chdir(saved_cwd)
        os.environ.pop("MCC_CONFIG_DIR", None)
        os.environ.update(saved_environment)
        paths.reset_config_dir_cache()
        reset_provider_registry()

    destination.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload
