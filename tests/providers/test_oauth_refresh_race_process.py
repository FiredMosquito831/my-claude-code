"""Refresh races between real processes (7.69.1, spec rules 5, 6 and 8).

A refresh token is single-use, several MCC servers and the official clients
share one credential file, and the only thing they share is the disk. So the
only honest test of "every refresh token is spent once" is one where separate
processes really race for it. Every participant here is a child process
started from a script file in ``oauth_race_children/`` with the worktree's
own interpreter:

* ``fake_token_endpoint.py`` -- 127.0.0.1, port 0. Single-use rotation: a
  reused or unknown refresh token gets ``400 invalid_grant``. Every POST is
  logged by fingerprint, and the answer is held back so the POST window the
  race needs is real;
* ``mcc_child.py`` -- MCC's own credential code, unmodified, pointed at that
  endpoint, with the lock budget shrunk from minutes to seconds;
* ``fake_claude_code.py`` -- Claude Code 2.1.283's refresh, lock for lock:
  ``mkdir`` locks with a heartbeat, the legacy lock, a compare-and-swap save;
* ``fake_codex_writer.py`` -- Codex rewriting ``auth.json`` inside MCC's POST
  window. Codex has no lock to join.

Every child installs the token-host block before it imports anything else
(asserted below from the script text), and every path it touches -- ``HOME``,
MCC's config dir, ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME`` -- is inside
``tmp_path``. A start barrier (``go``) makes the participants race for real.
No live token host is ever contacted.
"""

import ast
import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.local_serial

CHILDREN = Path(__file__).resolve().parent / "oauth_race_children"
ENDPOINT = CHILDREN / "fake_token_endpoint.py"
MCC = CHILDREN / "mcc_child.py"
CLAUDE_CODE = CHILDREN / "fake_claude_code.py"
CODEX = CHILDREN / "fake_codex_writer.py"

ACCOUNT_UUID = "race-account-0000-4000-8000-000000000001"
ACCOUNT_EMAIL = "race@example.invalid"
CLAUDE_ACCESS_0 = "fake-at-initial-claude"
CLAUDE_REFRESH_0 = "fake-rt-initial-claude"

CODEX_ACCOUNT = "acct-race-original"
FOREIGN_ACCOUNT = "acct-race-foreign"
CODEX_REFRESH_0 = "fake-rt-initial-codex"
CHATGPT_AUTH_CLAIM = "https://api.openai.com/auth"

#: The lock budget the MCC children and the fake Claude Code run with. The
#: real shape -- retries one to two seconds apart, then a liveness window, a
#: lock stale after a minute without a heartbeat -- shrunk so that a race costs
#: seconds. The budget (>= 4.2 s) outlasts every holder below.
FAST_LOCKS: dict[str, float] = {
    "stale": 2.0,
    "heartbeat": 0.25,
    "retries": 8,
    "retry-min": 0.15,
    "retry-max": 0.3,
    "liveness": 3.0,
}
#: How long the fake endpoint holds a contested refresh's answer: the winner
#: sits inside its locks at least this long, so every loser really waits.
CONTESTED_POST_SECONDS = 1.5

STARTUP_SECONDS = 90.0
PROCESS_SECONDS = 60.0

_DROPPED_ENVIRONMENT = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "MCC_ENV_FILE",
        "FCC_ENV_FILE",
        "FCC_CONFIG_DIR",
    }
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _fp(token: str | None) -> str:
    """``sha256[:16]`` -- how the children and the endpoint name a token."""

    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


def _jwt(claims: dict[str, Any]) -> str:
    """An unsigned JWT-shaped token; MCC reads Codex expiry from its ``exp``."""

    def part(document: dict[str, Any]) -> str:
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.sig"


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(document, indent=2).encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _stamp(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_mtime_ns, stat.st_size]


def _is_install_call(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "install_token_host_block"
    )


def _is_sys_path_insert(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "sys.path.insert"
    )


