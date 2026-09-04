"""The updater sees the wheel on a release, and nothing else on it.

One release stream carries the Python wheel *and* the desktop shell's four
archives plus its checksum file (spec ``PR-DESKTOP-WEBVIEW-SPEC.md`` §4.2,
decision Q6). That is only safe because ``_select_wheel_asset`` filters on the
``.whl`` suffix rather than, say, taking the first asset or the largest one.

These tests pin that. They exist so a future refactor of asset selection --
"just take assets[0]", "prefer the biggest download" -- fails here instead of
silently making the dashboard's Update button install a Windows zip.
"""

import random

import pytest

from my_claude_code.application.release_updates import _select_wheel_asset

WHEEL = {
    "name": "my_claude_code-6.43.0-py3-none-any.whl",
    "browser_download_url": "https://example.invalid/my_claude_code-6.43.0-py3-none-any.whl",
    "digest": "sha256:" + "a" * 64,
    "size": 900_000,
}

# Exactly what `shell-release.yml` attaches. Every one of these sorts before
# the wheel by name, which is the point: `MyClaudeCode-...` and
# `SHA256SUMS-...` both precede `my_claude_code-...` in ASCII order, so an
# implementation that took the first asset would pick one of them.
SHELL_ASSETS = [
    {
        "name": "MyClaudeCode-linux-x86_64.tar.gz",
        "browser_download_url": "https://example.invalid/MyClaudeCode-linux-x86_64.tar.gz",
        "size": 3_100_000,
    },
    {
        "name": "MyClaudeCode-macos-aarch64.tar.gz",
        "browser_download_url": "https://example.invalid/MyClaudeCode-macos-aarch64.tar.gz",
        "size": 2_900_000,
    },
    {
        "name": "MyClaudeCode-macos-x86_64.tar.gz",
        "browser_download_url": "https://example.invalid/MyClaudeCode-macos-x86_64.tar.gz",
        "size": 3_000_000,
    },
    {
        "name": "MyClaudeCode-linux-x86_64.deb",
        "browser_download_url": "https://example.invalid/MyClaudeCode-linux-x86_64.deb",
        "size": 3_200_000,
    },
    {
        "name": "MyClaudeCode-macos-universal.dmg",
        "browser_download_url": "https://example.invalid/MyClaudeCode-macos-universal.dmg",
        "size": 6_100_000,
    },
    {
        "name": "MyClaudeCode-Setup-windows-x86_64.exe",
        "browser_download_url": "https://example.invalid/MyClaudeCode-Setup-windows-x86_64.exe",
        "size": 3_400_000,
    },
    {
        "name": "MyClaudeCode-windows-x86_64.zip",
        "browser_download_url": "https://example.invalid/MyClaudeCode-windows-x86_64.zip",
        "size": 2_800_000,
    },
    {
        "name": "SHA256SUMS-desktop-shell.txt",
        "browser_download_url": "https://example.invalid/SHA256SUMS-desktop-shell.txt",
        "size": 380,
    },
]


def test_the_wheel_is_chosen_whatever_order_the_shell_assets_arrive_in():
    """Asset order is GitHub's business, so every order must give the wheel."""

    generator = random.Random(20260904)
    for _ in range(50):
        assets = [*SHELL_ASSETS, WHEEL]
        generator.shuffle(assets)
        chosen = _select_wheel_asset({"assets": assets})
        assert chosen is not None
        assert chosen["name"] == WHEEL["name"], [asset["name"] for asset in assets]


def test_the_shell_assets_really_do_sort_first():
    """Guards the guard: if this stops being true the test above proves less."""

    names = sorted(asset["name"] for asset in [*SHELL_ASSETS, WHEEL])
    assert names[-1] == WHEEL["name"]


def test_a_release_carrying_only_shell_assets_publishes_no_wheel():
    """A shell-only release must read as "no wheel", not as "wheel = the zip"."""

    assert _select_wheel_asset({"assets": list(SHELL_ASSETS)}) is None


@pytest.mark.parametrize("asset", SHELL_ASSETS, ids=lambda asset: asset["name"])
def test_no_single_shell_asset_is_ever_mistaken_for_a_wheel(asset: dict[str, object]):
    assert _select_wheel_asset({"assets": [asset]}) is None


def test_a_name_merely_containing_whl_is_not_a_wheel():
    """The check is a suffix, not a substring, and stays one."""

    assert (
        _select_wheel_asset(
            {"assets": [{"name": "MyClaudeCode-whl-notes.txt"}, {"name": "x.whl.asc"}]}
        )
        is None
    )
