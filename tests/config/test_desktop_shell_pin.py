"""The desktop shell's release pin, and its agreement with the rest of the repo.

The pin is four hand-maintained SHA-256 digests and one release tag. Nothing
about that combination fails loudly on its own: a stale tag downloads an old
window, a mistyped digest refuses to install one, and a target this table claims
but the release workflow does not build is a 404 on somebody's laptop. Each of
those is checked here against the source that actually decides it -- the
workflow's matrix, ``config/rtk.py``, and (network-gated) the real release.

Since 6.45.3 the release's own ``SHA256SUMS-desktop-shell.txt`` is **vendored**
under ``tests/fixtures/desktop_shell/<tag>/`` and compared with the table
offline. The network-gated test at the bottom proves the pin against the live
release, but it is opt-in and therefore silent on the pull request that moves
the pin -- which is exactly when a mistyped digest is cheap to catch and
expensive to miss. Vendoring the file makes the comparison ordinary CI. It also
makes the repin reviewable: the fixture and the table change in the same commit,
and a reviewer can diff them against each other.
"""

import io
import json
import os
import re
import tarfile
import urllib.request
from pathlib import Path

import pytest
import yaml

from my_claude_code.config import desktop_shell, rtk

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "shell-release.yml"
_SOURCE = Path(desktop_shell.__file__).read_text(encoding="utf-8")

#: The vendored copy of the pinned release's assets. Named for the tag, so a
#: repin that forgets to vendor the new release fails on a missing directory
#: rather than passing against the previous release's digests.
_FIXTURES = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "desktop_shell"
    / desktop_shell.DESKTOP_SHELL_RELEASE_TAG
)


def _workflow_assets() -> set[str]:
    matrix = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build"][
        "strategy"
    ]["matrix"]["include"]
    return {leg["asset"] for leg in matrix}


def test_the_pinned_tag_is_a_release_tag() -> None:
    assert re.fullmatch(r"v\d+\.\d+\.\d+", desktop_shell.DESKTOP_SHELL_RELEASE_TAG)


def test_the_download_url_embeds_the_pinned_tag() -> None:
    assert desktop_shell.DESKTOP_SHELL_RELEASE_BASE_URL.endswith(
        f"/{desktop_shell.DESKTOP_SHELL_RELEASE_TAG}"
    )


def test_every_target_has_a_distinct_sha256() -> None:
    digests = [digest for _asset, digest in desktop_shell._RELEASES.values()]
    assert len(digests) == 4
    assert len(set(digests)) == len(digests)
    for digest in digests:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_each_digest_literal_appears_once_in_the_module() -> None:
    """A digest copied from the previous pin is the classic way this goes wrong."""

    for _asset, digest in desktop_shell._RELEASES.values():
        assert _SOURCE.count(digest) == 1


def test_arch_aliases_match_rtk() -> None:
    """C6. Two tables, one meaning: ``arm64`` must not be ``aarch64`` in one
    module and an unknown architecture in the other."""

    for machine in ("AMD64", "x64", "arm64", "x86_64", "aarch64"):
        assert desktop_shell.normalized_architecture(
            machine
        ) == rtk._normalized_architecture(machine)


def test_every_pinned_asset_is_one_the_release_workflow_builds() -> None:
    """The pin cannot name a file no runner produces."""

    assets = {asset for asset, _digest in desktop_shell._RELEASES.values()}
    assert assets <= _workflow_assets()


def test_linux_aarch64_is_absent_because_no_runner_builds_it() -> None:
    assert ("linux", "aarch64") not in desktop_shell._RELEASES


def test_windows_is_the_only_zip() -> None:
    zipped = {
        key
        for key, (asset, _digest) in desktop_shell._RELEASES.items()
        if asset.endswith(".zip")
    }
    assert zipped == {("win32", "x86_64")}


def test_asset_names_carry_no_version() -> None:
    """Decision Q5: a shortcut or an ``Exec=`` line survives every upgrade."""

    for asset, _digest in desktop_shell._RELEASES.values():
        assert not re.search(r"\d+\.\d+\.\d+", asset)


