"""Three ways a tunable can be invisible, and the pin against each.

``test_every_setting_has_an_admin_field.py`` (6.11.0) answers one question --
does every ``Settings`` field have a field on a page -- and by 7.23.0 the
answer was a clean 0 missing. The gaps that remained were the ones it never
asked about, found by the 7.24.0 audit in
``specs/AUDIT-EVERY-SETTING-ON-THE-DASHBOARD.md``:

1. **Twenty-three settings had no line in ``.env.example``.** The template is
   not documentation: since 6.68.0 it ships as ``config/env.example`` and is
   read as a real configuration layer. A key absent from it is one an operator
   who configures by file cannot discover, and one
   ``test_shipped_defaults_agree`` -- which only checks keys the template
   mentions -- never looked at.
2. **Twenty-three numeric fields published no bound.** A number input with no
   minimum and no maximum accepts anything and finds out at the server. The
   twenty-three are frozen by name below rather than given invented ceilings:
   a bound on ``PORT`` or ``HTTP_READ_TIMEOUT`` would reject a value some
   install is running on today, and the seven per-adapter web-search knobs are
   numbers the search host itself validates. What this pins is that there is
   never a twenty-fourth.
3. **Nothing stopped a new ``os.environ`` read appearing.** Every name read in
   ``src/`` is either a ``Settings`` alias -- and therefore on a page, by the
   6.11.0 contract -- or one of the bootstrap names listed here, each of which
   is read before ``Settings`` exists or by a different process entirely. A
   name that is neither is a tunable nobody can see.
"""

import re
from pathlib import Path

from pydantic import AliasChoices

from my_claude_code.config.admin.manifest import FIELDS, env_keys
from my_claude_code.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "my_claude_code"


def _env_key(name: str) -> str:
    alias = Settings.model_fields[name].validation_alias
    if alias is None:
        return name.upper()
    if isinstance(alias, AliasChoices):
        return str(alias.choices[0])
    return str(alias)


def _settings_aliases() -> frozenset[str]:
    """Every env name any ``Settings`` field answers to, canonical or not."""

    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if alias is None:
            names.add(name.upper())
        elif isinstance(alias, AliasChoices):
            names.update(str(choice) for choice in alias.choices)
        else:
            names.add(str(alias))
    return frozenset(names)


# --------------------------------------------------------------- 1. template


def _template_keys() -> frozenset[str]:
    """Every key ``.env.example`` names, whether live or commented out.

    A commented line still documents the key, and it is the right form for a
    setting whose default is empty: a live ``KEY=`` would put an empty string
    into a real configuration layer, which is not the same as the key being
    absent.
    """

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)
    return frozenset(pattern.findall(text))


def test_every_dashboard_field_is_in_the_shipped_template() -> None:
    documented = _template_keys()
    missing = sorted(
        key
        for key in (_env_key(name) for name in Settings.model_fields)
        if key != "NIM" and key not in documented
    )
    assert not missing, (
        "These settings are on a page but have no line in .env.example, so an "
        "operator who configures by file cannot find them and "
        "test_shipped_defaults_agree never checks them:\n"
        + "\n".join(f"  {key}" for key in missing)
    )


# ----------------------------------------------------------------- 2. bounds