def _assert_children_install_the_block() -> None:
    """Every child script installs the token-host block before anything else.

    Read from the script text: before the install call there may be only the
    docstring, ``import sys``, ``from pathlib import Path``, the
    ``sys.path.insert`` of the worktree root and the import of the block
    itself; after it, no module-level import at all (everything else is
    imported inside ``main``, which runs after the block is in place).
    """

    scripts = sorted(CHILDREN.glob("*.py"))
    assert {script.name for script in scripts} >= {
        ENDPOINT.name,
        MCC.name,
        CLAUDE_CODE.name,
        CODEX.name,
    }
    assert not (CHILDREN / "__init__.py").exists()
    for script in scripts:
        assert not script.name.startswith("test_"), script
        assert not script.name.endswith("_test.py"), script
        body = list(ast.parse(script.read_text(encoding="utf-8")).body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        installs = [index for index, node in enumerate(body) if _is_install_call(node)]
        assert installs, f"{script.name} never installs the token-host block"
        before, after = body[: installs[0]], body[installs[0] + 1 :]
        for node in before:
            if isinstance(node, ast.Import):
                assert [alias.name for alias in node.names] == ["sys"], script
            elif isinstance(node, ast.ImportFrom):
                assert node.module in ("pathlib", "tests.support.token_host_block"), (
                    f"{script.name} imports {node.module} before the block"
                )
            else:
                assert _is_sys_path_insert(node), (
                    f"{script.name} runs {ast.unparse(node)!r} before the block"
                )
        assert any(_is_sys_path_insert(node) for node in before), script
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "tests.support.token_host_block"
            for node in before
        ), script
        assert not [
            node for node in after if isinstance(node, (ast.Import, ast.ImportFrom))
        ], f"{script.name} imports at module level after the block"


# ---------------------------------------------------------------------------
# The machine the children share
# ---------------------------------------------------------------------------


