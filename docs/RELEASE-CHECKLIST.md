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

The admin dashboard screenshots go stale whenever a view is rebuilt, not only
at a rebrand. **This is a manual step** — capture from a locally running
`mcc-server` in a browser. Do **not** point any automated screenshot tool at
the live WSL `:8082` instance.

**Capture against an isolated scratch instance, never your own `~/.fcc`.** The
6.39.1 docs pass used this recipe and it is the one to repeat:

1. Build a scratch `HOME`: copy `~/.fcc/.env` key-by-key, replacing every
   secret with a fake of the same shape (nothing beginning `sk-`), and copy
   `custom_providers.json` with its keys faked the same way.
2. `VACUUM INTO` a copy of `~/.fcc/logs/requests.db` — safe against the live
   writer, read-only on the source — so Analytics, Models and Coding agents
   render real-looking data.
3. Copy the `models-dev.json` cache read-only so the resolution ladder resolves
   offline.
4. Run this worktree's server with `HOME`/`USERPROFILE` pointed at the scratch
   directory and `PORT=8199 HOST=127.0.0.1` explicit. Check `netstat` first so
   you cannot collide with `:8082`.

**Every dashboard screenshot was re-captured during the 6.39.1 pass**, with one
documented exception below. The sidebar version badge reads `v6.39.0` or
`v6.39.1` depending on when in the pass a shot was taken; that is cosmetic and
not worth a re-shoot. These were re-captured:

- `admin-page.png`, `admin-version.png` — the shell and the running version.
- `admin-get-started.png` — the onboarding checklist, including the Coding
  agents step added in 6.39.1.
- `admin-providers.png`, `admin-custom-provider.png` — the provider grid, and a
  custom provider's card with its base URL, key pool and **Refresh models**.
- `admin-models.png`, `admin-models-bulk.png`, `admin-model-config.png` — the
  Models page with per-model sources and tier chips, the bulk visibility
  controls, and the tier mapping.
- `admin-coding-agents.png`, `admin-coding-agent-card.png` — every registered
  harness with its commands, generated file and tiers; Antigravity shown as not
  servable.
- `admin-analytics.png`, `admin-requests.png`, `admin-analytics-harness.png`,
  `admin-request-detail.png`, `admin-request-contradiction.png` — Analytics,
  including the harness attribution column, filter and breakdown.
- `admin-limits.png`, `admin-limits-calculator.png`,
  `admin-credential-health.png` — Limits & Resilience, the deadline calculator,
  and per-key health.
- `admin-websearch.png`, `admin-websearch-analytics.png`, `admin-messaging.png`,
  `admin-key-performance.png` — the remaining pages.

**The one exception: `assets/admin-update-banner.png`.** The update banner only
renders when a newer release actually exists, which an offline scratch instance
running the version under development cannot produce. It is not reachable from
the capture recipe above, so it still shows an older dashboard. Refresh it
opportunistically the next time you see a real update banner on your own
install; do not fabricate one.

Three shots are of *other* products' UIs and are not captured from MCC at all:
`cc-model-picker.png`, `claude-desktop-developer-menu.png`,
`claude-desktop-gateway-config.png` and `codex-model-picker.png`. They go stale
when that vendor changes its UI, not when MCC does.

**Two rules that have each broken a release:**

- **A screenshot lives in two places.** The in-dashboard Guide serves its own
  copies from `src/my_claude_code/api/admin_static/img/`; `README.md` and
  `docs/USAGE.md` read `assets/`. Refreshing a screenshot means writing the
  same filename into **both** directories, and a screenshot the Guide embeds
  for the first time has to be added to both. `tests/contracts/
  test_admin_asset_wiring.py` checks that every image the Guide references
  exists; it does not check that the two copies match.
