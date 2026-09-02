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

    ``mcc-init`` writes ``.env.example``, whose ``ANTHROPIC_AUTH_TOKEN`` is
    populated, and the admin manifest offers the same default. A first run
    therefore never meets the refusal; only an install that deliberately
    emptied the token while listening to the network does.
    """
    token_default = next(
        field.default for field in FIELDS if field.key == "ANTHROPIC_AUTH_TOKEN"
    )
    assert token_default
    assert (
        open_proxy_without_auth_error(host=Settings().host, auth_token=token_default)
        is None
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
