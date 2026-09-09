# Brand — My Claude Code (MCC)

This document is the **single source of truth** for the My Claude Code brand.
When copy, docs, the dashboard, or release assets conflict with anything here,
this file wins.

> Rebrand context: the product was "Free Claude Code" (FCC). The package was
> renamed `free_claude_code` → `my_claude_code` and the product rebranded to
> **My Claude Code**. A dual command family is preserved (`mcc-*` primary,
> `fcc-*` legacy aliases) so existing installs and muscle memory keep working.

## 1. Name & abbreviation

- **Name:** My Claude Code
- **Abbreviation:** MCC
- **Package:** `my-claude-code` (PyPI / wheel)
- **Server command:** `mcc-server` (legacy alias: `fcc-server`)
- **Launchers:** `mcc-claude`, `mcc-codex`, `mcc-pi`, `mcc-claude-old`
  (legacy aliases: `fcc-claude`, `fcc-codex`, `fcc-pi`, `fcc-claude-old`)

## 2. Positioning

A **local control plane** for the models, keys, routes, and coding agents the
user chooses. MCC is an Anthropic-compatible local proxy that sits between a
coding agent (Claude Code, Codex, Pi, and their IDE extensions) and whichever
model provider the user configures — forwarding requests, rotating keys, and
translating responses back into Anthropic's wire format on the user's own
machine.

## 3. Voice

Precise, local-first, active, factual, and explicit about trade-offs.

- State what the software does; do not overpromise.
- **Never promise "free"** as a product claim, and **never imply an Anthropic
  affiliation**. Anthropic is a third-party model provider like any other; MCC
  is independent.
- Prefer concrete mechanics ("rotates keys on a 429") over vague benefits.
- Name trade-offs when they exist (e.g. deferred Windows install, offline
  catalog limits).
- Use the active voice and imperative for instructions (`Run mcc-server`).

## 4. Primary line

> **Your models. Your keys. Your machine.**

Use this as the hero/positioning line. It is the canonical summary of the
product's local-first stance.

## 5. Signature mark — the MCC mark

> Superseded: earlier drafts of this document described a text "MC"
> monogram placeholder rendered at runtime with Pillow, and before that an
> unbuilt "MC route glyph" concept (two routing lanes converging through a
> single control point). Neither shipped. The product now ships a real,
> designed mark as static image assets — this section documents what
> actually exists in `src/my_claude_code/assets/`.

The brand mark ships as **two separate PNG renders of the same mark**, cut at
different margins for the two contexts they render in, plus packaged
multi-size `.ico`/`.icns` derivatives of the wider-margin render for native
OS icon slots:

| Asset | Margin | Size | Use case |
| --- | --- | --- | --- |
| `app-icon.png` | 10% | 256×256, RGBA, transparent | Windows/taskbar app icons, Start Menu shortcuts, `.app` bundle resources, docs |
| `app-icon.ico` | 10% (same render as above) | multi-size: 16/24/32/48/64/128/256 | Windows executables, `.lnk` shortcut icons |
| `app-icon.icns` | 10% (same render as above) | multi-size slots: 16/32/128/256/512 | macOS `.app` bundle `CFBundleIconFile` |
| `tray-icon.png` | 2% | 128×128, RGBA, transparent | System tray / menu-bar rendering at 16-24px, where the extra margin of the app-icon render would make the mark look too small |

These are deliberately **two separate source files**, not one file scaled at
render time: the tighter 2% margin on `tray-icon.png` keeps the mark legible
at the tiny sizes trays actually render (16-24px), where the 10% margin used
everywhere else would shrink the glyph too far to read. Always use
`app_icon_bytes()` / `tray_icon_bytes()` / `export_app_icon()` in
`src/my_claude_code/cli/desktop_assets.py` to read or export these — never
read the mark from a hardcoded string literal or re-derive it.

Rules:

- **Use the packaged mark**, not gradient initials, emoji, or a generic AI
  sparkle. It is the only approved logo mark.
- Do not add gradients, glow, drop shadows, or photographic elements on top of
  the mark; it ships pre-rendered.
- Never stretch, recolor, or crop the mark. Use the `app-icon` variant or the
  `tray-icon` variant as-is for their respective contexts.
- A Pillow-generated "MC" monogram remains in `desktop_assets.py` as a
  defensive fallback only, for the case where the packaged asset file is
  missing at runtime (e.g. a corrupted install). It is not a design choice
  and should be unreachable in normal operation.

## 6. Typography

Local/system-based fonts for **instant offline load** — no web-font fetch.

- **Fira Code** (monospace) for: routes, model references, commands, tokens,
  code, and tabular data.
- **Fira Sans** (sans) for body copy and UI labels.

Fall back to the system UI monospace / sans stack if Fira is unavailable; never
block render on a font download.

## 7. UI palette

Four themes, selected via a `[data-theme]` attribute on the dashboard root.
All colors are **semantic tokens** built on primitive tokens (see
`src/my_claude_code/api/admin_static/admin.css`).

| Theme | Purpose | Base |
| --- | --- | --- |
| **Midnight** | Dark operations console (default) | Deep navy/black surfaces, high-contrast text |
| **Paper** | True light | White/near-white surfaces, dark text |
| **High Contrast** | WCAG AAA | Maximum contrast pairings for both modes |
| **Velvet** | Warm dark accent theme | Deep navy surfaces with a crimson accent |

Token tiers: **primitives → semantic → component**. Components reference
semantic tokens only; never hardcode raw hex in component styles. Charts read
colors through the `token()` helper so they re-theme automatically.

### Measured mark color

The palette above is fixed and is not re-derived here. As a validation
check, the mark's red was sampled directly from the shipped
`src/my_claude_code/assets/app-icon.png` (fully-opaque pixels only, via
Pillow) rather than assumed:

- Darkest solid fill sampled: `#9f1131`
- Lightest solid fill sampled: `#dd2643`
- Average of all fully-opaque mark pixels: `#b22242`

All three sit in the roughly `#b01030`–`#f02040` red band, and land close to
the existing **Velvet** theme's accent, `#e6435e` — confirming the mark reads
correctly next to Velvet without needing a new accent. This is a measurement
of the existing asset, not a new palette decision; do not add a fifth theme
or new accent token from this sample.

## 8. Motion

Subtle only. 300–400ms, `power1.out` easing. Respect
`prefers-reduced-motion`: disable non-essential animation when set.

## 9. Accessibility (non-negotiable)

- 44px minimum touch targets.
- 4.5:1 text contrast (AAA in High Contrast theme).
- Visible focus rings on every interactive element.
- ARIA labels on icon-only controls; charts ship companion data tables.
- No emoji as icons.

## 10. Preserved legacy contracts (do NOT rebrand)

These published contracts stay exactly as they are, for backward
compatibility, even though the product is now My Claude Code:

- `FCC_*` environment variables (e.g. `FCC_ENV_FILE`, `FCC_OPEN_BROWSER`,
  `FCC_SMOKE_TARGETS`).
- Release repository `FiredMosquito831/my-claude-code` (RELEASE_REPO).
- Local proxy port `:8082`.
- Proxy auth token: generated per machine on first start (the literal `freecc` was retired in 6.65.0; it was a password printed in a public repository).
- Model ids `claude-3-freecc-*`.
- Codex provider id `fcc`.
- Pi scope `free-claude-code/**`.
- Config directory `.fcc`.
- Legacy command names `fcc-*` (kept as aliases).
- Display-name constant `LEGACY_DISPLAY_NAME = "Free Claude Code"`.

When writing docs or UI, prefer the new `mcc-*` names but note that the `fcc-*`
aliases still work.
