#!/usr/bin/env python3
"""Build the My Claude Code product site into docs/.

    python scripts/build_site.py            # write docs/
    python scripts/build_site.py --check    # build to a temp dir and byte-compare

Deterministic: no wall clock, no git history, no randomness, LF newlines.
The builder owns only the files it emits; the existing docs/ Markdown is never
touched (this repository's docs/ is also the MkDocs source tree). A committed
.site-manifest.json records the builder's output set so --check can detect a
stale generated file as well as a changed one.

Gates run before any output is written, and every gate can fail:
  1 secret scan  2 forbidden strings  3 internal links and fragments
  4 page metadata  5 alt text  6 JSON-LD  7 sitemap  8 OG image
  9 design tokens in CSS  10 revision-2 minimums (words, diagrams, images).
"""

import argparse
import html
import json
import pathlib
import re
import sys
import tempfile
from html.parser import HTMLParser

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO = pathlib.Path(__file__).resolve().parents[1]
SITE = REPO / "site"
DOCS = REPO / "docs"
BASE_URL = "https://myclaudecode.danubelabs.net"
MANIFEST_NAME = ".site-manifest.json"

# The machine-readable surfaces, and the deep-page families that carry a
# TechArticle. Both are named once here so the gates and the emitters agree.
LLMS_TXT = "llms.txt"
LLMS_FULL_TXT = "llms-full.txt"
SECURITY_TXT = "security.txt"
WELL_KNOWN_SECURITY_TXT = ".well-known/security.txt"
NOJEKYLL = ".nojekyll"
SECURITY_CONTACT = (
    "https://github.com/FiredMosquito831/my-claude-code/security/advisories/new"
)
# RFC 9116 asks for an Expires within a year of publication. The site's lastmod
# is the deliberate date the owner sets in site.json, so the expiry is derived
# from it rather than from the wall clock: the build stays deterministic.
SECURITY_EXPIRES = "2027-11-10"
TECH_ARTICLE_PREFIXES = ("/features/", "/architecture/", "/configuration/")
TECH_ARTICLE_PATHS = frozenset(
    {
        "/features/",
        "/architecture/",
        "/configuration/",
    }
)
HARNESS_PAGES = (
    "/for/claude-code/",
    "/for/codex/",
    "/for/opencode/",
    "/for/gemini-cli/",
)
# The harness each landing page names in its title, and the command it gives.
HARNESS_TITLES = {
    "/for/claude-code/": ("Claude Code", "mcc-claude"),
    "/for/codex/": ("Codex", "mcc-codex"),
    "/for/opencode/": ("OpenCode", "mcc-opencode"),
    "/for/gemini-cli/": ("Gemini CLI", "mcc-gemini"),
}
# The keyword cap and floor, and the reuse ceiling, from the SEO reference.
KEYWORDS_MIN = 8
KEYWORDS_MAX = 14
KEYWORD_PAGE_LIMIT = 3
# llms-full.txt is one fetch for a model, not an archive: cap it at a size a
# context window can take. The whole site's prose is well under this.
LLMS_FULL_MAX_BYTES = 400_000

# ---------------------------------------------------------------- page specs
# path -> (kind, min_words, max_words, min_images, min_diagrams)
PAGE_SPECS = {
    "/": ("home", 1400, 1800, 0, 1),
    "/features/": ("hub", 500, 800, 1, 0),
    "/features/routing/": ("feature", 900, 1600, 3, 0),
    "/features/reasoning-and-budgets/": ("feature", 900, 1600, 3, 0),
    "/features/providers-and-credentials/": ("feature", 900, 1600, 3, 0),
    "/features/agents/": ("feature", 900, 1600, 3, 0),
    "/features/observability-and-cost/": ("feature", 900, 1600, 3, 0),
    "/features/web-search/": ("feature", 800, 1400, 3, 0),
    "/features/messaging/": ("feature", 800, 1400, 3, 0),
    "/features/media/": ("feature", 800, 1400, 3, 0),
    "/features/platform/": ("feature", 800, 1400, 3, 0),
    "/features/cli/": ("feature", 800, 1400, 3, 0),
    "/features/dashboard/": ("feature", 800, 1400, 3, 0),
    "/quick-start/": ("page", 600, 1000, 0, 0),
    "/architecture/": ("architecture", 900, 1400, 0, 3),
    "/architecture/request-path/": ("architecture", 800, 1400, 0, 2),
    "/architecture/provider-routing/": ("architecture", 800, 1400, 0, 2),
    "/architecture/credential-health/": ("architecture", 800, 1400, 0, 2),
    "/architecture/cost-provenance/": ("architecture", 800, 1400, 0, 2),
    "/architecture/data-and-storage/": ("architecture", 800, 1400, 0, 2),
    "/architecture/security-model/": ("architecture", 800, 1400, 0, 2),
    "/configuration/": ("page", 600, 900, 0, 0),
    "/configuration/environment/": ("page", 600, 900, 0, 0),
    "/gallery/": ("gallery", 60, 2000, 0, 0),
    "/glossary/": ("terms", 1500, 2500, 0, 0),
    "/faq/": ("faq", 1500, 2500, 0, 0),
    "/troubleshooting/": ("page", 800, 1200, 0, 0),
    "/about/": ("page", 500, 800, 0, 0),
    "/license/": ("page", 400, 600, 0, 0),
    "/for/claude-code/": ("harness", 400, 700, 0, 0),
    "/for/codex/": ("harness", 400, 700, 0, 0),
    "/for/opencode/": ("harness", 400, 700, 0, 0),
    "/for/gemini-cli/": ("harness", 400, 700, 0, 0),
    "/404.html": ("404", 60, 100, 0, 0),
}

