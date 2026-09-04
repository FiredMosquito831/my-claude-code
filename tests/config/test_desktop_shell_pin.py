"""The desktop shell's release pin, and its agreement with the rest of the repo.

The pin is four hand-maintained SHA-256 digests and one release tag. Nothing
about that combination fails loudly on its own: a stale tag downloads an old
window, a mistyped digest refuses to install one, and a target this table claims
but the release workflow does not build is a 404 on somebody's laptop. Each of
those is checked here against the source that actually decides it -- the
workflow's matrix, ``config/rtk.py``, and (network-gated) the real release.
"""

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest
import yaml

from my_claude_code.config import desktop_shell, rtk

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "shell-release.yml"
_SOURCE = Path(desktop_shell.__file__).read_text(encoding="utf-8")


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
