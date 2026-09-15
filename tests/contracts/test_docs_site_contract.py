"""The documentation site says the same thing about where it lives.

Three files know the published host: ``mkdocs.yml`` (which generates every
``<link rel="canonical">`` and every ``<loc>`` in the sitemap), the staging
script (which writes ``CNAME`` and ``robots.txt``), and the workflow (which
asserts the built output). Nothing forces them to agree, and the failure mode
when they disagree is silent: the site serves correctly, every link a human
clicks works, and the canonical quietly tells search engines the pages live
somewhere else.

That is not hypothetical. The site moved to its own domain and `site_url` was
left on the github.io URL; the workflow's SEO step checked that a canonical tag
was *present*, which it was, and passed. **A canonical can be present and
wrong**, which is the whole reason these assertions check values and not
existence.
"""

import importlib.util
import pathlib
import re
from urllib.parse import urlparse

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MKDOCS = _REPO_ROOT / "mkdocs.yml"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docs-site.yml"
_STAGER = _REPO_ROOT / "scripts" / "build_docs_site.py"


def _mkdocs_config() -> dict[str, object]:
    # `yaml.safe_load` is enough here: the file carries no Python tags, and the
    # keys this module reads are plain strings.
    loaded = yaml.safe_load(_MKDOCS.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _stager_module():
    """Import the staging script by path; `scripts/` is not a package."""

    spec = importlib.util.spec_from_file_location("mcc_docs_stager", _STAGER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_site_url_is_the_published_host() -> None:
    """`site_url` is what every canonical and every sitemap entry is built on."""

    site_url = _mkdocs_config()["site_url"]
    assert isinstance(site_url, str)
    parsed = urlparse(site_url)
    assert parsed.scheme == "https", f"site_url must be https, got {site_url!r}"
    assert parsed.netloc == _stager_module().SITE_HOST
    assert parsed.path == "/", (
        "the site is served at the root of its own domain; a path here would be "
        f"written into every canonical and every sitemap entry: {site_url!r}"
    )


def test_the_staged_site_declares_the_same_host_three_ways(tmp_path) -> None:
    """CNAME, robots.txt and `site_url` cannot disagree about the host."""

    stager = _stager_module()
    out = tmp_path / "docs-src"
    stager.build(out)

    cname = (out / "CNAME").read_text(encoding="utf-8").strip()
    assert cname == stager.SITE_HOST

    robots = (out / "robots.txt").read_text(encoding="utf-8")
    assert f"Sitemap: https://{stager.SITE_HOST}/sitemap.xml" in robots
    assert "User-agent: *" in robots

    site_url = _mkdocs_config()["site_url"]
    assert isinstance(site_url, str)
    assert urlparse(site_url).netloc == cname


def _workflow_host() -> str:
    """The host the workflow's SEO step pins, read out of its `host=` line.

    The step spells the hostname once into a shell variable and refers to
    ``$host`` after that, which is the right shape -- so this reads the
    definition rather than expecting the literal at every use.
    """

    workflow = _WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r'^\s*host="([^"]+)"\s*$', workflow, re.M)
    assert match is not None, (
        "the docs-site workflow no longer pins a host for its SEO assertions"
    )
    return match.group(1)


def test_the_workflow_pins_the_same_host_as_everything_else() -> None:
    assert _workflow_host() == _stager_module().SITE_HOST


def test_the_workflow_asserts_the_canonical_value_and_not_merely_its_presence() -> None:
    """The check that let a wrong canonical through must now read its value.

    The earlier step ran `grep -q '<link rel="canonical"'` -- true of a
    canonical pointing at any domain in the world, which is why it passed while
    all seventeen pages named the old host.
    """

    # Join shell line-continuations first, the way the shell does: the
    # every-page sweep spans two lines and its host comparison is on the
    # second, so a per-line assertion would read half a statement.
    workflow = re.sub(r"\\\n\s*", " ", _WORKFLOW.read_text(encoding="utf-8"))
    canonical_checks = [
        line for line in workflow.splitlines() if "canonical" in line and "grep" in line
    ]
    assert canonical_checks, "the workflow no longer checks the canonical link"
    assert all("$host" in line for line in canonical_checks), (
        "a canonical check that does not compare against $host passes on any "
        f"domain at all: {canonical_checks}"
    )


def test_the_workflow_checks_every_sitemap_entry_is_on_the_host() -> None:
    """A sitemap of URLs on the wrong host is a sitemap for another site."""

    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "<loc>https://$host/" in workflow, (
        "the workflow does not count sitemap entries that name the published host"
    )
    assert 'test "$locs" -eq "$on_host"' in workflow, (
        "the workflow counts sitemap entries on the host but never requires that "
        "count to equal the total, so a partly-wrong sitemap would pass"
    )
