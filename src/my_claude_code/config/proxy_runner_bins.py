"""The pinned wireproxy binary MCC can run for a WireGuard source (7.108.0, PR-R1).

A runner source (``application/proxy_runner_sources.py``) turns a WireGuard
file -- the one a VPN account's page exports -- into a loopback SOCKS5 address,
by running `wireproxy <https://github.com/windtf/wireproxy>`_: a userspace
WireGuard client that needs no administrator, no TUN device and no route
change. MCC does not ship it. This module is where it comes from, and how it
is trusted, shaped like :mod:`my_claude_code.config.rtk` and
:mod:`my_claude_code.config.desktop_shell`:

* **The pin is in source.** :data:`WIREPROXY_VERSION` and a
  ``(sys.platform, arch) -> (asset, sha256)`` table, read from the upstream
  release page when the pin was taken. wireproxy's releases are not signed,
  so this table *is* the trust anchor; the pin was taken more than three
  months after the release was published (spec §10, risk 4).
* **The upstream ``checksums.txt`` must agree.** A download fetches it first
  and refuses unless it lists the asset with the pinned digest -- the
  desktop shell's two-source rule -- then refuses an archive whose own digest
  differs from both.
* **Exactly one executable, extracted atomically** into
  ``~/.mcc/bin/wireproxy/<version>/``, with ``wireproxy.receipt.json`` beside
  it naming the archive's digest and the executable's own. An archive holding
  no executable, two of them, a link or a path that climbs out is refused.
* **Downloaded only when the operator presses the button** (the user's
  decision 1 of 2026-09-25): nothing here runs at startup, and a fresh install
  contacts nobody. ``Use my own binary`` records a path the operator chose, the
  version it reports and its digest instead.
* **Re-verified on every start.** :func:`resolve_wireproxy` re-reads the
  receipt and re-hashes the executable (or the operator's own binary) each time
  a runner starts; a changed file is refused, never run.

There is no Windows arm64 build of wireproxy. MCC says so rather than falling
back to the 32-bit x86 build, which Windows Defender flags (upstream #209).
"""

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .owner_only import write_owner_only_json
from .paths import config_dir_path

WIREPROXY_TOOL = "wireproxy"
#: The pinned release. Bumping it is a reviewed commit that changes this
#: version, :data:`WIREPROXY_RELEASED` and every digest below together, and the
#: committed ``tests/fixtures/wireproxy/v<version>/checksums.txt`` with them.
WIREPROXY_VERSION = "1.1.3"
WIREPROXY_TAG = f"v{WIREPROXY_VERSION}"
#: When upstream published it, and when this pin was read from its release
#: page (``gh api repos/windtf/wireproxy/releases``).
WIREPROXY_RELEASED = "2026-07-16"
WIREPROXY_PINNED_ON = "2026-10-22"
WIREPROXY_REPOSITORY = "windtf/wireproxy"
WIREPROXY_LICENSE = "ISC"
WIREPROXY_RELEASE_BASE_URL = (
    f"https://github.com/{WIREPROXY_REPOSITORY}/releases/download/{WIREPROXY_TAG}"
)
WIREPROXY_SUMS_ASSET = "checksums.txt"
#: A proof serves the release from loopback (the ``MCC_DESKTOP_SHELL_BASE_URL``
#: precedent). Nothing else about the download changes: the checksums file is
#: still fetched and must still agree with the digests pinned below.
WIREPROXY_BASE_URL_ENV = "MCC_WIREPROXY_BASE_URL"
WIREPROXY_RECEIPT_FILENAME = "wireproxy.receipt.json"
#: The operator's own binary, when they chose one (``proxy_runners.json``).
PROXY_RUNNERS_FILENAME = "proxy_runners.json"
#: How long one download may take, and how long ``wireproxy -v`` may take.
DOWNLOAD_TIMEOUT_SECONDS = 60.0
VERSION_TIMEOUT_SECONDS = 10.0

