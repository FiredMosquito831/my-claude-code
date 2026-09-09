"""An unauthenticated proxy is never served to anything but this machine.

``require_proxy_auth`` returns early when ``ANTHROPIC_AUTH_TOKEN`` is empty, and
``HOST`` defaults to ``0.0.0.0``. Those two defaults together are an open proxy
on the LAN, and no request-time check can catch it because the request-time
check is the one that returns early. So the refusal happens before the socket,
and this module pins both halves of it -- the decision, and the sentence it
prints, whose page and card names have to be ones the dashboard really renders.
"""

import re
from pathlib import Path

import pytest

from my_claude_code.config.admin.manifest import FIELDS, SECTIONS
from my_claude_code.config.proxy_auth import (
    RUNTIME_PAGE_LABEL,
    RUNTIME_SECTION_ID,
    host_is_loopback,
    open_proxy_without_auth_error,
    proxy_auth_token,
)
from my_claude_code.config.settings import Settings

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "  127.0.0.1  "])
def test_a_loopback_bind_may_still_run_without_a_token(host: str) -> None:
    assert host_is_loopback(host)
    assert open_proxy_without_auth_error(host=host, auth_token="") is None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.test"])
def test_a_reachable_bind_without_a_token_is_refused(host: str) -> None:
    assert not host_is_loopback(host)
    message = open_proxy_without_auth_error(host=host, auth_token="")

    assert message is not None
    assert "ANTHROPIC_AUTH_TOKEN" in message
    assert "HOST" in message
    assert host in message


@pytest.mark.parametrize("token", ["freecc", "  spaced  "])
def test_a_configured_token_is_enough_on_any_host(token: str) -> None:
    assert open_proxy_without_auth_error(host="0.0.0.0", auth_token=token) is None


def test_whitespace_is_not_a_token() -> None:
    assert open_proxy_without_auth_error(host="0.0.0.0", auth_token="   ") is not None


def test_the_shipped_defaults_start_without_a_refusal() -> None:
    """The default install is not the one this refusal is aimed at.

    This used to read the *manifest* default (``'freecc'``) and was green
    while a genuinely fresh machine refused to start: the code default for
    ``anthropic_auth_token`` was ``''``, ``host`` was ``0.0.0.0``, and
    nothing on any install path ever wrote a ``.env``. So it asserts the
    path a fresh machine actually takes -- the ``Settings`` defaults, with
    no file and no environment -- which since 6.65.0 is loopback and
    therefore safe with no token at all.
    """
    defaults = Settings.model_fields
    assert defaults["host"].default == "127.0.0.1"
    assert defaults["anthropic_auth_token"].default == ""
    assert (
        open_proxy_without_auth_error(
            host=defaults["host"].default,
            auth_token=defaults["anthropic_auth_token"].default,
        )
        is None
    )


def test_the_manifest_and_the_code_agree_about_both_halves() -> None:
    """The manifest is what the dashboard offers; it must not disagree.

    The 6.30.0 guard test read the manifest and the server read the code,
    and for four releases they said different things. Pin them together.
    """
    manifest = {field.key: field.default for field in FIELDS}
    assert manifest["HOST"] == Settings.model_fields["host"].default
    assert manifest["ANTHROPIC_AUTH_TOKEN"] == (
        Settings.model_fields["anthropic_auth_token"].default
    )


def test_the_hint_names_the_card_that_really_owns_both_fields() -> None:
    section_ids = {
        field.section_id
        for field in FIELDS
        if field.key in {"ANTHROPIC_AUTH_TOKEN", "HOST"}
    }
    assert section_ids == {RUNTIME_SECTION_ID}

    card_label = next(
        section.label
        for section in SECTIONS
        if section.section_id == RUNTIME_SECTION_ID
    )
    message = open_proxy_without_auth_error(host="0.0.0.0", auth_token="")
    assert message is not None
    assert f"{RUNTIME_PAGE_LABEL} -> {card_label}" in message


def test_the_hint_names_a_dashboard_page_that_renders_that_card() -> None:
    """The page label is read back out of the shipped ``admin.js``.

    A page renamed on the dashboard and left alone in this string sends its
    reader looking for something that does not exist, which is worse than no
    hint at all.
    """
    source = ADMIN_JS.read_text(encoding="utf-8")
    group = re.search(
        r"\{[^{}]*?label:\s*\"" + re.escape(RUNTIME_PAGE_LABEL) + r"\"[^{}]*?"
        r"sections:\s*\[(?P<sections>[^\]]*)\][^{}]*?\}",
        source,
        re.S,
    )
    assert group is not None, f"no VIEW_GROUPS entry labelled {RUNTIME_PAGE_LABEL!r}"
    assert f'"{RUNTIME_SECTION_ID}"' in group.group("sections")


def test_the_launcher_marker_is_unchanged_by_the_startup_policy() -> None:
    assert proxy_auth_token("") == "fcc-no-auth"
    assert proxy_auth_token(" tok ") == "tok"
