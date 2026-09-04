"""The winget manifests, and the six facts they borrow from elsewhere.

``desktop-shell/installer/winget/`` is the in-repo source of truth for what
gets submitted to ``microsoft/winget-pkgs``. Nothing about a YAML file fails
loudly: a ``ProductCode`` that no longer matches the installer's ``AppId``
makes ``winget upgrade`` quietly stop seeing the package, a stale
``InstallerSha256`` makes every install fail after the download, and a
``DisplayName`` a character away from the Apps & Features string makes
``winget list`` show the package as not installed on a machine where it is.

So the manifests are *rendered*, and this module asserts that rendering the
release they name reproduces the committed bytes exactly. That is what makes
the checked-in copies safe to read, to review and to copy into a submission --
and it means the next release's manifests are one command, not a careful
retype.

Hermetic: the digest comes from the vendored copy of the release's own
``SHA256SUMS-desktop-shell.txt`` under ``tests/fixtures/``, and the release date
is passed in. Nothing here touches the network.
"""

import importlib.util
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WINGET_DIR = _REPO_ROOT / "desktop-shell" / "installer" / "winget"
_ISS = _REPO_ROOT / "desktop-shell" / "installer" / "windows" / "MyClaudeCode.iss"

#: The release the committed manifests describe. Deliberately a literal and not
#: read from ``desktop_shell.py``'s pin: a winget submission is a separate,
#: slower act than moving the Path A pin, and the day those two versions differ
#: this test should keep passing while saying which release it is checking.
MANIFEST_TAG = "v6.45.2"
MANIFEST_VERSION = MANIFEST_TAG[1:]

_SUMS = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "desktop_shell"
    / MANIFEST_TAG
    / "SHA256SUMS-desktop-shell.txt"
)

#: The release's publication date, as ``gh release view v6.45.2 --json
#: publishedAt`` reports it. ``render.py`` reads this from the GitHub API when
#: it is not given one; a test may not.
RELEASE_DATE = "2026-09-04"