#: ``(sys.platform, architecture) -> (asset, sha256 of the asset)``, copied
#: from the v1.1.3 release's own ``checksums.txt`` on 2026-10-22, which agreed
#: with the per-asset digests GitHub publishes for that release.
_RELEASES: dict[tuple[str, str], tuple[str, str]] = {
    ("win32", "x86_64"): (
        "wireproxy_windows_amd64.tar.gz",
        "bce041ea9fe0f8a3351301dcbe29cdf6a523bb25cf9c62f17ebb5699a8051d0f",
    ),
    ("linux", "x86_64"): (
        "wireproxy_linux_amd64.tar.gz",
        "e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c",
    ),
    ("linux", "aarch64"): (
        "wireproxy_linux_arm64.tar.gz",
        "370e00bd2167960d1ecd1c3c1439715bbaa94a0a110a2040468670c9af6021b6",
    ),
    ("darwin", "x86_64"): (
        "wireproxy_darwin_amd64.tar.gz",
        "5d89742a0f381d9508d3ad828a0d300dceee24fa49015427eaff42a23f7a50bd",
    ),
    ("darwin", "aarch64"): (
        "wireproxy_darwin_arm64.tar.gz",
        "28d34342c48c309b628d9c06ab4efc05b82fba49821f360605c02139c02547a5",
    ),
}

#: Platforms wireproxy publishes nothing MCC will run, and why, in a sentence.
_NO_BUILD: dict[tuple[str, str], str] = {
    ("win32", "aarch64"): (
        "wireproxy publishes no Windows arm64 build. MCC does not fall back to "
        "its 32-bit x86 build, which Windows Defender flags (upstream issue "
        "#209). Choose a wireproxy you built yourself under Use my own binary."
    ),
}

_SUMS_LINE = re.compile(r"^([0-9a-f]{64})  (\S+)$")
_VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")


class RunnerBinaryError(Exception):
    """A download, an extraction or a verification MCC refused, in a sentence."""


@dataclass(frozen=True, slots=True)
class ResolvedBinary:
    """A wireproxy that may be started: its path and where it came from.

    ``argv`` is what starts it; a test runs a script under ``sys.executable``
    by handing the supervisor its own ``argv``.
    """

    path: Path
    origin: str
    version: str = ""
    #: Arguments that come before wireproxy's own (a test's script path).
    #: Empty for every binary this module resolves.
    leading: tuple[str, ...] = ()

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.path), *self.leading)


@dataclass(frozen=True, slots=True)
class BinaryRefusal:
    """Why no wireproxy can be started right now."""

    sentence: str


# --------------------------------------------------------------------- where


def binary_name() -> str:
    return "wireproxy.exe" if sys.platform == "win32" else "wireproxy"


def normalized_architecture(machine: str) -> str:
    architecture = machine.strip().lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
    return aliases.get(architecture, architecture)


def current_platform() -> tuple[str, str]:
    return (sys.platform, normalized_architecture(platform.machine()))


def release_for(platform_name: str, architecture: str) -> tuple[str, str] | None:
    return _RELEASES.get((platform_name, normalized_architecture(architecture)))


def pinned_releases() -> dict[tuple[str, str], tuple[str, str]]:
    """A copy of the pin table, for the pin test and the page."""

    return dict(_RELEASES)


def unavailable_reason(platform_name: str, architecture: str) -> str:
    """Why this platform has no pinned build, or ``""`` when it has one."""

    key = (platform_name, normalized_architecture(architecture))
    if key in _RELEASES:
        return ""
    return _NO_BUILD.get(
        key,
        f"wireproxy {WIREPROXY_VERSION} publishes no build MCC pins for "
        f"{platform_name} {architecture or 'unknown architecture'}. Choose a "
        "wireproxy you built yourself under Use my own binary.",
    )


def wireproxy_bin_dir() -> Path:
    """``~/.mcc/bin/wireproxy/<version>/``: one directory per pinned version."""

    return config_dir_path() / "bin" / WIREPROXY_TOOL / WIREPROXY_VERSION


def managed_binary_path() -> Path:
    return wireproxy_bin_dir() / binary_name()


def receipt_path() -> Path:
    return wireproxy_bin_dir() / WIREPROXY_RECEIPT_FILENAME


def proxy_runners_path() -> Path:
    return config_dir_path() / PROXY_RUNNERS_FILENAME


def runners_dir() -> Path:
    """``~/.mcc/runners/``: one directory per runner source."""

    return config_dir_path() / "runners"


