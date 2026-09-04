"""The pinned desktop shell: where it comes from, and how it is trusted.

The desktop shell (``desktop-shell/``, a Tauri window) is a compiled binary
this project builds in CI and attaches to the same GitHub release as the
wheel. ``uv tool install`` cannot deliver a compiled binary, so Python fetches
it on first launch -- exactly the way :mod:`my_claude_code.config.rtk` has
delivered the RTK optimizer for five targets since before 6.40.0. This module
is that machinery for our own shell, and it is deliberately shaped like
``rtk.py``: a ``(sys.platform, arch) -> (asset, sha256)`` table, a download, a
digest comparison, a safe extraction and an atomic install.

Three things differ from ``rtk.py``, and each one is a decision:

* **The pin is a release tag, not a version.** The shell binary's name carries
  no version (decision Q5), so a shortcut or a ``.desktop`` ``Exec=`` line
  survives every upgrade. What the Python side pins is *which release's* shell
  it wants: :data:`DESKTOP_SHELL_RELEASE_TAG`. Bumping it is a release-checklist
  step, not something that happens by itself.

* **The digests are checked twice, against two sources.** The release carries
  ``SHA256SUMS-desktop-shell.txt``; the digests are *also* pinned in this file.
  A fetch downloads the sums file first and refuses if it disagrees with the
  in-source pin. Neither source alone is enough: a sums file on its own trusts
  whoever can write to the release, and an in-source digest on its own gives no
  signal when a release is re-uploaded. Both must agree, and the archive must
  then match both.

* **A receipt records what was installed.** ``MyClaudeCode.receipt.json`` sits
  next to the binary and names the tag and digest that produced it, so an
  unchanged pin never downloads anything again, and a moved pin always does.

Nothing here is on the server's startup path: ``mcc-server`` never imports this
module, and a contract test pins that. The only caller is ``mcc-desktop``.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

#: The release whose shell assets this build wants. Bumping it is a step in
#: ``docs/RELEASE-CHECKLIST.md``: cut the release, wait for
#: ``shell-release.yml`` to attach the five assets, then move this tag and the
#: four digests below together, in one commit.
DESKTOP_SHELL_RELEASE_TAG = "v6.43.0"

#: The repository the shell is released from. The same one
#: ``application/release_updates.py`` polls for the wheel -- one release stream
#: (decision Q6).
DESKTOP_SHELL_RELEASE_REPO = "FiredMosquito831/my-claude-code"

DESKTOP_SHELL_RELEASE_BASE_URL = (
    f"https://github.com/{DESKTOP_SHELL_RELEASE_REPO}/releases/download/"
    f"{DESKTOP_SHELL_RELEASE_TAG}"
)

#: The aggregated checksum file ``shell-release.yml`` attaches to the release.
#: Its format is a published contract: ``<64 hex><space><space><filename>``.
DESKTOP_SHELL_SUMS_ASSET = "SHA256SUMS-desktop-shell.txt"

#: The installed binary's name. Version-free on purpose (decision Q5).
DESKTOP_SHELL_BINARY_STEM = "MyClaudeCode"

#: The install receipt, beside the binary.
DESKTOP_SHELL_RECEIPT_FILENAME = "MyClaudeCode.receipt.json"

#: Overrides the install directory. Exists so a smoke run -- or a test -- can
#: exercise the whole fetch without writing into the developer's ``~/.local/bin``,
#: which on a real machine holds the live ``mcc-server`` shim.
DESKTOP_SHELL_DIR_ENV = "MCC_DESKTOP_SHELL_DIR"

#: ``off`` disables the shell entirely: ``auto`` (the default) lets it lead the
#: window chain. Read from the environment rather than declared as a ``Settings``
#: field because it is a launch-time switch for a *separate process* from the
#: server, and the dashboard cannot change a decision already taken.
DESKTOP_SHELL_ENABLED_ENV = "DESKTOP_SHELL"

#: Seconds for one HTTP read. Two are made: the sums file and the archive.
DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS = 60.0

#: ``(sys.platform, normalized arch) -> (asset name, sha256 of the archive)``.
#:
#: The four targets ``shell-release.yml`` builds. ``linux/aarch64`` is
#: deliberately absent: no runner builds it (see the workflow's matrix), so
#: claiming it here would mean a 404 on a machine that has a working fallback.
_RELEASES: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        "MyClaudeCode-linux-x86_64.tar.gz",
        "ec4f92d648aee24a90184d30cdb39204be466df81c3ef145ecb6ca701a08a55f",
    ),
    ("darwin", "x86_64"): (
        "MyClaudeCode-macos-x86_64.tar.gz",
        "41d059d34e8cca7b1cbdebce0e5b7e6e0ba6019aa5949927a91ec76bd1b7ff96",
    ),
    ("darwin", "aarch64"): (
        "MyClaudeCode-macos-aarch64.tar.gz",
        "cfc66ea64cb7a89fc006431240dd67c43ea1a79cc58e9a5918a67e7242d24183",
    ),
    ("win32", "x86_64"): (
        "MyClaudeCode-windows-x86_64.zip",
        "3a159d2494abadc1fc19855fb44765af3a0b28535711177de2c1852bfa92436e",
    ),
}

#: Machine-name aliases, identical to ``rtk.py``'s. ``test_desktop_shell_pin.py``
#: asserts the two tables stay equal rather than trusting a comment.
_ARCH_ALIASES: dict[str, str] = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "arm64": "aarch64",
}

#: One line of the checksum file. Two spaces, no mode marker -- the workflow
#: rebuilds every line from the raw digest so all four runners agree.
_SUMS_LINE = re.compile(r"^([0-9a-f]{64})  (\S.*)$")


class DesktopShellError(Exception):
    """Raised when the pinned shell cannot be resolved, verified or installed."""


# ---------------------------------------------------------------- placement


def desktop_shell_binary_name() -> str:
    """Return the installed executable's file name for this platform."""

    suffix = ".exe" if sys.platform == "win32" else ""
    return f"{DESKTOP_SHELL_BINARY_STEM}{suffix}"


