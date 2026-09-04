# The test suite's hermeticity contract

**A test run must leave this machine exactly as it found it.** Not "usually".
Not "unless a fixture argument was forgotten". The suite has twice deleted a
developer's real Windows autostart registration and repeatedly rewritten files
in their real `~/.fcc`, from a green run, so the rule is enforced by
`tests/support/hermetic.py` rather than by review.

Everything below is installed for the whole session from `pytest_configure` and
re-exported by `tests/conftest.py`. No test opts in; a test can only opt *out*,
explicitly, with a marker.

## What is guaranteed

| Guarantee | How |
| --- | --- |
| Every test has its own `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` under `tmp_path`, and no `MCC_CONFIG_DIR` | `isolate_the_machine` (autouse) |
| No write can land anywhere in the real user profile -- `~/.fcc`, `~/.mcc`, `~/.fcc-old`, `~/.claude`, `~/.claude.json`, `~/.codex`, `%APPDATA%`, `%LOCALAPPDATA%`, the Start Menu, `~/.config`, `~/.local`, `~/Library`, and every path inside any of them | `builtins.open`/`io.open`, `os.open`, `os.replace`/`rename`/`link`/`symlink`, `os.mkdir`/`makedirs`/`remove`/`unlink`/`rmdir`/`truncate`, `Path.mkdir`, `shutil.copy*`/`move`/`rmtree` |
| No test reaches the Windows registry. Reads answer from an in-memory store; writes fail | `sys.modules["winreg"]` is `HermeticWinreg` for the session |
| No test launches a browser, a coding-agent CLI, a package manager or a machine-state tool | `subprocess.Popen.__init__` |
| No test binds My Claude Code's default server port | `socket.socket.bind` |
| No test resolves the config directory into the real home | `pytest_runtest_teardown`, checked before the fixture finalisers clear it |

The protected root is the whole user profile, not a list of config
directories: My Claude Code's subject matter *is* other tools' configuration
(`~/.codex`, `~/.claude.json`, `~/.crush`, `~/.gemini`), and an enumeration goes
stale the next time a harness is added. Two carve-outs make that workable and
they are the only ones: the temporary directories (`tmp_path` lives under
`%LOCALAPPDATA%\Temp` on Windows) and the checkout itself (which lives under
`$HOME` on a Linux CI runner). Both are captured before any redirect.

## Opting out

Three markers, each of which still keeps the test off the real machine except
where that is impossible:

* `@pytest.mark.touches_registry` -- the registry write is the thing under
  test. It is recorded against `HermeticWinreg`, which is in memory. Nothing
  reaches the real registry, marker or not. For the Windows autostart branch
  prefer the `fake_winreg` fixture (`tests/support/fake_winreg.py`), which is
  what `tests/config/test_desktop.py` and `tests/cli/test_desktop_rtk.py` use.
* `@pytest.mark.spawns_process` -- the launch of a denied executable is the
  thing under test. `python`, `pwsh`/`powershell`, `node`, `sh`, `git`, `cmd`
  and `tasklist` are allowed without it; the suite runs those on purpose.
* `@pytest.mark.binds_reserved_port` -- the test must bind port 8082. Almost
  certainly it must not: ask the OS for a free port by binding 0 and reading
  `getsockname()` back, as `tests/cli/test_port_diagnostics.py` does.

There is no marker for writing into the real home. There is no correct reason
to do it.

## When the guard fails your test

The failure names the operation, the path and the real-machine root it is
inside. It is almost never a false positive; it is almost always one of:

* a module-level path resolved before the redirect, or cached across tests;
* a fan-out. The commonest shape by far: a fixture overrides *one* of a set of
  paths and the code under test then resolves the other eleven for real. That
  is what rewrote the developer's Crush and Cline catalogues from nine tests
  that all took `tmp_path` and looked isolated in review;
* a platform branch nobody meant to run -- `native_origin()` returns
  `"windows"` under the suite, so Windows autostart code executes for real.

Fix the test, not the guard. `HermeticityViolation` derives from
`BaseException` on purpose: the application catches `Exception` in the very
places that do this damage, and a guard that production code can swallow into a
`logger.warning` is not a guard.

## The guard's own tests

`tests/contracts/test_hermetic_guard.py` runs a deliberately leaking test under
a **child** pytest, one per leak class, and asserts that the child run fails and
names the artefact. Asserting in-process would only test the interceptor's
return value, not its effect on a test report -- which is exactly how the
previous config-directory assertion stayed dead code across three releases while
1619 tests resolved the developer's real home. The child is handed a stand-in
profile under `tmp_path`, so even the tests of the guard touch nothing real.

## What is *not* covered

The suite still reads the ambient process environment: a `Settings()` built with
no explicit values picks up whatever credentials the developer has exported.
Five tests were pinned against that (see
`tests/support/websearch_credentials.py`), but the general rule stands -- state
the credentials a case needs rather than inheriting them.
