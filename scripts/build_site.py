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
import shutil
import sys
import tempfile
from html.parser import HTMLParser

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO = pathlib.Path(__file__).resolve().parents[1]
SITE = REPO / "site"
DOCS = REPO / "docs"
BASE_URL = "https://myclaudecode.danubelabs.net"
MANIFEST_NAME = ".site-manifest.json"

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
    "/404.html": ("404", 60, 100, 0, 0),
}

SECRET_PATTERNS = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("stripe-key", re.compile(r"sk_(live|test)_[A-Za-z0-9]{8,}")),
    ("service-role", re.compile(r"service_role\s*[:=]\s*[A-Za-z0-9._-]{20,}")),
    ("postgres-url", re.compile(r"postgres(ql)?://[^\s\"'<]+")),
    ("mysql-url", re.compile(r"mysql://[^\s\"'<]+")),
    ("named-secret", re.compile(r"(?i)(password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*[^\s<]{6,}")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("private-ip", re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
    ("supabase-host", re.compile(r"\b[a-z]{20}\.supabase\.co\b")),
    ("owner-path", re.compile(r"C:\\Users\\|/home/|/Users/")),
]
FORBIDDEN = ["TODO", "TBD", "lorem", "YOUR_", "placeholder", "XXX", "example.com", "coming soon", "[insert", "FIXME"]
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
    fragment = re.sub(r"(?is)<(script|style|pre|aside|nav)[^>]*>.*?</\1>", " ", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    return re.sub(r"\s+", " ", fragment).strip()


def word_count(fragment: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", plain_text(fragment)))


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
        self.tokens = json.loads((SITE / "design-tokens.json").read_text(encoding="utf-8"))
        self.site = json.loads((SITE / "content" / "site.json").read_text(encoding="utf-8"))
        self.manifest = json.loads((SITE / "shots" / "manifest.json").read_text(encoding="utf-8"))
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
            if path.name == "site.json":
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
                        toc.append({"id": candidate, "text": plain_text(block["text"]), "level": 2})
                elif block["type"] == "diagram":
                    name = block["name"]
                    if name not in self.diagrams:
                        raise PageError(f"{page['path']}: unknown diagram {name}")
                    block["svg"] = self.diagrams[name]
                elif block["type"] == "gallery_area":
                    area = block["area"]
                    shots = [self.shot_thumb(m["slug"]) for m in self.manifest if m["area"] == area]
                    shots.sort(key=lambda s: s["slug"])
                    block["shots"] = shots
                elif block["type"] == "faq":
                    page["_faq"] = block["items"]
            page["_toc"] = toc
            page["_images"] = sorted({
                b["shot"] for b in page["blocks"] if b["type"] == "figure"
            })
            page["_diagram_count"] = sum(1 for b in page["blocks"] if b["type"] == "diagram")
            # gallery: every approved shot must appear exactly once across areas.
            if page["path"] == "/gallery/":
                covered = [m["slug"] for m in self.manifest]
                block_slugs = []
                for b in page["blocks"]:
                    if b["type"] == "gallery_area":
                        block_slugs.extend(s["slug"] for s in b["shots"])
                page["_gallery_covered"] = sorted(block_slugs)
                page["_gallery_expected"] = sorted(covered)

    # ------------------------------------------------------------ render
    def render_all(self) -> None:
        for page in self.pages:
            self.rendered[page["path"]] = self.render_page(page)

    def jsonld_for(self, page: dict, canonical: str) -> list[str]:
        blocks = [json.dumps({
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": page["title"],
            "url": canonical,
            "description": page["description"],
        }, ensure_ascii=False)]
        if page["path"] != "/" and page["path"] != "/404.html":
            crumbs = [{"@type": "ListItem", "position": 1, "name": "Home", "item": BASE_URL + "/"}]
            for i, crumb in enumerate(page.get("breadcrumbs", []), start=2):
                item = {"@type": "ListItem", "position": i, "name": plain_text(crumb["label"])}
                if crumb.get("href"):
                    item["item"] = BASE_URL + crumb["href"]
                crumbs.append(item)
            blocks.append(json.dumps({
                "@context": "https://schema.org",
                "@type": "BreadcrumbList",
                "itemListElement": crumbs,
            }, ensure_ascii=False))
        if page["path"] == "/":
            blocks.append(json.dumps({
                "@context": "https://schema.org",
                "@type": "SoftwareApplication",
                "name": "My Claude Code",
                "alternateName": "my-claude-code",
                "description": self.site["software_description"],
                "applicationCategory": "DeveloperApplication",
                "operatingSystem": "Windows, macOS, Linux",
                "softwareVersion": self.site["version"],
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                "license": "https://www.gnu.org/licenses/agpl-3.0.html",
                "isAccessibleForFree": True,
                "featureList": self.site["feature_list"],
                "codeRepository": self.site["repo"],
                "author": {"@type": "Person", "name": "FiredMosquito831", "url": "https://github.com/FiredMosquito831"},
            }, ensure_ascii=False))
        if page["path"] == "/faq/":
            items = page.get("_faq", [])
            blocks.append(json.dumps({
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {"@type": "Question", "name": plain_text(q["q"]),
                     "acceptedAnswer": {"@type": "Answer", "text": plain_text(q["a"])}}
                    for q in items
                ],
            }, ensure_ascii=False))
        if page["path"] == "/gallery/":
            images = [{"@type": "ImageObject", "contentUrl": f"{BASE_URL}/assets/shots/{m['slug']}.webp",
                       "description": m["alt"]} for m in self.manifest]
            images.sort(key=lambda x: x["contentUrl"])
            blocks.append(json.dumps({
                "@context": "https://schema.org",
                "@type": "ImageGallery",
                "image": images,
            }, ensure_ascii=False))
        return blocks

    def render_page(self, page: dict) -> str:
        path = page["path"]
        is_404 = path == "/404.html"
        canonical = "" if is_404 else BASE_URL + path
        nav = [dict(item, current=(path == item["href"] or (item["prefix"] != "/" and path.startswith(item["prefix"]))))
               for item in NAV]
        ctx = {
            "site": self.site,
            "title": page["title"],
            "og_title": page.get("og_title", page["title"]),
            "description": page["description"],
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
        targets = []
        for path in sorted(SITE.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".json", ".html", ".css", ".js", ".svg", ".md", ".txt"}:
                targets.append((path.relative_to(REPO).as_posix(), path.read_text(encoding="utf-8", errors="replace")))
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
                        self.problems.append(f"secret scan: {rel}:{lineno}: pattern {name}")

    def gate_forbidden(self) -> None:
        for rel, text in self._scan_targets():
            for lineno, line in enumerate(text.splitlines(), start=1):
                for token in FORBIDDEN:
                    if token.lower() in FORBIDDEN_CI:
                        hit = token.lower() in line.lower()
                    else:
                        hit = token in line
                    if hit:
                        self.problems.append(f"forbidden string: {rel}:{lineno}: {token}")

    def gate_metadata(self) -> None:
        titles, descriptions = {}, {}
        for page in self.pages:
            path = page["path"]
            title = page["title"]
            desc = page["description"]
            if len(title) > 60:
                self.problems.append(f"metadata: {path}: title is {len(title)} chars (max 60): {title}")
            if not (120 <= len(desc) <= 160):
                self.problems.append(f"metadata: {path}: description is {len(desc)} chars (need 120-160)")
            if title in titles:
                self.problems.append(f"metadata: {path}: duplicate title also on {titles[title]}")
            if desc in descriptions:
                self.problems.append(f"metadata: {path}: duplicate description also on {descriptions[desc]}")
            titles[title] = path
            descriptions[desc] = path

    def gate_links(self) -> None:
        produced = set()
        for page in self.pages:
            produced.add(self.path_to_file(page["path"]))
        produced.update({"assets/style.css", "assets/site.js", "favicon.svg", "og.png",
                         "sitemap.xml", "robots.txt", "CNAME"})
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
                self.problems.append(f"metadata: {page['path']}: {collector.h1} <h1> elements (need exactly 1)")
            for alt in collector.imgs:
                if not alt.strip():
                    self.problems.append(f"alt text: {page['path']}: an <img> has empty alt")
            for block in collector.ldjson:
                try:
                    json.loads(block)
                except json.JSONDecodeError as exc:
                    self.problems.append(f"json-ld: {page['path']}: does not parse: {exc}")
        for page in self.pages:
            collector = parsed[page["path"]]
            for kind, target in collector.links:
                if not target or target.startswith(("http://", "https://", "mailto:", "tel:", "data:")):
                    continue
                if target.startswith("#"):
                    if target[1:] not in collector.ids:
                        self.problems.append(f"link: {page['path']}: fragment {target} not found")
                    continue
                clean = target.split("#", 1)[0].split("?", 1)[0]
                if not clean:
                    continue
                resolved = self.resolve_link(page["path"], clean)
                if resolved not in produced:
                    self.problems.append(f"link: {page['path']}: {target} does not resolve to a produced file")
                    continue
                if "#" in target:
                    frag = target.split("#", 1)[1]
                    if resolved in page_ids and frag not in page_ids[resolved]:
                        self.problems.append(f"link: {page['path']}: fragment #{frag} not found in {resolved}")

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
                self.problems.append(f"sitemap: {BASE_URL + page['path']} has no file at {target}")

    def gate_tokens(self) -> None:
        css = (SITE / "static" / "style.css").read_text(encoding="utf-8")
        for lineno, line in enumerate(css.splitlines(), start=1):
            if re.search(r"#[0-9a-fA-F]{3,8}\b", line):
                self.problems.append(f"tokens: style.css:{lineno}: literal hex colour")
            if re.search(r"\b(rgb|rgba|hsl|hsla)\(", line):
                self.problems.append(f"tokens: style.css:{lineno}: literal colour function")

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
                self.problems.append(f"minimum: {path}: {len(page['_images'])} images (need {minimg})")
            if page["_diagram_count"] < mindia:
                self.problems.append(f"minimum: {path}: {page['_diagram_count']} diagrams (need {mindia})")
            if kind == "architecture":
                for b in page["blocks"]:
                    if b["type"] == "diagram":
                        if "<title" not in b["svg"] or "<desc" not in b["svg"] or 'role="img"' not in b["svg"]:
                            self.problems.append(f"diagram: {path}: {b['name']} lacks role/title/desc")
            if path == "/gallery/":
                if page["_gallery_covered"] != page["_gallery_expected"]:
                    self.problems.append("minimum: /gallery/: approved shots missing or duplicated")
        # every feature page image must exist in the manifest
        for page in self.pages:
            for slug in page["_images"]:
                if slug not in self.shots:
                    self.problems.append(f"minimum: {page['path']}: unknown shot {slug}")
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
                if not row.get("claim", "").strip() or not row.get("evidence", "").strip():
                    self.problems.append(f"claims: {page['path']}: claim {i} has no evidence")

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

    def build_files(self) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        for page in self.pages:
            rel = self.path_to_file(page["path"])
            files[rel] = self.rendered[page["path"]].encode("utf-8")
        # CSS: generated token block + static body
        css = token_css(self.tokens) + "\n" + (SITE / "static" / "style.css").read_text(encoding="utf-8")
        files["assets/style.css"] = css.encode("utf-8")
        files["assets/site.js"] = (SITE / "static" / "site.js").read_bytes()
        files["favicon.svg"] = (SITE / "static" / "favicon.svg").read_bytes()
        for name, svg in self.diagrams.items():
            files[f"assets/diagrams/{name}.svg"] = svg.encode("utf-8")
        for slug in sorted(self.shots):
            files[f"assets/shots/{slug}.webp"] = (SITE / "shots" / f"{slug}.webp").read_bytes()
            files[f"assets/shots/{slug}-thumb.webp"] = (SITE / "shots" / f"{slug}-thumb.webp").read_bytes()
        files["og.png"] = (SITE / "static" / "og.png").read_bytes()
        files["CNAME"] = (self.site["host"] + "\n").encode("utf-8")
        files["robots.txt"] = (f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n").encode("utf-8")
        # sitemap
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for url in self.sitemap_urls():
            path = url[len(BASE_URL):] or "/"
            lines.append("  <url>")
            lines.append(f"    <loc>{url}</loc>")
            lines.append(f"    <lastmod>{self.site['lastmod']}</lastmod>")
            lines.append(f"    <priority>{self.priority(path)}</priority>")
            lines.append("  </url>")
        lines.append("</urlset>")
        files["sitemap.xml"] = ("\n".join(lines) + "\n").encode("utf-8")
        files[MANIFEST_NAME] = (json.dumps(sorted(files), indent=2) + "\n").encode("utf-8")
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
        self.gate_links()
        self.gate_tokens()
        self.gate_minimums()
        self.gate_claims()
        self.gate_og_bytes()
        self.gate_sitemap()
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
