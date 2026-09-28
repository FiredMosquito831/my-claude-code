"""The suite can never reach an OAuth token host, and nothing may opt out.

``tests/support/token_host_block.py`` wraps ``socket.getaddrinfo`` and
``socket.create_connection`` for the hosts that issue or renew a token MCC
could hold, from ``pytest_configure`` and as the first statements of every
child script. These tests hold that network backstop to its word:

* every blocked host -- plus a subdomain, a trailing-dot and a mixed-case
  spelling of it -- is refused at the socket layer, by sync and async httpx
  alike, and the refusal is the block's own (``refused_hosts()`` grew);
* ``httpx.MockTransport`` and a monkeypatched ``_post_refresh`` never touch a
  socket, so they keep working underneath it;
* no file under ``tests/`` rebinds either socket function or switches the
  block off;
* a child process started by path installs it too.

Nothing here can reach a network: before the first attempt the test checks
that the block is installed and that no proxy would move the lookup
elsewhere, and it refuses to run otherwise.
"""

import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth.constants import TOKEN_URL
from my_claude_code.providers.anthropic_oauth.credentials import OAuthTokens
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from tests.support.token_host_block import (
    BLOCKED_TOKEN_HOSTS,
    HermeticityViolation,
    block_is_installed,
    is_blocked_host,
    refused_hosts,
)

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
BLOCK_MODULE = "tests.support.token_host_block"
#: The only two files allowed to touch the socket entry points.
OWNERS = frozenset(
    {
        TESTS / "support" / "token_host_block.py",
        TESTS / "support" / "hermetic.py",
    }
)
CHILD = TESTS / "support" / "token_block_child.py"
RACE_CHILDREN = TESTS / "providers" / "oauth_race_children"

PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
SOCKET_ENTRY_POINTS = frozenset({"getaddrinfo", "create_connection"})
#: Module state that, rebound, would make the block think it is not needed.
BLOCK_STATE = frozenset({"_installed"})
#: Calls that rebind or remove an attribute or a mapping entry.
PATCHERS = frozenset(
    {"setattr", "delattr", "setitem", "delitem", "patch", "object", "patch_object"}
)
#: A would-be uninstall of the block or of the hermetic interceptors.
UNINSTALL = re.compile(
    r"(uninstall|unblock|disable|remove|restore|undo).*"
    r"(token.?host|host.?block|hermetic|intercept|socket.?guard)"
    r"|(token.?host|host.?block|hermetic|intercept).*"
    r"(uninstall|unblock|disable|remove|restore|undo)"
    r"|uninstall.*block|unblock",
    re.IGNORECASE,
)
#: Every opt-out the scan can recognise spells one of these (or matches
#: :data:`UNINSTALL`), so a file with none of them is not parsed at all.
SCAN_TRIGGERS = re.compile(
    r"getaddrinfo|create_connection|_installed|token_host_block|hermetic"
)

#: Top-level statements a child may run before installing the block: making
#: the worktree importable, nothing else.
PRELUDE_MODULES = frozenset({"sys", "os", "os.path", "pathlib"})
PRELUDE_CALLS = frozenset(
    {
        "insert",
        "append",
        "Path",
        "resolve",
        "absolute",
        "parent",
        "str",
        "fspath",
        "dirname",
        "abspath",
        "realpath",
        "normpath",
        "join",
        "get",
        "getenv",
    }
)


def _spellings(host: str) -> tuple[str, ...]:
    """The host, a subdomain of it and a trailing-dot spelling."""
    return (host, f"oauth.{host}", f"{host}.")


def _refuse_to_run_unguarded() -> None:
    """Fail -- before any attempt -- unless every attempt is sure to be refused."""
    if not block_is_installed():
        pytest.fail("The token-host block is not installed; refusing to try.")
    if getattr(socket.create_connection, "__name__", "") != (
        "guarded_create_connection"
    ):
        pytest.fail("socket.create_connection is not the guarded one.")
    leaked = [name for name in PROXY_VARIABLES if os.environ.get(name)]
    if leaked:
        pytest.fail(f"A proxy would move the lookup off this machine: {leaked}")
    proxies = {
        scheme: url
        for scheme, url in urllib.request.getproxies().items()
        if scheme != "no"
    }
    if proxies:
        pytest.fail(f"A system proxy would move the lookup: {sorted(proxies)}")