def test_release_for_normalizes_the_machine_name() -> None:
    assert (
        desktop_shell.release_for("win32", "AMD64")
        == desktop_shell._RELEASES[("win32", "x86_64")]
    )
    assert (
        desktop_shell.release_for("darwin", "arm64")
        == desktop_shell._RELEASES[("darwin", "aarch64")]
    )
    assert desktop_shell.release_for("sunos5", "sparc") is None


@pytest.mark.skipif(
    os.environ.get("MCC_NETWORK_TESTS") != "1",
    reason="network-gated: set MCC_NETWORK_TESTS=1 to check the shell pin upstream",
)
def test_pin_matches_the_real_release() -> None:
    """Network-gated. Proves the pinned digests are the published ones."""

    tag = desktop_shell.DESKTOP_SHELL_RELEASE_TAG
    api = (
        f"https://api.github.com/repos/{desktop_shell.DESKTOP_SHELL_RELEASE_REPO}"
        f"/releases/tags/{tag}"
    )
    with urllib.request.urlopen(api, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    names = {asset["name"] for asset in release["assets"]}
    assert desktop_shell.DESKTOP_SHELL_SUMS_ASSET in names

    sums_url = (
        f"{desktop_shell.DESKTOP_SHELL_RELEASE_BASE_URL}/"
        f"{desktop_shell.DESKTOP_SHELL_SUMS_ASSET}"
    )
    with urllib.request.urlopen(sums_url, timeout=30) as response:
        published = desktop_shell.parse_sha256sums(
            response.read().decode("utf-8", "replace")
        )
    for asset, digest in desktop_shell._RELEASES.values():
        assert asset in names
        assert published[asset] == digest, asset


# ---------------------------------------------- the vendored published sums


def _vendored_sums() -> dict[str, str]:
    """The pinned release's own checksum file, as committed to this repo."""

    path = _FIXTURES / desktop_shell.DESKTOP_SHELL_SUMS_ASSET
    assert path.is_file(), (
        f"{path} is missing. Moving DESKTOP_SHELL_RELEASE_TAG means vendoring "
        f"that release's {desktop_shell.DESKTOP_SHELL_SUMS_ASSET} beside the "
        f"pin -- `gh release download "
        f"{desktop_shell.DESKTOP_SHELL_RELEASE_TAG} --repo "
        f"{desktop_shell.DESKTOP_SHELL_RELEASE_REPO} -p "
        f"{desktop_shell.DESKTOP_SHELL_SUMS_ASSET}`."
    )
    return desktop_shell.parse_sha256sums(path.read_text(encoding="utf-8"))


def test_every_pinned_digest_is_the_published_one() -> None:
    """The repin guard. Offline, and it fails on the commit that drifts.

    ``_confirm_published_digest`` makes this same comparison at runtime against
    the live release -- and refuses to install. That refusal is correct and it
    is also a user's first launch failing. This moves the same check to the
    pull request.
    """

    published = _vendored_sums()
    for target, (asset, digest) in sorted(desktop_shell._RELEASES.items()):
        assert asset in published, (
            f"{asset} (pinned for {target}) is not in the vendored "
            f"{desktop_shell.DESKTOP_SHELL_SUMS_ASSET}."
        )
        assert published[asset] == digest, (
            f"The pin for {target} disagrees with the published checksum: the "
            f"release says {published[asset]}, this build expects {digest}."
        )


def test_the_vendored_sums_file_carries_every_shell_asset() -> None:
    """Seven, since S5 and S9: four archives and three double-click installers.

    The wheel is deliberately absent -- it is checksummed by GitHub, not by
    ``shell-release.yml``.
    """

    assert set(_vendored_sums()) == {
        "MyClaudeCode-linux-x86_64.tar.gz",
        "MyClaudeCode-macos-aarch64.tar.gz",
        "MyClaudeCode-macos-x86_64.tar.gz",
        "MyClaudeCode-windows-x86_64.zip",
        "MyClaudeCode-Setup-windows-x86_64.exe",
        "MyClaudeCode-linux-x86_64.deb",
        "MyClaudeCode-macos-universal.dmg",
    }


def test_path_a_ignores_the_three_double_click_installers() -> None:
    """Delivery path A fetches archives; the installers are for humans.

    The module's own comment says the ``setup.exe`` line "is simply ignored".
    This is that comment as a test, extended to the ``.deb`` and the ``.dmg``:
    each of them installs the same binary by another route, and two installers
    placing one file is how a machine ends up with two of them.
    """

    pinned = {asset for asset, _digest in desktop_shell._RELEASES.values()}
    for installer in (
        "MyClaudeCode-Setup-windows-x86_64.exe",
        "MyClaudeCode-linux-x86_64.deb",
        "MyClaudeCode-macos-universal.dmg",
    ):
        assert installer in _vendored_sums()
        assert installer not in pinned


def test_the_vendored_sums_file_is_the_shape_the_module_parses() -> None:
    """`<64 hex><space><space><filename>` is a published contract."""

    text = (_FIXTURES / desktop_shell.DESKTOP_SHELL_SUMS_ASSET).read_text(
        encoding="utf-8"
    )
    for line in text.splitlines():
        if line.strip():
            assert re.fullmatch(r"[0-9a-f]{64}  \S.*", line), line


# ------------------------------------- the real Linux tarball's member list


#: One line of ``tar -tzvf``: mode, owner, size, date, time, name.
_TZVF_LINE = re.compile(
    r"^([-dl])\S{9}\s+\S+\s+(\d+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(.*)$"
)


def _real_linux_members() -> list[tuple[str, str, int]]:
    """``(kind, name, size)`` for every member of the pinned Linux tarball.

    Read from a vendored ``tar -tzvf`` listing rather than the 4.6 MB archive:
    what the extraction rule cares about is the member *list*, and a binary in
    the test fixtures would be a second copy of a release asset with no way to
    tell whether it is still the released one.
    """

    listing = _FIXTURES / "MyClaudeCode-linux-x86_64.tar.gz.tzvf.txt"
    members: list[tuple[str, str, int]] = []
    for line in listing.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = _TZVF_LINE.match(line)
        assert match is not None, f"unparsable tar listing line: {line!r}"
        members.append((match.group(1), match.group(3), int(match.group(2))))
    assert members, "the vendored tar listing is empty"
    return members


def _tar_of(members: list[tuple[str, str, int]]) -> bytes:
    """Rebuild the released tarball's *shape* -- names, types, one byte each."""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for kind, name, _size in members:
            info = tarfile.TarInfo(name.rstrip("/"))
            if kind == "d":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
                continue
            payload = b"x"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_the_released_linux_tarball_carries_exactly_one_executable() -> None:
    """S6 packs an installer script, a ``.desktop`` entry and four icons.

    Path A's rule was never "one member": it is "exactly one member whose base
    name is ``MyClaudeCode``, and no links". This reads the *released* archive's
    member list and checks that against the rule, so the next thing S6 adds to
    the tarball either passes here or is caught before it reaches a user.
    """

    members = _real_linux_members()
    names = [name for kind, name, _size in members if kind == "-"]
    assert sum(1 for name in names if name.rsplit("/", 1)[-1] == "MyClaudeCode") == 1
    assert not [kind for kind, _name, _size in members if kind == "l"]
    assert "install-desktop.sh" in names, (
        "PR #279 put install-desktop.sh in the Linux tarball; the vendored "
        "listing no longer shows it, so this fixture is stale."
    )


def test_no_member_of_the_released_tarball_is_an_unsafe_path() -> None:
    for _kind, name, _size in _real_linux_members():
        assert not desktop_shell._is_unsafe_member_name(name), name


def test_extracting_the_released_tarballs_shape_yields_the_binary(
    monkeypatch, tmp_path
) -> None:
    """End to end over the real member list, through the real extractor."""

    monkeypatch.setattr(desktop_shell.sys, "platform", "linux")
    archive = tmp_path / "MyClaudeCode-linux-x86_64.tar.gz"
    archive.write_bytes(_tar_of(_real_linux_members()))
    destination = tmp_path / "MyClaudeCode"

    desktop_shell._extract_binary(archive, archive.name, destination)

    assert destination.read_bytes() == b"x"
