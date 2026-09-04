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
