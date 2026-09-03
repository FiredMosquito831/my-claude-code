# CHECKPOINT — 6.40.0 `~/.mcc` config dir (feat/mcc-config-dir)

Written at coordinator request (quota limit). Nothing committed, nothing pushed,
no PR, no test run, no scratch process started (so nothing to stop).

- Worktree: `C:/Users/fgghk/Downloads/FCC_PATCH/mcc-configdir`
- Branch: `feat/mcc-config-dir`, HEAD `359b4013` (= v6.39.2), **`git status --short` is EMPTY**
- Scratchpad: `C:/tmp/claude/C--Users-fgghk-Downloads-FCC-PATCH/42dea513-e401-40ef-805f-e3ca86af26ce/scratchpad/configdir/`

## DONE

**No file in the worktree has been modified yet.** All work so far is (a) a
completed read/design pass over the codebase and (b) one finished code artifact
staged in the scratchpad.

Scratchpad artifacts (both real deliverables, ready to apply):

| File | Purpose |
|---|---|
| `scratchpad/configdir/paths_head.py` | **Finished replacement for the top ~120 lines of `src/my_claude_code/config/paths.py`** (module docstring through `config_dir_path`, plus new helpers). Contains: the new constants (`MCC_CONFIG_DIRNAME=".mcc"`, `LEGACY_CONFIG_DIRNAME=".fcc"`, `RETIRED_CONFIG_DIRNAME=".fcc-old"`, `CONFIG_DIR_ENV="MCC_CONFIG_DIR"`, `FCC_CONFIG_DIRNAME` kept as an alias of the legacy name), `LEGACY_REQUEST_LOG_COLUMNS`, the `LegacyHomeHealth` / `ConfigDirResolution` dataclasses, `display_path`, the four health checks, `check_legacy_home`, the pure `resolve_config_dir(env, home)`, the per-process cache (`config_dir_resolution`, `reset_config_dir_cache`), `config_dir_path`, `legacy_config_dir_path`, `retired_config_dir_path`, `request_log_path`. |
| `scratchpad/configdir/patch.py` | Edit helper: reads `newline=None`, writes `newline='\n'`, asserts an exact occurrence count per replacement (the CRLF-safe editing rule). Driven by a JSON spec: `[{"path":..., "edits":[{"old":..., "new":..., "count":1}]}]`. |

## IN PROGRESS

**Step: apply `scratchpad/configdir/paths_head.py` into
`src/my_claude_code/config/paths.py`.** It replaces everything from the module
docstring down to and including the existing `config_dir_path()` definition
(current file lines 1–40). Everything from `managed_env_path()` onward is kept
unchanged, except three docstring rewordings still to do in the same file:

- `legacy_env_paths()` docstring says "migrated to `~/.fcc/.env`" → `~/.mcc/.env`
- `harness_catalogue_path()` docstring says "Always under `~/.fcc`" → "Always under the resolved config directory"
- `chatgpt_oauth_auth_path()` / `anthropic_oauth_managed_store_path()` docstrings say "FCC's" → "MCC's"

**This edit was deliberately NOT applied**, because on its own it leaves the
tree broken: it depends on the `settings.py` lazy-`env_file` change described
under REMAINS §2, without which the health check cannot run.

## REMAINS (in order)

1. **`config/paths.py`** — apply `paths_head.py` + the docstring rewordings above.
2. **`config/settings.py` — lazy dotenv list (BLOCKER for 1).**
   `model_config = SettingsConfigDict(env_file=settings_env_files(), ...)` is
   evaluated at *class-body* time, i.e. `config_dir_path()` runs during the
   import of `config.settings`. That makes the health check's
   `from .settings import Settings` unusable at that moment (partially
   initialised module). Fix: add a `LazyEnvFiles(Sequence[Path])` to
   `config/env_files.py` that calls `settings_env_files()` on
   `__iter__`/`__len__`/`__getitem__`, and use `env_file=LazyEnvFiles()`.
   `configured_env_files()` already iterates it, so `env_file_override()` keeps
   working. **Risk to watch:** tests that monkeypatch `HOME` and previously got
   the import-time snapshot will now really read the tmp `.env` — compare the
   full-suite result against the 14–15 known baseline failures.