#: Numeric fields that shipped without a published bound before 7.24.0.
#:
#: They keep none. Every one of them is a number some install is already
#: running on -- a port, an HTTP timeout, a log retention count -- and
#: inventing a ceiling for it would turn "your value is unusual" into "your
#: value is refused". Each deserves a bound argued on its own; this list is
#: what makes that a deliberate choice instead of a blank space, and what
#: stops a twenty-fourth joining them quietly.
UNBOUNDED_BEFORE_7_24_0 = frozenset(
    {
        "PORT",
        "MESSAGING_RATE_LIMIT",
        "MESSAGING_RATE_WINDOW",
        "MAX_MESSAGE_LOG_ENTRIES_PER_CHAT",
        "SERVER_LOG_RETAIN_FILES",
        "FALLBACK_REASONING_ANSWER_TIMEOUT",
        "HTTP_READ_TIMEOUT",
        "HTTP_WRITE_TIMEOUT",
        "HTTP_CONNECT_TIMEOUT",
        "PROVIDER_RATE_WINDOW",
        "PROVIDER_MAX_CONCURRENCY",
        "REQUEST_LOG_LADDER_BODY_MAX_CHARS",
        "WEBSEARCH_LOG_CONTENT_MAX_CHARS",
        "WEBSEARCH_LOG_MAX_ROWS",
        "WEBSEARCH_DIGEST_CHARS",
        "WEBSEARCH_DIGEST_CONTENT_CHARS",
        # Per-adapter web-search knobs. Each is a number the search host
        # itself validates and re-prices; MCC does not know that host's
        # ceiling and must not invent one.
        "BRAVE_LLM_MAX_TOKENS",
        "EXA_MAX_AGE_HOURS",
        "JINA_MAX_TOKENS",
        "PARALLEL_EXCERPT_CHARS",
        "PARALLEL_TOTAL_CHARS",
        "PERPLEXITY_MAX_TOKENS_PER_PAGE",
        "TAVILY_CHUNKS_PER_SOURCE",
    }
)


def test_every_new_numeric_field_publishes_a_bound() -> None:
    unbounded = sorted(
        field.key
        for field in FIELDS
        if field.field_type == "number"
        and field.minimum is None
        and field.maximum is None
        and field.key not in UNBOUNDED_BEFORE_7_24_0
    )
    assert not unbounded, (
        "A number input with no minimum and no maximum accepts anything and "
        "finds out at the server. Add a wide, sane range to config/limits.py "
        "or to the field itself:\n" + "\n".join(f"  {key}" for key in unbounded)
    )


def test_the_unbounded_allow_list_does_not_outlive_its_reason() -> None:
    """An escape hatch that names fields which no longer exist is a lie."""

    known = {field.key for field in FIELDS}
    stale = sorted(key for key in UNBOUNDED_BEFORE_7_24_0 if key not in known)
    assert not stale, (
        "These keys are excused from publishing a bound but are no longer "
        "manifest fields; drop them from the list: " + ", ".join(stale)
    )


# -------------------------------------------------------------- 3. bootstrap

#: Environment variables ``src/`` reads that are NOT settings, and why each one
#: cannot be. Two kinds, and neither could be honoured from a dashboard field:
#:
#: * **Read before ``Settings`` exists.** ``MCC_CONFIG_DIR`` decides which
#:   directory the ``.env`` is in, so reading it from that ``.env`` is
#:   circular. ``LOG_FILE`` is where a failure is reported when the failure may
#:   be "settings would not load".
#: * **Read by a different process.** The installer, the desktop shell and the
#:   ``mcc-*`` launchers never load the server's settings; a field for one of
#:   their variables would be a field that does nothing. The same goes for
#:   another program's own variable -- ``CLAUDE_CONFIG_DIR`` belongs to Claude
#:   Code, ``CODEX_HOME`` to Codex -- and for plain facts about the machine.
#:
#: Each is documented in ``.env.example`` or on the Guide page. Adding a name
#: here is a claim that a dashboard field for it would not work; adding one
#: because the test went red is how this list rots.
BOOTSTRAP_ENVIRONMENT = frozenset(
    {
        # Resolved before there is a settings file to read.
        "MCC_CONFIG_DIR",
        "MCC_ENV_FILE",
        "FCC_ENV_FILE",
        "LOG_FILE",
        # The installers and the update helper.
        "MCC_INSTALL_NO_START",
        "MCC_INSTALL_NO_DESKTOP",
        "MCC_INSTALL_LOG",
        # The desktop process and its shell, which never load Settings.
        # MCC_OPEN_BROWSER is not here: it IS a setting, and cli/desktop.py
        # reads it from the process environment as that setting's own
        # top layer.
        "FCC_OPEN_BROWSER",
        "MCC_DESKTOP_SKIP_AUTOSTART",
        "MCC_DESKTOP_SHELL_DIR",
        "MCC_DESKTOP_SHELL_BASE_URL",
        "MCC_DESKTOP_SHELL_TRAY",
        "MCC_SHELL_DESKTOP_COMMAND",
        "MCC_SHELL_SERVER_COMMAND",
        "DESKTOP_SHELL",
        # Other programs' own configuration, which MCC reads but never owns.
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "NPM_CONFIG_PREFIX",
        "RTK_TELEMETRY_DISABLED",
        "PTB_TIMEDELTA",
        # Facts about the machine.
        "APPDATA",
        "LOCALAPPDATA",
        "USER",
        "USERNAME",
        "WSL_DISTRO_NAME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        # The legacy-prefix detector, and the smoke harness's own target list.
        "FCC_",
        "FCC_SMOKE_TARGETS",
        # Read by the migration CLI before the server's settings are loaded.
        # PORT is also a real setting; this spelling is the pre-settings read.
        "PORT",
    }
)

