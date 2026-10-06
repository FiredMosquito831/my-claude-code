"""A fresh install routes four Claude tiers to Zen's free model; no existing install moves.

The user's decision of 2026-10-06: a new install's Mythos, Fable, Opus and
Sonnet routes start on OpenCode Zen's free Muse Spark model, which answers on
OpenCode's anonymous ``public`` credential with no key at all (7.34.0). Haiku
and ``MODEL`` were not named and do not change.

The binding half is the other one: **existing installs must not change.** A
``.env`` is written once, by the first start, and never back-filled. So an
existing install's file holds these routes in one of two shapes, and both
must keep meaning "follow MODEL" exactly as v7.77.1 did:

* ``MODEL_MYTHOS=`` present and blank -- every install first started from
  7.2.0 (2026-09-13) on, because the template has shipped the line blank;
* the line ABSENT -- every install first started before 7.2.0 has no
  ``MODEL_MYTHOS`` line, and one from before Fable has no ``MODEL_FABLE``
  line. An absent key is therefore the signature of an OLD install, never of
  a new one, which is why the new value is not the ``Settings`` code default.

A first start is the one moment that is unambiguously a fresh install, so the
value lives in ``render_default_env`` and nowhere else.
"""

from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.routing import ModelRouter
from my_claude_code.cli import first_start
from my_claude_code.cli import migrate_config_dir as migration
from my_claude_code.config import paths
from my_claude_code.config.admin import persistence, sources
from my_claude_code.config.constants import (
    FRESH_INSTALL_ROUTE_KEYS,
    FRESH_INSTALL_ROUTE_MODEL,
)
from my_claude_code.config.env_files import settings_env_files
from my_claude_code.config.env_template import load_env_template, render_default_env
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.providers.runtime.opencode_credentials import model_is_free_tier

ZEN_FREE = "opencode/muse-spark-1.3-contributor-free"
#: The shipped ``MODEL`` default, unchanged by this release.
SHIPPED_MODEL = "nvidia_nim/nvidia/nemotron-3-super-120b-a12b"

#: One Claude model name per route, as Claude Code asks for them.
CLAUDE_NAMES = {
    "mythos": "claude-mythos-5.1",
    "fable": "claude-fable-5.1",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}
FOUR = ("mythos", "fable", "opus", "sonnet")
ROUTE_ATTRS = (
    "model_mythos",
    "model_fable",
    "model_opus",
    "model_sonnet",
    "model_haiku",
)

