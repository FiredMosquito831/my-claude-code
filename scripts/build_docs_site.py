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

    ``../ARCHITECTURE.md`` and ``../CONTRIBUTING.md`` are copied into the
    staged tree, so those links become same-directory links and the pages
    exist on the site. Everything else that points above ``docs/`` -- the
    README, ``.env.example``, the install and uninstall scripts -- is a file
    you would read on GitHub anyway, so those links become absolute
    ``blob/main`` URLs rather than dangling.

    ``docs/README.md`` becomes ``index.md``: the site's home page is the
    documentation index, not the project README. The README is a map written
    for someone deciding whether to install MCC; a docs site is for someone
    who already did.
"""

import argparse
import pathlib
import re
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
BLOB = "https://github.com/FiredMosquito831/my-claude-code/blob/main"

#: Copied up into the staged root so their links keep working as pages.
PROMOTED = ("ARCHITECTURE.md", "CONTRIBUTING.md")

#: ``](../thing)`` -> the target, for any link that escapes ``docs/``.
_PARENT_LINK = re.compile(r"\]\(\.\./([A-Za-z0-9._/-]+)\)")


def _rewrite(text: str) -> str:
    """Point every escaping link at something that exists on the site."""

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        if target in PROMOTED:
            return f"]({target})"
        if target == "README.md":
            # The site's own index is docs/README.md; the project README is a
            # different document and lives on GitHub.
            return f"]({BLOB}/README.md)"
        return f"]({BLOB}/{target})"

    return _PARENT_LINK.sub(replace, text)


def build(out: pathlib.Path) -> int:
    docs = REPO / "docs"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    written = 0
    for source in sorted(docs.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(docs)
        target = out / ("index.md" if relative.as_posix() == "README.md" else relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".md":
            target.write_text(
                _rewrite(source.read_text(encoding="utf-8")),
                encoding="utf-8",
                newline="\n",
            )
        else:
            shutil.copy2(source, target)
        written += 1

    for name in PROMOTED:
        source = REPO / name
        if not source.exists():
            raise SystemExit(f"{name} is missing from the repository root")
        (out / name).write_text(
            _rewrite(source.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="\n",
        )
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
