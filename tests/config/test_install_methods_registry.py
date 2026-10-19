"""Static guards on what the Install button may run (7.102.0, spec §8.4).

The table in ``config/install_methods.py`` is everything a run can do, so the
guards are on the table: https and a per-method host allow-list, never sudo /
runas / --unsafe-perm in anything that runs, every npm method names its Node
bound, no button for Antigravity or (until its check passes) Kimi, and the
frozen ``cli/`` and ``scripts/`` trees never learn about any of it -- the
installer installs MCC and its prerequisites, nothing else.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from my_claude_code.config.desktop_apps import DESKTOP_APPS, DESKTOP_APPS_BY_ID
from my_claude_code.config.harnesses import HARNESS_SPECS, harness_spec
from my_claude_code.config.install_methods import (
    DESKTOP_SAME_AS_CLI,
    NODE_INSTALLERS,
    DownloadAction,
    Elevation,
    InstallKind,
    InstallMethod,
    InstallOs,
    Interpreter,
    methods_for_os,
)

REPO = Path(__file__).resolve().parents[2]
FORBIDDEN = ("sudo", "runas", "--unsafe-perm")
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
DATE = re.compile(r"^2026-\d{2}-\d{2}$")


def _all_methods() -> list[tuple[str, InstallMethod]]:
    rows = [(spec.id, m) for spec in HARNESS_SPECS for m in spec.install_methods]
    rows += [
        (f"desktop:{app.id}", m) for app in DESKTOP_APPS for m in app.install_methods
    ]
    return rows


ALL = _all_methods()


def _executed_text(method: InstallMethod) -> list[str]:
    """Every string the runner could hand a process for this method."""

    return [
        method.url,
        method.package,
        *method.npm_args,
        *method.script_args,
        *(f"{key}={value}" for key, value in method.env),
        *method.bins,
    ]


def test_the_table_is_not_vacuous():
    assert len(ALL) > 80
    assert {m.kind for _, m in ALL} == set(InstallKind)


@pytest.mark.parametrize(
    ("owner", "method"), ALL, ids=[f"{o}-{m.os}-{m.id}" for o, m in ALL]
)
def test_install_methods_are_https_allowlisted_and_never_sudo(owner, method):
    for url in (method.url, method.doc_url):
        if not url:
            continue
        parts = urlsplit(url)
        assert parts.scheme == "https", (owner, url)
    if method.url:
        host = urlsplit(method.url).hostname
        assert host in method.hosts, (owner, method.url, method.hosts)
    for host in method.hosts:
        assert HOST.match(host), (owner, host)
    for text in _executed_text(method):
        for word in FORBIDDEN:
            assert word not in text.lower().split(), (owner, text)
            assert f"/{word}" not in text.lower(), (owner, text)
    # A line that names sudo is only ever printed, never run.
    shown = f"{method.copy_command} {method.printed_command}".lower()
    if "sudo" in shown:
        assert method.elevates is Elevation.SUDO_PRINTED_ONLY, owner
        assert method.runnable is False, owner
    assert "--unsafe-perm" not in shown
    assert "runas" not in shown
    assert DATE.match(method.checked_on), owner
    assert method.checked_by, owner
    assert method.copy_command, owner


def test_every_npm_method_names_a_node_bound_and_nothing_else_needs_node():
    for owner, method in ALL:
        if method.kind is InstallKind.NPM:
            bound = method.needs_node
            assert bound is not None, owner
            assert bound.minimum_parts, owner
            assert bound.source, owner
            assert method.package and method.bins, owner
        else:
            assert method.needs_node is None, owner


def test_scripts_name_their_interpreter_and_windows_never_uses_a_posix_shell():
    for owner, method in ALL:
        if method.kind is not InstallKind.SCRIPT:
            assert method.interpreter is None, owner
            continue
        assert method.interpreter is not None, owner
        if method.os is InstallOs.WINDOWS:
            # Decision 7: an MCC on Windows installs for Windows, never WSL.
            assert method.interpreter is Interpreter.POWERSHELL, owner
        else:
            assert method.interpreter in {Interpreter.BASH, Interpreter.SH}, owner


def test_downloads_say_what_happens_to_the_file():
    for owner, method in ALL:
        if method.kind is InstallKind.DOWNLOAD:
            assert method.action is not None, owner
            if method.action is DownloadAction.PRINT_ONLY:
                assert method.printed_command, owner
            if method.action in {
                DownloadAction.DMG_COPY,
                DownloadAction.ZIP_TO_PROGRAMS,
                DownloadAction.ZIP_TO_APPLICATIONS,
            }:
                assert method.app_name, owner
        else:
            assert method.action is None, owner


def test_method_ids_are_unique_per_os_and_arch():
    owners = {spec.id: spec.install_methods for spec in HARNESS_SPECS}
    owners.update({f"desktop:{app.id}": app.install_methods for app in DESKTOP_APPS})
    for owner, methods in owners.items():
        for os_ in InstallOs:
            for arch in ("x64", "arm64"):
                ids = [m.id for m in methods_for_os(methods, os_, arch)]
                assert len(ids) == len(set(ids)), (owner, os_, arch, ids)


def test_antigravity_has_no_method_and_kimi_waits_for_its_check():
    assert harness_spec("antigravity").install_methods == ()
    assert DESKTOP_APPS_BY_ID["antigravity"].install_methods == ()
    assert "never runs agy" in harness_spec("antigravity").install_unavailable_reason
    kimi = harness_spec("kimi_code")
    assert kimi.install_methods == ()
    assert "mcc-kimi" in kimi.install_unavailable_reason


def test_every_servable_cli_has_a_runnable_method_on_every_os():
    """The user's words: buttons "for each and every coding agent"."""

    for spec in HARNESS_SPECS:
        if not spec.available or spec.id == "kimi_code":
            continue
        for os_ in InstallOs:
            runnable = [
                m
                for m in methods_for_os(spec.install_methods, os_, "x64")
                if m.runnable
            ]
            assert runnable, (spec.id, os_)
            first = runnable[0]
            # Decision 1: the vendor's own installer first, npm only where it
            # is the only way.
            if any(m.kind is not InstallKind.NPM for m in runnable):
                assert first.kind is not InstallKind.NPM, (spec.id, os_)