def _assert_refused(name: str, before: int) -> None:
    hits = refused_hosts()
    assert len(hits) > before, f"{name}: the block did not fire"
    wanted = name.lower().rstrip(".")
    assert wanted in hits[-1].lower(), (name, hits[-1])


# ---------------------------------------------------------------------------
# The block refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_token_host_is_unreachable_from_the_suite() -> None:
    _refuse_to_run_unguarded()
    tried = 0

    for host in BLOCKED_TOKEN_HOSTS:
        for name in _spellings(host):
            # Never attempt a name the block would not refuse.
            assert is_blocked_host(name), name
            url = f"https://{name}/v1/oauth/token"
            body = {"grant_type": "refresh_token", "refresh_token": "fixture"}

            before = len(refused_hosts())
            with pytest.raises(HermeticityViolation):
                socket.getaddrinfo(name, 443)
            _assert_refused(name, before)

            before = len(refused_hosts())
            with pytest.raises(HermeticityViolation):
                socket.create_connection((name, 443), timeout=1)
            _assert_refused(name, before)

            before = len(refused_hosts())
            with pytest.raises(HermeticityViolation):
                httpx.post(url, json=body, timeout=1)
            _assert_refused(name, before)

            before = len(refused_hosts())
            with pytest.raises(HermeticityViolation):
                async with httpx.AsyncClient(timeout=1) as client:
                    await client.post(url, json=body)
            _assert_refused(name, before)
            tried += 1

        # Case never matters to a resolver, so it must not matter here. httpx
        # lowercases the host itself; the socket layer is where it could slip.
        shouted = host.upper()
        before = len(refused_hosts())
        with pytest.raises(HermeticityViolation):
            socket.getaddrinfo(shouted, 443)
        _assert_refused(shouted, before)
        before = len(refused_hosts())
        with pytest.raises(HermeticityViolation):
            socket.create_connection((shouted, 443), timeout=1)
        _assert_refused(shouted, before)

    assert tried == 3 * len(BLOCKED_TOKEN_HOSTS)
    # MCC's own token endpoint is one of them.
    assert is_blocked_host(urlsplit(TOKEN_URL).hostname)


