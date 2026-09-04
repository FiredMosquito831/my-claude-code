"""The loopback callback sign-in, and the paste flow's URL handling.

Claude Code offers both transports (``l3t``, offset 182767278 of the 2.1.260
bundle: ``redirect_uri`` is either the hosted callback page or
``http://localhost:<port>/callback``) and so does MCC. The loopback server
binds an ephemeral port on 127.0.0.1 -- Claude Code's own port is
caller-supplied, so there is no well-known number to collide over.
"""

import urllib.error
import urllib.parse
import urllib.request

import pytest

from my_claude_code.providers.anthropic_oauth import loopback
from my_claude_code.providers.anthropic_oauth.constants import (
    LOOPBACK_BIND_HOST,
    loopback_redirect_uri,
)
from my_claude_code.providers.anthropic_oauth.oauth_login import (
    build_authorize_url,
    split_pasted_code,
)


@pytest.fixture(autouse=True)
def _no_leftover_server():
    loopback.cancel_loopback_login()
    yield
    loopback.cancel_loopback_login()


# ---------------------------------------------------------------------------
# B8 -- what the operator actually pastes
# ---------------------------------------------------------------------------


def test_split_pasted_code_accepts_bare_code_and_code_hash_state() -> None:
    """Regression guard: the two shapes that already worked."""
    assert split_pasted_code("ac_01AbC-xyz#st_99") == ("ac_01AbC-xyz", "st_99")
    assert split_pasted_code("ac_01AbC-xyz") == ("ac_01AbC-xyz", None)
    assert split_pasted_code("  ac_01AbC-xyz#st_99  ") == ("ac_01AbC-xyz", "st_99")


@pytest.mark.parametrize(
    "pasted",
    [
        "https://platform.claude.com/oauth/code/callback?code=ac_1&state=st_2",
        "http://localhost:53211/callback?code=ac_1&state=st_2",
        "https://platform.claude.com/oauth/code/callback?state=st_2&code=ac_1",
        "?code=ac_1&state=st_2",
        "code=ac_1&state=st_2",
    ],
)
def test_split_pasted_code_accepts_a_pasted_callback_url(pasted: str) -> None:
    """Pasting the address bar is the likeliest mistake in a paste flow.

    Before 6.43.0 the whole URL went to the token endpoint as the code, which
    answered 400, which MCC reported as "the pasted code was rejected -- it is
    single-use and short-lived, so start the sign-in again". That advice is
    wrong for this case and sends the operator round the loop forever.
    """
    assert split_pasted_code(pasted) == ("ac_1", "st_2")


def test_split_pasted_code_reports_a_callback_url_with_no_code_as_empty() -> None:
    """``?error=access_denied`` must not be forwarded as if it were a code."""
    code, state = split_pasted_code(
        "https://platform.claude.com/oauth/code/callback?error=access_denied"
    )
    assert code == ""
    assert state is None


def test_split_pasted_code_does_not_truncate_a_code_containing_an_ampersand() -> None:
    """The bare-query rule is strict on purpose: a code is an opaque token."""
    assert split_pasted_code("not-a-query&still-the-code")[0] == (
        "not-a-query&still-the-code"
    )


# ---------------------------------------------------------------------------
# the authorize URL for each transport
# ---------------------------------------------------------------------------


def test_the_authorize_url_defaults_to_the_manual_redirect() -> None:
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(build_authorize_url("verifier-value")).query
    )

    assert query["redirect_uri"] == ["https://platform.claude.com/oauth/code/callback"]
    assert query["code"] == ["true"]
    assert query["code_challenge_method"] == ["S256"]


def test_the_authorize_url_can_carry_a_loopback_redirect() -> None:
    redirect = loopback_redirect_uri(53211)
    # Claude Code spells the host `localhost`, and the authorization server
    # matches the redirect URI as a string.
    assert redirect == "http://localhost:53211/callback"

    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(
            build_authorize_url("verifier-value", redirect_uri=redirect)
        ).query
    )
    assert query["redirect_uri"] == [redirect]


# ---------------------------------------------------------------------------
# the callback server
# ---------------------------------------------------------------------------


def test_the_callback_server_binds_an_ephemeral_loopback_port() -> None:
    started = loopback.start_loopback_login(allow_remote=True, open_browser=False)

    redirect = urllib.parse.urlsplit(started["redirect_uri"])
    assert redirect.hostname == "localhost"
    assert redirect.path == "/callback"
    assert redirect.port and redirect.port > 1024

    flow, ready = loopback.loopback_login_state()
    assert flow is not None
    assert ready is False


def test_the_callback_server_captures_the_code_and_state() -> None:
    started = loopback.start_loopback_login(allow_remote=True, open_browser=False)
    flow, _ = loopback.loopback_login_state()
    assert flow is not None

    url = f"http://{LOOPBACK_BIND_HOST}:{flow.port}/callback?code=ac_1&state=st_2"
    with urllib.request.urlopen(url, timeout=10) as response:
        assert response.status == 200
        body = response.read().decode("utf-8")
    assert "Signed in" in body

    settled, ready = loopback.loopback_login_state()
    assert ready is True
    assert settled is not None
    assert settled.code == "ac_1"
    assert settled.state == "st_2"
    assert started["authorize_url"].startswith("https://claude.com/cai/oauth/authorize")


def test_the_callback_server_records_an_authorization_error() -> None:
    loopback.start_loopback_login(allow_remote=True, open_browser=False)
    flow, _ = loopback.loopback_login_state()
    assert flow is not None

    url = f"http://{LOOPBACK_BIND_HOST}:{flow.port}/callback?error=access_denied"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(url, timeout=10)
    assert excinfo.value.code == 400

    settled, ready = loopback.loopback_login_state()
    assert ready is True
    assert settled is not None
    assert settled.code is None
    assert "access_denied" in (settled.error or "")


def test_the_callback_server_ignores_every_other_path() -> None:
    loopback.start_loopback_login(allow_remote=True, open_browser=False)
    flow, _ = loopback.loopback_login_state()
    assert flow is not None

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"http://{LOOPBACK_BIND_HOST}:{flow.port}/nope", timeout=10
        )
    assert excinfo.value.code == 404

    _, ready = loopback.loopback_login_state()
    assert ready is False


@pytest.mark.parametrize(
    "environ",
    [
        {"WSL_DISTRO_NAME": "Ubuntu"},
        {"SSH_CONNECTION": "10.0.0.1 22 10.0.0.2 22"},
        {"CODESPACES": "true"},
    ],
)
def test_loopback_is_refused_where_localhost_means_two_things(
    environ: dict[str, str],
) -> None:
    """Better a clear refusal than a callback that silently never arrives."""
    assert loopback.loopback_unavailable_reason(environ) is not None


def test_loopback_is_allowed_on_an_ordinary_desktop() -> None:
    assert loopback.loopback_unavailable_reason({}) is None


def test_starting_a_second_sign_in_replaces_the_first() -> None:
    loopback.start_loopback_login(allow_remote=True, open_browser=False)
    first, _ = loopback.loopback_login_state()
    assert first is not None

    loopback.start_loopback_login(allow_remote=True, open_browser=False)
    second, _ = loopback.loopback_login_state()
    assert second is not None
    assert second is not first
    assert second.port != first.port
