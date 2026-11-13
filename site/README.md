# Site README

The product site is generated from `site/` into `docs/` by `scripts/build_site.py`.

## Rebuild

```
python -m pip install --requirement requirements-site.txt
python scripts/build_site.py            # write docs/
python scripts/build_site.py --check    # rebuild to a temp dir and byte-compare
```

Python 3.12 and Jinja2 only. No Node, no external service, no network call.

## Inputs

- `site/content/*.json` — one file per page plus `site.json`. Each page carries a
  `claims` list that the builder enforces.
- `site/content/seo.json` — the per-page SEO data: one block per page path plus a
  `site` block for the home page. It carries the title, the description and the
  keyword set, and the builder emits all three. A page with no block fails the
  build, so a new page cannot ship unoptimised.
- `site/templates/` — `base.html`, `page.html`, `home.html`, `404.html`, the block
  macros and the brand mark.
- `site/static/` — `style.css` (token references only), `site.js`, `favicon.svg`,
  `og.png`, and `diagrams/*.svg`.
- `site/shots/` — the reviewed screenshot derivatives and `manifest.json`.
- `site/design-tokens.json` — the only source of colour, type, spacing and motion.
- `site/design-system/` — the design record. Documentation, not rendered.

## Output

The builder writes only into `docs/`. That directory also holds the repository's
Markdown documentation, which is a read-only source: the builder never deletes or
overwrites a Markdown page, and `.site-manifest.json` records exactly which files
the builder owns so `--check` can detect a changed or stale generated file.

## Gates

The builder fails the build, before writing, on: a secret-shaped string, a
forbidden stock phrase, a broken internal link or fragment, page metadata
outside its bounds, an image without alt text, malformed JSON-LD, a sitemap entry
with no file, an OG image that is not 1200×630, a literal colour in the CSS, a
page below its revision-2 minimums, and a page with no claims or a claim with no
evidence. Every gate is proven against a broken input in the internal records.

The SEO gates are part of the same run: a page with no keyword set, a title over
60 characters, a description outside 120–160, a keyword set outside 8–14 terms, a
keyword on more than three pages, a deep page with no `TechArticle`, a home page
with no `SoftwareApplication`, a missing or wrong-host `llms.txt`, `llms-full.txt`
or `security.txt`, a missing `.nojekyll`, a harness page that does not name its
harness or give its command, and a harness page not in the sitemap at priority
0.6 all fail it.

## Machine-readable files

The builder writes these beside the pages, from the same content the HTML is
built from, so they cannot drift:

- `llms.txt` — a Markdown index of the site, with absolute links.
- `llms-full.txt` — every page's prose as plain text, without navigation.
- `security.txt` and `.well-known/security.txt` — RFC 9116, with the advisories
  URL as the contact.
- `.nojekyll` — empty, and required: GitHub Pages runs Jekyll, which drops
  dot-directories, so without it `/.well-known/security.txt` is never served.
