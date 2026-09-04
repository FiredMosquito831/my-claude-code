#!/usr/bin/env python3
"""Render the winget manifests for one release of the Windows desktop app.

    uv run --offline python desktop-shell/installer/winget/render.py v6.45.2

Writes the three files a multi-file winget manifest needs into
``desktop-shell/installer/winget/<version>/``, which is laid out exactly like
the leaf directory a submission to ``microsoft/winget-pkgs`` goes in
(``manifests/f/FiredMosquito831/MyClaudeCode/<version>/``), so publishing is a
copy and never a retype. See ``SUBMIT.md`` beside this file.

WHY A RENDERER AND NOT THREE CHECKED-IN FILES
    Six of the values in these manifests are facts that live somewhere else in
    this repository and cannot be allowed to drift:

    * the ``ProductCode`` is Inno Setup's uninstall key, which is ``AppId`` from
      ``../windows/MyClaudeCode.iss`` with ``_is1`` appended -- change the
      ``AppId`` and every winget correlation silently breaks;
    * ``DisplayName`` and ``Publisher`` under ``AppsAndFeaturesEntries`` are not
      marketing copy, they are the strings that installer writes into
      ``HKCU\\...\\Uninstall``, and they come from the same file;
    * ``License`` is ``pyproject.toml``'s, verbatim;
    * ``InstallerUrl`` and ``InstallerSha256`` come from the release itself --
      the second one is read from the release's own
      ``SHA256SUMS-desktop-shell.txt``, never typed.

    So this script reads all six and prints the manifests. Rendering v6.45.2 has
    to reproduce the committed files byte for byte;
    ``tests/scripts/test_winget_manifest.py`` asserts exactly that, which is
    what makes the checked-in copies trustworthy to read and to submit.

STDLIB ONLY, ON PURPOSE
    It has to run from a bare checkout on a machine that is cutting a release,
    and it emits YAML that a schema validates -- so the YAML is written by hand
    with quoting rules narrow enough to be obviously right, rather than by a
    dependency whose emitter style would decide the bytes.
"""

import argparse
import json
import re
import sys
import textwrap
import tomllib
import urllib.request
from pathlib import Path

#: The multi-file manifest schema these files are written against. Verified
#: against ``microsoft/winget-pkgs`` ``doc/manifest/schema/`` -- 1.28.0 is the
#: newest published there, and the client on the release machine (winget
#: v1.29.290) validates it. Do not guess this: bump it only after reading the
#: schema's own "Summary of Changes" page.
MANIFEST_VERSION = "1.28.0"

#: ``Publisher.Package``, case sensitive, and it must equal the folder path
#: under the winget-pkgs partition directory. The publisher segment is the
#: GitHub owner rather than a company name: that is the community repository's
#: convention for a project whose publisher *is* its GitHub account (the same
#: shape as ``sharkdp.bat`` or ``ajeetdsouza.zoxide``), and it is the only name
#: a user could guess from the URL they downloaded the installer from.
PACKAGE_IDENTIFIER = "FiredMosquito831.MyClaudeCode"

#: Which release asset winget installs. Delivery path B -- the Inno installer a
#: human double-clicks -- never the ``.zip`` that ``config/desktop_shell.py``
#: fetches. Two installers writing the same binary is how you get two of them.
INSTALLER_ASSET = "MyClaudeCode-Setup-windows-x86_64.exe"

DEFAULT_LOCALE = "en-US"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ISS = _REPO_ROOT / "desktop-shell" / "installer" / "windows" / "MyClaudeCode.iss"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: One line of ``SHA256SUMS-desktop-shell.txt``. The same shape
#: ``config/desktop_shell.py`` parses, and for the same reason: the workflow
#: rebuilds every line from the raw digest, so anything else is not that file.
_SUMS_LINE = re.compile(r"^([0-9a-f]{64})  (\S.*)$")

SHORT_DESCRIPTION = (
    "A desktop window onto the My Claude Code dashboard; it installs and "
    "starts the server for you."
)