def _render_module():
    """Import ``render.py`` by path.

    It lives under ``desktop-shell/``, which is not a Python package and is not
    in the wheel -- it is a release-time tool that happens to be written in
    Python. Importing it by location is the honest way to test it; running it as
    a subprocess would trip the hermeticity guard for no gain.
    """

    path = _WINGET_DIR / "render.py"
    spec = importlib.util.spec_from_file_location("winget_render", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _render_module()

_COMMITTED_DIR = _WINGET_DIR / MANIFEST_VERSION


def _committed() -> dict[str, str]:
    """The three manifests as they sit in the tree, read as raw bytes."""

    return {
        path.name: path.read_text(encoding="utf-8", newline="")
        for path in sorted(_COMMITTED_DIR.glob("*.yaml"))
    }


def _rendered() -> dict[str, str]:
    return render.render(MANIFEST_TAG, _SUMS, RELEASE_DATE)


# ------------------------------------------------------------ the round trip


def test_rendering_reproduces_the_committed_manifests_byte_for_byte() -> None:
    """The whole point. If this fails, someone hand-edited a manifest."""

    assert _rendered() == _committed()


def test_the_committed_directory_holds_exactly_three_manifests() -> None:
    """A multi-file manifest is version + defaultLocale + installer, no more."""

    assert sorted(_committed()) == [
        "FiredMosquito831.MyClaudeCode.installer.yaml",
        "FiredMosquito831.MyClaudeCode.locale.en-US.yaml",
        "FiredMosquito831.MyClaudeCode.yaml",
    ]


@pytest.mark.parametrize("name", sorted(_committed()))
def test_every_manifest_is_lf_utf8_without_a_bom(name: str) -> None:
    """``.gitattributes`` checks this tree out with LF; the bytes must match.

    A CRLF or BOM'd manifest still validates, but it makes the byte-for-byte
    comparison above depend on which platform ran the renderer.
    """

    raw = (_COMMITTED_DIR / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


# --------------------------------------------------- agreement with the .iss


def _installer_manifest() -> str:
    return _committed()["FiredMosquito831.MyClaudeCode.installer.yaml"]


def test_the_product_code_is_the_installers_uninstall_key() -> None:
    """C: ``AppId`` is fixed forever precisely so this can be relied on.

    Inno writes ``HKCU\\...\\Uninstall\\{AppId}_is1``. Read the ``AppId`` out of
    the ``.iss`` here rather than repeating it, so changing one and not the
    other is a red test and not a package winget stops recognising.
    """

    source = _ISS.read_text(encoding="utf-8")
    app_id = re.search(r"^AppId=\{(\{.*\})\s*$", source, re.M)
    assert app_id is not None, "the .iss no longer declares a braced AppId"
    expected = f"{app_id.group(1)}_is1"
    assert _installer_manifest().count(f"ProductCode: '{expected}'") == 2


def test_the_arp_display_name_is_the_installers_uninstall_display_name() -> None:
    source = _ISS.read_text(encoding="utf-8")
    app_name = re.search(r'^#define AppName "([^"]+)"', source, re.M)
    assert app_name is not None
    assert f"DisplayName: {app_name.group(1)} (desktop app)" in _installer_manifest()


def test_the_arp_publisher_is_the_installers_publisher() -> None:
    """Not the locale manifest's ``Publisher``, and that is deliberate.

    ``AppsAndFeaturesEntries`` mirrors the registry, so its ``Publisher`` is
    whatever the installer wrote there ("My Claude Code"). The locale
    manifest's ``Publisher`` is the community repository's publisher segment,
    which is the GitHub owner. They are two different questions with two
    different right answers.
    """

    source = _ISS.read_text(encoding="utf-8")
    publisher = re.search(r'^#define AppPublisher "([^"]+)"', source, re.M)
    assert publisher is not None
    assert f"Publisher: {publisher.group(1)}" in _installer_manifest()


def test_the_display_version_is_the_version_the_installer_stamps() -> None:
    """``shell-release.yml`` passes ``/DAppVersion=<tag without the v>``."""

    assert f"DisplayVersion: {MANIFEST_VERSION}" in _installer_manifest()


# ----------------------------------------------------- agreement with the release


def test_the_installer_digest_is_the_published_one() -> None:
    """Never hand-typed: read from the release's own checksum file."""

    digests = render.parse_sha256sums(_SUMS.read_text(encoding="utf-8"))
    expected = digests[render.INSTALLER_ASSET].upper()
    assert f"InstallerSha256: {expected}" in _installer_manifest()


def test_the_installer_url_points_at_this_tags_setup_exe() -> None:
    assert (
        f"InstallerUrl: https://github.com/FiredMosquito831/my-claude-code/"
        f"releases/download/{MANIFEST_TAG}/{render.INSTALLER_ASSET}"
    ) in _installer_manifest()


def test_path_a_and_path_b_do_not_fetch_the_same_asset() -> None:
    """winget installs the setup exe; ``desktop_shell.py`` fetches the zip.

    Two installers placing the same binary is how a machine ends up with two of
    them, so the two deliveries must name different assets. This is the
    assertion that would fail if someone "simplified" one of them into the
    other.
    """

    from my_claude_code.config import desktop_shell

    path_a = {asset for asset, _digest in desktop_shell._RELEASES.values()}
    assert render.INSTALLER_ASSET not in path_a


# ------------------------------------------------------------- schema shape


def test_all_three_manifests_declare_the_same_schema_version() -> None:
    for name, text in _committed().items():
        assert f"ManifestVersion: {render.MANIFEST_VERSION}" in text, name
        assert f".{render.MANIFEST_VERSION}.schema.json" in text, name


def test_the_manifest_version_is_a_schema_winget_pkgs_publishes() -> None:
    """Pinned, so bumping it is a deliberate read of the schema's changelog."""

    assert re.fullmatch(r"1\.\d+\.\d+", render.MANIFEST_VERSION)
    assert render.MANIFEST_VERSION == "1.28.0"


@pytest.mark.parametrize("name", sorted(_committed()))
def test_every_manifest_names_the_same_package_and_version(name: str) -> None:
    text = _committed()[name]
    assert f"PackageIdentifier: {render.PACKAGE_IDENTIFIER}" in text
    assert f"PackageVersion: {MANIFEST_VERSION}" in text


def test_the_identifiers_publisher_segment_is_the_github_owner() -> None:
    """The winget-pkgs folder path is derived from this; it cannot drift."""

    owner, package = render.PACKAGE_IDENTIFIER.split(".")
    source = _ISS.read_text(encoding="utf-8")
    app_url = re.search(r'^#define AppUrl "https://github\.com/([^/]+)/', source, re.M)
    assert app_url is not None
    assert owner == app_url.group(1)
    assert package == "MyClaudeCode"


def test_the_license_is_the_projects_own_spdx_expression() -> None:
    import tomllib

    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    expected = pyproject["project"]["license"]
    locale = _committed()["FiredMosquito831.MyClaudeCode.locale.en-US.yaml"]
    assert f"License: {expected}" in locale


def test_no_installer_switches_are_hand_written() -> None:
    """``InstallerType: inno`` already means winget's own Inno switch set.

    Spelling ``/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`` out here would be a
    second copy of a list winget maintains, and the winget documentation
    explicitly asks you not to.
    """

    assert "InstallerSwitches" not in _installer_manifest()


def test_no_commands_are_claimed() -> None:
    """The installer puts nothing on PATH, so the manifest must promise none."""

    assert "Commands:" not in _installer_manifest()


def test_the_scope_is_per_user() -> None:
    """``PrivilegesRequired=lowest``: there is no machine-wide installer."""

    assert "Scope: user" in _installer_manifest()


def test_submission_instructions_exist_and_are_not_a_submission() -> None:
    """SUBMIT.md is prepared, deliberately un-run.

    External publication is the user's call, so the repo carries the steps and
    the PR text and stops there. This asserts the file is present and still
    describes the version the manifests are for.
    """

    submit = (_WINGET_DIR / "SUBMIT.md").read_text(encoding="utf-8")
    assert f"manifests/f/FiredMosquito831/MyClaudeCode/{MANIFEST_VERSION}/" in submit