@pytest.mark.asyncio
async def test_mock_transports_still_work_under_the_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-code-config"))
    refused_before = len(refused_hosts())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(
            200,
            json={
                "access_token": "sk-ant-oat01-fixture-mocked",
                "refresh_token": "sk-ant-ort01-fixture-mocked",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    for host in BLOCKED_TOKEN_HOSTS:
        url = f"https://{host}/v1/oauth/token"
        with httpx.Client(transport=transport) as client:
            assert client.post(url, json={"fixture": True}).status_code == 200
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.post(url, json={"fixture": True})).status_code == 200

    assert seen == [host for host in BLOCKED_TOKEN_HOSTS for _ in range(2)]

    # MCC's real ``_post_refresh``, its client handed the MockTransport: the
    # exchange is addressed to a blocked host and still succeeds.
    real_async_client = httpx.AsyncClient

    def mocked_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        del args
        timeout = kwargs.get("timeout", 30.0)
        assert isinstance(timeout, (int, float))
        return real_async_client(transport=transport, timeout=timeout)

    with monkeypatch.context() as patch:
        patch.setattr(creds.httpx, "AsyncClient", mocked_client)
        response = await creds._post_refresh("sk-ant-ort01-fixture-native")
    assert response.status_code == 200
    assert seen[-1] == urlsplit(TOKEN_URL).hostname
    assert is_blocked_host(seen[-1])

    # A monkeypatched ``_post_refresh``: the NATIVE refresh, end to end.
    posted: list[str] = []

    async def fake_post_refresh(refresh_token: str) -> httpx.Response:
        posted.append(refresh_token)
        return httpx.Response(
            200,
            json={
                "access_token": "sk-ant-oat01-fixture-rotated",
                "refresh_token": "sk-ant-ort01-fixture-rotated",
                "expires_in": 3600,
            },
        )

    native = creds.add_or_update_account(
        OAuthTokens(
            access_token="sk-ant-oat01-fixture-native",
            refresh_token="sk-ant-ort01-fixture-native",
            expires_at=int(time.time() + 60),
            subscription_type="max",
            account_uuid="uuid-native",
            source="mcc",
        ),
        origin=ORIGIN_MCC,
        adopt_origin=True,
    )
    with monkeypatch.context() as patch:
        patch.setattr(creds, "_post_refresh", fake_post_refresh)
        refreshed = await creds.refresh_tokens(native.tokens, account_id=native.id)

    assert posted == ["sk-ant-ort01-fixture-native"]
    assert refreshed.access_token == "sk-ant-oat01-fixture-rotated"
    # Not one of these went near a socket.
    assert len(refused_hosts()) == refused_before


# ---------------------------------------------------------------------------
# Nothing opts out
# ---------------------------------------------------------------------------


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _names_the_block(value: object) -> bool:
    """A string argument naming a socket entry point or the block's state."""
    if not isinstance(value, str):
        return False
    leaf = value.rsplit(".", 1)[-1]
    return leaf in SOCKET_ENTRY_POINTS or leaf in BLOCK_STATE


def _rebinds_the_block(target: ast.expr) -> bool:
    if isinstance(target, ast.Attribute):
        return target.attr in SOCKET_ENTRY_POINTS or target.attr in BLOCK_STATE
    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
        return _names_the_block(target.slice.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_rebinds_the_block(element) for element in target.elts)
    return False


def _opt_outs(source: str, filename: str) -> list[str]:
    """Every place ``source`` rebinds the socket entry points or unblocks."""
    found: list[str] = []
    if not (SCAN_TRIGGERS.search(source) or UNINSTALL.search(source)):
        return found
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, (ast.Assign, ast.Delete, ast.AugAssign, ast.AnnAssign)):
            targets = (
                list(node.targets)
                if isinstance(node, (ast.Assign, ast.Delete))
                else [node.target]
            )
            if any(_rebinds_the_block(target) for target in targets):
                found.append(f"{filename}:{node.lineno}: {ast.unparse(node)[:100]}")
            continue
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node.func)
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        patches_the_block = callee in PATCHERS and any(
            isinstance(argument, ast.Constant) and _names_the_block(argument.value)
            for argument in arguments
        )
        reloads_the_block = callee == "reload" and any(
            "token_host_block" in ast.unparse(argument)
            or "hermetic" in ast.unparse(argument)
            for argument in arguments
        )
        if patches_the_block or reloads_the_block or UNINSTALL.search(callee):
            found.append(f"{filename}:{node.lineno}: {ast.unparse(node)[:100]}")
    return found


#: Each of these must be caught, or the scan below proves nothing.
KNOWN_OPT_OUTS = (
    "import socket\nsocket.getaddrinfo = lambda *a, **k: []\n",
    "import socket as s\ns.create_connection = None\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(socket, 'getaddrinfo', f)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr('socket.create_connection', f)\n",
    "def t(monkeypatch):\n    monkeypatch.delattr(socket, 'getaddrinfo')\n",
    "def t(monkeypatch):\n    monkeypatch.setitem(vars(socket), 'getaddrinfo', f)\n",
    "from unittest import mock\nmock.patch('socket.getaddrinfo', f)\n",
    "from unittest.mock import patch\npatch.object(socket, 'create_connection')\n",
    "vars(socket)['getaddrinfo'] = f\n",
    "from tests.support import token_host_block as b\nb._installed = False\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(b, '_installed', False)\n",
    "from tests.support import token_host_block as b\nb.uninstall_token_host_block()\n",
    "import importlib\nimportlib.reload(tests.support.token_host_block)\n",
)


