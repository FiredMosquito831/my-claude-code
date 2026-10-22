"""7.108.0 PR-R1: the pinned wireproxy -- pin, download, refusals, receipt.

A local HTTP server serves a fixture archive and a ``checksums.txt``; the
pin table is pointed at the fixture's digest for the test platform only,
and the committed copy of the real release's ``checksums.txt`` pins the
shipped table. Nothing here reaches the internet (the suite's autouse
fixture refuses the module's own downloader; each test hands
``install_wireproxy`` the real one, aimed at loopback).
"""

import hashlib
import http.server
import io
import tarfile
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from my_claude_code.config import proxy_runner_bins
from my_claude_code.config.proxy_runner_bins import (
    WIREPROXY_SUMS_ASSET,
    WIREPROXY_VERSION,
    BinaryRefusal,
    ResolvedBinary,
    RunnerBinaryError,
    binary_name,
    choose_own_binary,
    current_platform,
    file_sha256,
    install_wireproxy,
    managed_binary_path,
    managed_binary_verified,
    parse_checksums,
    parse_wireproxy_version,
    pinned_releases,
    read_receipt,
    release_for,
    resolve_wireproxy,
    unavailable_reason,
    wireproxy_status,
)
from my_claude_code.config.proxy_runner_bins import _download as REAL_DOWNLOAD

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wireproxy"
EXE = b"MZ-fixture-wireproxy-not-a-real-program\n" * 32


