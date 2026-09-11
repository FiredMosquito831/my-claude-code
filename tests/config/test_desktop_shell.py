"""Fetching, verifying, extracting and installing the pinned desktop shell.

Every case here builds a real archive on disk and serves it through a fake
``urlopen``, so the code under test does the real zip/tar reading, the real
digest arithmetic and the real atomic install. Nothing reaches the network and
nothing is written outside ``tmp_path``: the install directory is redirected
with ``MCC_DESKTOP_SHELL_DIR``, which exists for exactly this reason.

The interesting assertions are the refusals. A checksum that does not match, a
published checksum file that disagrees with the pin, an archive carrying a
symlink or a ``..`` path, and a machine with no network must each produce a
``DesktopShellError`` naming the cause -- and must leave no half-installed
binary behind, because the receipt is what the next launch trusts.
"""

import hashlib
import io
import json
import os
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from my_claude_code.config import desktop_shell
from my_claude_code.config.desktop_shell import (
    DESKTOP_SHELL_RELEASE_TAG,
    DESKTOP_SHELL_SUMS_ASSET,
    DesktopShellError,
    desktop_shell_path,
    ensure_desktop_shell,
    fetch_desktop_shell,
    is_desktop_shell_installed,
    parse_sha256sums,
)

_PAYLOAD = b"#!/not/really/an/executable\n" + b"x" * 4096


@pytest.fixture
def shell_dir(tmp_path, monkeypatch):
    """Redirect the install directory; never the developer's ``~/.local/bin``."""

    directory = tmp_path / "bin"
    monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_DIR_ENV, str(directory))
    monkeypatch.delenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, raising=False)
    return directory