3. **`config/settings.py` — env aliases + new setting.**
   `open_admin_browser` → `AliasChoices("MCC_OPEN_BROWSER", "FCC_OPEN_BROWSER")`;
   `FCC_ENV_FILE` → `MCC_ENV_FILE` first in `config/env_files.py:explicit_env_path`;
   one deprecation log line per process listing every legacy `FCC_*` name found;
   new `server_log_retain_files: int = Field(default=10, validation_alias="SERVER_LOG_RETAIN_FILES")`;
   add `request_log_path` as a **`@property`** (not a Field — a Field would trip
   `tests/contracts/test_every_setting_has_an_admin_field.py`) returning
   `paths.request_log_path()`.
4. **`config/admin/persistence.py:281`** — `_FCC_OWNED_ENV_PREFIX = "FCC_"` must
   become `_OWNED_ENV_PREFIXES = ("MCC_", "FCC_")` and the `key.startswith(...)`
   at ~`:317` must take the tuple, or `MCC_*` keys silently stop being saved.
5. **`core/request_log.py`** — delete `_FCC_CONFIG_DIRNAME = ".fcc"` (line 31).
   **Decision taken (see DEVIATIONS):** pass the path in rather than duplicate
   the rule. `default_request_log_path()` keeps its zero-arg shape but reads a
   process default set by `set_request_log_path()`; `store_from_settings()`
   passes `getattr(settings, "request_log_path", None)` straight through;
   `cli/commands.py:418 compact_log()` passes
   `default_request_log_path(config_dir_path())`-equivalent explicitly. Also
   export `required_request_columns()` =
   `frozenset(_REQUEST_INSERT_COLUMNS) - {c for c, _ in _ADDED_COLUMNS}` for the
   contract test. `tests/conftest.py:33-44` must switch from monkeypatching
   `default_request_log_path` to calling `set_request_log_path`.
6. **`config/logging_config.py`** — `_add_file_sink` currently hardcodes
   `retention=5`; take the cap from `SERVER_LOG_RETAIN_FILES` (default 10, `0` =
   keep all → `retention=None`), and add a startup sweep in `configure_logging`
   that deletes the oldest rotated `server.*.log` beyond the cap, never the
   current file, logging what it deleted. (The user has ~340 rotated 50 MB files
   despite `retention=5`, so the sweep is the part that actually fixes it.)
7. **New `cli/migrate_config_dir.py` + `mcc-migrate` / `fcc-migrate` console
   scripts** in `pyproject.toml [project.scripts]`. Behaviour: refuse unless
   `~/.mcc` is absent; single `os.replace(~/.fcc, ~/.mcc)`; on
   `PermissionError`/`OSError` report the likely holders (tray via
   `~/.fcc/desktop.lock`, the server, `mcc-*` launcher sessions, the deferred
   updater — PID/command-line scan on Windows via `tasklist`/`wmic`-free
   `netstat -ano` + process-name reasoning, POSIX without `lsof`), move nothing,
   and tell the user to close them and re-run. On success create an **empty**
   `~/.fcc-old/` holding only `RESTORE.txt` (literal one-line `Move-Item` / `mv`
   command + the date); if `~/.fcc-old` already exists, leave it and say so.
   Print "restart the server yourself" — never restart anything.
8. **`POST /admin/api/migrate-config-dir`** behind `require_loopback_admin` in
   `api/admin_routes.py`, calling the same function; plus a
   `GET`-side payload so the Get Started banner can render. Banner markup goes in
   `api/admin_static/index.html` near `#getStartedPanel` (line ~42) with the
   button wired in `admin.js`; text: "Your data lives in the legacy `~/.fcc`.
   Run `mcc-migrate` to move it to `~/.mcc`." plus the failed-check name when the
   legacy home was rejected.