def desktop_shell_dir() -> Path:
    """Return where the shell is installed.

    ``~/.local/bin`` by default -- the directory ``install.sh`` already adds to
    ``PATH`` and where ``rtk.py`` puts its own managed binary -- overridable
    with :data:`DESKTOP_SHELL_DIR_ENV` so nothing that is only being exercised
    has to write there.
    """

    override = os.environ.get(DESKTOP_SHELL_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "bin"


def desktop_shell_path() -> Path:
    """Return the full path the shell binary is installed at."""

    return desktop_shell_dir() / desktop_shell_binary_name()


def desktop_shell_receipt_path() -> Path:
    """Return the install receipt's path, beside the binary."""

    return desktop_shell_dir() / DESKTOP_SHELL_RECEIPT_FILENAME


def desktop_shell_enabled() -> bool:
    """Return whether the shell may be used at all on this machine.

    ``DESKTOP_SHELL=off`` is the documented opt-out. Anything else -- unset,
    ``auto``, a typo -- leaves the shell enabled, because a guardrail that
    turns a misspelling into a missing window is worse than one that ignores it.
    """

    return os.environ.get(DESKTOP_SHELL_ENABLED_ENV, "auto").strip().lower() != "off"


# ----------------------------------------------------------------- the pin


def normalized_architecture(machine: str) -> str:
    """Return a canonical architecture name for a ``platform.machine()`` value."""

    architecture = machine.strip().lower()
    return _ARCH_ALIASES.get(architecture, architecture)


def release_for(platform_name: str, architecture: str) -> tuple[str, str] | None:
    """Return ``(asset, sha256)`` for one target, or ``None`` when unsupported."""

    return _RELEASES.get((platform_name, normalized_architecture(architecture)))


def release_for_current_platform() -> tuple[str, str]:
    """Return this machine's ``(asset, sha256)``, or explain why there is none."""

    machine = platform.machine()
    release = release_for(sys.platform, machine)
    if release is None:
        raise DesktopShellError(
            f"The desktop app is not built for {sys.platform} "
            f"{machine or 'unknown architecture'}."
        )
    return release


# ------------------------------------------------------------- the receipt


def read_receipt() -> dict[str, str] | None:
    """Return the install receipt, or ``None`` when there is not a valid one."""

    try:
        data = json.loads(desktop_shell_receipt_path().read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        name: value
        for name, value in data.items()
        if isinstance(name, str) and isinstance(value, str)
    }


def installed_release_tag() -> str | None:
    """Return the tag the installed binary came from, or ``None``."""

    receipt = read_receipt()
    return None if receipt is None else receipt.get("tag")


def is_desktop_shell_installed() -> bool:
    """Return whether the binary on disk is the one the pin asks for.

    Both halves matter. A binary with no receipt is something we did not put
    there and cannot vouch for; a receipt naming another tag is the signal that
    the pin moved and the next launch must fetch again.
    """

    if not desktop_shell_path().is_file():
        return False
    receipt = read_receipt()
    if receipt is None:
        return False
    if receipt.get("tag") != DESKTOP_SHELL_RELEASE_TAG:
        return False
    expected = release_for(sys.platform, platform.machine())
    return expected is not None and receipt.get("sha256") == expected[1]


def _write_receipt(asset: str, digest: str) -> None:
    path = desktop_shell_receipt_path()
    payload = json.dumps(
        {
            "tag": DESKTOP_SHELL_RELEASE_TAG,
            "asset": asset,
            "sha256": digest,
            "binary": desktop_shell_binary_name(),
        },
        indent=2,
    )
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise DesktopShellError(
            f"Could not record the desktop app install receipt at {path}: {exc}"
        ) from exc


# --------------------------------------------------------------- the fetch


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``SHA256SUMS-desktop-shell.txt`` into ``{filename: digest}``.

    Strict on purpose. The workflow asserts the file's shape before uploading
    it, so a line this cannot read means the file is not the one that workflow
    produced, and guessing at it is exactly the wrong response.
    """

    digests: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        match = _SUMS_LINE.match(line)
        if match is None:
            raise DesktopShellError(
                f"{DESKTOP_SHELL_SUMS_ASSET} has a line that is not "
                f"'<64 hex><space><space><filename>': {line!r}"
            )
        digests[match.group(2)] = match.group(1)
    if not digests:
        raise DesktopShellError(f"{DESKTOP_SHELL_SUMS_ASSET} listed no checksums.")
    return digests


def _download(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return bytes(response.read())
    except OSError as exc:
        raise DesktopShellError(f"Could not download {url}: {exc}") from exc


def _confirm_published_digest(asset: str, pinned: str, timeout: float) -> None:
    """Refuse unless the release's own checksum file agrees with the pin.

    This is the half of the trust story that source control cannot provide. A
    release's assets can be replaced after the fact by anyone who can write to
    the repository; the digest in this file cannot, because changing it is a
    reviewed commit. Requiring the two to agree means a swapped asset produces a
    refusal here rather than an unexpected binary on someone's machine.
    """

    sums_url = f"{DESKTOP_SHELL_RELEASE_BASE_URL}/{DESKTOP_SHELL_SUMS_ASSET}"
    published = parse_sha256sums(
        _download(sums_url, timeout).decode("utf-8", "replace")
    )
    found = published.get(asset)
    if found is None:
        raise DesktopShellError(
            f"{DESKTOP_SHELL_SUMS_ASSET} on {DESKTOP_SHELL_RELEASE_TAG} does not "
            f"list {asset}."
        )
    if found != pinned:
        raise DesktopShellError(
            f"The desktop app's published checksum for {asset} does not match "
            f"the one pinned in this build: the release says {found}, this build "
            f"expects {pinned}. Refusing to install it."
        )


def _is_unsafe_member_name(name: str) -> bool:
    """Return whether an archive member name may not be trusted.

    Absolute paths, drive letters, and any ``..`` component. The extraction
    below writes to one path it chose itself, so traversal cannot happen by
    construction -- but an archive that *contains* such a member is not the
    archive our workflow produced, and the right answer to that is to stop.
    """

    if not name or name.startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:", name):
        return True
    parts = PurePosixPath(name.replace("\\", "/")).parts
    return ".." in parts


def _extract_zip(archive_path: Path, binary_name: str, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        members = []
        for info in archive.infolist():
            if _is_unsafe_member_name(info.filename):
                raise DesktopShellError(
                    f"The desktop app archive contains an unsafe path: "
                    f"{info.filename!r}."
                )
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise DesktopShellError(
                    "The desktop app archive contains a symbolic link "
                    f"({info.filename!r}); it should hold one executable."
                )
            if info.is_dir():
                continue
            if PurePosixPath(info.filename).name == binary_name:
                members.append(info)
        if len(members) != 1:
            raise DesktopShellError(
                f"The desktop app archive must contain exactly one "
                f"{binary_name}; it holds {len(members)}."
            )
        with archive.open(members[0]) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _extract_tar(archive_path: Path, binary_name: str, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = []
        for member in archive.getmembers():
            if _is_unsafe_member_name(member.name):
                raise DesktopShellError(
                    f"The desktop app archive contains an unsafe path: {member.name!r}."
                )
            if member.issym() or member.islnk():
                raise DesktopShellError(
                    "The desktop app archive contains a link "
                    f"({member.name!r}); it should hold one executable."
                )
            if not member.isfile():
                continue
            if PurePosixPath(member.name).name == binary_name:
                members.append(member)
        if len(members) != 1:
            raise DesktopShellError(
                f"The desktop app archive must contain exactly one "
                f"{binary_name}; it holds {len(members)}."
            )
        source = archive.extractfile(members[0])
        if source is None:
            raise DesktopShellError("The desktop app executable could not be read.")
        with source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)


def _extract_binary(archive_path: Path, asset: str, destination: Path) -> None:
    binary_name = desktop_shell_binary_name()
    try:
        if asset.endswith(".zip"):
            _extract_zip(archive_path, binary_name, destination)
        else:
            _extract_tar(archive_path, binary_name, destination)
    except DesktopShellError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise DesktopShellError(
            f"Could not extract the verified desktop app archive: {exc}"
        ) from exc
    if destination.stat().st_size == 0:
        raise DesktopShellError("The desktop app executable was empty.")


def _sweep_renamed_aside(directory: Path) -> None:
    """Delete previous rename-aside copies that are no longer running."""

    with suppress(OSError):
        for stale in directory.glob(f"{DESKTOP_SHELL_BINARY_STEM}.old-*"):
            with suppress(OSError):
                stale.unlink()


def _install_atomically(staged: Path, destination: Path) -> None:
    """Replace the installed binary, renaming a locked one aside first.

    ``os.replace`` is atomic everywhere and is the whole story on POSIX. On
    Windows it fails with a sharing violation when the target is a *running*
    executable -- which is precisely the case that matters here, because the
    thing being upgraded is the window the user may still have open. Windows
    does allow an open file to be renamed, so the running copy is moved out of
    the way and swept on a later launch. This is the same shape as the
    installer's shim rename-aside, for the same reason.
    """

    try:
        os.replace(staged, destination)
        return
    except OSError:
        if not destination.exists():
            raise
    aside = destination.with_name(
        f"{DESKTOP_SHELL_BINARY_STEM}.old-{int(time.time())}{destination.suffix}"
    )
    try:
        os.replace(destination, aside)
        os.replace(staged, destination)
    except OSError as exc:
        raise DesktopShellError(
            f"Could not install the desktop app at {destination}: {exc}. "
            "Close the My Claude Code window and try again."
        ) from exc


def fetch_desktop_shell(
    *, timeout: float = DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS
) -> Path:
    """Download, verify and install the pinned shell. Returns its path."""

    asset, pinned_digest = release_for_current_platform()
    directory = desktop_shell_dir()
    destination = directory / desktop_shell_binary_name()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DesktopShellError(
            f"Could not create the desktop app directory {directory}: {exc}"
        ) from exc

    _confirm_published_digest(asset, pinned_digest, timeout)

    payload = _download(f"{DESKTOP_SHELL_RELEASE_BASE_URL}/{asset}", timeout)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != pinned_digest:
        raise DesktopShellError(
            f"Checksum verification failed for {asset}: expected "
            f"{pinned_digest}, got {digest}."
        )

    _sweep_renamed_aside(directory)
    staged = destination.with_name(f".{destination.name}.tmp")
    with tempfile.TemporaryDirectory(prefix="mcc-desktop-shell-") as scratch:
        archive_path = Path(scratch) / asset
        try:
            archive_path.write_bytes(payload)
            _extract_binary(archive_path, asset, staged)
            staged.chmod(
                staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            _install_atomically(staged, destination)
        except DesktopShellError:
            with suppress(OSError):
                staged.unlink(missing_ok=True)
            raise
        except OSError as exc:
            with suppress(OSError):
                staged.unlink(missing_ok=True)
            raise DesktopShellError(
                f"Could not install the desktop app at {destination}: {exc}"
            ) from exc

    _write_receipt(asset, pinned_digest)
    return destination


def ensure_desktop_shell(
    *,
    download: bool = True,
    timeout: float = DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS,
) -> Path:
    """Return a verified shell binary, fetching the pinned release if needed.

    The short-circuit is the receipt, not the file: an unchanged pin costs one
    JSON read and no network at all, and a moved pin always re-fetches.
    """

    if not desktop_shell_enabled():
        raise DesktopShellError(
            f"{DESKTOP_SHELL_ENABLED_ENV}=off, so the desktop app is not used."
        )
    if is_desktop_shell_installed():
        return desktop_shell_path()
    if not download:
        raise DesktopShellError(
            f"The desktop app for {DESKTOP_SHELL_RELEASE_TAG} is not installed."
        )
    return fetch_desktop_shell(timeout=timeout)


def desktop_shell_report() -> dict[str, object]:
    """Return what ``--print-status`` says about the shell. Reads only."""

    path = desktop_shell_path()
    ready = is_desktop_shell_installed()
    return {
        "shell_binary": str(path) if ready else None,
        "shell_release_tag": DESKTOP_SHELL_RELEASE_TAG,
        "shell_ready": ready,
    }