def _zip_archive(members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            # The high 16 bits of external_attr are the POSIX mode; 0o120000 is
            # S_IFLNK, which is how a zip carries a symbolic link.
            info.external_attr = (0o120777 << 16) | 0o40
            archive.writestr(info, "elsewhere")
    return buffer.getvalue()


def _tar_archive(members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "elsewhere"
            archive.addfile(info)
    return buffer.getvalue()


@dataclass
class _Release:
    """The one release the fake ``urlopen`` serves, and what it did serve."""

    asset: str
    archive: bytes
    digest: str
    sums: str | None = None
    offline: bool = False
    requested: list[str] = field(default_factory=list)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def release(monkeypatch, shell_dir):
    """Serve one target's archive and checksum file from memory.

    The fixture answers as this machine's platform so ``fetch_desktop_shell``
    exercises the real ``(sys.platform, arch)`` lookup rather than a stubbed
    one. What it *does* stub is the pinned digest, because the payload is
    invented here.
    """

    asset = _current_asset()
    archive = (
        _zip_archive({desktop_shell.desktop_shell_binary_name(): _PAYLOAD})
        if asset.endswith(".zip")
        else _tar_archive({desktop_shell.desktop_shell_binary_name(): _PAYLOAD})
    )
    state = _Release(
        asset=asset,
        archive=archive,
        digest=hashlib.sha256(archive).hexdigest(),
    )

    def _sums_text() -> str:
        if state.sums is not None:
            return state.sums
        return f"{state.digest}  {asset}\n"

    def _urlopen(url: str, timeout: float | None = None) -> _Response:
        state.requested.append(url)
        if state.offline:
            raise OSError("getaddrinfo failed")
        if url.endswith(DESKTOP_SHELL_SUMS_ASSET):
            return _Response(_sums_text().encode("utf-8"))
        if url.endswith(asset):
            return _Response(state.archive)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(desktop_shell.urllib.request, "urlopen", _urlopen)
    monkeypatch.setitem(desktop_shell._RELEASES, _current_key(), (asset, state.digest))
    return state


def _current_key() -> tuple[str, str]:
    import platform
    import sys

    return (sys.platform, desktop_shell.normalized_architecture(platform.machine()))


def _current_asset() -> str:
    release = desktop_shell._RELEASES.get(_current_key())
    if release is None:
        pytest.skip(f"no pinned shell for {_current_key()}")
    return release[0]


class TestParseSums:
    def test_reads_the_published_two_space_format(self) -> None:
        text = f"{'a' * 64}  a.zip\n{'b' * 64}  b.tar.gz\n"

        assert parse_sha256sums(text) == {"a.zip": "a" * 64, "b.tar.gz": "b" * 64}

    def test_tolerates_crlf_and_blank_lines(self) -> None:
        text = f"\r\n{'c' * 64}  a.zip\r\n\r\n"

        assert parse_sha256sums(text) == {"a.zip": "c" * 64}

    def test_refuses_a_binary_mode_marker(self) -> None:
        """``sha256sum`` on Git for Windows writes ``<digest> *<name>``."""

        with pytest.raises(DesktopShellError, match="64 hex"):
            parse_sha256sums(f"{'d' * 64} *a.zip\n")

    def test_refuses_an_empty_file(self) -> None:
        with pytest.raises(DesktopShellError, match="no checksums"):
            parse_sha256sums("\n\n")


class TestFetch:
    def test_installs_a_verified_archive_and_records_a_receipt(
        self, release, shell_dir
    ) -> None:
        path = fetch_desktop_shell()

        assert path == shell_dir / desktop_shell.desktop_shell_binary_name()
        assert path.read_bytes() == _PAYLOAD
        receipt = desktop_shell.read_receipt()
        assert receipt is not None
        assert receipt["tag"] == DESKTOP_SHELL_RELEASE_TAG
        assert receipt["sha256"] == release.digest
        assert is_desktop_shell_installed()

    def test_the_installed_binary_is_executable(self, release, shell_dir) -> None:
        path = fetch_desktop_shell()

        assert os.access(path, os.X_OK)

    def test_the_checksum_file_is_read_before_the_archive(self, release) -> None:
        """Defence in depth is only defence if the cheap check runs first."""

        fetch_desktop_shell()

        assert release.requested[0].endswith(DESKTOP_SHELL_SUMS_ASSET)

    def test_a_tampered_archive_is_refused(self, release, shell_dir) -> None:
        release.archive = release.archive + b"trailing"

        with pytest.raises(DesktopShellError, match="Checksum verification failed"):
            fetch_desktop_shell()

        assert not desktop_shell_path().exists()
        assert not is_desktop_shell_installed()

    def test_a_published_checksum_that_disagrees_with_the_pin_is_refused(
        self, release, shell_dir
    ) -> None:
        """The whole point of pinning the digest as well as reading the file."""

        release.sums = f"{'e' * 64}  {release.asset}\n"

        with pytest.raises(DesktopShellError, match="does not match"):
            fetch_desktop_shell()

        assert not desktop_shell_path().exists()

    def test_a_checksum_file_missing_our_asset_is_refused(
        self, release, shell_dir
    ) -> None:
        release.sums = f"{'f' * 64}  SomethingElse.zip\n"

        with pytest.raises(DesktopShellError, match="does not list"):
            fetch_desktop_shell()

    def test_offline_is_a_refusal_that_names_the_reason(
        self, release, shell_dir
    ) -> None:
        release.offline = True

        with pytest.raises(DesktopShellError, match="Could not download"):
            fetch_desktop_shell()

        assert not desktop_shell_path().exists()

    def test_an_unbuilt_target_is_refused_by_name(self, monkeypatch, shell_dir) -> None:
        monkeypatch.setattr(desktop_shell.sys, "platform", "sunos5")
        monkeypatch.setattr(desktop_shell.platform, "machine", lambda: "sparc")

        with pytest.raises(DesktopShellError, match="not built for sunos5"):
            fetch_desktop_shell()


class TestUnsafeArchives:
    def _install_archive(self, release, archive: bytes) -> None:
        release.archive = archive
        release.digest = hashlib.sha256(archive).hexdigest()
        desktop_shell._RELEASES[_current_key()] = (release.asset, release.digest)

    def test_a_traversal_entry_is_refused(self, release, shell_dir) -> None:
        name = desktop_shell.desktop_shell_binary_name()
        build = _zip_archive if release.asset.endswith(".zip") else _tar_archive
        self._install_archive(
            release, build({name: _PAYLOAD, "../escaped.txt": b"nope"})
        )

        with pytest.raises(DesktopShellError, match="unsafe path"):
            fetch_desktop_shell()

        assert not desktop_shell_path().exists()

    def test_an_absolute_entry_is_refused(self, release, shell_dir) -> None:
        name = desktop_shell.desktop_shell_binary_name()
        build = _zip_archive if release.asset.endswith(".zip") else _tar_archive
        self._install_archive(release, build({name: _PAYLOAD, "/etc/passwd": b"nope"}))

        with pytest.raises(DesktopShellError, match="unsafe path"):
            fetch_desktop_shell()

    def test_a_symlink_is_refused(self, release, shell_dir) -> None:
        name = desktop_shell.desktop_shell_binary_name()
        build = _zip_archive if release.asset.endswith(".zip") else _tar_archive
        self._install_archive(
            release, build({name: _PAYLOAD}, symlink="MyClaudeCode.link")
        )

        with pytest.raises(DesktopShellError, match="link"):
            fetch_desktop_shell()

    def test_an_archive_without_the_executable_is_refused(
        self, release, shell_dir
    ) -> None:
        build = _zip_archive if release.asset.endswith(".zip") else _tar_archive
        self._install_archive(release, build({"README.txt": b"nothing here"}))

        with pytest.raises(DesktopShellError, match="exactly one"):
            fetch_desktop_shell()

    def test_a_second_copy_of_the_executable_is_refused(
        self, release, shell_dir
    ) -> None:
        """Two candidates is ambiguous, and ambiguity here means a wrong exe."""

        name = desktop_shell.desktop_shell_binary_name()
        build = _zip_archive if release.asset.endswith(".zip") else _tar_archive
        self._install_archive(
            release, build({name: _PAYLOAD, f"nested/{name}": b"another one"})
        )

        with pytest.raises(DesktopShellError, match="exactly one"):
            fetch_desktop_shell()

    def test_the_release_archives_extra_members_are_ignored(
        self, release, shell_dir
    ) -> None:
        """S6 put more than a binary in the Linux tarball, and that is fine.

        ``shell-release.yml`` now packs ``install-desktop.sh``, a ``.desktop``
        entry and four icons alongside the executable so the archive doubles as
        the Fedora/no-root installer. Delivery path A keeps working because the
        rule was never "one member": it is "exactly one member named
        ``MyClaudeCode``, and no links". The binary is listed last here on
        purpose -- order must not matter either.
        """

        name = desktop_shell.desktop_shell_binary_name()
        build = _zip_archive if release.asset.endswith(".zip") else _tar_archive
        self._install_archive(
            release,
            build(
                {
                    "install-desktop.sh": b"#!/bin/sh\nexit 0\n",
                    "my-claude-code-desktop.desktop": b"[Desktop Entry]\n",
                    "icons/256x256.png": b"\x89PNG\r\n",
                    "icons/32x32.png": b"\x89PNG\r\n",
                    name: _PAYLOAD,
                }
            ),
        )

        fetch_desktop_shell()

        assert desktop_shell_path().read_bytes() == _PAYLOAD


class TestEnsure:
    def test_an_unchanged_pin_never_downloads_again(self, release, shell_dir) -> None:
        ensure_desktop_shell()
        before = len(release.requested)

        again = ensure_desktop_shell()

        assert len(release.requested) == before
        assert again == desktop_shell_path()

    def test_a_moved_pin_downloads_again(self, release, shell_dir) -> None:
        ensure_desktop_shell()
        receipt = Path(desktop_shell.desktop_shell_receipt_path())
        receipt.write_text(
            receipt.read_text(encoding="utf-8").replace(
                DESKTOP_SHELL_RELEASE_TAG, "v0.0.1"
            ),
            encoding="utf-8",
        )
        before = len(release.requested)

        ensure_desktop_shell()

        assert len(release.requested) > before
        assert desktop_shell.installed_release_tag() == DESKTOP_SHELL_RELEASE_TAG

    def test_the_receipt_records_the_binarys_own_digest(
        self, release, shell_dir
    ) -> None:
        """Decision Q6 / D6-Q13.

        ``sha256`` in the receipt is the digest of the published ARCHIVE, which
        cannot answer "is the executable beside this receipt still the one we
        extracted?" -- the executable is not the archive. So the executable's
        own digest is recorded at the moment it is written.
        """

        ensure_desktop_shell()
        receipt = desktop_shell.read_receipt()

        assert receipt is not None
        assert receipt["binary_sha256"] == desktop_shell.binary_digest_of(
            desktop_shell_path()
        )
        assert receipt["binary_sha256"] != receipt["sha256"]

    def test_a_replaced_binary_with_an_intact_receipt_is_refused(
        self, release, shell_dir
    ) -> None:
        """The hole D6-Q13 named: the file itself was never re-checked.

        Before 6.72.0 "is the app up to date?" was answered entirely by a small
        JSON file sitting next to the exe, so a truncated, half-written or
        swapped exe with an intact receipt passed and was launched.
        """

        ensure_desktop_shell()
        assert is_desktop_shell_installed()

        desktop_shell_path().write_bytes(b"not the binary we installed")

        assert not is_desktop_shell_installed()

    def test_a_replaced_binary_is_downloaded_again(self, release, shell_dir) -> None:
        """And the refusal has to lead somewhere: it re-fetches."""

        ensure_desktop_shell()
        desktop_shell_path().write_bytes(b"corrupted")
        before = len(release.requested)

        ensure_desktop_shell()

        assert len(release.requested) > before
        assert is_desktop_shell_installed()

    def test_a_receipt_written_before_the_digest_existed_still_passes(
        self, release, shell_dir
    ) -> None:
        """Absent is not a mismatch.

        Every machine already holding the right shell has a receipt with no
        ``binary_sha256`` in it. Treating that as a failure would re-download
        the whole shell on all of them to fill in a field that did not exist
        when their receipt was written.
        """

        ensure_desktop_shell()
        receipt_path = Path(desktop_shell.desktop_shell_receipt_path())
        record = json.loads(receipt_path.read_text(encoding="utf-8"))
        del record["binary_sha256"]
        receipt_path.write_text(json.dumps(record), encoding="utf-8")

        assert is_desktop_shell_installed()

    def test_a_binary_with_no_receipt_is_not_trusted(self, release, shell_dir) -> None:
        shell_dir.mkdir(parents=True, exist_ok=True)
        desktop_shell_path().write_bytes(b"who put this here")

        assert not is_desktop_shell_installed()

        ensure_desktop_shell()

        assert desktop_shell_path().read_bytes() == _PAYLOAD

    def test_reinstalling_over_a_previous_install_is_idempotent(
        self, release, shell_dir
    ) -> None:
        first = fetch_desktop_shell()
        second = fetch_desktop_shell()

        assert first == second
        assert second.read_bytes() == _PAYLOAD
        assert is_desktop_shell_installed()

    def test_download_false_refuses_instead_of_reaching_the_network(
        self, release, shell_dir
    ) -> None:
        with pytest.raises(DesktopShellError, match="is not installed"):
            ensure_desktop_shell(download=False)

        assert release.requested == []

    def test_the_opt_out_refuses_before_anything_else(
        self, release, shell_dir, monkeypatch
    ) -> None:
        monkeypatch.setenv(desktop_shell.DESKTOP_SHELL_ENABLED_ENV, "off")

        with pytest.raises(DesktopShellError, match="off"):
            ensure_desktop_shell()

        assert release.requested == []
        assert not desktop_shell.desktop_shell_enabled()


class TestReport:
    def test_reports_nothing_installed_before_a_fetch(self, shell_dir) -> None:
        report = desktop_shell.desktop_shell_report()

        assert report["shell_ready"] is False
        assert report["shell_binary"] is None
        assert report["shell_release_tag"] == DESKTOP_SHELL_RELEASE_TAG

    def test_reports_the_path_once_installed(self, release, shell_dir) -> None:
        fetch_desktop_shell()

        report = desktop_shell.desktop_shell_report()

        assert report["shell_ready"] is True
        assert report["shell_binary"] == str(desktop_shell_path())


class TestStage:
    """``mcc-desktop --ensure-shell``: the command BUG-0 was missing.

    Until 6.60.0 the pin was enforced by ``ShellWindow.create()`` alone, so a
    window launched from the Start Menu -- or from the Programs-folder install
    the native installer makes -- kept whatever shell it first received. One
    user ran v6.43.0 for fifteen releases while the wheel moved to 6.58.4.
    """

    def test_a_binary_that_already_matches_the_pin_costs_no_network(
        self, release, shell_dir
    ) -> None:
        fetch_desktop_shell()
        release.requested.clear()

        report = desktop_shell.stage_desktop_shell()

        assert report == {
            "updated": False,
            "from_tag": DESKTOP_SHELL_RELEASE_TAG,
            "to_tag": DESKTOP_SHELL_RELEASE_TAG,
            "staged_path": None,
            "restart_required": False,
        }
        assert release.requested == [], "an unchanged pin must not touch the network"

    def test_a_missing_binary_is_installed_outright_and_needs_no_restart(
        self, release, shell_dir
    ) -> None:
        """There is no running process to protect, so there is nothing to stage."""

        report = desktop_shell.stage_desktop_shell()

        binary = desktop_shell.desktop_shell_path()
        assert report["updated"] is True
        assert report["from_tag"] is None
        assert report["to_tag"] == DESKTOP_SHELL_RELEASE_TAG
        assert report["restart_required"] is False
        assert report["staged_path"] == str(binary)
        assert binary.read_bytes() == _PAYLOAD
        assert desktop_shell.is_desktop_shell_installed()

    def test_a_stale_binary_nothing_is_running_is_simply_updated(
        self, release, shell_dir
    ) -> None:
        """The transition case, and the common one.

        Staging unconditionally would make upgrading *into* this mechanism
        impossible: the window a pre-6.60.0 user runs has no swap step in it,
        so a ``.new`` left beside it would sit there forever. Nothing is
        running this file, so it is updated.
        """

        binary = desktop_shell.desktop_shell_path()
        shell_dir.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"the window nobody has open")
        desktop_shell.receipt_path_for(binary).write_text(
            json.dumps({"tag": "v6.43.0", "sha256": "0" * 64}), encoding="utf-8"
        )

        report = desktop_shell.stage_desktop_shell()

        assert report["updated"] is True
        assert report["from_tag"] == "v6.43.0"
        assert report["to_tag"] == DESKTOP_SHELL_RELEASE_TAG
        assert report["restart_required"] is False
        assert report["staged_path"] == str(binary)
        assert binary.read_bytes() == _PAYLOAD
        assert (
            desktop_shell.installed_release_tag_at(binary) == DESKTOP_SHELL_RELEASE_TAG
        )
        # Nothing is left behind for a swap that is not going to happen.
        assert not desktop_shell.staged_binary_path(binary).exists()
        assert not desktop_shell.staged_receipt_path(binary).exists()

    def test_a_binary_something_is_running_is_staged_and_never_written_over(
        self, release, shell_dir, monkeypatch
    ) -> None:
        """The whole contract: the running window's file is not touched.

        The refusal is the operating system's -- Windows will not replace a
        running image -- so the test is of what this module does *with* that
        refusal, which is the part this repository owns.
        """

        binary = desktop_shell.desktop_shell_path()
        shell_dir.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"the window the user is looking at")
        desktop_shell.receipt_path_for(binary).write_text(
            json.dumps({"tag": "v6.43.0", "sha256": "0" * 64}), encoding="utf-8"
        )
        real_replace = os.replace

        def _refuse(source, destination):
            if str(destination) == str(binary):
                raise PermissionError(
                    32, "The process cannot access the file because it is being used"
                )
            return real_replace(source, destination)

        monkeypatch.setattr(desktop_shell.os, "replace", _refuse)

        report = desktop_shell.stage_desktop_shell()

        staged = desktop_shell.staged_binary_path(binary)
        assert report["updated"] is True
        assert report["from_tag"] == "v6.43.0"
        assert report["restart_required"] is True
        assert report["staged_path"] == str(staged)
        assert binary.read_bytes() == b"the window the user is looking at"
        assert staged.read_bytes() == _PAYLOAD
        # And the receipt waits beside it, so the swap is one more rename and
        # not a second question for Python.
        staged_receipt = json.loads(
            desktop_shell.staged_receipt_path(binary).read_text(encoding="utf-8")
        )
        assert staged_receipt["tag"] == DESKTOP_SHELL_RELEASE_TAG
        assert staged_receipt["sha256"] == release.digest
        # Until the swap, the install on disk is still the old one. Anything
        # else would have the status document claiming a version nobody runs.
        assert desktop_shell.installed_release_tag_at(binary) == "v6.43.0"

    def test_a_named_target_is_updated_rather_than_the_default_install(
        self, release, shell_dir, tmp_path
    ) -> None:
        """The Programs-folder case: the window names its own executable."""

        programs = tmp_path / "Programs" / "My Claude Code"
        programs.mkdir(parents=True)
        binary = programs / desktop_shell.desktop_shell_binary_name()
        binary.write_bytes(b"the installed window")
        desktop_shell.receipt_path_for(binary).write_text(
            json.dumps({"tag": "v6.58.3", "sha256": "0" * 64}), encoding="utf-8"
        )

        report = desktop_shell.stage_desktop_shell(binary)

        assert report["from_tag"] == "v6.58.3"
        assert report["staged_path"] == str(binary)
        assert binary.read_bytes() == _PAYLOAD
        assert (
            desktop_shell.installed_release_tag_at(binary) == DESKTOP_SHELL_RELEASE_TAG
        )
        # The default install is somewhere else entirely and was not touched.
        assert not desktop_shell.desktop_shell_path().exists()

    def test_a_swapped_archive_is_refused_and_nothing_is_staged(
        self, release, shell_dir
    ) -> None:
        """Verification is not relaxed for the staging path."""

        binary = desktop_shell.desktop_shell_path()
        shell_dir.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"the window the user is looking at")
        release.archive = b"not the archive that was published"

        with pytest.raises(DesktopShellError, match="Checksum verification failed"):
            desktop_shell.stage_desktop_shell()

        assert binary.read_bytes() == b"the window the user is looking at"
        assert not desktop_shell.staged_binary_path(binary).exists()

    def test_the_base_url_can_be_pointed_at_a_feed_that_is_not_github(
        self, monkeypatch, shell_dir
    ) -> None:
        """So the end-to-end proof can serve the release from loopback.

        The override moves *where* the assets come from and nothing else: both
        digest checks still run against them.
        """

        monkeypatch.setenv(
            desktop_shell.DESKTOP_SHELL_BASE_URL_ENV, "http://127.0.0.1:9/feed/"
        )

        assert desktop_shell.desktop_shell_base_url() == "http://127.0.0.1:9/feed"

        monkeypatch.delenv(desktop_shell.DESKTOP_SHELL_BASE_URL_ENV)
        assert (
            desktop_shell.desktop_shell_base_url()
            == desktop_shell.DESKTOP_SHELL_RELEASE_BASE_URL
        )


class TestUpdateReport:
    def test_nothing_installed_is_not_an_update_anybody_asked_for(
        self, shell_dir
    ) -> None:
        report = desktop_shell.desktop_shell_update_report()

        assert report == {
            "shell_installed_tag": None,
            "shell_pinned_tag": DESKTOP_SHELL_RELEASE_TAG,
            "shell_update_available": False,
        }

    def test_a_stale_receipt_is_an_update(self, shell_dir) -> None:
        shell_dir.mkdir(parents=True, exist_ok=True)
        desktop_shell.desktop_shell_receipt_path().write_text(
            json.dumps({"tag": "v6.43.0"}), encoding="utf-8"
        )

        report = desktop_shell.desktop_shell_update_report()

        assert report["shell_installed_tag"] == "v6.43.0"
        assert report["shell_update_available"] is True

    def test_a_current_receipt_is_not(self, shell_dir) -> None:
        shell_dir.mkdir(parents=True, exist_ok=True)
        desktop_shell.desktop_shell_receipt_path().write_text(
            json.dumps({"tag": DESKTOP_SHELL_RELEASE_TAG}), encoding="utf-8"
        )

        assert (
            desktop_shell.desktop_shell_update_report()["shell_update_available"]
            is False
        )
