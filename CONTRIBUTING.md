# Contributing

Thanks for helping improve My Claude Code. Keep changes focused, test the behavior you change, and preserve the public Claude Code and Codex workflows.

## Before Opening A Pull Request

- Open an issue before proposing README changes.
- Do not open Docker integration pull requests.
- For bugs, include every model mapping, the active model when the failure occurred, the complete error, and reproducible steps.
- Add focused tests for behavior changes and relevant edge cases.
- Read [tests/README.md](tests/README.md) before adding a test that touches the filesystem, the registry, a subprocess or a socket. The suite is hermetic and enforces it; a test that reaches the real machine fails rather than damaging it.
- Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing package boundaries, providers, protocol conversion, launchers, or messaging.

## Development Setup

Install [uv](https://docs.astral.sh/uv/) and Python 3.14, then run directly from the checkout:

```bash
git clone https://github.com/FiredMosquito831/my-claude-code.git
cd my-claude-code
uv python install 3.14.0
uv run mcc-server
```

Use `uv run` for Python commands. Do not run the project with a global Python interpreter.

## Quality Checks

Run the complete local CI sequence before opening a pull request:

```bash
./scripts/ci.sh
```

```powershell
.\scripts\ci.ps1
```

Useful iteration flags are `--only`, `--skip`, and `--dry-run` on macOS/Linux, or `-Only`, `-Skip`, and `-DryRun` in PowerShell.

Individual repair and test commands:

```bash
uv run ruff format
uv run ruff check --fix
uv run ty check
uv run pytest -v --tb=short
```

GitHub CI runs Ruff in check-only mode and also bans `# type: ignore`, `# ty: ignore`, and legacy annotation workarounds. Fix underlying typing and import-boundary problems instead of suppressing them.

### The dashboard's jsdom suite

`tests/api/test_admin_static_jsdom.py` is the only test that executes the dashboard's `admin.js`. It runs the real script in [jsdom](https://github.com/jsdom/jsdom) through a `node` subprocess, so it needs Node and jsdom installed — otherwise it skips itself, and a skipped suite proves nothing about the page.

```bash
npm ci --prefix tests            # installs the pinned jsdom from tests/package-lock.json
uv run pytest tests/api/test_admin_static_jsdom.py -n 0
```

- Run the file **alone** and with `-n 0`. Every test in it reads one of two module-scoped harness runs; xdist would build a copy of the fixture per worker, and concurrent jsdom runs starve each other of CPU, miss their own debounce windows and report the half-rendered page as hundreds of script errors. The harness takes a lock (`tests/api/.admin_jsdom.lock`) and a second concurrent run against the same tree refuses rather than producing nonsense.
- One harness run is about a minute on a CI runner and three to four on a loaded laptop. The subprocess bound is `MCC_JSDOM_TIMEOUT_SECONDS` (default 900); the payload reports `harnessWallMs` so the bound can be argued from measurement.
- `npm install` is fine locally, but `tests/package-lock.json` is the pin CI installs from — change the version in `tests/package.json` and regenerate the lockfile together.
- CI runs this in its own `jsdom` job, with `MCC_CI=1` so a missing `node` or a missing jsdom **fails** instead of skipping. The ordinary `pytest` job excludes the file for exactly that reason.

## Project Standards

- Target Python 3.14 and rely on native lazy annotations; do not add `from __future__ import annotations`.
- Python 3.14 supports multiple exception types without parentheses, such as `except TypeError, ValueError:`.
- Keep shared Anthropic protocol behavior under `src/my_claude_code/core/anthropic/` rather than importing utilities from another provider.
- Keep provider-specific configuration in the provider that owns it.
- Remove dead compatibility code when completing migrations unless preserving a published interface is explicitly required.

## Versioning

Changes to runtime code, packaging, dependencies, or install/CI scripts require a semantic version bump in `pyproject.toml` and a matching `uv lock` update in the same commit. Documentation, tests, smoke coverage, and repository configuration do not require a version bump by themselves.

See [ARCHITECTURE.md](ARCHITECTURE.md) for extension checklists and the full system design.