def test_no_test_opts_out_of_the_token_host_block() -> None:
    for snippet in KNOWN_OPT_OUTS:
        assert _opt_outs(snippet, "<known>"), f"the scan misses: {snippet!r}"

    scanned = 0
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts or path in OWNERS:
            continue
        scanned += 1
        offenders.extend(
            _opt_outs(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
        )

    assert scanned > 100, f"only {scanned} files scanned"
    assert offenders == [], (
        "Only tests/support/token_host_block.py and tests/support/hermetic.py "
        "may touch socket.getaddrinfo / socket.create_connection or the "
        "block's state:\n" + "\n".join(offenders)
    )
    assert block_is_installed()
    assert socket.getaddrinfo.__name__ == "guarded_getaddrinfo"
    assert socket.create_connection.__name__ == "guarded_create_connection"


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


def _is_install(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and _callee(statement.value.func) == "install_token_host_block"
    )


def _is_prelude(statement: ast.stmt) -> bool:
    """Making the worktree importable, and importing the block: nothing else."""
    if isinstance(statement, ast.Import):
        return all(
            alias.name in PRELUDE_MODULES or alias.name == BLOCK_MODULE
            for alias in statement.names
        )
    if isinstance(statement, ast.ImportFrom):
        if statement.level:
            return False
        if statement.module in PRELUDE_MODULES or statement.module == BLOCK_MODULE:
            return True
        return statement.module == "tests.support" and all(
            alias.name == "token_host_block" for alias in statement.names
        )
    if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)):
        calls = [node for node in ast.walk(statement) if isinstance(node, ast.Call)]
        return all(_callee(call.func) in PRELUDE_CALLS for call in calls)
    return False


def _install_problem(source: str, filename: str) -> str | None:
    """Why ``source`` does not open by installing the block, or ``None``."""
    body = ast.parse(source, filename=filename).body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    for statement in body:
        if _is_install(statement):
            return None
        if not _is_prelude(statement):
            return (
                f"line {statement.lineno} runs before install_token_host_block(): "
                f"{ast.unparse(statement)[:80]}"
            )
    return "never calls install_token_host_block() at module level"


def _child_problem(path: Path) -> str | None:
    return _install_problem(path.read_text(encoding="utf-8"), str(path))


_OPENING = (
    '"""A child."""\n\nimport sys\nfrom pathlib import Path\n\n'
    "sys.path.insert(0, str(Path(__file__).resolve().parents[3]))\n\n"
    "from tests.support.token_host_block import install_token_host_block\n\n"
    "install_token_host_block()\n"
)
#: Each of these must be flagged, or the check below proves nothing.
KNOWN_BAD_OPENINGS = (
    # Something that could open a connection is imported first.
    "import httpx\n" + _OPENING,
    "import sys\nfrom my_claude_code.providers.anthropic_oauth import credentials\n"
    + _OPENING,
    # Real work before the install.
    _OPENING.replace(
        "install_token_host_block()\n", "main()\ninstall_token_host_block()\n"
    ),
    # Installed, but only inside a function.
    '"""A child."""\n\ndef main():\n    install_token_host_block()\n',
)


def test_child_processes_install_the_block() -> None:
    assert _install_problem(_OPENING, "<opening>") is None
    for source in KNOWN_BAD_OPENINGS:
        assert _install_problem(source, "<known>") is not None, source
    assert _child_problem(CHILD) is None, _child_problem(CHILD)

    completed = subprocess.run(
        [sys.executable, str(CHILD)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["installed"] is True
    assert report["outcome"] == "refused", report
    assert report["refused"] == ["platform.claude.com"]

    # Every race child opens the same way (the directory may not exist yet).
    if RACE_CHILDREN.is_dir():
        problems = {
            path.name: _child_problem(path)
            for path in sorted(RACE_CHILDREN.glob("*.py"))
            if path.name != "__init__.py"
        }
        assert problems, f"{RACE_CHILDREN} holds no child script"
        assert all(problem is None for problem in problems.values()), problems
