#!/usr/bin/env python3
"""Stage the documentation into a directory MkDocs can build.

    uv run --offline python scripts/build_docs_site.py --out build/docs-src

WHY A STAGING STEP AND NOT `docs_dir: docs`

    The pages in ``docs/`` are read in three places and only one of them is
    the website:

    * on GitHub, where a relative ``../ARCHITECTURE.md`` resolves;
    * in the dashboard's own **Docs** page, which bundles them into the wheel
      and resolves the same relative links itself (``Document.bundle_as`` and
      ``resolve_relative_link``); and
    * on the site built from here, where ``docs/`` is the root and anything
      above it does not exist.

    Rewriting those links in the repository would fix the third reader by
    breaking the second -- the dashboard would send someone to GitHub for a
    page it has locally, offline, for the version they actually installed.
    So the rewrite happens on a copy, and the committed Markdown keeps the
    links the other two readers need.

WHAT IT REWRITES

    Every relative link, resolved against the directory the page came FROM
    rather than where it is staged. A link whose target is also a page on the
    site stays a link between pages; anything else in the repository becomes
    an absolute ``blob/main`` URL, because those are files you read on GitHub
    and there is nowhere else for them to point.

    ``ARCHITECTURE.md`` and ``CONTRIBUTING.md`` are copied up into the staged
    tree so their links become same-directory links and the pages exist. That
    matters more than it looks: ARCHITECTURE lives at the repository root, so
    ITS relative links are root-relative (``src/...``, ``pyproject.toml``) and
    a rewriter that only understood ``../`` left seventeen of them dangling --
    which ``mkdocs build --strict`` refused, on the pull request, which is
    what that flag is for.

    ``docs/_site_home.md`` becomes ``index.md`` and ``docs/README.md`` becomes
    ``documentation.md``.

    The site's home page used to be the documentation index, which is a table
    of what exists -- the right page for a reader who is already here and the
    wrong one for the reader this site is for, who arrived from a search engine
    and does not yet know what MCC is. Those two jobs do not fit on one page,
    and ``docs/README.md`` cannot be turned into the second of them: it is the
    index the dashboard's Docs page renders offline, for the version someone
    installed. So the site gets a page of its own, and the index keeps its job
    one URL over. ``SITE_ONLY`` names the files that exist for this reader and
    nobody else; they are staged under their site names and never under their
    own.

WHAT ELSE IT WRITES

    A YAML front-matter ``title:`` on the staged copy of a handful of pages
    (``SITE_TITLES``). Material takes a page's ``<title>`` from its first
    heading, and a heading written to sit at the top of a document is not
    always a good search result: "My Claude Code — Complete Usage Guide"
    renders as "My Claude Code — Complete Usage Guide - My Claude Code". The
    front matter is added to the copy for the same reason the links are
    rewritten on the copy -- the heading is what the other two readers see.
"""

import argparse
import pathlib
import posixpath
import re
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
GITHUB = "https://github.com/FiredMosquito831/my-claude-code"

#: The published host. Written into `CNAME` and `robots.txt`; `mkdocs.yml`'s
#: `site_url` carries the same host for the canonical links and the sitemap,
#: and `tests/contracts/test_docs_site_contract.py` pins the two together so
#: they cannot drift into disagreeing about where the site is.
SITE_HOST = "myclaudecode.danubelabs.net"

#: Copied up into the staged root so their links keep working as pages.
PROMOTED = ("ARCHITECTURE.md", "CONTRIBUTING.md")

#: ``docs/`` path -> the name it is staged under. Everything in here is written
#: for the site alone, so it is staged under the site's name and never under
#: its own: staging ``_site_home.md`` as well would publish the home page twice
#: at two URLs, which is a duplicate a search engine has to pick between.
SITE_ONLY = {
    "_site_home.md": "index.md",
}

#: ``docs/README.md`` is the index the dashboard renders. It stays a page here
#: -- other pages link to it -- but one URL over, because the site's home page
#: is now `_site_home.md`.
DOCS_INDEX = "documentation.md"