- **No secret may be legible, not even a truncated one.** Before committing,
  look at every re-captured PNG and confirm no string beginning `sk-` appears
  anywhere — a masked key *label* such as `sk-n…oKuN` still leaks the prefix
  and the tail, and one shipped this way until 6.39.1. Never capture an
  expanded request or response body; MCC's own "REQUEST BODY SENT (NO PROMPT
  TEXT)" block, which shows structural fields and character counts only, is
  what belongs in a screenshot.

## 5. Pre-release verification

- [ ] `uv run pytest tests/test_brand_contract.py` passes (rebrand + kept contracts)
- [ ] `uv run ruff format --check && uv run ruff check && uv run ty check`
- [ ] `uv run pytest` green
- [ ] Both remotes mirrored, tags pushed
- ] Wheel contains `my_claude_code/` and `free_claude_code/`
- [ ] Dashboard screenshots refreshed from a scratch `mcc-server` (§4), written to **both** image directories, and scanned for `sk-`
- [ ] README quickstart uses `mcc-server`; `fcc-server` still documented as alias
- [ ] After publishing: the **Desktop shell release** workflow (`shell-release.yml`) ran automatically on `release: published`. Check all four legs are green and that the six assets appear on the release within about 15 minutes (§7)
- [ ] Windows installer smoked by hand from the published file: digest checked, `/VERYSILENT` install, launched from the Start Menu, uninstalled from Apps & Features, and `mcc-server` still working afterwards (§7)

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


## 7. The desktop shell's release assets (`shell-release.yml`)

The desktop shell is a Rust binary, so it is not in the wheel. It rides the
**same GitHub release** (decision Q6): publishing a release fires
`.github/workflows/shell-release.yml` on `release: published`, which builds the
shell on four runners and attaches six more assets to the release you just
made. Nothing is required of the releaser except to look, and -- once, on
Windows -- to install the installer.

**The step, at release time:** publish the release with the wheel as usual, then
open Actions -> *Desktop shell release*. There is one run per release. Confirm
all four legs are green, and that the assets are on the release page within
about 15 minutes (a cold cargo cache is the long pole; a warm one is nearer
five). Then check the release page carries exactly these six:

| Asset | Runner | Rust target |
| --- | --- | --- |
| `MyClaudeCode-windows-x86_64.zip` | `windows-latest` | `x86_64-pc-windows-msvc` |
| `MyClaudeCode-Setup-windows-x86_64.exe` | `windows-latest` | (the same binary, in an Inno Setup 6 installer) |
| `MyClaudeCode-linux-x86_64.tar.gz` | `ubuntu-22.04` | `x86_64-unknown-linux-gnu` |
| `MyClaudeCode-macos-aarch64.tar.gz` | `macos-latest` | `aarch64-apple-darwin` |
| `MyClaudeCode-macos-x86_64.tar.gz` | `macos-15-intel` | `x86_64-apple-darwin` |
| `SHA256SUMS-desktop-shell.txt` | the aggregating job | -- |

The names carry no version (decision Q5). The checksum file is plain
`sha256  filename` lines, so `sha256sum -c SHA256SUMS-desktop-shell.txt` next to
the downloaded archives is the whole verification, and the Python-side pin
(spec S4) parses the same file.

**If a leg is red, the release is still fine.** The wheel is already published
and the update path is untouched -- `_select_wheel_asset` returns the first
asset whose name ends `.whl` and cannot see an archive or a checksum file, which
`tests/application/test_release_updates_ignores_shell_assets.py` pins. Fix the
platform, then re-run just the shell:

```bash
gh workflow run shell-release.yml --repo FiredMosquito831/my-claude-code -f tag=v6.43.0
```

The dispatch takes the tag of an existing release and uploads with `--clobber`,
so re-running is safe and idempotent. Do not cut a new version to fix a shell
build.

A second, optional input separates *what is built* from *where it goes*. `ref`
defaults to `tag`, which is what a real release wants: the tag carries the
source that release is made of. To backfill a release published before the
shell existed -- or before a fix to the workflow itself landed -- name a newer
commit explicitly:

```bash
gh workflow run shell-release.yml --repo FiredMosquito831/my-claude-code \
    -f tag=v6.43.0 -f ref=main
```

**Smoke the Windows installer once per release.** The workflow already runs
`desktop-shell/smoke/windows-installer.ps1` on the runner -- silent install,
Start Menu shortcut, `HKCU` Apps & Features entry, silent uninstall, and a
before/after snapshot of `HKCU\...\Uninstall`, `HKCU\...\Run` and the Start
Menu that must come back identical. Do it once by hand anyway, on the file that
is actually on the release page, because a runner is not a desktop:

1. download `MyClaudeCode-Setup-windows-x86_64.exe` and check its digest against
   `SHA256SUMS-desktop-shell.txt`;
2. run it silently, into a scratch directory, and confirm it needs no
   administrator rights:

   ```powershell
   .\MyClaudeCode-Setup-windows-x86_64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=C:\tmp\mcc-app
   ```

3. launch **My Claude Code** from the Start Menu and confirm the dashboard
   loads;
4. uninstall from Apps & Features -- the entry is **"My Claude Code (desktop
   app)"** -- and confirm `mcc-server`, `~/.mcc` and the start-at-login value
   are all still there afterwards.

Set `MCC_DESKTOP_SKIP_AUTOSTART=1` in any shell you use to launch a real
`mcc-desktop` against a scratch configuration directory during this. The
start-at-login registration is machine-global while the preference driving it is
per-config-directory, so without the switch a test launch silently deletes the
registration belonging to your real install.

**What the release must not be asked to prove:** the workflow runs each
platform's real-binary smoke (`desktop-shell/smoke/`) -- the binary launches,
reads a fake `mcc-desktop --print-status`, shows the port-conflict page and
starts nothing -- but a smoke on a headless runner is not a human looking at a
window. First-launch SmartScreen and Gatekeeper behaviour cannot be observed in
CI at all: reputation is per file hash and accrues from real downloads.


## 8. Moving the shell pin (`config/desktop_shell.py`)

Since 6.44.0 `mcc-desktop` fetches the shell on launch from **one pinned release
tag**, verifying the archive against digests pinned in the wheel *and* against
the release's own `SHA256SUMS-desktop-shell.txt`. Both have to agree or the fetch
refuses, which is the point -- and it is also why the pin cannot move on its own.

**Moving the pin is a separate, later commit, never part of cutting a release.**
The digests for release *N* do not exist until `shell-release.yml` has finished
building release *N*, which happens minutes after the release is published. A
release that tried to pin itself would be pinning digests nobody has computed.

The sequence, when the shell has actually changed and users should get the new
one:

1. Cut release *N* as usual. The pin still names *N-1*, and that is correct: the
   shell it names is real, published and verified.
2. Wait for `shell-release.yml` on *N* to be green and the five assets attached
   (section 7).
3. Read the published digests:

   ```bash
   curl -fsSL https://github.com/FiredMosquito831/my-claude-code/releases/download/vN/SHA256SUMS-desktop-shell.txt
   ```

4. In `src/my_claude_code/config/desktop_shell.py`, move
   `DESKTOP_SHELL_RELEASE_TAG` to `vN` and replace **all four** digests in
   `_RELEASES` with the lines from that file, in one commit. Never move the tag
   without the digests: the two are one fact.
5. Ship that as the next PATCH. `tests/config/test_desktop_shell_pin.py` checks
   the shape offline; run the network-gated check to prove the pin against the
   real release:

   ```bash
   MCC_NETWORK_TESTS=1 uv run pytest -q tests/config/test_desktop_shell_pin.py
   ```

**When the shell has not changed, leave the pin alone.** Every user who already
has that binary reads a receipt and downloads nothing; moving the pin for its own
sake makes all of them re-download an identical window.