#: Cleared from the process environment so a developer shell (or the isolation
#: shell, which sets PORT) can neither lock a field nor outrank the file.
KEYS_USED = (
    "MODEL",
    "MODEL_MYTHOS",
    "MODEL_FABLE",
    "MODEL_OPUS",
    "MODEL_SONNET",
    "MODEL_HAIKU",
    "MODEL_FALLBACKS",
    "MODEL_MYTHOS_FALLBACKS",
    "MODEL_FABLE_FALLBACKS",
    "MODEL_OPUS_FALLBACKS",
    "MODEL_SONNET_FALLBACKS",
    "MODEL_HAIKU_FALLBACKS",
    "LOG_LEVEL",
    "MCC_ENV_FILE",
    "PORT",
    "HOST",
    "ANTHROPIC_AUTH_TOKEN",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine with nothing on it; ``~/.mcc`` is where the first start writes."""

    monkeypatch.delenv(paths.CONFIG_DIR_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(migration, "_mcc_is_running", lambda home: "")
    # The repo layer is ``./.env``; there is none in an empty directory.
    monkeypatch.chdir(tmp_path)
    for key in KEYS_USED:
        monkeypatch.delenv(key, raising=False)
    paths.reset_config_dir_cache()
    sources.clear_env_parse_cache()
    return tmp_path


def _settings() -> Settings:
    """``Settings`` over exactly the files a server reads, low to high.

    The suite pins ``env_file`` to ``None`` (``tests/conftest.py``) so no test
    reads a developer's real ``.env``; this names the production layering
    explicitly instead -- the repo ``./.env`` (absent here) and ``~/.mcc/.env``.
    """

    # pydantic-settings' ``_env_file`` init argument, passed the way the type
    # checker accepts it: it is not a declared field.
    init: dict[str, Any] = {"_env_file": settings_env_files()}
    return Settings(**init)


def _managed(home: Path) -> Path:
    return home / ".mcc" / ".env"


def _write_managed(home: Path, text: str) -> Path:
    path = _managed(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    paths.reset_config_dir_cache()
    sources.clear_env_parse_cache()
    return path


def _v7_77_1_first_start_text() -> str:
    """What every first start from 7.2.0 to v7.77.1 wrote: the template, token in.

    The template's four route lines are unchanged by this release (pinned
    below), so the template with its token filled is exactly that file.
    """

    template = load_env_template()
    assert "ANTHROPIC_AUTH_TOKEN=\n" in template
    return template.replace(
        "ANTHROPIC_AUTH_TOKEN=\n", 'ANTHROPIC_AUTH_TOKEN="token-for-this-test"\n', 1
    )


def _without_lines(text: str, *keys: str) -> str:
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not any(line.startswith(f"{key}=") for key in keys)
    )


def _routes(settings: Settings) -> dict[str, tuple[str, ...]]:
    """Every route's full plan: primary, then the chain, as the router builds it."""

    router = ModelRouter(settings)
    plans: dict[str, tuple[str, ...]] = {}
    for route, name in CLAUDE_NAMES.items():
        request = MessagesRequest.model_validate(
            {"model": name, "messages": [{"role": "user", "content": "hi"}]}
        )
        plans[route] = router.resolve_messages_plan(request).model_refs()
    return plans


# ------------------------------------------------------------ the value itself


def test_the_fresh_install_model_is_zens_free_muse_spark() -> None:
    assert FRESH_INSTALL_ROUTE_MODEL == ZEN_FREE
    assert FRESH_INSTALL_ROUTE_KEYS == (
        "MODEL_MYTHOS",
        "MODEL_FABLE",
        "MODEL_OPUS",
        "MODEL_SONNET",
    )
    # Inside the free-tier scope by the vendor's own ``-free`` tag, which is
    # what sends it on the anonymous credential with no key configured.
    assert model_is_free_tier(ZEN_FREE.split("/", 1)[1])


def test_the_code_default_of_every_route_stays_unset() -> None:
    """An absent key is an OLD install; it must keep following MODEL."""

    for attr in ROUTE_ATTRS:
        assert Settings.model_fields[attr].default is None, attr
    assert Settings.model_fields["model"].default == SHIPPED_MODEL


def test_the_template_still_ships_the_routes_blank() -> None:
    """The template is also the dashboard's fallback layer for an unset key."""

    template_lines = load_env_template().splitlines()
    for key in (*FRESH_INSTALL_ROUTE_KEYS, "MODEL_HAIKU"):
        assert f"{key}=" in template_lines, key
    assert f'MODEL="{SHIPPED_MODEL}"' in template_lines


def test_a_first_start_fills_exactly_the_four_routes_and_the_token() -> None:
    template = load_env_template().splitlines()
    rendered = render_default_env(token="token-for-this-test").splitlines()

    assert len(rendered) == len(template)
    changed = {
        before.partition("=")[0]: after
        for before, after in zip(template, rendered, strict=True)
        if before != after
    }
    assert changed == {
        "MODEL_MYTHOS": f'MODEL_MYTHOS="{ZEN_FREE}"',
        "MODEL_FABLE": f'MODEL_FABLE="{ZEN_FREE}"',
        "MODEL_OPUS": f'MODEL_OPUS="{ZEN_FREE}"',
        "MODEL_SONNET": f'MODEL_SONNET="{ZEN_FREE}"',
        "ANTHROPIC_AUTH_TOKEN": 'ANTHROPIC_AUTH_TOKEN="token-for-this-test"',
    }
    assert "MODEL_HAIKU=" in rendered


# ------------------------------------------------- a fresh install, end to end