class _World:
    """One simulated machine inside ``tmp_path``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.mcc = root / "mcc-config"
        self.claude = root / "claude-config"
        self.codex = root / "codex-home"
        self.sync = root / "sync"
        self.endpoint = root / "endpoint"
        self.logs = root / "logs"
        for directory in (
            self.home / "AppData" / "Roaming",
            self.home / "AppData" / "Local",
            self.mcc,
            self.claude,
            self.codex,
            self.sync,
            self.endpoint,
            self.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def credentials(self) -> Path:
        return self.claude / ".credentials.json"

    @property
    def anthropic_store(self) -> Path:
        return self.mcc / "anthropic_oauth.json"

    @property
    def chatgpt_store(self) -> Path:
        return self.mcc / "auth" / "chatgpt-oauth.json"

    @property
    def auth_json(self) -> Path:
        return self.codex / "auth.json"

    def env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in _DROPPED_ENVIRONMENT
        }
        env.update(
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            APPDATA=str(self.home / "AppData" / "Roaming"),
            LOCALAPPDATA=str(self.home / "AppData" / "Local"),
            MCC_CONFIG_DIR=str(self.mcc),
            CLAUDE_CONFIG_DIR=str(self.claude),
            CODEX_HOME=str(self.codex),
            ANTHROPIC_OAUTH_WRITE_BACK="true",
            CHATGPT_OAUTH_WRITE_BACK="true",
            NO_PROXY="127.0.0.1,localhost",
            PYTHONIOENCODING="utf-8",
        )
        return env

    # -- Claude Code's files ------------------------------------------------

    def write_claude_code_login(self) -> None:
        """Claude Code's file, holding an expired access token, and its config."""

        _write_json(
            self.credentials,
            {
                "claudeAiOauth": {
                    "accessToken": CLAUDE_ACCESS_0,
                    "refreshToken": CLAUDE_REFRESH_0,
                    "expiresAt": int((time.time() - 600) * 1000),
                    "scopes": ["user:inference", "user:profile"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                },
                # Another client's entry: write-back replaces claudeAiOauth only.
                "mcpOAuth": {"race-server": {"serverName": "race"}},
            },
        )
        _write_json(
            self.claude / ".claude.json",
            {
                "oauthAccount": {
                    "accountUuid": ACCOUNT_UUID,
                    "emailAddress": ACCOUNT_EMAIL,
                }
            },
        )

    def write_anthropic_store(self, *, origin: str) -> None:
        """MCC's own store: one account holding the same expired credential.

        ``origin="claude-code"`` is an imported (SHARED) record watching
        Claude Code's file from the stamp it has now; ``origin="mcc"`` is an
        account MCC signed in itself (NATIVE). The baseline stamp means the
        record resolves without a store write, so what the MCC processes race
        for is the refresh lock and nothing before it.
        """

        tokens: dict[str, Any] = {
            "accessToken": CLAUDE_ACCESS_0,
            "refreshToken": CLAUDE_REFRESH_0,
            "expiresAt": int((time.time() - 600) * 1000),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
            "refreshTokenExpiresAt": None,
            "rateLimitTier": "default_claude_max_20x",
            "accountUuid": ACCOUNT_UUID,
            "accountEmail": ACCOUNT_EMAIL,
            "organizationName": None,
        }
        entry: dict[str, Any] = {
            **tokens,
            "id": ACCOUNT_UUID,
            "origin": origin,
            "originPath": str(self.credentials) if origin == "claude-code" else "",
            "writeBack": origin == "claude-code",
            "addedAt": "2026-09-28T00:00:00Z",
            "ordinal": 1,
        }
        if origin == "claude-code":
            entry["sourceStamp"] = _stamp(self.credentials)
        _write_json(
            self.anthropic_store,
            {**tokens, "accountsVersion": 1, "accounts": [entry]},
        )

    def claude_block(self) -> dict[str, Any]:
        block = _read_json(self.credentials)["claudeAiOauth"]
        assert isinstance(block, dict)
        return block

    def assert_no_lock_left(self) -> None:
        """Every lock was released, and nobody left Claude Code's pid record."""

        leftovers = [
            path
            for path in (
                self.claude / ".oauth_refresh.lock",
                Path(os.path.realpath(self.claude) + ".lock"),
                self.claude / ".storage-write.lock",
                self.claude / ".oauth_refresh.lock.owner",
                self.anthropic_store.with_name("anthropic_oauth.json.refresh.lock"),
                self.chatgpt_store.with_name("chatgpt-oauth.json.refresh.lock"),
            )
            if path.exists()
        ]
        assert leftovers == []

    # -- Codex's file -------------------------------------------------------

    def write_codex_login(self) -> dict[str, Any]:
        now = int(time.time())
        auth = {"chatgpt_account_id": CODEX_ACCOUNT}
        document: dict[str, Any] = {
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": _jwt(
                    {
                        "exp": now - 600,
                        "iat": now - 4200,
                        "email": ACCOUNT_EMAIL,
                        CHATGPT_AUTH_CLAIM: auth,
                    }
                ),
                "access_token": _jwt(
                    {"exp": now - 600, "iat": now - 4200, CHATGPT_AUTH_CLAIM: auth}
                ),
                "refresh_token": CODEX_REFRESH_0,
                "account_id": CODEX_ACCOUNT,
            },
            "last_refresh": "2026-09-27T00:00:00Z",
        }
        _write_json(self.auth_json, document)
        return document


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------


