# My Claude Code — site design system (MASTER)

Hand-built from the product's own dashboard captures. This is the source document
for `site/design-tokens.json`; `DECISIONS.md` records what was adopted, rejected
and measured.

## The identity: the control room

The product is a dark control plane with a left rail, status dots, monospace
identifiers and compact tables. The site borrows that vocabulary and gives it
more air: a faint blueprint grid, a readout board in the hero, green for "this is
live", blue for "this is a route", amber for "this is paused or limited", red only
for a refusal.

One motif, applied everywhere: **the status readout**. It appears as the hero
board, as the `facts` strip, as the status-dot bullet, and as the rung/bench
language in the diagrams.

## Colour

| Token | Value | Use |
|---|---|---|
| `bg` | `#0B0F14` | page background |
| `bg-deep` | `#070A0E` | code blocks, footer |
| `surface` | `#111821` | cards, figures, tables |
| `surface-2` | `#18212C` | table heads, inline code, hover |
| `surface-3` | `#1F2A37` | the third layer (rare) |
| `text` | `#E6EDF3` | body copy |
| `text-muted` | `#A9B7C6` | leads, captions |
| `text-faint` | `#7D8FA3` | metadata, labels |
| `accent` | `#3FB950` | status, primary action |
| `accent-contrast` | `#04130A` | text on the accent |
| `accent-soft` | `#12351C` | the status-dot halo |
| `border` | `#5D6E84` | every 1 px boundary |
| `border-strong` | `#7C8EA6` | hover boundary |
| `focus` | `#58A6FF` | focus ring |
| `success` | `#3FB950` | live |
| `warning` | `#D29922` | paused, limited, update |
| `error` | `#F85149` | refused |
| `info` | `#58A6FF` | links, route markers |

Dark is the only theme. The product's own light variants exist in the dashboard,
but a documentation site with a single dark identity is the choice here; a light
theme would be a second unverified palette.

## Type

System stacks only. No external font is loaded.

- Sans: `system-ui, -apple-system, 'Segoe UI', Roboto, ...`
- Mono: `ui-monospace, 'Cascadia Mono', ..., Consolas, ...` for identifiers,
  numbers, labels, code and every value that could be mistaken for prose.

Scale: `clamp()` from 2 rem to 3.6 rem for h1, 1.6–2.35 rem for h2, 1.15–1.45 rem
for h3. Body 1 rem / 1.65. Measure capped at `72ch`.

## Layout

- Max width `1180px`; gutter `clamp(16px, 4vw, 32px)`.
- Deep pages: content column plus a `250px` sticky table of contents; below
  `960px` the TOC is removed rather than stacked above the heading.
- Hubs and the home page use card grids with `auto-fill` and a `280px` minimum.
- Every illustration is a `figure` with a caption; every diagram adds a visible
  text alternative below it.

## Components

- `status-board` / `readout`: the hero's verified quick facts.
- `card`: one capability, with a kicker, a title, a sentence and an "Open" cue.
- `facts`: a bordered strip of values and labels.
- `figure` / `figure--diagram`: image or inline SVG with a caption.
- `callout`: note, warning and accent variants, left border only.
- `table-wrap`: horizontally scrollable, never wider than its column.
- `faq-item`: native `<details>`; the answer is in the HTML whether or not it is open.
- `term`: a glossary entry with an anchor id.

## Motion

CSS transitions only, 120–200 ms. No parallax, ticker or autoplay. Under
`prefers-reduced-motion: reduce`, all transitions and animations are disabled and
smooth scrolling is turned off.

## Accessibility

- One `<h1>` per page; logical heading order (a gate checks the jump).
- Jump link as the first focusable element; `header`, `nav`, `main`, `footer` landmarks.
- Visible focus ring on every interactive element; 44×44 minimum targets.
- No colour-only meaning: every status dot is paired with a word.
- The site reads without JavaScript; the script only toggles the mobile nav and
  marks the current TOC section.
- Print stylesheet: chrome removed, links expanded, figures kept whole.
