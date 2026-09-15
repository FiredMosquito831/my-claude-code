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

    ``docs/README.md`` becomes ``index.md``: the site's home page is the
    documentation index, not the project README. The README is a map written
    for someone deciding whether to install MCC; a docs site is for someone
    who already did.
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

#: ``](target)`` for an inline Markdown link. Deliberately not a Markdown
#: parser: the only links that need touching are the relative ones, and their
#: targets are plain paths.
_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^()\s]+)\)")


def _staged_name(repo_path: str, staged: frozenset[str]) -> str | None:
    """Where ``repo_path`` ends up in the staged tree, or ``None``.

    Membership in ``staged`` is checked rather than assumed. An earlier
    revision returned a name for anything under ``docs/`` on the strength of
    the prefix alone, which quietly claimed that ``docs/research/...`` and a
    link to the ``adr`` *directory* were pages -- and a link to a page that
    does not exist is exactly what this function is supposed to catch.
    """

    if repo_path == "docs/README.md":
        candidate = "index.md"
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
        target = out / ("index.md" if relative.as_posix() == "README.md" else relative)
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
        path.write_text(
            _rewrite(
                path.read_text(encoding="utf-8"), source_dir=source_dir, staged=staged
            ),
            encoding="utf-8",
            newline="\n",
        )

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