#: What MCC writes in ``runners/<source>/``: the generated config (it holds
#: the private key), the running pid and its creation time, and the redacted
#: log. Nothing else is ever put there.
RUNNER_CONFIG_FILENAME = "wireproxy.conf"
RUNNER_PID_FILENAME = "pid.json"
RUNNER_LOG_FILENAME = "runner.log"
RUNNER_FILES: tuple[str, ...] = (
    RUNNER_CONFIG_FILENAME,
    RUNNER_PID_FILENAME,
    RUNNER_LOG_FILENAME,
)


def remove_runner_files(source_id: str, base: Path | None = None) -> None:
    """Delete the three files a runner source has and its directory, if empty.

    Only those names: a file somebody else put there is left, and so is the
    directory then.
    """

    directory = (base if base is not None else runners_dir()) / source_id
    for name in RUNNER_FILES:
        with suppress(OSError):
            (directory / name).unlink(missing_ok=True)
    with suppress(OSError):
        directory.rmdir()


def base_url() -> str:
    override = os.environ.get(WIREPROXY_BASE_URL_ENV, "").strip()
    return override.rstrip("/") if override else WIREPROXY_RELEASE_BASE_URL


# ------------------------------------------------------------------ digests


def file_sha256(path: Path) -> str | None:
    """The sha256 of a file, read in chunks; ``None`` when it cannot be read."""

    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    """``checksums.txt`` as ``{asset: sha256}``. Strict: a line it cannot read
    means the file is not the one upstream's release workflow wrote."""

    digests: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        match = _SUMS_LINE.match(line)
        if match is None:
            raise RunnerBinaryError(
                f"{WIREPROXY_SUMS_ASSET} has a line that is not "
                "'<64 hex><two spaces><file name>', so it is not the file the "
                "release published. Nothing was installed."
            )
        digests[match.group(2)] = match.group(1)
    if not digests:
        raise RunnerBinaryError(f"{WIREPROXY_SUMS_ASSET} lists no checksums.")
    return digests


# ---------------------------------------------------------------- versions


def parse_wireproxy_version(text: str) -> str:
    """The ``x.y.z`` a ``wireproxy -v`` printed, or ``""``."""

    match = _VERSION.search(text or "")
    return match.group(1) if match else ""