def test_a_fresh_install_routes_four_tiers_to_zen_free(home: Path) -> None:
    first_start.ensure_config_home()
    assert _managed(home).is_file()
    sources.clear_env_parse_cache()

    settings = _settings()

    for attr in ("model_mythos", "model_fable", "model_opus", "model_sonnet"):
        assert getattr(settings, attr) == ZEN_FREE, attr
    assert settings.model_haiku is None
    assert settings.model == SHIPPED_MODEL
    plans = _routes(settings)
    for route in FOUR:
        assert plans[route] == (ZEN_FREE,), route
    # Haiku was not named: it follows MODEL on a fresh install too.
    assert plans["haiku"] == (SHIPPED_MODEL,)


# ------------------------------------------- existing installs do not change


def test_an_install_with_blank_route_lines_still_follows_model(home: Path) -> None:
    """Every install first started from 7.2.0 to v7.77.1."""

    _write_managed(home, _v7_77_1_first_start_text())

    settings = _settings()

    for attr in ROUTE_ATTRS:
        assert getattr(settings, attr) is None, attr
    assert _routes(settings) == dict.fromkeys(CLAUDE_NAMES, (SHIPPED_MODEL,))


def test_an_install_older_than_the_mythos_route_still_follows_model(
    home: Path,
) -> None:
    """First started before 7.2.0 (no Mythos line) or before Fable (no Fable line)."""

    _write_managed(
        home, _without_lines(_v7_77_1_first_start_text(), "MODEL_MYTHOS", "MODEL_FABLE")
    )

    settings = _settings()

    for attr in ROUTE_ATTRS:
        assert getattr(settings, attr) is None, attr
    assert _routes(settings) == dict.fromkeys(CLAUDE_NAMES, (SHIPPED_MODEL,))


def test_blank_and_absent_route_lines_route_identically(home: Path) -> None:
    """The two existing shapes are one behaviour, and it is MODEL's."""

    _write_managed(home, _v7_77_1_first_start_text())
    blank = _routes(_settings())
    _write_managed(
        home, _without_lines(_v7_77_1_first_start_text(), *FRESH_INSTALL_ROUTE_KEYS)
    )
    absent = _routes(_settings())

    assert blank == absent


@pytest.mark.parametrize(
    "drop",
    [(), ("MODEL_MYTHOS",), ("MODEL_MYTHOS", "MODEL_FABLE")],
    ids=["since-7.2.0", "before-7.2.0", "before-fable"],
)
def test_a_dashboard_save_never_repoints_an_existing_route(
    home: Path, drop: tuple[str, ...]
) -> None:
    """A Save of any other field writes the four routes back exactly as they were."""

    managed = _write_managed(home, _without_lines(_v7_77_1_first_start_text(), *drop))

    prepared = persistence.prepare_admin_update({"LOG_LEVEL": "DEBUG"})
    assert prepared.valid, prepared.errors
    persistence.commit_prepared_admin_update(prepared)
    sources.clear_env_parse_cache()

    written = managed.read_text(encoding="utf-8")
    assert ZEN_FREE not in written
    for key in FRESH_INSTALL_ROUTE_KEYS:
        lines = [line for line in written.splitlines() if line.startswith(f"{key}=")]
        assert lines == ([] if key in drop else [f"{key}="]), key
    settings = _settings()
    assert settings.log_level == "DEBUG"
    assert _routes(settings) == dict.fromkeys(CLAUDE_NAMES, (SHIPPED_MODEL,))


def test_use_default_on_a_fresh_route_returns_it_to_model(home: Path) -> None:
    """ "Use default" clears the field; the default it names is "none" (MODEL)."""

    first_start.ensure_config_home()
    sources.clear_env_parse_cache()

    prepared = persistence.prepare_admin_update({"MODEL_OPUS": ""})
    assert prepared.valid, prepared.errors
    persistence.commit_prepared_admin_update(prepared)
    sources.clear_env_parse_cache()

    plans = _routes(_settings())
    assert plans["opus"] == (SHIPPED_MODEL,)
    for route in ("mythos", "fable", "sonnet"):
        assert plans[route] == (ZEN_FREE,), route