9. **New admin field `SERVER_LOG_RETAIN_FILES`** in
   `config/admin/manifest.py` — put it in the **`diagnostics`** section right
   after `LOG_LEVEL` (~line 696), because rotation lives in `logging_config.py`
   which that card owns. `restart_required=True`.
10. **Installers/uninstallers**: verify `install.ps1`/`install.sh` create nothing
    under either dir; banners/docs say `~/.mcc` (`install.ps1:1212`,
    `install.sh:759`); `install.ps1:1076` hardcodes
    `Join-Path $env:USERPROFILE ".fcc"` for `app-icon.ico` → resolve through the
    same rule; `uninstall.ps1:18,238-261` and `uninstall.sh:11,25,143-211,249`
    purge `~/.mcc` **and** `~/.fcc` if present, keeping the refuse-while-running
    guard, and leave `~/.fcc-old` alone. `mcc-init` writes `.env` into the
    resolved dir (`cli/commands.py:342 init()`).
11. **Docs**: README, `docs/USAGE.md`, the in-dashboard Guide
    (`api/admin_static/index.html`), `ARCHITECTURE.md`, Get Started — `~/.mcc`
    everywhere plus a "Legacy `~/.fcc`" subsection and the `mcc-migrate`
    walkthrough. Extend `tests/contracts/test_docs_reference_drift.py` with a
    **fifth check** for config-dir paths (`~/.mcc` allowed; `~/.fcc` only inside
    the legacy subsections / `tests/contracts/docs_reference_allowlist.txt`).
12. **Tests** (names from the spec §7, adapted to the opt-in design):
    `tests/config/test_paths.py` (new: default, `MCC_CONFIG_DIR`, tilde),
    `tests/config/test_config_dir_resolution.py` (new: the four rules + the four
    health checks + both-exist warning + nothing-moved assertions),
    `tests/cli/test_migrate_config_dir.py` (new: rename, `.fcc-old/RESTORE.txt`,
    second run, refusal with an open handle + holder naming),
    `tests/config/test_env_aliases.py` (new: `FCC_OPEN_BROWSER` still works,
    `MCC_*` wins, one deprecation line, `persistence` saves `MCC_*`),
    `tests/config/test_logging_config.py` (extend: 15 fake rotated logs → 10
    kept, current untouched, `0` keeps all),
    `tests/contracts/test_config_dir_is_single_sourced.py` (new: `paths` vs
    `request_log` agree; `LEGACY_REQUEST_LOG_COLUMNS` == `required_request_columns()`;
    `CUSTOM_PROVIDERS_FILENAME` matches `provider_registry`'s; no `.fcc`/`.mcc`
    literal in `src/` outside the allowlist),
    plus the mechanical `tmp_path / ".fcc"` → `.mcc` sweep across
    `tests/api/*`, `tests/api/support.py:83-84`, `tests/api/admin_jsdom_harness.mjs`,
    `tests/config/*`, `smoke/*`, and the two settings contract tests
    (`test_settings_to_consumer_contract.py`, `test_every_setting_has_an_admin_field.py`).
13. **Version 6.40.0** in `pyproject.toml` + `uv lock --offline` in the same commit.
14. **Proofs a–i** in a redirected `HOME`/`USERPROFILE` under
    `scratchpad/configdir/`, browser drive of the banner + button
    (chrome-devtools-cli; screenshots to
    `C:/Users/fgghk/Downloads/FCC_PATCH/screenshots-configdir/`), then commit,
    PR via `--body-file`, `gh pr checks --watch`, squash merge, wheel from a
    fresh detached worktree at `fork/main`, `gh release create v6.40.0`, and
    **remove every worktree I created** (`mcc-configdir` + the release worktree).