def _tar(members: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        for name in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            bundle.addfile(info)
    return buffer.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


class _Release:
    """A loopback release: what it serves, and every path it was asked for."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.asked: list[str] = []
        release = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                name = self.path.rsplit("/", 1)[-1]
                release.asked.append(name)
                body = release.files.get(name)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = (
            f"http://127.0.0.1:{self.server.server_address[1]}/v{WIREPROXY_VERSION}"
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, asset: str, archive: bytes, listed: str | None = None) -> str:
        digest = hashlib.sha256(archive).hexdigest()
        self.files[asset] = archive
        self.files[WIREPROXY_SUMS_ASSET] = (
            f"{listed or digest}  {asset}\n{'0' * 64}  wireproxy_other.tar.gz\n"
        ).encode()
        return digest


@pytest.fixture
def release(monkeypatch) -> Iterator[_Release]:
    served = _Release()
    monkeypatch.setenv(proxy_runner_bins.WIREPROXY_BASE_URL_ENV, served.url)
    yield served
    served.server.shutdown()
    served.server.server_close()


def _pin(monkeypatch, asset: str, digest: str) -> None:
    monkeypatch.setitem(
        proxy_runner_bins._RELEASES, current_platform(), (asset, digest)
    )


def _probe(argv: tuple[str, ...]) -> str:
    return WIREPROXY_VERSION


def _install():
    return install_wireproxy(download=REAL_DOWNLOAD, probe=_probe)


# ------------------------------------------------------------------ the pin


def test_the_pin_agrees_with_the_committed_upstream_checksums() -> None:
    """The ``test_rtk_version_pin`` precedent: every pinned digest is the one
    the v1.1.3 release's own ``checksums.txt`` names for that asset."""

    published = parse_checksums(
        (FIXTURES / f"v{WIREPROXY_VERSION}" / "checksums.txt").read_text("utf-8")
    )
    table = pinned_releases()
    assert table
    for (platform_name, architecture), (asset, digest) in table.items():
        assert published.get(asset) == digest, (platform_name, architecture, asset)


def test_windows_arm64_says_so_and_never_falls_back_to_386() -> None:
    assert release_for("win32", "ARM64") is None
    reason = unavailable_reason("win32", "arm64")
    assert "no Windows arm64 build" in reason
    assert "#209" in reason
    assert not any("386" in asset for asset, _ in pinned_releases().values())
    assert release_for("win32", "AMD64") == (
        "wireproxy_windows_amd64.tar.gz",
        pinned_releases()[("win32", "x86_64")][1],
    )


def test_checksums_are_read_strictly() -> None:
    assert parse_checksums(f"{'a' * 64}  wireproxy_linux_amd64.tar.gz\r\n") == {
        "wireproxy_linux_amd64.tar.gz": "a" * 64
    }
    with pytest.raises(RunnerBinaryError, match="not the file the release published"):
        parse_checksums("deadbeef wireproxy_linux_amd64.tar.gz\n")
    with pytest.raises(RunnerBinaryError, match="no checksums"):
        parse_checksums("\n\n")


def test_versions_are_read_from_what_the_binary_prints() -> None:
    assert parse_wireproxy_version("wireproxy, version v1.1.3\n") == "1.1.3"
    assert parse_wireproxy_version("1.0.9-dev") == "1.0.9"
    assert parse_wireproxy_version("usage: wireproxy") == ""


def test_a_test_can_never_download_by_default() -> None:
    """The autouse isolation: no downloader passed, nothing leaves loopback."""

    if release_for(*current_platform()) is None:
        pytest.skip("no pinned build for this platform")
    with pytest.raises(RunnerBinaryError, match="tests may not download"):
        install_wireproxy(probe=_probe)


# -------------------------------------------------------------- the download


def test_the_button_installs_one_verified_executable_with_a_receipt(
    release: _Release, monkeypatch
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    digest = release.publish(
        asset, _tar({f"dir/{binary_name()}": EXE, "LICENSE": b"ISC"})
    )
    _pin(monkeypatch, asset, digest)

    outcome = _install()

    assert outcome.installed and outcome.downloaded
    assert release.asked == [WIREPROXY_SUMS_ASSET, asset]
    binary = managed_binary_path()
    assert binary.read_bytes() == EXE
    assert binary.parent.name == WIREPROXY_VERSION
    assert binary.parent.parent.name == "wireproxy"
    receipt = read_receipt()
    assert receipt is not None
    assert receipt["sha256"] == digest
    assert receipt["binary_sha256"] == hashlib.sha256(EXE).hexdigest()
    assert receipt["reported_version"] == WIREPROXY_VERSION
    assert managed_binary_verified() == (True, WIREPROXY_VERSION)
    resolved = resolve_wireproxy()
    assert isinstance(resolved, ResolvedBinary)
    assert resolved.origin == "pinned"
    assert resolved.path == binary
    # No staging file or archive is left beside it.
    assert sorted(path.name for path in binary.parent.iterdir()) == sorted(
        [binary_name(), "wireproxy.receipt.json"]
    )


def test_a_second_press_downloads_nothing(release: _Release, monkeypatch) -> None:
    asset = "wireproxy_fixture.tar.gz"
    _pin(monkeypatch, asset, release.publish(asset, _tar({binary_name(): EXE})))
    _install()
    asked = list(release.asked)

    again = _install()

    assert again.installed and not again.downloaded
    assert release.asked == asked
    assert "Nothing was downloaded" in again.sentence


def test_an_archive_whose_digest_differs_is_refused(
    release: _Release, monkeypatch
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    release.publish(asset, _tar({binary_name(): EXE}))
    _pin(monkeypatch, asset, "f" * 64)
    # The release's sums agree with the pin, but the archive does not.
    release.files[WIREPROXY_SUMS_ASSET] = f"{'f' * 64}  {asset}\n".encode()

    with pytest.raises(RunnerBinaryError, match="not the pinned"):
        _install()
    assert not managed_binary_path().exists()
    assert read_receipt() is None


def test_sums_that_disagree_with_the_pin_are_refused_before_the_archive(
    release: _Release, monkeypatch
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    digest = release.publish(asset, _tar({binary_name(): EXE}), listed="e" * 64)
    _pin(monkeypatch, asset, digest)

    with pytest.raises(RunnerBinaryError, match="must agree"):
        _install()
    assert release.asked == [WIREPROXY_SUMS_ASSET]
    assert not managed_binary_path().exists()


def test_sums_that_do_not_list_the_asset_are_refused(
    release: _Release, monkeypatch
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    digest = release.publish(asset, _tar({binary_name(): EXE}))
    release.files[WIREPROXY_SUMS_ASSET] = f"{digest}  something_else.tar.gz\n".encode()
    _pin(monkeypatch, asset, digest)

    with pytest.raises(RunnerBinaryError, match="does not list"):
        _install()


@pytest.mark.parametrize(
    ("members", "links", "match"),
    [
        ({"README.md": b"no exe"}, (), "exactly one"),
        ({f"a/{binary_name()}": EXE, f"b/{binary_name()}": EXE}, (), "exactly one"),
        ({binary_name(): EXE, "../escape.txt": b"x"}, (), "unsafe path"),
        ({binary_name(): EXE, "/abs/thing": b"x"}, (), "unsafe path"),
        ({binary_name(): EXE}, ("link-to-elsewhere",), "link"),
    ],
    ids=["no-exe", "two-exes", "traversal", "absolute", "symlink"],
)
def test_an_archive_that_is_not_one_executable_is_refused(
    release: _Release, monkeypatch, members, links, match
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    _pin(monkeypatch, asset, release.publish(asset, _tar(members, links)))

    with pytest.raises(RunnerBinaryError, match=match):
        _install()
    assert not managed_binary_path().exists()
    assert read_receipt() is None


def test_a_zip_with_two_executables_is_refused(release: _Release, monkeypatch) -> None:
    asset = "wireproxy_fixture.zip"
    _pin(
        monkeypatch,
        asset,
        release.publish(
            asset, _zip({f"x/{binary_name()}": EXE, f"y/{binary_name()}": EXE})
        ),
    )

    with pytest.raises(RunnerBinaryError, match="holds 2"):
        _install()


def test_a_binary_changed_after_install_is_never_resolved(
    release: _Release, monkeypatch
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    _pin(monkeypatch, asset, release.publish(asset, _tar({binary_name(): EXE})))
    _install()

    managed_binary_path().write_bytes(EXE + b"tampered")

    verified, why = managed_binary_verified()
    assert not verified
    assert "changed after MCC installed it" in why
    assert isinstance(resolve_wireproxy(), BinaryRefusal)


def test_a_receipt_for_another_pin_is_not_trusted(
    release: _Release, monkeypatch
) -> None:
    asset = "wireproxy_fixture.tar.gz"
    digest = release.publish(asset, _tar({binary_name(): EXE}))
    _pin(monkeypatch, asset, digest)
    _install()

    _pin(monkeypatch, asset, "1" * 64)

    verified, why = managed_binary_verified()
    assert not verified
    assert "does not name the build" in why


def test_nothing_installed_is_a_refusal_naming_the_button() -> None:
    resolved = resolve_wireproxy()
    assert isinstance(resolved, BinaryRefusal)
    assert "Download wireproxy" in resolved.sentence
    status = wireproxy_status()
    assert status["ready"] is False
    assert status["version"] == WIREPROXY_VERSION
    assert status["managed"]["installed"] is False


# ---------------------------------------------------------- your own binary


def test_your_own_binary_is_recorded_with_its_version_and_digest(
    tmp_path: Path,
) -> None:
    own = tmp_path / "my-wireproxy.exe"
    own.write_bytes(b"own build")

    sentence = choose_own_binary(str(own), probe=lambda argv: "1.1.2")

    assert "1.1.2" in sentence and "pins 1.1.3" in sentence
    resolved = resolve_wireproxy()
    assert isinstance(resolved, ResolvedBinary)
    assert (resolved.origin, resolved.path, resolved.version) == ("own", own, "1.1.2")
    assert wireproxy_status()["using"] == "own"

    own.write_bytes(b"own build, changed")
    refused = resolve_wireproxy()
    assert isinstance(refused, BinaryRefusal)
    assert "changed after you chose it" in refused.sentence

    assert "Forgot" in choose_own_binary("")
    assert isinstance(resolve_wireproxy(), BinaryRefusal)
    assert wireproxy_status()["own"] is None


def test_a_binary_that_reports_no_version_is_refused(tmp_path: Path) -> None:
    own = tmp_path / "not-wireproxy"
    own.write_bytes(b"x")
    with pytest.raises(RunnerBinaryError, match="did not report a wireproxy version"):
        choose_own_binary(str(own), probe=lambda argv: "")
    with pytest.raises(RunnerBinaryError, match="There is no file"):
        choose_own_binary(str(tmp_path / "missing"), probe=_probe)
    assert file_sha256(tmp_path / "missing") is None