def test_every_desktop_app_has_a_button_a_link_or_a_cli_twin():
    for app in DESKTOP_APPS:
        if app.id == "antigravity":
            continue
        if app.id in DESKTOP_SAME_AS_CLI:
            assert app.install_methods == ()
            assert harness_spec(DESKTOP_SAME_AS_CLI[app.id]).install_methods
            continue
        for os_ in InstallOs:
            assert methods_for_os(app.install_methods, os_, "x64"), (app.id, os_)


def test_apps_mcc_cannot_route_get_a_button_only_with_a_direct_download():
    """Decision 8: Kimi/Qwen desktop and Warp are links; LM Studio has a
    download on Windows only."""

    for app_id in ("kimi_desktop", "qwen_desktop", "warp"):
        kinds = {m.kind for m in DESKTOP_APPS_BY_ID[app_id].install_methods}
        assert kinds == {InstallKind.LINK}, app_id
    lm = DESKTOP_APPS_BY_ID["lm_studio"].install_methods
    assert [m.os for m in lm if m.kind is InstallKind.DOWNLOAD] == [InstallOs.WINDOWS]


def test_node_is_installed_officially_on_windows_and_macos_and_with_fnm_on_linux():
    assert NODE_INSTALLERS[InstallOs.WINDOWS].kind == "msi"
    assert NODE_INSTALLERS[InstallOs.MACOS].kind == "pkg"
    linux = NODE_INSTALLERS[InstallOs.LINUX]
    assert linux.kind == "fnm"
    assert linux.elevates is Elevation.NONE
    for installer in NODE_INSTALLERS.values():
        parts = urlsplit(installer.url)
        assert parts.scheme == "https"
        assert parts.hostname in installer.hosts


def test_the_frozen_trees_never_learn_about_the_install_button():
    """The boundary (IV.3): nothing in ``cli/`` or ``scripts/`` installs an agent."""

    needles = ("install_methods", "agent_installer", "agent-installs", "InstallMethod")
    for tree in (REPO / "src" / "my_claude_code" / "cli", REPO / "scripts"):
        for path in tree.rglob("*"):
            if not path.is_file() or path.suffix in {".png", ".ico", ".exe"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                assert needle not in text, f"{path} names {needle}"