## DECISIONS / DEVIATIONS FROM THE BRIEF

1. **`core/request_log.py`: "pass the resolved path in", not "keep the duplicate."**
   The brief let me pick. `config` may import nothing and `core` may import
   nothing (`tests/contracts/test_import_boundaries.py`:
   `ALLOWED_PACKAGE_DEPENDENCIES = {"config": set(), "core": set()}`), so a
   mirrored rule in `core` would have to duplicate the *entire* health check,
   not just a dirname literal. Passing the path in from `config`/`cli` deletes
   the `.fcc` literal from `core` outright. Cost: `tests/conftest.py` and four
   test call sites that use the bare `get_request_log_store()` need updating.
2. **`config/settings.py` gains a lazy dotenv list** (REMAINS §2). Not asked for,
   but required: without it `config_dir_path()` runs during the import of
   `config.settings` and the `Settings()` half of the health check cannot
   execute. It is also strictly more correct — the current code snapshots the
   dotenv paths at import time.
3. **Health check `.env` rule is strict about absence.** A legacy `~/.fcc` with
   no `.env` at all fails the `env` check and is left untouched while a fresh
   `~/.mcc` is created. The brief says ".env present and `Settings()` builds
   from it", so absence is a failure.
4. **A legacy `requests.db` with no `requests` table at all passes.** That is an
   empty file the store creates its schema in on open; only a *present but
   short* table fails.
5. **`SERVER_LOG_RETAIN_FILES` goes on the Diagnostics card**, not
   Analytics/Request-log-storage: rotation is owned by `config/logging_config.py`,
   whose only dashboard field today (`LOG_LEVEL`) lives in `diagnostics`.
6. **`FCC_CONFIG_DIRNAME` is kept as an alias** of `LEGACY_CONFIG_DIRNAME` rather
   than deleted, so existing importers do not break in the same PR.

## FACTS RE-CONFIRMED THIS SESSION (so they need not be re-derived)

- `config_dir_path()` today is `Path.home() / ".fcc"` with **no** env override
  (`config/paths.py:37-40`). CONFIRMED.
- `core/request_log.py:31` `_FCC_CONFIG_DIRNAME = ".fcc"`, used only by
  `default_request_log_path()` at `:1051`, whose only callers are
  `cli/commands.py:418` and `request_log.py:4852` (the `db_path=None` fallback in
  `get_request_log_store`). CONFIRMED.
- `config/logging_config.py:126-137` already passes `retention=5` to loguru, and
  the user still has ~340 rotated files — so the **startup sweep**, not the
  rotation parameter, is the part that matters. CONFIRMED (code read).
- `settings.py:1583-1586` `model_config` snapshots `settings_env_files()` at
  import time. CONFIRMED.
- `settings.py:1194` `open_admin_browser` uses `validation_alias="FCC_OPEN_BROWSER"`.
  CONFIRMED.
- `config/admin/persistence.py:281` `_FCC_OWNED_ENV_PREFIX = "FCC_"`, consumed by
  `unmanaged_env_values()` (~`:317`). CONFIRMED.
- `tests/conftest.py:33-44` monkeypatches `request_log.default_request_log_path`;
  `:100-112` monkeypatches `config_dir_path` into `tmp_path/"fcc-config"` for
  `provider_registry` and `models_dev` only. CONFIRMED.
- Ruff `select` does **not** include `PLC0415`, so the one function-level import
  in `paths.py` will not be flagged. CONFIRMED (`pyproject.toml [tool.ruff.lint]`).
- Get Started panel is `#getStartedPanel` at `api/admin_static/index.html:42`;
  onboarding routes at `api/admin_routes.py:886-913` already use
  `require_loopback_admin`. CONFIRMED.
- Live server is PID **29024** on 127.0.0.1:8082 (`netstat -ano`). Untouched, GETs
  only, never restarted. CONFIRMED.
