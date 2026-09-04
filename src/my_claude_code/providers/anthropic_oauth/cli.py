"""``mcc-anthropic-oauth-login`` -- sign in to a Claude subscription.

Two flows, the same as Claude Code's own:

* **loopback** (default) -- a callback server on ``127.0.0.1:<ephemeral>``
  catches the redirect, so approving in the browser is the whole interaction.
* **paste** (``--paste``, and the automatic fallback) -- Anthropic's hosted
  callback page shows a ``code#state`` string to paste back. Used whenever the
  browser cannot reach this process's ``localhost``: WSL, SSH, a container.

Nothing here raises a traceback at an operator. A refused code, a closed pipe
and a Ctrl-C are all one printed line and exit status 1.
"""

import asyncio
import contextlib
import sys
import webbrowser
from collections.abc import Sequence

from .credentials import (
    claude_credentials_path,
    detect_available_sources,
    managed_store_path,
)
from .loopback import (
    AnthropicOAuthLoopbackUnavailableError,
    cancel_loopback_login,
    loopback_login_status,
    loopback_unavailable_reason,
    start_loopback_login,
)
from .oauth_login import (
    AnthropicOAuthLoginError,
    build_authorize_url,
    exchange_code,
    generate_pkce_verifier,
    split_pasted_code,
)

_USAGE = """\
mcc-anthropic-oauth-login -- sign in to a Claude subscription for My Claude Code

Usage:
  mcc-anthropic-oauth-login [--paste] [--no-browser]
  mcc-anthropic-oauth-login --help | --version

Options:
  --paste        Skip the local callback server and paste the code by hand.
                 Use this when the browser cannot reach this machine's
                 localhost -- WSL, SSH, a container, a remote desktop.
  --no-browser   Print the URL instead of opening a browser.
  --help         Show this message and exit.
  --version      Print the My Claude Code version and exit.

What happens:
  1. A consent notice is printed. You must type 'yes' to continue.
  2. Your browser opens Anthropic's approval page.
  3. Approval returns here automatically (or you paste the code, with --paste).
  4. The credential is written to
       {store}
     with mode 0600. Claude Code's own credential file is never written to.

My Claude Code will only use this credential for requests that come from the
Claude Code CLI or the Claude Agent SDK. Anything else is refused.

Read docs/ANTHROPIC-SUBSCRIPTION.md before using this: Anthropic's terms say
subscription credentials are for Claude Code and Claude.ai only, and
enforcement is account-level.
"""

_WARNING = """
================================ READ THIS ================================
Anthropic's published terms say OAuth credentials from Claude Free, Pro and
Max plans are for Claude Code and Claude.ai only, and that third-party
products may not offer Claude.ai login or route requests through plan
credentials.

  https://code.claude.com/docs/en/legal-and-compliance

Signing in here does exactly that. Anthropic states it may enforce these
restrictions without prior notice, and enforcement is account-level -- the
risk is to the Claude account you are about to sign in with.

There is a supported alternative already in MCC: the `anthropic` provider,
which uses a Claude Console API key and is billed per token.
===========================================================================
"""


def _print_usage() -> None:
    print(_USAGE.format(store=managed_store_path()))


def _report(message: str) -> None:
    """One line, to stderr, and nothing else. No traceback ever reaches here."""
    print(message, file=sys.stderr)


def _confirm() -> bool:
    """Ask for consent. ``False`` when the operator declined or could not answer."""
    return input("Type 'yes' to continue: ").strip().lower() == "yes"


def _describe_existing() -> None:
    sources = detect_available_sources()
    if sources["claude_code"]:
        print(
            "Note: a Claude Code credential already exists at\n"
            f"  {claude_credentials_path()}\n"
            "MCC can use it directly -- you do not have to sign in again.\n"
            "Signing in here stores a separate credential MCC owns and can\n"
            "refresh without disturbing your Claude Code login.\n"
        )
    if sources["mcc"]:
        print(f"An MCC credential already exists at {managed_store_path()}.")
        print("Continuing will replace it.\n")