class _Race:
    """Start the endpoint and the participants, release them together, reap."""

    def __init__(self, world: _World) -> None:
        self.world = world
        self.env = world.env()
        self.port_file = world.endpoint / "endpoint.port"
        self.endpoint: subprocess.Popen[bytes] | None = None
        self.children: dict[str, subprocess.Popen[bytes]] = {}

    def __enter__(self) -> _Race:
        return self

    def __exit__(self, *exc: object) -> None:
        processes = list(self.children.values())
        if self.endpoint is not None:
            processes.append(self.endpoint)
        for process in processes:
            if process.poll() is None:
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
                process.communicate(timeout=10)

    def _stderr(self, name: str) -> Any:
        return (self.world.logs / f"{name}.err").open("wb")

    def _stderr_tail(self, name: str) -> str:
        path = self.world.logs / f"{name}.err"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[-4000:]

    def start_endpoint(
        self,
        *,
        mode: str,
        valid: dict[str, str],
        delay_seconds: float = 0.0,
        hold_until: Path | None = None,
    ) -> None:
        config = self.world.endpoint / "config.json"
        _write_json(
            config,
            {
                "mode": mode,
                "workdir": str(self.world.endpoint),
                "valid_refresh_tokens": valid,
                "delay_seconds": delay_seconds,
                "hold_until": str(hold_until) if hold_until else None,
                "hold_timeout": 20.0,
                "expires_in": 3600,
            },
        )
        # Not waited for here: the participants start alongside it and read
        # the port it reports before they declare themselves ready.
        with self._stderr("endpoint") as stderr:
            self.endpoint = subprocess.Popen(
                [sys.executable, str(ENDPOINT), "--config", str(config)],
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )

    def spawn(self, name: str, script: Path, *args: str) -> None:
        assert name not in self.children
        with self._stderr(name) as stderr:
            self.children[name] = subprocess.Popen(
                [sys.executable, str(script), *args],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )

    def mcc(
        self,
        name: str,
        *,
        mode: str = "claude",
        wait_for: Path | None = None,
        codex_import: bool = False,
        locks: dict[str, float] = FAST_LOCKS,
    ) -> None:
        args = [
            "--mode",
            mode,
            "--name",
            name,
            "--port-file",
            str(self.port_file),
            "--sync-dir",
            str(self.world.sync),
        ]
        if wait_for is not None:
            args += ["--wait-for", str(wait_for)]
        if codex_import:
            args.append("--codex-import")
        for key, value in locks.items():
            args += [f"--{key}", str(value)]
        self.spawn(name, MCC, *args)

    def claude_code(self, name: str, *, heartbeat: float = 0.25) -> None:
        args = [
            "--claude-dir",
            str(self.world.claude),
            "--sync-dir",
            str(self.world.sync),
            "--name",
            name,
            "--port-file",
            str(self.port_file),
        ]
        for key, value in {**FAST_LOCKS, "heartbeat": heartbeat}.items():
            args += [f"--{key}", str(value)]
        self.spawn(name, CLAUDE_CODE, *args)

    def codex_writer(self, name: str, *, scenario: str) -> None:
        self.spawn(
            name,
            CODEX,
            "--auth-json",
            str(self.world.auth_json),
            "--sync-dir",
            str(self.world.sync),
            "--endpoint-dir",
            str(self.world.endpoint),
            "--name",
            name,
            "--scenario",
            scenario,
            "--foreign-account",
            FOREIGN_ACCOUNT,
        )

    def go(self) -> None:
        """Wait until every participant is ready, then release them at once."""

        deadline = time.monotonic() + STARTUP_SECONDS
        for name, process in self.children.items():
            ready = self.world.sync / f"ready-{name}"
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(
                        f"{name} never reached the start barrier:\n"
                        + self._stderr_tail(name)
                        + "\n--- endpoint ---\n"
                        + self._stderr_tail("endpoint")
                    )
                time.sleep(0.02)
        assert self.endpoint is not None and self.endpoint.poll() is None, (
            "the fake token endpoint died:\n" + self._stderr_tail("endpoint")
        )
        (self.world.sync / "go").write_text("1", encoding="utf-8")

    def finish(self) -> dict[str, dict[str, Any]]:
        """Each participant's one JSON line."""

        results: dict[str, dict[str, Any]] = {}
        for name, process in self.children.items():
            try:
                stdout, _ = process.communicate(timeout=PROCESS_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
                pytest.fail(f"{name} did not finish:\n" + self._stderr_tail(name))
            results[name] = self._last_json(name, stdout)
        return results

    def stop_endpoint(self) -> list[dict[str, Any]]:
        """Stop the endpoint; every POST it saw, in arrival order."""

        assert self.endpoint is not None
        stdout, _ = self.endpoint.communicate(input=b"", timeout=15)
        summary = self._last_json("endpoint", stdout)
        posts = summary["posts"]
        log = self.world.endpoint / "endpoint.log"
        logged = (
            [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            if log.exists()
            else []
        )
        assert posts == logged
        return posts

    def _last_json(self, name: str, stdout: bytes) -> dict[str, Any]:
        for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                with contextlib.suppress(ValueError):
                    document = json.loads(line)
                    if isinstance(document, dict) and "role" in document:
                        return document
        pytest.fail(f"{name} printed no result line:\n" + self._stderr_tail(name))


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _one_spend(posts: list[dict[str, Any]], *, presented: str) -> dict[str, Any]:
    """One contested refresh: exactly one POST, it succeeded, nothing reused."""

    refused = [post for post in posts if post["status"] != 200]
    assert refused == [], f"a refresh token was reused or unknown: {refused}"
    spent = Counter(post["presented_fp"] for post in posts)
    assert all(count == 1 for count in spent.values()), spent
    assert len(posts) == 1, posts
    assert posts[0]["presented_fp"] == _fp(presented)
    return posts[0]


def _assert_mcc_ok(result: dict[str, Any]) -> None:
    assert result["ok"], result
    assert result["posts"] == len(result["statuses"]), result
    assert all(status == 200 for status in result["statuses"]), result


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_two_mcc_processes_and_claude_code_spend_each_refresh_token_once(
    tmp_path: Path,
) -> None:
    """Rule 5: MCC joins Claude Code's own locks, so three racers spend one token.

    Two MCC servers on the automatic fallback (SHARED, no record) and Claude
    Code itself all find the same expired access token and all go for the
    refresh at once. Whoever wins, the refresh token is presented exactly
    once, nobody gets ``invalid_grant``, and all three end on the winner's
    token -- which is what Claude Code's file holds afterwards.
    """

    _assert_children_install_the_block()
    world = _World(tmp_path)
    world.write_claude_code_login()
    with _Race(world) as race:
        race.start_endpoint(
            mode="claude",
            valid={CLAUDE_REFRESH_0: ACCOUNT_UUID},
            delay_seconds=CONTESTED_POST_SECONDS,
        )
        race.mcc("mcc-a")
        race.mcc("mcc-b")
        race.claude_code("claude-code")
        race.go()
        results = race.finish()
        posts = race.stop_endpoint()

    spend = _one_spend(posts, presented=CLAUDE_REFRESH_0)
    assert sum(result["posts"] for result in results.values()) == 1
    winners = [name for name, result in results.items() if result["posts"] == 1]
    assert len(winners) == 1, results

    claude_code = results["claude-code"]
    assert claude_code["statuses"] in ([], [200])
    assert claude_code["outcome"] in ("refreshed", "race_resolved"), claude_code
    assert (claude_code["outcome"] == "refreshed") == (winners == ["claude-code"])
    assert not claude_code["compromised"], "MCC touched Claude Code's live lock"
    assert claude_code["final_access_fp"] == spend["issued_access_fp"]

    for name in ("mcc-a", "mcc-b"):
        mcc = results[name]
        _assert_mcc_ok(mcc)
        assert mcc["credential_mode"] == "shared", mcc
        assert mcc["final_access_fp"] == spend["issued_access_fp"], mcc
        if name in winners:
            assert "shared:refreshed+wrote-back" in mcc["decisions"], mcc
        else:
            assert {"shared:waited", "shared:adopted"} & set(mcc["decisions"]), mcc

    block = world.claude_block()
    assert _fp(block["accessToken"]) == spend["issued_access_fp"]
    assert _fp(block["refreshToken"]) == spend["issued_refresh_fp"]
    assert _read_json(world.credentials)["mcpOAuth"] == {
        "race-server": {"serverName": "race"}
    }
    world.assert_no_lock_left()


def test_the_loser_adopts_the_winners_token_with_zero_posts(tmp_path: Path) -> None:
    """Rule 8: the MCC that loses the lock re-reads under it and adopts.

    Two MCC servers holding the same imported (SHARED) account race for the
    expired token. The endpoint holds the winner's answer for 1.5 s, so
    the loser genuinely waits on Claude Code's lock; under it, it finds the
    winner's token already in the file and adopts it without a POST.
    """

    _assert_children_install_the_block()
    world = _World(tmp_path)
    world.write_claude_code_login()
    world.write_anthropic_store(origin="claude-code")
    with _Race(world) as race:
        race.start_endpoint(
            mode="claude",
            valid={CLAUDE_REFRESH_0: ACCOUNT_UUID},
            delay_seconds=CONTESTED_POST_SECONDS,
        )
        race.mcc("mcc-a")
        race.mcc("mcc-b")
        race.go()
        results = race.finish()
        posts = race.stop_endpoint()

    spend = _one_spend(posts, presented=CLAUDE_REFRESH_0)
    for result in results.values():
        _assert_mcc_ok(result)
        assert result["credential_mode"] == "shared", result
    ranked = sorted(results.values(), key=lambda result: result["posts"])
    loser, winner = ranked
    assert (loser["posts"], winner["posts"]) == (0, 1), results
    assert "shared:refreshed+wrote-back" in winner["decisions"], winner
    assert "shared:waited" in loser["decisions"], loser
    assert loser["final_access_fp"] == winner["final_access_fp"]
    assert winner["final_access_fp"] == spend["issued_access_fp"]
    assert loser["final_refresh_fp"] == spend["issued_refresh_fp"]

    # Claude Code's file ends on the one token that was issued, and so does
    # MCC's mirror of it -- nothing pending, nothing orphaned.
    block = world.claude_block()
    assert _fp(block["accessToken"]) == spend["issued_access_fp"]
    assert _fp(block["refreshToken"]) == spend["issued_refresh_fp"]
    [record] = _read_json(world.anthropic_store)["accounts"]
    assert record["origin"] == "claude-code"
    assert _fp(record["accessToken"]) == spend["issued_access_fp"]
    assert _fp(record["refreshToken"]) == spend["issued_refresh_fp"]
    assert not record.get("pendingWriteBack", False)
    world.assert_no_lock_left()


def test_claude_code_holding_the_lock_makes_mcc_wait_then_adopt(
    tmp_path: Path,
) -> None:
    """Rule 5: a live Claude Code lock is waited for, never broken.

    Claude Code takes its refresh locks first and holds them through a POST
    the endpoint keeps open for 2.5 s, heartbeating every 0.2 s. MCC's stale
    window is set to 1 s -- shorter than the hold -- so an MCC that ignored
    the heartbeat would break the lock, POST the spent token and get
    ``invalid_grant``. Instead it waits, re-reads under the lock, and adopts
    Claude Code's token with zero POSTs; Claude Code's heartbeat never sees
    its lock touched.
    """

    _assert_children_install_the_block()
    world = _World(tmp_path)
    world.write_claude_code_login()
    world.write_anthropic_store(origin="claude-code")
    impatient = {**FAST_LOCKS, "stale": 1.0}
    with _Race(world) as race:
        race.start_endpoint(
            mode="claude",
            valid={CLAUDE_REFRESH_0: ACCOUNT_UUID},
            delay_seconds=2.5,
        )
        race.claude_code("claude-code", heartbeat=0.2)
        race.mcc(
            "mcc",
            wait_for=world.sync / "holding-claude-code",
            locks=impatient,
        )
        race.go()
        results = race.finish()
        posts = race.stop_endpoint()

    spend = _one_spend(posts, presented=CLAUDE_REFRESH_0)
    assert spend["client"] == "fake-claude-code"

    claude_code = results["claude-code"]
    assert claude_code["outcome"] == "refreshed", claude_code
    assert claude_code["posts"] == 1
    assert not claude_code["compromised"], "MCC broke or touched a live lock"
    assert claude_code["held_seconds"] > impatient["stale"], claude_code
    assert claude_code["heartbeats"] >= 5, claude_code

    mcc = results["mcc"]
    _assert_mcc_ok(mcc)
    assert mcc["posts"] == 0, mcc
    assert "shared:waited" in mcc["decisions"], mcc
    assert mcc["elapsed"] > impatient["stale"], mcc
    assert mcc["final_access_fp"] == spend["issued_access_fp"]

    block = world.claude_block()
    assert _fp(block["accessToken"]) == spend["issued_access_fp"]
    assert _fp(block["refreshToken"]) == spend["issued_refresh_fp"]
    [record] = _read_json(world.anthropic_store)["accounts"]
    assert _fp(record["accessToken"]) == spend["issued_access_fp"]
    world.assert_no_lock_left()


def test_two_mcc_processes_refresh_a_native_account_once(tmp_path: Path) -> None:
    """Rule 8: a NATIVE account is single-flight across MCC servers.

    Two MCC servers share MCC's own store holding an account MCC signed in
    itself. The lock beside the store (``anthropic_oauth.json.refresh.lock``)
    makes one of them POST; the other waits, re-reads the store under the
    lock and adopts. Claude Code's files and locks are never involved.
    """

    _assert_children_install_the_block()
    world = _World(tmp_path)
    world.write_anthropic_store(origin="mcc")
    with _Race(world) as race:
        race.start_endpoint(
            mode="claude",
            valid={CLAUDE_REFRESH_0: ACCOUNT_UUID},
            delay_seconds=CONTESTED_POST_SECONDS,
        )
        race.mcc("mcc-a")
        race.mcc("mcc-b")
        race.go()
        results = race.finish()
        posts = race.stop_endpoint()

    spend = _one_spend(posts, presented=CLAUDE_REFRESH_0)
    for result in results.values():
        _assert_mcc_ok(result)
        assert result["credential_mode"] == "native", result
    loser, winner = sorted(results.values(), key=lambda result: result["posts"])
    assert (loser["posts"], winner["posts"]) == (0, 1), results
    assert "native:refreshed" in winner["decisions"], winner
    assert "native:waited" in loser["decisions"], loser
    assert loser["final_access_fp"] == winner["final_access_fp"]
    assert winner["final_access_fp"] == spend["issued_access_fp"]

    store = _read_json(world.anthropic_store)
    assert _fp(store["accessToken"]) == spend["issued_access_fp"]
    assert _fp(store["refreshToken"]) == spend["issued_refresh_fp"]
    [record] = store["accounts"]
    assert record["origin"] == "mcc"
    assert _fp(record["refreshToken"]) == spend["issued_refresh_fp"]
    # A native account owns no file of Claude Code's and takes none of its
    # locks.
    assert sorted(path.name for path in world.claude.iterdir()) == []
    world.assert_no_lock_left()


def test_codex_without_a_lock_never_writes_an_older_or_foreign_token(
    tmp_path: Path,
) -> None:
    """Rule 6: with no lock to join, MCC compares-and-swaps or adopts.

    MCC imports Codex's login (SHARED; the import itself must never POST),
    then refreshes the expired token for a real request. The endpoint holds
    its answer until the fake Codex has had its go at ``auth.json``:

    * ``none`` -- the control: Codex touches nothing, and MCC's write-back
      lands, keeping ``account_id`` and every other key;
    * ``newer`` -- Codex rewrote the file with a newer token for the same
      account inside MCC's POST window: MCC adopts it and never writes its
      own, now older, rotation over it;
    * ``foreign`` -- Codex switched accounts inside the window: MCC adopts
      the new account and never writes the old identity's token.

    The three machines race side by side.
    """

    _assert_children_install_the_block()
    scenarios = ("none", "newer", "foreign")
    worlds = {scenario: _World(tmp_path / scenario) for scenario in scenarios}
    originals = {
        scenario: world.write_codex_login() for scenario, world in worlds.items()
    }
    outcomes: dict[str, tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]] = {}
    with contextlib.ExitStack() as stack:
        races = {
            scenario: stack.enter_context(_Race(world))
            for scenario, world in worlds.items()
        }
        for scenario, race in races.items():
            race.start_endpoint(
                mode="codex",
                valid={CODEX_REFRESH_0: CODEX_ACCOUNT},
                hold_until=race.world.endpoint / "release",
            )
            race.mcc("mcc", mode="codex", codex_import=True)
            race.codex_writer("codex", scenario=scenario)
        for race in races.values():
            race.go()
        for scenario, race in races.items():
            outcomes[scenario] = (race.finish(), race.stop_endpoint())

    for scenario, (results, posts) in outcomes.items():
        world = worlds[scenario]
        mcc, codex = results["mcc"], results["codex"]
        spend = _one_spend(posts, presented=CODEX_REFRESH_0)
        _assert_mcc_ok(mcc)
        assert mcc["posts_before_go"] == 0, "importing from Codex refreshed"
        assert mcc["posts"] == 1, mcc
        assert codex["saw_inflight"], f"{scenario}: the POST window was not real"
        document = _read_json(world.auth_json)
        tokens = document["tokens"]
        store = _read_json(world.chatgpt_store)
        records = {record["id"]: record for record in store["accounts"]}
        world.assert_no_lock_left()

        if scenario == "none":
            assert not codex["wrote"]
            assert "shared:refreshed+wrote-back:codex" in mcc["decisions"], mcc
            assert _fp(tokens["access_token"]) == spend["issued_access_fp"]
            assert _fp(tokens["refresh_token"]) == spend["issued_refresh_fp"]
            assert tokens["account_id"] == CODEX_ACCOUNT
            assert document["OPENAI_API_KEY"] is None
            assert document["last_refresh"] == originals[scenario]["last_refresh"]
            assert mcc["final_access_fp"] == spend["issued_access_fp"]
            assert mcc["account_id"] == CODEX_ACCOUNT
            continue

        # Codex wrote inside the window: the file is byte-for-byte what Codex
        # wrote, MCC never wrote Codex's file at all (not even a backup), and
        # MCC serves Codex's token -- its own rotation is discarded.
        assert codex["wrote"], codex
        assert (
            hashlib.sha256(world.auth_json.read_bytes()).hexdigest()
            == (codex["file_sha"])
        ), f"{scenario}: MCC wrote over Codex's newer file"
        assert list(world.codex.glob("auth.json.bak-*")) == []
        assert _fp(tokens["access_token"]) != spend["issued_access_fp"]
        assert _fp(tokens["refresh_token"]) != spend["issued_refresh_fp"]
        assert mcc["final_access_fp"] == codex["access_fp"], mcc
        assert mcc["account_id"] == codex["account_id"], mcc
        assert (
            _fp(records[codex["account_id"]]["tokens"]["access_token"])
            == (codex["access_fp"])
        )
        if scenario == "newer":
            assert tokens["account_id"] == CODEX_ACCOUNT
            assert "shared:superseded:codex" in mcc["decisions"], mcc
        else:
            assert tokens["account_id"] == FOREIGN_ACCOUNT
            assert "shared:identity-changed" in mcc["decisions"], mcc
            assert CODEX_ACCOUNT not in records, "the old identity was kept"