#: One string per paragraph. The schema documentation asks that no line in a
#: manifest run past 100 characters, so these are emitted as a *folded* block
#: scalar: the source wraps, the rendered text does not.
DESCRIPTION_PARAGRAPHS = (
    "My Claude Code is a local proxy that connects coding agents -- Claude "
    "Code, Codex, Gemini CLI and a dozen others -- to OpenAI-compatible AI "
    "providers, with routing, fallback, rate-limit handling and a web "
    "dashboard on 127.0.0.1:8082.",
    "This package installs the desktop app: a small native window that renders "
    "that dashboard and puts an icon in the tray. It carries no Python, no "
    "server and no configuration. On first launch, if the server is not "
    "installed yet, the window shows you the exact install command and runs it "
    "in front of you, then starts the server and loads the dashboard.",
    "It installs per user, needs no administrator rights, and uninstalling it "
    "removes the window and nothing else -- your configuration, your keys and "
    "the mcc-server command are left alone.",
)

TAGS = (
    "ai",
    "claude",
    "coding-agent",
    "dashboard",
    "developer-tools",
    "gateway",
    "llm",
    "openai-compatible",
    "proxy",
)


# ------------------------------------------------------------ the repo's facts


def _iss_define(name: str, source: str) -> str:
    """Return a ``#define <name> "<value>"`` from the Inno script."""

    match = re.search(rf'^#define\s+{name}\s+"([^"]*)"', source, re.M)
    if match is None:
        raise SystemExit(f"{_ISS.name} has no #define {name}")
    return match.group(1)


def _iss_app_id(source: str) -> str:
    """Return the ``AppId``, with Inno's doubled leading brace undone.

    ``AppId={{5FC8...}`` is how Inno spells a value *starting* with a literal
    ``{``: the constant syntax uses braces, so the first one is escaped by
    doubling. The registry key the installer actually writes is
    ``{5FC8...}_is1``, and that is what winget has to be told.
    """

    match = re.search(r"^AppId=(\S+)\s*$", source, re.M)
    if match is None:
        raise SystemExit(f"{_ISS.name} has no AppId")
    value = match.group(1)
    if not value.startswith("{{"):
        raise SystemExit(f"{_ISS.name}'s AppId does not start with a doubled brace")
    return value[1:]


def installer_facts() -> dict[str, str]:
    """Return everything the manifests borrow from the installer and pyproject."""

    source = _ISS.read_text(encoding="utf-8")
    app_name = _iss_define("AppName", source)
    app_url = _iss_define("AppUrl", source)
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return {
        # `{AppId}_is1` -- Inno Setup's uninstall subkey, which is the
        # `ProductCode` winget correlates an installed package by.
        "product_code": f"{_iss_app_id(source)}_is1",
        # `UninstallDisplayName` in the .iss: the exact Apps & Features string.
        "display_name": f"{app_name} (desktop app)",
        "publisher": _iss_define("AppPublisher", source),
        "package_name": app_name,
        "repo_url": app_url,
        "repo_slug": app_url.removeprefix("https://github.com/"),
        "license": pyproject["project"]["license"],
    }