SECRET_PATTERNS = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("stripe-key", re.compile(r"sk_(live|test)_[A-Za-z0-9]{8,}")),
    ("service-role", re.compile(r"service_role\s*[:=]\s*[A-Za-z0-9._-]{20,}")),
    ("postgres-url", re.compile(r"postgres(ql)?://[^\s\"'<]+")),
    ("mysql-url", re.compile(r"mysql://[^\s\"'<]+")),
    (
        "named-secret",
        re.compile(
            r"(?i)(password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*[^\s<]{6,}"
        ),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    (
        "private-ip",
        re.compile(
            r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    ("supabase-host", re.compile(r"\b[a-z]{20}\.supabase\.co\b")),
    ("owner-path", re.compile(r"C:\\Users\\|/home/|/Users/")),
]
FORBIDDEN = [
    "TODO",
    "TBD",
    "lorem",
    "YOUR_",
    "placeholder",
    "XXX",
    "example.com",
    "coming soon",
    "[insert",
    "FIXME",
]
FORBIDDEN_CI = {"todo", "tbd", "lorem", "placeholder", "coming soon", "fixme"}

NAV = [
    {"label": "Features", "href": "/features/", "prefix": "/features/"},
    {"label": "Architecture", "href": "/architecture/", "prefix": "/architecture/"},
    {"label": "Quick start", "href": "/quick-start/", "prefix": "/quick-start/"},
    {"label": "Gallery", "href": "/gallery/", "prefix": "/gallery/"},
    {"label": "FAQ", "href": "/faq/", "prefix": "/faq/"},
    {"label": "Glossary", "href": "/glossary/", "prefix": "/glossary/"},
]

TOKEN_VARS = {
    "color.bg": "--color-bg",
    "color.bg-deep": "--color-bg-deep",
    "color.surface": "--color-surface",
    "color.surface-2": "--color-surface-2",
    "color.surface-3": "--color-surface-3",
    "color.text": "--color-text",
    "color.text-muted": "--color-text-muted",
    "color.text-faint": "--color-text-faint",
    "color.accent": "--color-accent",
    "color.accent-contrast": "--color-accent-contrast",
    "color.accent-soft": "--color-accent-soft",
    "color.border": "--color-border",
    "color.border-strong": "--color-border-strong",
    "color.focus": "--color-focus",
    "color.success": "--color-success",
    "color.warning": "--color-warning",
    "color.warning-contrast": "--color-warning-contrast",
    "color.error": "--color-error",
    "color.info": "--color-info",
    "font.sans": "--font-sans",
    "font.mono": "--font-mono",
    "radius.sm": "--radius-sm",
    "radius.md": "--radius-md",
    "radius.lg": "--radius-lg",
    "radius.pill": "--radius-pill",
    "shadow.card": "--shadow-card",
    "motion.fast": "--motion-fast",
    "motion.base": "--motion-base",
    "motion.ease": "--motion-ease",
    "focus.ring": "--focus-ring",
    "focus.offset": "--focus-offset",
    "layout.max": "--layout-max",
    "layout.prose": "--layout-prose",
    "layout.gutter": "--layout-gutter",
}


class PageError(RuntimeError):
    pass


# ---------------------------------------------------------------- utilities
def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "section"


def token_css(tokens: dict) -> str:
    lines = [":root {"]
    for key, var in TOKEN_VARS.items():
        group, name = key.split(".", 1)
        value = tokens[group][name]
        lines.append(f"  {var}: {value};")
    for name, value in tokens["space"].items():
        lines.append(f"  --space-{name}: {value};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def plain_text(fragment: str) -> str:
    fragment = re.sub(
        r"(?is)<(script|style|pre|aside|nav)[^>]*>.*?</\1>", " ", fragment
    )
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    return re.sub(r"\s+", " ", fragment).strip()


def word_count(fragment: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", plain_text(fragment)))


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.ids: set[str] = set()
        self.imgs: list[str] = []
        self.h1 = 0
        self.ldjson: list[str] = []
        self._in_ld = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "href" in a:
            self.links.append(("href", a["href"]))
        elif tag in ("link", "script") and "src" in a:
            self.links.append(("src", a["src"]))
        elif tag == "img":
            self.links.append(("src", a.get("src", "")))
            self.imgs.append(a.get("alt", ""))
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "h1":
            self.h1 += 1
        if tag == "script" and a.get("type") == "application/ld+json":
            self._in_ld = True
            self._ld_buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ldjson.append("".join(self._ld_buf))
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)


class Build:
    def __init__(self, out: pathlib.Path) -> None:
        self.out = out
        self.tokens = json.loads(
            (SITE / "design-tokens.json").read_text(encoding="utf-8")
        )
        self.site = json.loads(
            (SITE / "content" / "site.json").read_text(encoding="utf-8")
        )
        self.seo = json.loads(
            (SITE / "content" / "seo.json").read_text(encoding="utf-8")
        )
        self.manifest = json.loads(
            (SITE / "shots" / "manifest.json").read_text(encoding="utf-8")
        )
        self.shots = {m["slug"]: m for m in self.manifest}
        self.pages = self._load_pages()
        self.rendered: dict[str, str] = {}
        self.output_files: dict[str, bytes] = {}
        self.problems: list[str] = []
        self.env = Environment(
            loader=FileSystemLoader(str(SITE / "templates")),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    # ------------------------------------------------------------ load
    def _load_pages(self) -> list[dict]:
        pages = []
        for path in sorted((SITE / "content").glob("*.json")):
            if path.name in ("site.json", "seo.json"):
                continue
            page = json.loads(path.read_text(encoding="utf-8"))
            page["_source"] = path.name
            pages.append(page)
        seen = set()
        for page in pages:
            p = page["path"]
            if p in seen:
                raise PageError(f"duplicate page path {p} in {page['_source']}")
            seen.add(p)
            if p not in PAGE_SPECS:
                raise PageError(f"page {p} has no spec in PAGE_SPECS")
        missing = set(PAGE_SPECS) - seen
        if missing:
            raise PageError("missing pages: " + ", ".join(sorted(missing)))
        pages.sort(key=lambda pg: (pg["path"] != "/", pg["path"]))
        return pages

    def shot_thumb(self, slug: str) -> dict:
        shot = dict(self.shots[slug])
        # Thumb dimensions follow the same 640 max-width rule as the generator.
        w = shot["width"]
        h = shot["height"]
        if w > 640:
            h = int(h * 640 / w)
            w = 640
        shot["thumb_w"], shot["thumb_h"] = w, h
        return shot

    # ------------------------------------------------------------ prepare
    def prepare(self) -> None:
        diagram_dir = SITE / "static" / "diagrams"
        self.diagrams = {}
        for path in sorted(diagram_dir.glob("*.svg")):
            self.diagrams[path.stem] = path.read_text(encoding="utf-8")
        for page in self.pages:
            toc = []
            used = set()
            for block in page["blocks"]:
                if block["type"] in ("h2", "h3"):
                    base = slugify(block["text"])
                    candidate = base
                    n = 1
                    while candidate in used:
                        n += 1
                        candidate = f"{base}-{n}"
                    used.add(candidate)
                    block["id"] = candidate
                    if block["type"] == "h2":
                        toc.append(
                            {
                                "id": candidate,
                                "text": plain_text(block["text"]),
                                "level": 2,
                            }
                        )
                elif block["type"] == "diagram":
                    name = block["name"]
                    if name not in self.diagrams:
                        raise PageError(f"{page['path']}: unknown diagram {name}")
                    block["svg"] = self.diagrams[name]
                elif block["type"] == "gallery_area":
                    area = block["area"]
                    shots = [
                        self.shot_thumb(m["slug"])
                        for m in self.manifest
                        if m["area"] == area
                    ]
                    shots.sort(key=lambda s: s["slug"])
                    block["shots"] = shots
                elif block["type"] == "faq":
                    page["_faq"] = block["items"]
            page["_toc"] = toc
            page["_images"] = sorted(
                {b["shot"] for b in page["blocks"] if b["type"] == "figure"}
            )
            page["_diagram_count"] = sum(
                1 for b in page["blocks"] if b["type"] == "diagram"
            )
            # gallery: every approved shot must appear exactly once across areas.
            if page["path"] == "/gallery/":
                covered = [m["slug"] for m in self.manifest]
                block_slugs = []
                for b in page["blocks"]:
                    if b["type"] == "gallery_area":
                        block_slugs.extend(s["slug"] for s in b["shots"])
                page["_gallery_covered"] = sorted(block_slugs)
                page["_gallery_expected"] = sorted(covered)
            # SEO: the curated title, description and keyword set come from
            # site/content/seo.json, keyed by the page path. A page with no
            # block keeps the title and description it already had and fails
            # the keyword gate, so a new page cannot ship unoptimised.
            block = self.seo_block(page["path"])
            page["_seo"] = block
            if block is not None:
                page["title"] = block["title"]
                page["description"] = block["description"]
                page["keywords"] = block["keywords"]
            else:
                page["keywords"] = []

    def seo_block(self, path: str) -> dict | None:
        """The curated SEO block for a page path, or None when it has none."""
        if path == "/":
            return self.seo.get("site")
        return self.seo.get("pages", {}).get(path.strip("/"))

    @staticmethod
    def is_deep_page(path: str) -> bool:
        """A deep page is a features, architecture or configuration page.

        The hub pages (``/features/``, ``/architecture/``, ``/configuration/``)
        are deep pages too: each is the index of its own family and states the
        behaviour of the whole area.
        """
        if path in TECH_ARTICLE_PATHS:
            return True
        return path.startswith(TECH_ARTICLE_PREFIXES) and path.endswith("/")

    def tech_article_about(self, path: str) -> dict:
        """The ``about`` of a TechArticle, as the family the page belongs to."""
        if path.startswith("/architecture"):
            return {"@type": "Thing", "name": "My Claude Code architecture"}
        if path.startswith("/configuration"):
            return {"@type": "Thing", "name": "My Claude Code configuration"}
        return {"@type": "Thing", "name": "My Claude Code features"}

    # ------------------------------------------------------------ render
    def render_all(self) -> None:
        for page in self.pages:
            self.rendered[page["path"]] = self.render_page(page)

    def jsonld_for(self, page: dict, canonical: str) -> list[str]:
        blocks = [
            json.dumps(
                {
                    "@context": "https://schema.org",
                    "@type": "WebPage",
                    "name": page["title"],
                    "url": canonical,
                    "description": page["description"],
                },
                ensure_ascii=False,
            )
        ]
        if page["path"] != "/" and page["path"] != "/404.html":
            crumbs = [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "name": "Home",
                    "item": BASE_URL + "/",
                }
            ]
            for i, crumb in enumerate(page.get("breadcrumbs", []), start=2):
                item = {
                    "@type": "ListItem",
                    "position": i,
                    "name": plain_text(crumb["label"]),
                }
                if crumb.get("href"):
                    item["item"] = BASE_URL + crumb["href"]
                crumbs.append(item)
            blocks.append(
                json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "BreadcrumbList",
                        "itemListElement": crumbs,
                    },
                    ensure_ascii=False,
                )
            )
        if page["path"] == "/":
            blocks.append(
                json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "SoftwareApplication",
                        "name": "My Claude Code",
                        "alternateName": "my-claude-code",
                        "description": self.site["software_description"],
                        "applicationCategory": "DeveloperApplication",
                        # Only the platforms the repository verifies: the
                        # README's install table and the release workflows.
                        "operatingSystem": "Windows, macOS, Linux",
                        "softwareVersion": self.site["version"],
                        "offers": {
                            "@type": "Offer",
                            "price": "0",
                            "priceCurrency": "USD",
                        },
                        "license": "https://www.gnu.org/licenses/agpl-3.0.html",
                        "isAccessibleForFree": True,
                        "featureList": self.site["feature_list"],
                        "codeRepository": self.site["repo"],
                        "author": {
                            "@type": "Person",
                            "name": "FiredMosquito831",
                            "url": "https://github.com/FiredMosquito831",
                        },
                    },
                    ensure_ascii=False,
                )
            )
        if self.is_deep_page(page["path"]):
            # A deep page is a documentation page, so it carries the type a
            # documentation page carries. proficiencyLevel is "Expert" only
            # where the page is the reference for its subject: the architecture
            # and configuration pages describe the implementation, the feature
            # pages describe behaviour. Every one of them is written from the
            # source, so "Expert" is true of all of them and is not a boast.
            blocks.append(
                json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "TechArticle",
                        "headline": page["title"],
                        "description": page["description"],
                        "url": canonical,
                        "about": self.tech_article_about(page["path"]),
                        "proficiencyLevel": "Expert",
                        "inLanguage": "en",
                        "author": {
                            "@type": "Person",
                            "name": "FiredMosquito831",
                            "url": "https://github.com/FiredMosquito831",
                        },
                    },
                    ensure_ascii=False,
                )
            )
        if page["path"] == "/faq/":
            items = page.get("_faq", [])
            blocks.append(
                json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "FAQPage",
                        "mainEntity": [
                            {
                                "@type": "Question",
                                "name": plain_text(q["q"]),
                                "acceptedAnswer": {
                                    "@type": "Answer",
                                    "text": plain_text(q["a"]),
                                },
                            }
                            for q in items
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        if page["path"] == "/gallery/":
            images = [
                {
                    "@type": "ImageObject",
                    "contentUrl": f"{BASE_URL}/assets/shots/{m['slug']}.webp",
                    "description": m["alt"],
                }
                for m in self.manifest
            ]
            images.sort(key=lambda x: x["contentUrl"])
            blocks.append(
                json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "ImageGallery",
                        "image": images,
                    },
                    ensure_ascii=False,
                )
            )
        return blocks

    def render_page(self, page: dict) -> str:
        path = page["path"]
        is_404 = path == "/404.html"
        canonical = "" if is_404 else BASE_URL + path
        nav = [
            dict(
                item,
                current=(
                    path == item["href"]
                    or (item["prefix"] != "/" and path.startswith(item["prefix"]))
                ),
            )
            for item in NAV
        ]
        ctx = {
            "site": self.site,
            "title": page["title"],
            "og_title": page.get("og_title", page["title"]),
            "description": page["description"],
            "keywords": page["keywords"],
            "canonical": canonical,
            "noindex": is_404,
            "theme_color": self.tokens["color"]["bg"],
            "jsonld": self.jsonld_for(page, canonical or BASE_URL + "/"),
            "nav": nav,
            "h1": page["h1"],
            "eyebrow": page.get("eyebrow", ""),
            "intro": page.get("intro", ""),
            "blocks": page["blocks"],
            "shots": self.shots,
            "breadcrumbs": page.get("breadcrumbs", []),
            "toc": page["_toc"],
            "page_grid": page.get("page_grid", False),
        }
        if path == "/":
            ctx["readouts"] = page.get("readouts", [])
            template = self.env.get_template("home.html")
        elif is_404:
            template = self.env.get_template("404.html")
        else:
            template = self.env.get_template("page.html")
        return template.render(**ctx)

    # ------------------------------------------------------------ gates
    def _scan_targets(self):
        """Every text file the site publishes: the source under site/ and the
        rendered output the builder emits. The repository's existing docs/
        Markdown is a read-only source, not part of this output."""
        targets = [
            (
                path.relative_to(REPO).as_posix(),
                path.read_text(encoding="utf-8", errors="replace"),
            )
            for path in sorted(SITE.rglob("*"))
            if path.is_file()
            and path.suffix.lower()
            in {".json", ".html", ".css", ".js", ".svg", ".md", ".txt"}
        ]
        text_suffixes = {".html", ".css", ".js", ".xml", ".txt", ".svg", ".json"}
        for rel, data in sorted(self.output_files.items()):
            if pathlib.PurePosixPath(rel).suffix.lower() in text_suffixes:
                targets.append((f"docs/{rel}", data.decode("utf-8", errors="replace")))
        return targets

    def gate_secrets(self) -> None:
        for rel, text in self._scan_targets():
            for lineno, line in enumerate(text.splitlines(), start=1):
                for name, rx in SECRET_PATTERNS:
                    if rx.search(line):
                        self.problems.append(
                            f"secret scan: {rel}:{lineno}: pattern {name}"
                        )

    def gate_forbidden(self) -> None:
        for rel, text in self._scan_targets():
            for lineno, line in enumerate(text.splitlines(), start=1):
                for token in FORBIDDEN:
                    if token.lower() in FORBIDDEN_CI:
                        hit = token.lower() in line.lower()
                    else:
                        hit = token in line
                    if hit:
                        self.problems.append(
                            f"forbidden string: {rel}:{lineno}: {token}"
                        )

    def gate_metadata(self) -> None:
        titles, descriptions = {}, {}
        for page in self.pages:
            path = page["path"]
            title = page["title"]
            desc = page["description"]
            if len(title) > 60:
                self.problems.append(
                    f"metadata: {path}: title is {len(title)} chars (max 60): {title}"
                )
            if not (120 <= len(desc) <= 160):
                self.problems.append(
                    f"metadata: {path}: description is {len(desc)} chars (need 120-160)"
                )
            if title in titles:
                self.problems.append(
                    f"metadata: {path}: duplicate title also on {titles[title]}"
                )
            if desc in descriptions:
                self.problems.append(
                    f"metadata: {path}: duplicate description also on {descriptions[desc]}"
                )
            titles[title] = path
            descriptions[desc] = path

    def gate_keywords(self) -> None:
        """Every page has a truthful keyword set of 8-14 terms.

        A page with no block in seo.json fails here, which is what stops a new
        page shipping unoptimised. A keyword on more than three pages is a
        site-level term, not a page term, and fails too.
        """
        seen: dict[str, list[str]] = {}
        for page in self.pages:
            path = page["path"]
            keywords = page["keywords"]
            if not keywords:
                self.problems.append(f"keywords: {path}: no keyword set")
                continue
            if not (KEYWORDS_MIN <= len(keywords) <= KEYWORDS_MAX):
                self.problems.append(
                    f"keywords: {path}: {len(keywords)} terms "
                    f"(need {KEYWORDS_MIN}-{KEYWORDS_MAX})"
                )
            lowered = [k.lower() for k in keywords]
            if len(set(lowered)) != len(lowered):
                self.problems.append(f"keywords: {path}: a term appears twice")
            for term in lowered:
                seen.setdefault(term, []).append(path)
        for term, paths in sorted(seen.items()):
            if len(paths) > KEYWORD_PAGE_LIMIT:
                self.problems.append(
                    f"keywords: {term!r} is on {len(paths)} pages "
                    f"(max {KEYWORD_PAGE_LIMIT}): {', '.join(sorted(paths))}"
                )

    def gate_structured_data(self) -> None:
        """The home page carries SoftwareApplication, every deep page TechArticle."""
        for page in self.pages:
            path = page["path"]
            collector = LinkCollector()
            collector.feed(self.rendered[path])
            types = []
            for block in collector.ldjson:
                try:
                    data = json.loads(block)
                except json.JSONDecodeError:
                    continue  # gate_links already reports the parse failure
                types.append(data.get("@type", ""))
            if path == "/":
                if "SoftwareApplication" not in types:
                    self.problems.append(
                        "structured data: /: no SoftwareApplication block"
                    )
                else:
                    app = next(
                        json.loads(b)
                        for b in collector.ldjson
                        if json.loads(b).get("@type") == "SoftwareApplication"
                    )
                    for field in (
                        "name",
                        "alternateName",
                        "applicationCategory",
                        "operatingSystem",
                        "softwareVersion",
                        "offers",
                        "license",
                        "isAccessibleForFree",
                        "featureList",
                        "author",
                    ):
                        if field not in app:
                            self.problems.append(
                                f"structured data: /: SoftwareApplication has no {field}"
                            )
                    if app.get("applicationCategory") != "DeveloperApplication":
                        self.problems.append(
                            "structured data: /: applicationCategory is not "
                            "DeveloperApplication"
                        )
                    offers = app.get("offers") or {}
                    if offers.get("price") != "0":
                        self.problems.append(
                            "structured data: /: offers.price is not 0"
                        )
                    if app.get("softwareVersion") != self.site["version"]:
                        self.problems.append(
                            "structured data: /: softwareVersion does not match "
                            "site.json"
                        )
            if self.is_deep_page(path):
                if "TechArticle" not in types:
                    self.problems.append(
                        f"structured data: {path}: no TechArticle block"
                    )
                    continue
                article = next(
                    json.loads(b)
                    for b in collector.ldjson
                    if json.loads(b).get("@type") == "TechArticle"
                )
                for field in (
                    "headline",
                    "about",
                    "proficiencyLevel",
                    "inLanguage",
                    "description",
                ):
                    if not article.get(field):
                        self.problems.append(
                            f"structured data: {path}: TechArticle has no {field}"
                        )
                if article.get("description") != page["description"]:
                    self.problems.append(
                        f"structured data: {path}: TechArticle description is "
                        "not the page's own"
                    )

    def gate_machine_files(self) -> None:
        """llms.txt, llms-full.txt, both security.txt files and .nojekyll exist
        and name the real host."""
        for name in (LLMS_TXT, LLMS_FULL_TXT, SECURITY_TXT, WELL_KNOWN_SECURITY_TXT):
            if name not in self.output_files:
                self.problems.append(f"machine files: docs/{name} is missing")
                continue
            text = self.output_files[name].decode("utf-8", errors="replace")
            if self.site["host"] not in text:
                self.problems.append(
                    f"machine files: docs/{name} does not name {self.site['host']}"
                )
        if NOJEKYLL not in self.output_files:
            self.problems.append(f"machine files: docs/{NOJEKYLL} is missing")
        # RFC 9116: a Contact and an Expires are required.
        if SECURITY_TXT in self.output_files:
            text = self.output_files[SECURITY_TXT].decode("utf-8")
            for field in ("Contact:", "Expires:", "Preferred-Languages:", "Canonical:"):
                if field not in text:
                    self.problems.append(
                        f"machine files: security.txt has no {field} line"
                    )
            if SECURITY_CONTACT not in text:
                self.problems.append(
                    "machine files: security.txt Contact is not the advisories URL"
                )
            if f"Canonical: {BASE_URL}/.well-known/{SECURITY_TXT}" not in text:
                self.problems.append(
                    "machine files: security.txt Canonical is not the well-known URL"
                )
        # llms.txt is a Markdown index: a title, a summary and absolute links.
        if LLMS_TXT in self.output_files:
            text = self.output_files[LLMS_TXT].decode("utf-8")
            if not text.startswith("# "):
                self.problems.append("machine files: llms.txt has no title")
            if not re.search(r"^> ", text, re.MULTILINE):
                self.problems.append("machine files: llms.txt has no summary line")
            links = re.findall(r"^- \[[^\]]+\]\(([^)]+)\)", text, re.MULTILINE)
            if not links:
                self.problems.append("machine files: llms.txt has no links")
            for link in links:
                if not link.startswith(BASE_URL):
                    self.problems.append(
                        f"machine files: llms.txt link is not absolute: {link}"
                    )
        # llms-full.txt carries every page's prose and stays a sensible size.
        if LLMS_FULL_TXT in self.output_files:
            text = self.output_files[LLMS_FULL_TXT].decode("utf-8")
            for page in self.pages:
                if page["path"] == "/404.html":
                    continue
                if f"## {page['title']}" not in text:
                    self.problems.append(
                        f"machine files: llms-full.txt is missing {page['path']}"
                    )
            if len(text.encode("utf-8")) > LLMS_FULL_MAX_BYTES:
                self.problems.append(
                    f"machine files: llms-full.txt is {len(text.encode('utf-8'))} "
                    f"bytes (max {LLMS_FULL_MAX_BYTES})"
                )

    def gate_harness_pages(self) -> None:
        """The four harness pages exist, each names its harness, and each gives
        that harness's own command."""
        for path in HARNESS_PAGES:
            if path not in self.rendered:
                self.problems.append(f"harness: {path} does not exist")
                continue
            page = next(p for p in self.pages if p["path"] == path)
            harness, command = HARNESS_TITLES[path]
            if harness.lower() not in page["title"].lower():
                self.problems.append(
                    f"harness: {path}: title does not name {harness}: {page['title']}"
                )
            if command not in self.rendered[path]:
                self.problems.append(
                    f"harness: {path}: the page does not give the {command} command"
                )
            if BASE_URL + "/features/agents/" not in self.rendered[path]:
                self.problems.append(
                    f"harness: {path}: no link back to /features/agents/"
                )
            if BASE_URL + "/quick-start/" not in self.rendered[path] and (
                'href="/quick-start/"' not in self.rendered[path]
            ):
                self.problems.append(f"harness: {path}: no link back to /quick-start/")

    def gate_sitemap_priority(self) -> None:
        """The four harness pages are in the sitemap at priority 0.6."""
        text = self.output_files["sitemap.xml"].decode("utf-8")
        for path in HARNESS_PAGES:
            url = BASE_URL + path
            if f"<loc>{url}</loc>" not in text:
                self.problems.append(f"sitemap: {url} is missing")
                continue
            entry = text.split(f"<loc>{url}</loc>", 1)[1].split("</url>", 1)[0]
            if "<priority>0.6</priority>" not in entry:
                self.problems.append(
                    f"sitemap: {url} is not at priority 0.6"
                )
        # llms.txt and security.txt are not pages and must not be in the sitemap.
        for name in (LLMS_TXT, LLMS_FULL_TXT, SECURITY_TXT):
            if f"/{name}</loc>" in text:
                self.problems.append(f"sitemap: {name} is not a page and is listed")

    def gate_links(self) -> None:
        produced = set()
        for page in self.pages:
            produced.add(self.path_to_file(page["path"]))
        produced.update(
            {
                "assets/style.css",
                "assets/site.js",
                "favicon.svg",
                "og.png",
                "sitemap.xml",
                "robots.txt",
                "CNAME",
                LLMS_TXT,
                LLMS_FULL_TXT,
                SECURITY_TXT,
                WELL_KNOWN_SECURITY_TXT,
                NOJEKYLL,
            }
        )
        for name in self.diagrams:
            produced.add(f"assets/diagrams/{name}.svg")
        for slug in self.shots:
            produced.add(f"assets/shots/{slug}.webp")
            produced.add(f"assets/shots/{slug}-thumb.webp")
        page_ids = {}
        parsed = {}
        for page in self.pages:
            collector = LinkCollector()
            collector.feed(self.rendered[page["path"]])
            parsed[page["path"]] = collector
            page_ids[self.path_to_file(page["path"])] = collector.ids
            if collector.h1 != 1:
                self.problems.append(
                    f"metadata: {page['path']}: {collector.h1} <h1> elements (need exactly 1)"
                )
            for alt in collector.imgs:
                if not alt.strip():
                    self.problems.append(
                        f"alt text: {page['path']}: an <img> has empty alt"
                    )
            for block in collector.ldjson:
                try:
                    json.loads(block)
                except json.JSONDecodeError as exc:
                    self.problems.append(
                        f"json-ld: {page['path']}: does not parse: {exc}"
                    )
        for page in self.pages:
            collector = parsed[page["path"]]
            for _kind, target in collector.links:
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "tel:", "data:")
                ):
                    continue
                if target.startswith("#"):
                    if target[1:] not in collector.ids:
                        self.problems.append(
                            f"link: {page['path']}: fragment {target} not found"
                        )
                    continue
                clean = target.split("#", 1)[0].split("?", 1)[0]
                if not clean:
                    continue
                resolved = self.resolve_link(page["path"], clean)
                if resolved not in produced:
                    self.problems.append(
                        f"link: {page['path']}: {target} does not resolve to a produced file"
                    )
                    continue
                if "#" in target:
                    frag = target.split("#", 1)[1]
                    if resolved in page_ids and frag not in page_ids[resolved]:
                        self.problems.append(
                            f"link: {page['path']}: fragment #{frag} not found in {resolved}"
                        )

    @staticmethod
    def path_to_file(path: str) -> str:
        if path == "/":
            return "index.html"
        if path.endswith("/"):
            return path.lstrip("/") + "index.html"
        return path.lstrip("/")

    @staticmethod
    def resolve_link(from_path: str, clean: str) -> str | None:
        if clean.startswith("/"):
            target = clean.lstrip("/")
            if clean.endswith("/"):
                return target + "index.html"
            return target
        # relative link: resolve against the page's directory
        base = pathlib.PurePosixPath(Build.path_to_file(from_path)).parent
        parts = []
        for part in (str(base) + "/" + clean).split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        resolved = "/".join(parts)
        if clean.endswith("/"):
            resolved += "/index.html"
        return resolved

    def gate_sitemap(self) -> None:
        for page in self.pages:
            if page["path"] == "/404.html":
                continue
            target = self.path_to_file(page["path"])
            if target not in self.output_files:
                self.problems.append(
                    f"sitemap: {BASE_URL + page['path']} has no file at {target}"
                )

    def gate_tokens(self) -> None:
        css = (SITE / "static" / "style.css").read_text(encoding="utf-8")
        for lineno, line in enumerate(css.splitlines(), start=1):
            if re.search(r"#[0-9a-fA-F]{3,8}\b", line):
                self.problems.append(f"tokens: style.css:{lineno}: literal hex colour")
            if re.search(r"\b(rgb|rgba|hsl|hsla)\(", line):
                self.problems.append(
                    f"tokens: style.css:{lineno}: literal colour function"
                )

    def gate_minimums(self) -> None:
        for page in self.pages:
            path = page["path"]
            kind, minw, maxw, minimg, mindia = PAGE_SPECS[path]
            main_html = re.search(r"(?is)<main[^>]*>(.*?)</main>", self.rendered[path])
            body = main_html.group(1) if main_html else self.rendered[path]
            words = word_count(body)
            if words < minw:
                self.problems.append(f"minimum: {path}: {words} words (need {minw})")
            if maxw and words > maxw:
                self.problems.append(f"minimum: {path}: {words} words (max {maxw})")
            if len(page["_images"]) < minimg:
                self.problems.append(
                    f"minimum: {path}: {len(page['_images'])} images (need {minimg})"
                )
            if page["_diagram_count"] < mindia:
                self.problems.append(
                    f"minimum: {path}: {page['_diagram_count']} diagrams (need {mindia})"
                )
            if kind == "architecture":
                for b in page["blocks"]:
                    if b["type"] == "diagram" and (
                        "<title" not in b["svg"]
                        or "<desc" not in b["svg"]
                        or 'role="img"' not in b["svg"]
                    ):
                        self.problems.append(
                            f"diagram: {path}: {b['name']} lacks role/title/desc"
                        )
            if path == "/gallery/" and (
                page["_gallery_covered"] != page["_gallery_expected"]
            ):
                self.problems.append(
                    "minimum: /gallery/: approved shots missing or duplicated"
                )
        # every feature page image must exist in the manifest
        for page in self.pages:
            for slug in page["_images"]:
                if slug not in self.shots:
                    self.problems.append(
                        f"minimum: {page['path']}: unknown shot {slug}"
                    )
        # no image slot on a page that has none: nothing to check here.

    def gate_claims(self) -> None:
        for page in self.pages:
            if page["path"] == "/404.html":
                continue
            claims = page.get("claims", [])
            if not claims:
                self.problems.append(f"claims: {page['path']}: no claims list")
                continue
            for i, row in enumerate(claims, start=1):
                if (
                    not row.get("claim", "").strip()
                    or not row.get("evidence", "").strip()
                ):
                    self.problems.append(
                        f"claims: {page['path']}: claim {i} has no evidence"
                    )

    # ------------------------------------------------------------ output
    def sitemap_urls(self) -> list[str]:
        urls = []
        for page in self.pages:
            if page["path"] == "/404.html":
                continue
            urls.append(BASE_URL + page["path"])
        return urls

    @staticmethod
    def priority(path: str) -> str:
        if path == "/":
            return "1.0"
        if path.startswith("/features") or path.startswith("/quick-start"):
            return "0.9"
        if path.startswith("/architecture") or path.startswith("/configuration"):
            return "0.8"
        if path in ("/gallery/", "/faq/", "/glossary/", "/troubleshooting/"):
            return "0.7"
        if path == "/about/":
            return "0.5"
        if path == "/license/":
            return "0.4"
        return "0.6"
    # ------------------------------------------------- machine-readable files
    def page_prose(self, path: str) -> str:
        """A page's main prose as plain text, without navigation or chrome.

        Built from the same blocks the HTML is built from, so the two cannot
        drift: a sentence added to a page appears in both surfaces or neither.
        """
        page = next(p for p in self.pages if p["path"] == path)
        parts = [plain_text(page.get("h1", "")), plain_text(page.get("intro", ""))]
        for block in page["blocks"]:
            kind = block["type"]
            if kind in ("h2", "h3", "p"):
                parts.append(plain_text(block.get("text", "")))
            elif kind in ("ul", "ol"):
                parts.extend(plain_text(x) for x in block.get("items", []))
            elif kind == "table":
                parts.append(plain_text(block.get("caption", "")))
                parts.extend(plain_text(h) for h in block.get("head", []))
                for row in block.get("rows", []):
                    parts.extend(plain_text(c) for c in row)
            elif kind == "figure":
                parts.append(plain_text(block.get("caption", "")))
            elif kind == "diagram":
                parts.append(plain_text(block.get("caption", "")))
                parts.append(plain_text(block.get("alt", "")))
            elif kind == "code":
                parts.append(plain_text(block.get("label", "")))
                parts.append(block.get("text", ""))
            elif kind == "callout":
                parts.append(plain_text(block.get("text", "")))
            elif kind == "cards":
                for card in block.get("cards", []):
                    parts.append(plain_text(card.get("title", "")))
                    parts.append(plain_text(card.get("text", "")))
            elif kind == "facts":
                for fact in block.get("items", []):
                    parts.append(plain_text(fact.get("value", "")))
                    parts.append(plain_text(fact.get("label", "")))
            elif kind == "related":
                parts.extend(plain_text(l.get("label", "")) for l in block.get("links", []))
            elif kind == "faq":
                for item in block.get("items", []):
                    parts.append(plain_text(item.get("q", "")))
                    parts.append(plain_text(item.get("a", "")))
            elif kind == "terms":
                for term in block.get("items", []):
                    parts.append(plain_text(term.get("term", "")))
                    parts.append(plain_text(term.get("def", "")))
            elif kind == "gallery_area":
                parts.append(plain_text(block.get("title", "")))
                parts.append(plain_text(block.get("intro", "")))
        return "\n\n".join(p for p in parts if p.strip())

    def llms_txt(self) -> str:
        """The llms.txt index: a map of the site, not a pitch."""
        summary = (
            "A local multi-provider LLM proxy, model router and control plane "
            "for AI coding agents."
        )
        lines = [
            "# My Claude Code",
            "",
            f"> {summary}",
            "",
            (
                "My Claude Code (MCC) is a local LLM gateway: a proxy, a model "
                "router and a control plane that sits in front of the coding "
                "agents you already use. It is licensed AGPL-3.0-or-later, or "
                "under a commercial licence by negotiation, and this site "
                f"documents release {self.site['version']}. It runs entirely on "
                "your machine, with your keys: there is no telemetry, no "
                "account and no hosted dashboard."
            ),
            "",
            "## Docs",
            "",
        ]
        index = [
            ("/features/", "Every capability area, with a deep page for each."),
            (
                "/architecture/",
                "How the proxy, the router and the credential layer are built.",
            ),
            (
                "/quick-start/",
                "Install, run, and the first five minutes with a coding agent.",
            ),
            (
                "/configuration/",
                "Where the settings live and how a change takes effect.",
            ),
            ("/gallery/", "The product's own screens, reviewed and captioned."),
            ("/faq/", "The questions this site is asked most, answered."),
            ("/glossary/", "Every term the product uses, defined once."),
            ("/license/", "The AGPL terms, the commercial option and trademarks."),
        ]
        for path, description in index:
            lines.append(f"- [{path}]({BASE_URL}{path}): {description}")
        lines.extend(["", "## For your agent", ""])
        for path in HARNESS_PAGES:
            block = self.seo["pages"][path.strip("/")]
            lines.append(f"- [{path}]({BASE_URL}{path}): {block['description']}")
        lines.extend(
            [
                "",
                "## Optional",
                "",
                (
                    f"- [{LLMS_FULL_TXT}]({BASE_URL}/{LLMS_FULL_TXT}): "
                    "the prose of every page in one plain-text file."
                ),
                (
                    f"- [{SECURITY_TXT}]({BASE_URL}/.well-known/{SECURITY_TXT}): "
                    "how to report a vulnerability, per RFC 9116."
                ),
            ]
        )
        return "\n".join(lines) + "\n"

    def llms_full_txt(self) -> str:
        """Every page's prose, concatenated, generated from the same content."""
        lines = [
            "# My Claude Code",
            "",
            f"Release {self.site['version']}. {self.site['software_description']}",
            "",
            self.site["licence_line"],
            "",
        ]
        for page in self.pages:
            if page["path"] == "/404.html":
                continue
            lines.append(f"## {page['title']}")
            lines.append(f"{BASE_URL}{page['path']}")
            lines.append("")
            lines.append(self.page_prose(page["path"]))
            lines.append("")
        return "\n".join(lines).rstrip("\n") + "\n"

    def security_txt(self) -> str:
        """RFC 9116, at both the root and the well-known path."""
        return (
            f"Contact: {SECURITY_CONTACT}\n"
            f"Expires: {SECURITY_EXPIRES}\n"
            "Preferred-Languages: en\n"
            f"Canonical: {BASE_URL}/.well-known/{SECURITY_TXT}\n"
        )

    def build_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for page in self.pages:
            rel = self.path_to_file(page["path"])
            files[rel] = self.rendered[page["path"]].encode("utf-8")
        # CSS: generated token block + static body
        css = (
            token_css(self.tokens)
            + "\n"
            + (SITE / "static" / "style.css").read_text(encoding="utf-8")
        )
        files["assets/style.css"] = css.encode("utf-8")
        files["assets/site.js"] = (SITE / "static" / "site.js").read_bytes()
        files["favicon.svg"] = (SITE / "static" / "favicon.svg").read_bytes()
        for name, svg in self.diagrams.items():
            files[f"assets/diagrams/{name}.svg"] = svg.encode("utf-8")
        for slug in sorted(self.shots):
            files[f"assets/shots/{slug}.webp"] = (
                SITE / "shots" / f"{slug}.webp"
            ).read_bytes()
            files[f"assets/shots/{slug}-thumb.webp"] = (
                SITE / "shots" / f"{slug}-thumb.webp"
            ).read_bytes()
        files["og.png"] = (SITE / "static" / "og.png").read_bytes()
        files["CNAME"] = (self.site["host"] + "\n").encode("utf-8")
        files["robots.txt"] = (
            f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n"
        ).encode()
        files[LLMS_TXT] = self.llms_txt().encode("utf-8")
        files[LLMS_FULL_TXT] = self.llms_full_txt().encode("utf-8")
        security = self.security_txt()
        files[SECURITY_TXT] = security.encode("utf-8")
        files[WELL_KNOWN_SECURITY_TXT] = security.encode("utf-8")
        # GitHub Pages runs Jekyll, which drops dot-directories: without this
        # file /.well-known/security.txt is never served. The content is empty
        # by design; the file's existence is the whole signal.
        files[NOJEKYLL] = b""
        # sitemap
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for url in self.sitemap_urls():
            path = url[len(BASE_URL) :] or "/"
            lines.append("  <url>")
            lines.append(f"    <loc>{url}</loc>")
            lines.append(f"    <lastmod>{self.site['lastmod']}</lastmod>")
            lines.append(f"    <priority>{self.priority(path)}</priority>")
            lines.append("  </url>")
        lines.append("</urlset>")
        files["sitemap.xml"] = ("\n".join(lines) + "\n").encode("utf-8")
        files[MANIFEST_NAME] = (json.dumps(sorted(files), indent=2) + "\n").encode(
            "utf-8"
        )
        return files

    def write(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        for rel, data in sorted(self.output_files.items()):
            target = self.out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def run(self) -> int:
        self.prepare()
        self.render_all()
        # First pass output set so the sitemap/asset gates have files to see.
        self.output_files = self.build_files()
        # Write into the target only after gates that need existing assets;
        # the OG gate reads from the produced bytes instead.
        self.gate_secrets()
        self.gate_forbidden()
        self.gate_metadata()
        self.gate_keywords()
        self.gate_links()
        self.gate_structured_data()
        self.gate_machine_files()
        self.gate_harness_pages()
        self.gate_tokens()
        self.gate_minimums()
        self.gate_claims()
        self.gate_og_bytes()
        self.gate_sitemap()
        self.gate_sitemap_priority()
        if self.problems:
            print(f"build failed: {len(self.problems)} problem(s)", file=sys.stderr)
            for problem in self.problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        return 0

    def gate_og_bytes(self) -> None:
        data = self.output_files["og.png"]
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            self.problems.append("og image: docs/og.png is not a PNG")
            return
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if (width, height) != (1200, 630):
            self.problems.append(f"og image: is {width}x{height} (need 1200x630)")


def compare(out: pathlib.Path, docs: pathlib.Path) -> list[str]:
    manifest_path = docs / MANIFEST_NAME
    if not manifest_path.exists():
        return [MANIFEST_NAME]
    old_bytes = manifest_path.read_bytes()
    new_bytes = (out / MANIFEST_NAME).read_bytes()
    old = set(json.loads(old_bytes.decode("utf-8")))
    new = set(json.loads(new_bytes.decode("utf-8")))
    diffs = []
    if old_bytes != new_bytes:
        diffs.append(MANIFEST_NAME)
    for rel in sorted(new):
        fresh = (out / rel).read_bytes()
        target = docs / rel
        if not target.exists() or target.read_bytes() != fresh:
            diffs.append(rel)
    for rel in sorted(old - new):
        if rel not in diffs:
            diffs.append(rel + " (stale)")
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            build = Build(tmpdir)
            code = build.run()
            if code:
                return code
            build.write()
            diffs = compare(tmpdir, DOCS)
            if diffs:
                print("check: docs/ differs from a fresh build:")
                for name in diffs:
                    print(f"  {name}")
                return 1
            print("check: docs/ is byte-identical to a fresh build")
            return 0

    out = args.out or DOCS
    build = Build(out)
    code = build.run()
    if code:
        return code
    build.write()
    print(f"built {len(build.output_files)} files into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
