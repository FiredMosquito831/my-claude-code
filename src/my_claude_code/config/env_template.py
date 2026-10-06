"""Canonical env template loading for init and Admin UI defaults."""

import importlib.resources
import re
import secrets
from pathlib import Path

from .constants import FRESH_INSTALL_ROUTE_KEYS, FRESH_INSTALL_ROUTE_MODEL

#: Bytes of entropy behind a generated proxy token. ``token_urlsafe(32)``
#: yields 43 URL-safe characters, which is short enough to read off the
#: dashboard and long enough that nothing will guess it.
PROXY_TOKEN_ENTROPY_BYTES = 32

#: The token line ``render_default_env`` rewrites. Matched at the start of a
#: line so a mention of the name inside a comment is never touched.
_TOKEN_LINE = re.compile(r"^ANTHROPIC_AUTH_TOKEN=.*$", re.MULTILINE)

#: The route lines a first start fills in (7.78.0). Only a line that is
#: exactly ``KEY=`` -- the template's own blank -- is touched, so a template
#: that one day ships a value of its own for one of them keeps that value.
_FRESH_INSTALL_ROUTE_LINES = re.compile(
    r"^(" + "|".join(map(re.escape, FRESH_INSTALL_ROUTE_KEYS)) + r")=[ \t]*$",
    re.MULTILINE,
)


def load_env_template() -> str:
    """Load the root ``.env.example`` template from wheel resources or checkout."""

    packaged = importlib.resources.files("my_claude_code.config").joinpath(
        "env.example"
    )
    if packaged.is_file():
        return packaged.read_text("utf-8")

    source_template = Path(__file__).resolve().parents[3] / ".env.example"
    if source_template.is_file():
        return source_template.read_text(encoding="utf-8")

    raise FileNotFoundError("Could not find bundled or source .env.example template.")


def load_env_template_or_empty() -> str:
    """Return the env template, or an empty template when unavailable."""

    try:
        return load_env_template()
    except FileNotFoundError:
        return ""


def generate_proxy_auth_token() -> str:
    """Return a fresh ``ANTHROPIC_AUTH_TOKEN`` for this machine.

    Never a constant. Until 6.65.0 the template shipped the literal ``freecc``,
    which meant every install on earth shared one password -- and on the old
    ``HOST=0.0.0.0`` default that password was enough to spend the operator's
    provider credits from anywhere on their network. A value printed in a
    public repository is not a secret, so the value that lands in a real
    ``.env`` is generated on the machine that writes it.
    """

    return secrets.token_urlsafe(PROXY_TOKEN_ENTROPY_BYTES)


def render_default_env(token: str | None = None) -> str:
    """Return the ``.env`` text a first start writes.

    The shipped template with two substitutions, both of which only a brand-new
    configuration receives:

    * the deliberately empty ``ANTHROPIC_AUTH_TOKEN=`` line becomes a
      generated per-machine token;
    * the blank ``MODEL_MYTHOS=`` / ``MODEL_FABLE=`` / ``MODEL_OPUS=`` /
      ``MODEL_SONNET=`` lines name :data:`FRESH_INSTALL_ROUTE_MODEL`, OpenCode
      Zen's free model, which needs no key (7.78.0).

    The second lives here and not in the template or the ``Settings`` defaults
    on purpose. The template is also the dashboard's "what a field falls back
    to" layer, and the code default is what a key missing from the file means;
    an install first started before 7.2.0 has no ``MODEL_MYTHOS`` line at all,
    so changing either would repoint routes on installs that already exist.
    A first start is the one moment that is unambiguously a fresh install.

    The template already carries explicit ``HOST=127.0.0.1`` and ``PORT=8082``
    lines, so the two knobs that decide who can reach this server are visible
    in the file the user edits rather than implied by a code default.
    """

    template = load_env_template()
    value = generate_proxy_auth_token() if token is None else token
    rendered, replaced = _TOKEN_LINE.subn(
        f'ANTHROPIC_AUTH_TOKEN="{value}"', template, count=1
    )
    if not replaced:
        # A template without the line is a packaging accident, not a reason to
        # write a config whose token silently stays empty.
        rendered = f'{template.rstrip()}\n\nANTHROPIC_AUTH_TOKEN="{value}"\n'
    return _FRESH_INSTALL_ROUTE_LINES.sub(
        lambda match: f'{match.group(1)}="{FRESH_INSTALL_ROUTE_MODEL}"', rendered
    )