#: Names MCC *writes into a child harness's* environment. The value already
#: comes from a setting; the name is the wire into another program's
#: configuration, and a dashboard field for it would mean nothing.
_CHILD_HARNESS_PREFIXES = ("MCC_", "ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_")

_ENV_READ = re.compile(
    r"""os\.(?:environ|getenv)[\.\(\[]\s*(?:get\s*\(\s*)?["']([A-Z][A-Z0-9_]*)["']"""
)


def _environment_names_read_in_src() -> dict[str, list[str]]:
    """Every literal env name read through ``os.environ`` / ``os.getenv``."""

    found: dict[str, list[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path.name == "settings.py" and path.parent.name == "config":
            continue
        text = path.read_text(encoding="utf-8")
        for name in _ENV_READ.findall(text):
            found.setdefault(name, []).append(
                str(path.relative_to(REPO_ROOT)).replace("\\", "/")
            )
    return found


def test_no_environment_variable_is_read_without_being_reachable() -> None:
    aliases = _settings_aliases()
    found = _environment_names_read_in_src()
    unexplained = {
        name: sorted(set(files))
        for name, files in found.items()
        if name not in aliases
        and name not in BOOTSTRAP_ENVIRONMENT
        and not name.startswith(_CHILD_HARNESS_PREFIXES)
    }
    assert not unexplained, (
        "These environment variables are read in src/ but are neither a "
        "Settings field (and so on a page) nor a documented bootstrap name. "
        "Either give it a Settings field and a manifest entry, or add it to "
        "BOOTSTRAP_ENVIRONMENT with the reason a dashboard field could not "
        "work:\n"
        + "\n".join(
            f"  {name}: {', '.join(files)}"
            for name, files in sorted(unexplained.items())
        )
    )


def test_the_bootstrap_list_does_not_claim_a_settings_key() -> None:
    """A name that IS a setting must not be excused as bootstrap-only.

    ``PORT`` is the one deliberate overlap -- the migration CLI reads it before
    the server's settings exist, and the server reads it as a setting -- so it
    is named rather than covered by a rule.
    """

    aliases = _settings_aliases()
    overlap = sorted(BOOTSTRAP_ENVIRONMENT & aliases)
    assert overlap == ["PORT"], (
        "A bootstrap name that is also a Settings alias hides a field that "
        "does work: " + ", ".join(overlap)
    )


def test_the_six_promoted_knobs_stay_exposed() -> None:
    """7.24.0 turned six literals into settings. Names them, so a removal says so."""

    exposed = env_keys()
    for key in (
        "PROXY_CHECK_TIMEOUT_SECONDS",
        "PROXY_CHECK_MAX_CONCURRENCY",
        "PROXY_FEED_TIMEOUT_SECONDS",
        "PROXY_FEED_MAX",
        "PROXY_CANDIDATE_BULK_MAX",
        "PROXY_FETCH_PERSIST_INTERVAL_SECONDS",
    ):
        assert key in exposed, f"{key} was removed from the admin manifests"
