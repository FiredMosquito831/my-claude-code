# Design decisions and the record

## Where the design came from

This is a **hand-built fallback system**, not the output of the design-intelligence
tool. The tool was not run for this build; the palette and the type feel were
sampled directly from the product's own dashboard captures, which is the higher
source anyway:

- `assets/admin-providers.png` — page background, card surface, border, body text,
  the green "Configured" badge and the amber "Missing key" badge.
- `assets/readme-analytics-overview.png` — the stat-card rhythm, the all-time strip,
  the mono numerals.
- `assets/admin-model-config.png` — the rail/chain layout that the tier diagram echoes.
- `assets/admin-credential-health.png` — the two-column setting block and the
  plain-words help text.

Sampled values were then adjusted for contrast rather than copied literally. The
product's border colour is deliberately subtle inside the dashboard; on a page
read at a distance it would fail the 3:1 UI threshold, so the site uses
`#5D6E84` (3.12:1 at its worst pair).

## Adopted

- **Dark control-room palette.** Matches the product, keeps the screenshots
  legible against the page.
- **Monospace for identifiers and numbers.** The product already does this; it is
  what makes a rung, a port or a version read as a value and not as prose.
- **Status dot plus word.** The dashboard never relies on colour alone for a
  state; neither does the site.
- **A readout board in the hero.** The single motif that carries the identity.
- **A faint 64 px blueprint grid.** Subtle, fixed, no animation. It reads as a
  control surface without competing with text.
- **Cards for hubs, prose for deep pages, figures for diagrams.** Density is
  calmer than the product UI, never denser.

## Rejected

- **A light theme.** The dashboard has one, but a second site palette would be
  unverified and would double the contrast surface. Rejected.
- **The product's exact border value.** Too low-contrast for a page; replaced by
  the measured `#5D6E84`.
- **A centred hero with three identical feature cards.** The generic "AI landing
  page" composition. Rejected in favour of the asymmetric hero plus a readout.
- **A gradient blob, a testimonial, a carousel.** Nothing on the site is invented.
- **External fonts, icons or any CDN.** The site makes no third-party request.
- **A search box.** The site is static and has no search index; a box would lie.
- **Motion beyond CSS.** A GSAP-style timeline would add a dependency for no
  information.

## Contrast (WCAG 2.2 AA, computed over every final token pair)

```
text/bg                          #E6EDF3 on #0B0F14  16.27:1  need 4.5:1  PASS
text/surface                     #E6EDF3 on #111821  15.11:1  need 4.5:1  PASS
text/surface-2                   #E6EDF3 on #18212C  13.75:1  need 4.5:1  PASS
text-muted/bg                    #A9B7C6 on #0B0F14   9.40:1  need 4.5:1  PASS
text-muted/surface               #A9B7C6 on #111821   8.73:1  need 4.5:1  PASS
text-muted/surface-2             #A9B7C6 on #18212C   7.95:1  need 4.5:1  PASS
text-faint/bg                    #7D8FA3 on #0B0F14   5.79:1  need 4.5:1  PASS
text-faint/surface               #7D8FA3 on #111821   5.38:1  need 4.5:1  PASS
accent/bg                        #3FB950 on #0B0F14   7.57:1  need 4.5:1  PASS
accent/surface                   #3FB950 on #111821   7.03:1  need 4.5:1  PASS
accent/surface-2                 #3FB950 on #18212C   6.39:1  need 4.5:1  PASS
accent-contrast/accent           #04130A on #3FB950   7.50:1  need 4.5:1  PASS
warning/bg                       #D29922 on #0B0F14   7.61:1  need 4.5:1  PASS
warning/surface                  #D29922 on #111821   7.07:1  need 4.5:1  PASS
error/bg                         #F85149 on #0B0F14   5.73:1  need 4.5:1  PASS
error/surface                    #F85149 on #111821   5.32:1  need 4.5:1  PASS
info/bg                          #58A6FF on #0B0F14   7.61:1  need 4.5:1  PASS
info/surface                     #58A6FF on #111821   7.07:1  need 4.5:1  PASS
border/bg (UI 3:1)               #5D6E84 on #0B0F14   3.69:1  need 3.0:1  PASS
border/surface (UI 3:1)          #5D6E84 on #111821   3.42:1  need 3.0:1  PASS
border/surface-2 (UI 3:1)        #5D6E84 on #18212C   3.12:1  need 3.0:1  PASS
border-strong/bg (UI 3:1)        #7C8EA6 on #0B0F14   5.74:1  need 3.0:1  PASS
focus/bg (UI 3:1)                #58A6FF on #0B0F14   7.61:1  need 3.0:1  PASS
lowest margin: 1.04x of the threshold
```

The lowest pair is the border on the second surface at 3.12:1. It was raised from
a more subtle value to clear the threshold; the threshold was not lowered.

## Token enforcement

`scripts/build_site.py` reads `site/design-tokens.json`, emits the `:root` custom
properties, and fails the build if `site/static/style.css` contains a literal hex
colour or an `rgb()`/`hsl()` value. The gate is proven in `gate_proofs.py`.

## Screenshots in the design

Screenshots are evidence, not decoration. They sit in figures with captions, at
the page's full column width, with the browser's own chrome cropped away. A
capture that could not be cleaned is not used. See `site/shots/manifest.json`.