# --------------------------------------------------------------- the release


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``SHA256SUMS-desktop-shell.txt`` into ``{filename: digest}``."""

    digests: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        match = _SUMS_LINE.match(line)
        if match is None:
            raise SystemExit(f"not a checksum line: {line!r}")
        digests[match.group(2)] = match.group(1)
    return digests


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def installer_sha256(tag: str, sums_path: Path | None) -> str:
    """Return the digest of the setup exe, from the release or a local copy."""

    repo = installer_facts()["repo_slug"]
    if sums_path is None:
        text = _fetch(
            f"https://github.com/{repo}/releases/download/{tag}/"
            "SHA256SUMS-desktop-shell.txt"
        )
    else:
        text = sums_path.read_text(encoding="utf-8")
    digests = parse_sha256sums(text)
    digest = digests.get(INSTALLER_ASSET)
    if digest is None:
        raise SystemExit(
            f"{tag}'s checksum file does not list {INSTALLER_ASSET}; it has "
            f"{sorted(digests)}"
        )
    # winget's own tooling writes the hash upper case, and every manifest in
    # the community repository does; the schema accepts either.
    return digest.upper()


def release_date(tag: str, given: str | None) -> str:
    """Return the release's publication date as ``YYYY-MM-DD``."""

    if given is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", given):
            raise SystemExit(f"--release-date must be YYYY-MM-DD, got {given!r}")
        return given
    repo = installer_facts()["repo_slug"]
    payload = json.loads(
        _fetch(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    )
    return str(payload["published_at"])[:10]


# ------------------------------------------------------------------ the YAML


def _quote(value: str) -> str:
    """Return a YAML scalar for a plain one-line string.

    Only two cases arise here and both are handled by single quotes: a value
    starting with ``{`` (the ProductCode, which YAML would otherwise read as a
    flow mapping) and anything containing a ``: `` or a ``#``. Everything else
    is emitted bare, which is what the community repository's manifests look
    like and what makes a diff between two versions readable.
    """

    if value.startswith(("{", "[", "'", '"', "*", "&", "!", "%", "@", "`", ">", "|")):
        return "'" + value.replace("'", "''") + "'"
    if ": " in value or " #" in value or value != value.strip():
        return "'" + value.replace("'", "''") + "'"
    return value


def _folded(key: str, paragraphs: tuple[str, ...], indent: str) -> list[str]:
    """Render paragraphs as a folded block scalar wrapped at 100 columns.

    ``>-`` folds each run of lines back into one line and turns a blank line
    into a paragraph break, so the wrapping below is purely how the file reads
    -- what winget shows the user is the paragraph, unwrapped.
    """

    lines = [f"{key}: >-"]
    for index, paragraph in enumerate(paragraphs):
        if index:
            lines.append("")
        lines.extend(
            f"{indent}{line}"
            for line in textwrap.wrap(paragraph, width=100 - len(indent))
        )
    return lines


def _header(kind: str) -> list[str]:
    return [
        "# Rendered by desktop-shell/installer/winget/render.py -- do not edit "
        "by hand.",
        f"# yaml-language-server: $schema=https://aka.ms/winget-manifest."
        f"{kind}.{MANIFEST_VERSION}.schema.json",
        "",
    ]


def render_version(version: str) -> str:
    lines = [
        *_header("version"),
        f"PackageIdentifier: {PACKAGE_IDENTIFIER}",
        f"PackageVersion: {version}",
        f"DefaultLocale: {DEFAULT_LOCALE}",
        "ManifestType: version",
        f"ManifestVersion: {MANIFEST_VERSION}",
    ]
    return "\n".join(lines) + "\n"


def render_installer(
    version: str, tag: str, digest: str, date: str, facts: dict[str, str]
) -> str:
    url = f"{facts['repo_url']}/releases/download/{tag}/{INSTALLER_ASSET}"
    lines = [
        *_header("installer"),
        f"PackageIdentifier: {PACKAGE_IDENTIFIER}",
        f"PackageVersion: {version}",
        "Installers:",
        "  - Architecture: x64",
        # `inno` and not `exe`: winget then supplies Inno's own
        # `/SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`, which is exactly what
        # this installer is built and smoked for -- so there is deliberately no
        # `InstallerSwitches` block. Hand-written switches here would be a
        # second, divergent copy of a set winget already knows.
        "    InstallerType: inno",
        # `PrivilegesRequired=lowest`: it installs into %LOCALAPPDATA%\Programs
        # and writes HKCU. There is no machine-scope installer to offer.
        "    Scope: user",
        f"    InstallerUrl: {url}",
        f"    InstallerSha256: {digest}",
        # Both the installer node and the ARP entry carry the ProductCode; the
        # schema documentation asks for it in both places when
        # AppsAndFeaturesEntries is used.
        f"    ProductCode: {_quote(facts['product_code'])}",
        "    AppsAndFeaturesEntries:",
        # These four are read back out of HKCU\...\Uninstall to decide whether
        # the package is installed and whether it is current, so every one of
        # them is the string Inno writes -- not the prettier name in the locale
        # manifest.
        f"      - DisplayName: {_quote(facts['display_name'])}",
        f"        DisplayVersion: {version}",
        f"        Publisher: {_quote(facts['publisher'])}",
        f"        ProductCode: {_quote(facts['product_code'])}",
        "        InstallerType: inno",
        f"    ReleaseDate: {date}",
        # No `Commands:`. The installer deliberately puts nothing on PATH -- it
        # ships one windowed executable into %LOCALAPPDATA%\Programs and a Start
        # Menu shortcut -- and a `Commands` entry winget cannot honour would be
        # a promise to the user that their shell will find `MyClaudeCode`.
        "ManifestType: installer",
        f"ManifestVersion: {MANIFEST_VERSION}",
    ]
    return "\n".join(lines) + "\n"


def render_locale(version: str, tag: str, facts: dict[str, str]) -> str:
    repo = facts["repo_url"]
    publisher = PACKAGE_IDENTIFIER.split(".", 1)[0]
    lines = [
        *_header("defaultLocale"),
        f"PackageIdentifier: {PACKAGE_IDENTIFIER}",
        f"PackageVersion: {version}",
        f"PackageLocale: {DEFAULT_LOCALE}",
        f"Publisher: {publisher}",
        f"PublisherUrl: https://github.com/{publisher}",
        f"PublisherSupportUrl: {repo}/issues",
        f"Author: {publisher}",
        f"PackageName: {facts['package_name']}",
        f"PackageUrl: {repo}",
        # Verbatim from pyproject.toml's `license`, which is the SPDX
        # expression the wheel is published under.
        f"License: {facts['license']}",
        f"LicenseUrl: {repo}/blob/main/LICENSE",
        "Copyright: Copyright (c) 2026 FiredMosquito831",
        f"CopyrightUrl: {repo}/blob/main/LICENSE",
        f"ShortDescription: {_quote(SHORT_DESCRIPTION)}",
        *_folded("Description", DESCRIPTION_PARAGRAPHS, "  "),
        "Moniker: my-claude-code",
        "Tags:",
        *[f"  - {tag_name}" for tag_name in TAGS],
        f"ReleaseNotesUrl: {repo}/releases/tag/{tag}",
        "Documentations:",
        "  - DocumentLabel: Usage",
        f"    DocumentUrl: {repo}/blob/main/docs/USAGE.md",
        "ManifestType: defaultLocale",
        f"ManifestVersion: {MANIFEST_VERSION}",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the command


def render(tag: str, sums_path: Path | None, date: str | None) -> dict[str, str]:
    """Return ``{filename: contents}`` for one release tag."""

    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit(f"expected a release tag like v6.45.2, got {tag!r}")
    version = tag[1:]
    facts = installer_facts()
    digest = installer_sha256(tag, sums_path)
    return {
        f"{PACKAGE_IDENTIFIER}.yaml": render_version(version),
        f"{PACKAGE_IDENTIFIER}.installer.yaml": render_installer(
            version, tag, digest, release_date(tag, date), facts
        ),
        f"{PACKAGE_IDENTIFIER}.locale.{DEFAULT_LOCALE}.yaml": render_locale(
            version, tag, facts
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the winget manifests for one release tag."
    )
    parser.add_argument("tag", help="the release tag, e.g. v6.45.2")
    parser.add_argument(
        "--sums",
        type=Path,
        default=None,
        help=(
            "a local SHA256SUMS-desktop-shell.txt to read the installer digest "
            "from; the release's own copy is downloaded when this is omitted"
        ),
    )
    parser.add_argument(
        "--release-date",
        default=None,
        help="YYYY-MM-DD; read from the GitHub release when omitted",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where to write; defaults to <this directory>/<version>",
    )
    args = parser.parse_args(argv)

    files = render(args.tag, args.sums, args.release_date)
    out = args.out or (Path(__file__).resolve().parent / args.tag[1:])
    out.mkdir(parents=True, exist_ok=True)
    for name, contents in files.items():
        path = out / name
        # LF and UTF-8 without a BOM. winget tolerates a BOM; the community
        # repository's own manifests do not have one, and `.gitattributes`
        # checks this tree out with LF on every platform.
        path.write_text(contents, encoding="utf-8", newline="\n")
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