def _creationflags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def run_version(argv: tuple[str, ...]) -> str:
    """Run ``<binary> -v`` and return the version it reports, or ``""``.

    No shell, no window, nothing on stdin, a bounded wait. A binary that will
    not run, or prints no version, reports ``""``.
    """

    try:
        completed = subprocess.run(
            [*argv, "-v"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
    except OSError, subprocess.SubprocessError, ValueError:
        return ""
    return parse_wireproxy_version(f"{completed.stdout}\n{completed.stderr}")


# -------------------------------------------------------------- the receipt


def read_receipt(path: Path | None = None) -> dict[str, str] | None:
    resolved = path if path is not None else receipt_path()
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    return {str(key): str(value) for key, value in raw.items()}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def managed_binary_verified() -> tuple[bool, str]:
    """Whether the installed binary is exactly what the pin and receipt say.

    ``(True, version)``, or ``(False, why)``. The receipt must name this
    build's version and the pinned archive digest for this platform, and the
    executable on disk must still hash to the digest the receipt recorded
    when it was written.
    """

    binary = managed_binary_path()
    if not binary.is_file():
        return False, "wireproxy is not installed: press Download wireproxy."
    receipt = read_receipt()
    release = release_for(*current_platform())
    if receipt is None:
        return False, (
            f"{binary} has no receipt beside it, so MCC cannot vouch for it. "
            "Press Download wireproxy to install the pinned build."
        )
    if (
        release is None
        or receipt.get("version") != WIREPROXY_VERSION
        or receipt.get("asset") != release[0]
        or receipt.get("sha256") != release[1]
    ):
        return False, (
            "The installed wireproxy's receipt does not name the build this MCC "
            "pins. Press Download wireproxy to install it."
        )
    if file_sha256(binary) != receipt.get("binary_sha256"):
        return False, (
            f"{binary} changed after MCC installed it, so it is not run. Press "
            "Download wireproxy to put the pinned build back."
        )
    return True, receipt.get("reported_version", "")


# ------------------------------------------------------------ the download


Downloader = Callable[[str, float], bytes]
VersionProbe = Callable[[tuple[str, ...]], str]


def _download(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return bytes(response.read())
    except OSError as exc:
        raise RunnerBinaryError(
            f"Could not download {urlsplit(url).path.rsplit('/', 1)[-1]} from "
            f"{urlsplit(url).netloc}: {type(exc).__name__}. Nothing was installed."
        ) from exc


def _is_unsafe_member(name: str) -> bool:
    if not name or name.startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    return ".." in PurePosixPath(name.replace("\\", "/")).parts


def _unsafe(name: str) -> RunnerBinaryError:
    return RunnerBinaryError(
        f"The wireproxy archive contains an unsafe path ({name!r}), so it is "
        "not the archive upstream published. Nothing was installed."
    )


def _count_refusal(found: int) -> RunnerBinaryError:
    return RunnerBinaryError(
        f"The wireproxy archive must hold exactly one {binary_name()}; it holds "
        f"{found}. Nothing was installed."
    )


def _extract_tar(archive: Path, destination: Path) -> None:
    name = binary_name()
    with tarfile.open(archive, "r:gz") as bundle:
        found = []
        for member in bundle.getmembers():
            if _is_unsafe_member(member.name):
                raise _unsafe(member.name)
            if member.issym() or member.islnk():
                raise RunnerBinaryError(
                    f"The wireproxy archive contains a link ({member.name!r}); "
                    "it should hold one executable. Nothing was installed."
                )
            if member.isfile() and PurePosixPath(member.name).name == name:
                found.append(member)
        if len(found) != 1:
            raise _count_refusal(len(found))
        source = bundle.extractfile(found[0])
        if source is None:
            raise _count_refusal(0)
        with source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _extract_zip(archive: Path, destination: Path) -> None:
    name = binary_name()
    with zipfile.ZipFile(archive) as bundle:
        found = []
        for info in bundle.infolist():
            if _is_unsafe_member(info.filename):
                raise _unsafe(info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RunnerBinaryError(
                    f"The wireproxy archive contains a link ({info.filename!r}); "
                    "it should hold one executable. Nothing was installed."
                )
            if not info.is_dir() and PurePosixPath(info.filename).name == name:
                found.append(info)
        if len(found) != 1:
            raise _count_refusal(len(found))
        with bundle.open(found[0]) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _extract(archive: Path, asset: str, destination: Path) -> None:
    try:
        if asset.endswith(".zip"):
            _extract_zip(archive, destination)
        else:
            _extract_tar(archive, destination)
    except RunnerBinaryError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
        raise RunnerBinaryError(
            f"Could not read the wireproxy archive ({type(exc).__name__}). "
            "Nothing was installed."
        ) from exc
    if destination.stat().st_size == 0:
        raise RunnerBinaryError("The wireproxy executable was empty.")


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """What a press of *Download wireproxy* did."""

    installed: bool
    downloaded: bool
    sentence: str


def install_wireproxy(
    *,
    download: Downloader | None = None,
    probe: VersionProbe | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    platform_key: tuple[str, str] | None = None,
) -> InstallOutcome:
    """Download, verify and install the pinned wireproxy. The button's whole job.

    A receipt that already matches the pin and an executable that still
    hashes to it is a no-op (nothing downloaded). Otherwise: ``checksums.txt``
    first, which must agree with the pin; then the archive, whose digest must
    match both; then exactly one executable, extracted beside its final place
    and moved there in one rename, and the receipt written last.
    """

    key = platform_key if platform_key is not None else current_platform()
    release = release_for(*key)
    if release is None:
        raise RunnerBinaryError(unavailable_reason(*key))
    asset, pinned = release
    # Looked up now, not bound as defaults, so a test can isolate them.
    download = download if download is not None else _download
    probe = probe if probe is not None else run_version
    verified, _ = managed_binary_verified()
    if verified:
        return InstallOutcome(
            installed=True,
            downloaded=False,
            sentence=(
                f"wireproxy {WIREPROXY_VERSION} is already installed and its "
                "digest still matches the pin. Nothing was downloaded."
            ),
        )
    root = base_url()
    published = parse_checksums(
        download(f"{root}/{WIREPROXY_SUMS_ASSET}", timeout).decode("utf-8", "replace")
    )
    listed = published.get(asset)
    if listed is None:
        raise RunnerBinaryError(
            f"The release's {WIREPROXY_SUMS_ASSET} does not list {asset}. "
            "Nothing was installed."
        )
    if listed != pinned:
        raise RunnerBinaryError(
            f"The release's {WIREPROXY_SUMS_ASSET} says {asset} is {listed}, but "
            f"this MCC pins {pinned}. The two must agree, so nothing was "
            "installed."
        )
    body = download(f"{root}/{asset}", timeout)
    digest = hashlib.sha256(body).hexdigest()
    if digest != pinned:
        raise RunnerBinaryError(
            f"{asset} hashed to {digest}, not the pinned {pinned}. Nothing was "
            "installed."
        )
    directory = wireproxy_bin_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = secrets.token_hex(4)
    archive = directory / f".download-{stamp}-{asset}"
    staged = directory / f".{binary_name()}.{stamp}.tmp"
    final = managed_binary_path()
    try:
        archive.write_bytes(body)
        _extract(archive, asset, staged)
        staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        reported = probe((str(staged),))
        os.replace(staged, final)
    except RunnerBinaryError:
        with suppress(OSError):
            staged.unlink(missing_ok=True)
        raise
    except OSError as exc:
        with suppress(OSError):
            staged.unlink(missing_ok=True)
        raise RunnerBinaryError(
            f"Could not install wireproxy at {final}: {type(exc).__name__}. "
            "If a runner is using it, stop the runner first."
        ) from exc
    finally:
        with suppress(OSError):
            archive.unlink(missing_ok=True)
    write_owner_only_json(
        receipt_path(),
        {
            "tool": WIREPROXY_TOOL,
            "version": WIREPROXY_VERSION,
            "tag": WIREPROXY_TAG,
            "asset": asset,
            "sha256": pinned,
            "binary": binary_name(),
            "binary_sha256": file_sha256(final) or "",
            "reported_version": reported,
            "installed_at": _now(),
            "from": urlsplit(root).netloc,
        },
    )
    said = f" It reports version {reported}." if reported else ""
    return InstallOutcome(
        installed=True,
        downloaded=True,
        sentence=(
            f"Installed wireproxy {WIREPROXY_VERSION} ({asset}, sha256 {pinned[:12]}…, "
            f"matching the release's {WIREPROXY_SUMS_ASSET}) at {final}.{said}"
        ),
    )


# -------------------------------------------------------- your own binary


def _read_runner_settings() -> dict[str, Any]:
    try:
        raw = json.loads(proxy_runners_path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def own_binary_record() -> dict[str, str] | None:
    """The operator's own wireproxy, when they chose one: path, digest, version."""

    raw = _read_runner_settings().get("own_binary")
    if not isinstance(raw, dict) or not str(raw.get("path") or "").strip():
        return None
    return {str(key): str(value) for key, value in raw.items()}


def choose_own_binary(path_text: str, *, probe: VersionProbe | None = None) -> str:
    """Record the operator's own wireproxy, or forget it (``path_text`` empty).

    It must be a file that runs and reports a version. Its digest is recorded
    so that a start can refuse a binary that changed since it was chosen.
    """

    cleaned = path_text.strip().strip('"')
    document = _read_runner_settings()
    if not cleaned:
        document.pop("own_binary", None)
        write_owner_only_json(proxy_runners_path(), document | {"version": 1})
        return "Forgot your own wireproxy: runners use the pinned download."
    binary = Path(cleaned).expanduser()
    if not binary.is_file():
        raise RunnerBinaryError(f"There is no file at {binary}.")
    reported = (probe if probe is not None else run_version)((str(binary),))
    if not reported:
        raise RunnerBinaryError(
            f"{binary} did not report a wireproxy version when asked "
            "(`-v`), so MCC will not run it."
        )
    digest = file_sha256(binary)
    if digest is None:
        raise RunnerBinaryError(f"{binary} cannot be read.")
    document["own_binary"] = {
        "path": str(binary),
        "sha256": digest,
        "version": reported,
        "chosen_at": _now(),
    }
    write_owner_only_json(proxy_runners_path(), document | {"version": 1})
    differs = (
        f" This MCC pins {WIREPROXY_VERSION}; yours is used as it is."
        if reported != WIREPROXY_VERSION
        else ""
    )
    return f"Runners will use your wireproxy {reported} at {binary}.{differs}"


def resolve_wireproxy() -> ResolvedBinary | BinaryRefusal:
    """The wireproxy a start may run, re-verified now, or why there is none."""

    own = own_binary_record()
    if own is not None:
        binary = Path(own["path"])
        if not binary.is_file():
            return BinaryRefusal(
                f"Your own wireproxy at {binary} is gone. Choose it again, or "
                "forget it and use the pinned download."
            )
        if file_sha256(binary) != own.get("sha256"):
            return BinaryRefusal(
                f"Your own wireproxy at {binary} changed after you chose it, so "
                "it is not run. Choose it again if the change is yours."
            )
        return ResolvedBinary(binary, "own", own.get("version", ""))
    verified, said = managed_binary_verified()
    if not verified:
        return BinaryRefusal(said)
    return ResolvedBinary(managed_binary_path(), "pinned", said)


def wireproxy_status() -> dict[str, Any]:
    """The page's wireproxy card: the pin, what is installed, what runs."""

    key = current_platform()
    release = release_for(*key)
    verified, said = managed_binary_verified()
    receipt = read_receipt() if verified else None
    own = own_binary_record()
    resolved = resolve_wireproxy()
    return {
        "tool": WIREPROXY_TOOL,
        "version": WIREPROXY_VERSION,
        "tag": WIREPROXY_TAG,
        "released": WIREPROXY_RELEASED,
        "pinned_on": WIREPROXY_PINNED_ON,
        "repository": WIREPROXY_REPOSITORY,
        "license": WIREPROXY_LICENSE,
        "asset": release[0] if release is not None else "",
        "sha256": release[1] if release is not None else "",
        "unavailable": unavailable_reason(*key),
        "managed": {
            "installed": verified,
            "path": str(managed_binary_path()),
            "sentence": "" if verified else said,
            "reported_version": receipt.get("reported_version", "") if receipt else "",
            "installed_at": receipt.get("installed_at", "") if receipt else "",
        },
        "own": None
        if own is None
        else {
            "path": own.get("path", ""),
            "version": own.get("version", ""),
            "chosen_at": own.get("chosen_at", ""),
        },
        "using": resolved.origin if isinstance(resolved, ResolvedBinary) else "",
        "ready": isinstance(resolved, ResolvedBinary),
        "sentence": "" if isinstance(resolved, ResolvedBinary) else resolved.sentence,
    }


__all__ = [
    "DOWNLOAD_TIMEOUT_SECONDS",
    "PROXY_RUNNERS_FILENAME",
    "RUNNER_CONFIG_FILENAME",
    "RUNNER_FILES",
    "RUNNER_LOG_FILENAME",
    "RUNNER_PID_FILENAME",
    "WIREPROXY_BASE_URL_ENV",
    "WIREPROXY_RECEIPT_FILENAME",
    "WIREPROXY_RELEASED",
    "WIREPROXY_RELEASE_BASE_URL",
    "WIREPROXY_REPOSITORY",
    "WIREPROXY_SUMS_ASSET",
    "WIREPROXY_TAG",
    "WIREPROXY_TOOL",
    "WIREPROXY_VERSION",
    "BinaryRefusal",
    "InstallOutcome",
    "ResolvedBinary",
    "RunnerBinaryError",
    "base_url",
    "binary_name",
    "choose_own_binary",
    "current_platform",
    "file_sha256",
    "install_wireproxy",
    "managed_binary_path",
    "managed_binary_verified",
    "own_binary_record",
    "parse_checksums",
    "parse_wireproxy_version",
    "pinned_releases",
    "proxy_runners_path",
    "read_receipt",
    "receipt_path",
    "release_for",
    "remove_runner_files",
    "resolve_wireproxy",
    "run_version",
    "runners_dir",
    "unavailable_reason",
    "wireproxy_bin_dir",
    "wireproxy_status",
]
