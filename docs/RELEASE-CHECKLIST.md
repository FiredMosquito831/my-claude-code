# Release Cutover Checklist â€” My Claude Code (MCC) v5

> **Prep only. Do NOT execute during the rebrand integration.** This document
> records the plan for the repo/release cutover so the actual release can be
> performed deliberately. Nothing here changes live infrastructure.

## 1. What rebranded vs. what stayed

| Surface | State |
| --- | --- |
| Package | `free_claude_code` â†’ `my_claude_code` (rename done in v5.0.0) |
| Product name | Free Claude Code â†’ **My Claude Code** (MCC) |
| PyPI / wheel name | `my-claude-code` |
| Server command | `mcc-server` (primary); `fcc-server` kept as alias |
| Launchers | `mcc-claude`/`mcc-codex`/`mcc-pi`/`mcc-claude-old` (primary); `fcc-*` kept |
| **GitHub repo slug** | **UNCHANGED** â€” `free-claude-code` (do not rename) |
| **Release repo** | **UNCHANGED** â€” `FiredMosquito831/my-claude-code` (`RELEASE_REPO`) |
| **FCC_* env vars** | **UNCHANGED** (`FCC_ENV_FILE`, `FCC_OPEN_BROWSER`, `FCC_SMOKE_TARGETS`â€¦) |
| **Proxy port / token** | **UNCHANGED** â€” `:8082`, token `freecc` |
| **Model ids / provider ids** | **UNCHANGED** â€” `claude-3-freecc-*`, Codex id `fcc`, Pi scope `free-claude-code/**` |
| **Config dir** | **UNCHANGED** â€” `.fcc` |
| **Display name constant** | **UNCHANGED** â€” `LEGACY_DISPLAY_NAME = "Free Claude Code"` |

## 2. Dual-repo mirroring plan

The product rebrand does **not** rename the GitHub repository. The repo stays
`free-claude-code`; only the package, product name, and command family rebrand.
Two remotes stay in play, and the roles below are the ones actually in use —
this section previously described them the other way round:

- `FiredMosquito831/my-claude-code` — **development and release repo.** All
  branches, pull requests, merges and GitHub releases happen here, and
  `RELEASE_REPO` points at it, so this is what the installer and the "update
  available" banner read.
- `Alishahryar1/free-claude-code` — **upstream, for provenance.** Never pushed
  to and never PR'd against. Note that `gh` defaults to it, so every `gh`
  command needs an explicit `--repo`.

Cutover steps (run at release time, NOT now):

1. Ensure both remotes are configured locally:
   `git remote -v` should list the development remote (FiredMosquito831) and
   upstream (Alishahryar1).
2. Land the v5 rebrand on the integration branch, pass CI (ruff-format,
   ruff-check, ty, pytest), then merge to `main`.
3. Push `main` to both remotes so they stay mirrored:
   `git push origin main && git push <release-remote> main`.
4. Tag the release (`git tag v5.0.0`) and push tags to both remotes.
5. Build and publish the wheel to the release repo (see Â§3). The install
   scripts already fetch from `FiredMosquito831/my-claude-code/main`, so the
   published wheel there is what end users receive â€” repo URL needs no change.

> Do **not** rename the repo or migrate issues/PRs. The `FCC_*` env vars,
> `:8082`, `freecc` token, and `fcc-*` aliases are published contracts; renaming
> the repo would force a breaking change for every existing install.

## 3. Building both wheels (release helper note)

`pyproject.toml` ships **one** wheel that contains **both** packages:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/my_claude_code", "src/free_claude_code"]
```

- `src/my_claude_code` â€” the canonical implementation.
- `src/free_claude_code` â€” the compatibility shim that re-exports
  `my_claude_code`, so the legacy `fcc-*` command family and `free-claude-code`
  entry point keep resolving.

Build sequence (release time):

```bash
uv lock                                  # reflect the version bump
uv build --wheel                         # produces dist/my_claude_code-*.whl
```

Verify both packages landed in the wheel:

```bash
python -m zipfile -l dist/my_claude_code-*.whl | grep -E "my_claude_code/|free_claude_code/"
```

Both should appear. Then upload to the release repo's release (the existing
publish flow), and the checksum-verified in-place update path serves it.

## 4. Dashboard screenshots (manual step)

The admin dashboard screenshots under `assets/` (e.g. `admin-analytics.png`,
`admin-model-config.png`) should be re-captured against the rebranded console
to reflect the new "My Claude Code" header, "MC" brand mark, and any theme
changes. **This is a manual step** â€” capture from a locally running
`mcc-server` (default `:8082`) in a browser. Do **not** point any automated
screenshot tool at the live WSL `:8082` instance.

Captured views to refresh:
- `assets/admin-page.png`, `assets/admin-version.png`, `assets/admin-analytics.png`
- `assets/admin-model-config.png`, `assets/admin-websearch.png`
- `assets/admin-key-performance.png`, `assets/admin-requests.png`
- `assets/admin-messaging.png`, `assets/admin-update-banner.png`

## 5. Pre-release verification

- [ ] `uv run pytest tests/test_brand_contract.py` passes (rebrand + kept contracts)
- [ ] `uv run ruff format --check && uv run ruff check && uv run ty check`
- [ ] `uv run pytest` green
- [ ] Both remotes mirrored, tags pushed
- ] Wheel contains `my_claude_code/` and `free_claude_code/`
- [ ] Dashboard screenshots refreshed from local `mcc-server`
- [ ] README quickstart uses `mcc-server`; `fcc-server` still documented as alias

## 6. Wheel end-to-end guard (`wheel-e2e.yml`)

`tests/api/test_docs_bundle_wheel.py` only builds a real wheel behind
`MCC_WHEEL_TESTS=1`, and ordinary CI must never set that flag (a nested
`uv build` inside `uv run pytest` hung CI until the fixture was gated).
`.github/workflows/wheel-e2e.yml` is therefore the flag's only setter:

- **What it runs**: `MCC_WHEEL_TESTS=1 pytest tests/api/test_docs_bundle_wheel.py`
  on `windows-latest` -- a real `uv build --wheel`, then the bundle
  assertions inside it (every curated document present, non-empty,
  uniquely named, and no developer-only documents shipped).
- **When**: Wednesdays 02:41 UTC and manual *Run workflow* dispatches.
  Never on `push`/`pull_request`, so it cannot gate merges.
- **What a red run means**: the *shipped wheel* is missing, renaming, or
  truncating a curated document -- every installed user gets an empty
  Docs page while source checkouts render fine. Fix the
  `[tool.hatch.build.targets.wheel.force-include]` table (see section 3),
  then re-dispatch the workflow before releasing.