def _run_paste_flow(*, open_browser: bool) -> str | None:
    """Manual-redirect sign-in. Returns the subscription type, or ``None``."""
    verifier = generate_pkce_verifier()
    url = build_authorize_url(verifier)
    print("\nOpen this URL and approve access:\n")
    print(f"  {url}\n")
    if open_browser:
        # Headless, WSL without wslu, or no browser: the printed URL is enough.
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    print(
        "After approving, Anthropic shows a code. Paste it here. Pasting the\n"
        "whole callback URL from the address bar works too.\n"
    )
    pasted = input("Paste the code shown after approving: ").strip()
    if not pasted:
        print("No code entered. Aborted.")
        return None

    code, state = split_pasted_code(pasted)
    if not code:
        print("That did not contain an authorization code. Aborted.")
        return None
    tokens = asyncio.run(exchange_code(code, verifier, state))
    return tokens.subscription_type


def _run_loopback_flow(*, open_browser: bool) -> str | None:
    """Loopback sign-in. Returns the subscription type, or ``None``."""
    started = start_loopback_login(allow_remote=True, open_browser=open_browser)
    print("\nApprove access in your browser:\n")
    print(f"  {started['authorize_url']}\n")
    print("Waiting for the browser to come back... (Ctrl-C to cancel)")
    try:
        result = asyncio.run(loopback_login_status_until_settled())
    finally:
        cancel_loopback_login()
    if result["status"] != "complete":
        raise AnthropicOAuthLoginError(0, result.get("message", "sign-in failed"))
    return result.get("subscription_type") or None


async def loopback_login_status_until_settled() -> dict[str, str]:
    """Poll the loopback flow until it is no longer pending."""
    while True:
        status = await loopback_login_status()
        if status["status"] != "pending":
            return status
        await asyncio.sleep(1.0)


def anthropic_oauth_login_command(argv: Sequence[str] | None = None) -> None:
    """Run the interactive PKCE login and store the credential.

    Raises :class:`SystemExit` rather than letting anything propagate: this is
    a console entry point, and a traceback is never the right thing to show an
    operator who mistyped a code or pressed Ctrl-C.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        # Before the consent notice and before any prompt: `--help` must work
        # under a non-tty (CI, a wrapper script, a piped invocation).
        _print_usage()
        return

    unknown = [
        arg
        for arg in args
        if arg not in {"--paste", "--no-browser", "--version"} and arg.startswith("-")
    ]
    if unknown:
        _report(f"Unknown option: {unknown[0]}. Try --help.")
        raise SystemExit(1)

    use_paste = "--paste" in args
    open_browser = "--no-browser" not in args

    print(_WARNING)
    _describe_existing()

    try:
        if not _confirm():
            print("Aborted. Nothing was changed.")
            return

        if not use_paste and (reason := loopback_unavailable_reason()):
            print(f"Using the paste flow: {reason}.\n")
            use_paste = True

        if use_paste:
            subscription = _run_paste_flow(open_browser=open_browser)
        else:
            try:
                subscription = _run_loopback_flow(open_browser=open_browser)
            except AnthropicOAuthLoopbackUnavailableError as error:
                print(f"Falling back to the paste flow: {error}\n")
                subscription = _run_paste_flow(open_browser=open_browser)
    except EOFError:
        _report(
            "No input available (stdin is closed), so the sign-in was "
            "cancelled. Nothing was changed."
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        _report("\nCancelled. Nothing was changed.")
        raise SystemExit(1) from None
    except AnthropicOAuthLoginError as error:
        _report(f"Sign-in failed: {error}")
        raise SystemExit(1) from None

    if subscription is None:
        return

    print(f"\nSigned in. Credential stored at {managed_store_path()} (mode 0600).")
    if subscription:
        print(f"Subscription: {subscription}")
    print(
        "\nMCC will only use this credential for requests that come from the\n"
        "Claude Code CLI or the Claude Agent SDK. Anything else is refused; set\n"
        "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE=false to change that, having read\n"
        "docs/ANTHROPIC-SUBSCRIPTION.md."
    )