#: Staged name -> the ``<title>`` the page should carry, injected as YAML front
#: matter on the copy. Only the pages whose first heading reads badly as a
#: search result are listed; a page absent from here keeps its heading, which
#: is the right default.
SITE_TITLES = {
    # "My Claude Code — Complete Usage Guide" renders as
    # "My Claude Code — Complete Usage Guide - My Claude Code".
    "USAGE.md": "Usage guide",
    # "Documentation" alone says nothing about whose.
    DOCS_INDEX: "All documentation",
    # "Release Cutover Checklist — My Claude Code (MCC) v5" names a version
    # this project passed four majors ago.
    "RELEASE-CHECKLIST.md": "Release checklist",
    "BRAND.md": "Brand",
    # "AGENT SPEC — Websearch Advanced Options + Rich Digest" is a working
    # title in shouting case.
    "AGENT_SPEC_WEBSEARCH_ADV.md": "Web search: advanced options and rich digest",
}

#: ``](target)`` for an inline Markdown link. Deliberately not a Markdown
#: parser: the only links that need touching are the relative ones, and their
#: targets are plain paths.
_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^()\s]+)\)")

#: ``src="..."`` on a raw HTML ``<img>``. Every screenshot in these pages is
#: one of these rather than a Markdown image, because the committed Markdown
#: centres them in a ``<div align="center">`` that GitHub honours.
_IMG_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")(?!https?://|data:)([^"]+)(")')


def _staged_name(repo_path: str, staged: frozenset[str]) -> str | None:
    """Where ``repo_path`` ends up in the staged tree, or ``None``.

    Membership in ``staged`` is checked rather than assumed. An earlier
    revision returned a name for anything under ``docs/`` on the strength of
    the prefix alone, which quietly claimed that ``docs/research/...`` and a
    link to the ``adr`` *directory* were pages -- and a link to a page that
    does not exist is exactly what this function is supposed to catch.
    """

    if repo_path == "docs/README.md":
        candidate = DOCS_INDEX
    elif repo_path.startswith("docs/") and repo_path[len("docs/") :] in SITE_ONLY:
        candidate = SITE_ONLY[repo_path[len("docs/") :]]
    elif repo_path.startswith("docs/"):
        candidate = repo_path[len("docs/") :]
    elif repo_path in PROMOTED or repo_path.startswith("assets/"):
        # Images are staged flat under `assets/`, and an image must stay an
        # image: a `blob/main` URL renders GitHub's file page, so a picture
        # sent there is a broken picture rather than a working link.
        candidate = repo_path
    else:
        return None
    return candidate if candidate in staged else None


def _rewrite(text: str, *, source_dir: str, staged: frozenset[str]) -> str:
    """Repoint every relative link so it resolves on the site.

    ``source_dir`` is the directory the page came from, relative to the
    repository root (``docs`` for most pages, ``""`` for a promoted root file)
    -- a link is relative to where the file WAS, not to where it is staged.

    A link to something that is also a page on the site stays a link between
    pages. A link to anything else in the repository -- source files,
    ``pyproject.toml``, the install scripts -- becomes an absolute ``blob/main``
    URL, because those are files you read on GitHub and there is nowhere else
    for them to point.
    """

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        path, _, fragment = target.partition("#")
        suffix = f"#{fragment}" if fragment else ""
        if not path:
            return match.group(0)
        repo_path = posixpath.normpath(posixpath.join(source_dir, path))
        name = _staged_name(repo_path, staged)
        if name is not None:
            # Every staged page sits at the root of the staged tree except the
            # decision records, so a bare name is the link between them.
            return f"]({name}{suffix})"
        # `blob` for a file, `tree` for a directory: GitHub serves a directory
        # listing under `tree` and 404s it under `blob`.
        kind = "tree" if (REPO / repo_path).is_dir() else "blob"
        return f"]({GITHUB}/{kind}/main/{repo_path}{suffix})"

    return _LINK.sub(replace, text)


def _rewrite_images(text: str, *, source_dir: str, url_prefix: str) -> str:
    """Repoint every raw ``<img src>`` at the flat staged ``assets/``.

    An ``<img>`` is not a Markdown link, so ``_rewrite`` never sees one, and
    until 7.13 nothing rewrote them: a page in ``docs/`` says
    ``../assets/x.png``, the page is served from ``/USAGE/``, and ``../assets``
    from there happens to be ``/assets`` -- correct by coincidence rather than
    by construction.

    ``ARCHITECTURE.md`` is where the coincidence ran out. It lives at the
    repository ROOT, so its images are written ``assets/x.png``, which from
    ``/ARCHITECTURE/`` resolves to ``/ARCHITECTURE/assets/x.png``. Both of its
    screenshots were broken on the published site and no check caught it:
    ``mkdocs build --strict`` validates Markdown links and says nothing about
    the ``src`` of raw HTML.

    So this resolves each ``src`` against the directory the page came FROM,
    exactly as the link rewriter does, and re-emits it against the directory
    the page is SERVED from -- ``url_prefix``, which is ``../`` for a page at
    the root of the staged tree because ``use_directory_urls`` serves
    ``USAGE.md`` at ``/USAGE/``.
    """

    def replace(match: re.Match[str]) -> str:
        head, target, tail = match.groups()
        repo_path = posixpath.normpath(posixpath.join(source_dir, target))
        if not repo_path.startswith("assets/"):
            # Anything that is not one of the staged images is left exactly as
            # it is: this function's job is images, not link repair.
            return match.group(0)
        return f"{head}{url_prefix}{repo_path}{tail}"

    return _IMG_SRC.sub(replace, text)


def build(out: pathlib.Path) -> int:
    docs = REPO / "docs"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    written = 0
    # Markdown staged in the first pass, and the directory it came from. The
    # rewrite is a second pass because it has to know what was actually
    # staged, which is not true until the tree is finished.
    pending: dict[pathlib.Path, str] = {}

    for source in sorted(docs.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(docs)
        posix = relative.as_posix()
        staged_as = DOCS_INDEX if posix == "README.md" else SITE_ONLY.get(posix, posix)
        target = out / staged_as
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if source.suffix == ".md":
            pending[target] = "docs"
        written += 1

    for name in PROMOTED:
        source = REPO / name
        if not source.exists():
            raise SystemExit(f"{name} is missing from the repository root")
        shutil.copy2(source, out / name)
        pending[out / name] = ""
        written += 1

    # The images the staged pages reference, and the brand mark the theme uses
    # for its logo and favicon.
    assets = out / "assets"
    assets.mkdir(exist_ok=True)
    for pattern in ("*.png", "*.svg"):
        for image in sorted((REPO / "assets").glob(pattern)):
            shutil.copy2(image, assets / image.name)
            written += 1
    shutil.copy2(
        REPO / "src" / "my_claude_code" / "assets" / "app-icon.png",
        assets / "app-icon.png",
    )
    written += 1

    # The custom domain, as a file in the published site as well as a setting on
    # the repository. With an Actions deploy the setting alone usually holds,
    # but a redeploy that loses it takes the domain down, and a file cannot be
    # lost that way. MkDocs copies anything it does not recognise straight
    # through to the built site.
    (out / "CNAME").write_text(f"{SITE_HOST}\n", encoding="utf-8", newline="\n")

    # MkDocs writes a sitemap but no robots.txt, so the sitemap is something a
    # crawler has to be told about rather than find.
    (out / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: https://{SITE_HOST}/sitemap.xml\n",
        encoding="utf-8",
        newline="\n",
    )
    written += 2

    staged = frozenset(
        path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()
    )
    for path, source_dir in pending.items():
        relative = path.relative_to(out).as_posix()
        text = _rewrite(
            path.read_text(encoding="utf-8"), source_dir=source_dir, staged=staged
        )
        # How deep the page's URL is, which is what a relative `src` on it
        # resolves against. `index.md` is served at the root of its directory;
        # every other page gets a directory of its own.
        depth = relative.count("/") + (0 if relative.endswith("index.md") else 1)
        text = _rewrite_images(text, source_dir=source_dir, url_prefix="../" * depth)
        title = SITE_TITLES.get(relative)
        if title is not None:
            # MkDocs' `meta` extension strips this before the Markdown is
            # rendered, so it changes the <title> and the nav label and leaves
            # the body -- heading included -- exactly as the other two readers
            # see it.
            text = f"---\ntitle: {title}\n---\n\n{text}"
        path.write_text(text, encoding="utf-8", newline="\n")

    print(f"staged {written} files into {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # A literal, not `__doc__.splitlines()[0]`: `__doc__` is `str | None`
    # because it is stripped under `-OO`, and the checker is right to say so.
    parser = argparse.ArgumentParser(
        description="Stage the documentation into a directory MkDocs can build."
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=REPO / "build" / "docs-src",
        help="where to stage; defaults to build/docs-src",
    )
    args = parser.parse_args(argv)
    return build(args.out.resolve())


if __name__ == "__main__":
    sys.exit(main())
