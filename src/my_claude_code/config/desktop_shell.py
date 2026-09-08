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

Nothing here is on the server's *cold start* path: building the ASGI app must
not import this module, and a contract test pins that. From 6.61.0 there is one
more caller than ``mcc-desktop`` -- :func:`auto_update_desktop_shells`, run on a
thread after the server is ready and importing this module lazily at that point
-- so a stale desktop app is brought up to the pin without anybody running a
command. It is the same code path (:func:`stage_desktop_shell`), one more
caller, and it is still nowhere near the bind.
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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: The release whose shell assets this build wants. Bumping it is a step in
#: ``docs/RELEASE-CHECKLIST.md``: cut the release, wait for
#: ``shell-release.yml`` to attach the eight assets, then move this tag and the
#: four digests below together, in one commit -- and vendor that release's own
#: ``SHA256SUMS-desktop-shell.txt`` under ``tests/fixtures/desktop_shell/`` so
#: ``test_desktop_shell_pin.py`` can compare the two offline. A pin that drifts
#: from the published sums file is the one failure this table cannot survive,
#: and it is not something a reviewer can see by eye.
DESKTOP_SHELL_RELEASE_TAG = "v6.61.0"

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

#: What a *staged* replacement is called, beside the file it will become.
#:
#: A running executable may not be written over -- that is the whole of BUG-0's
#: second half -- so ``--ensure-shell`` puts the verified new binary next to the
#: old one under this suffix and stops there. The window that starts next finds
#: it and does the rename itself (``swap_staged_binary`` in the shell's
#: ``lib.rs``), which is the one moment nothing is holding the old file open.
DESKTOP_SHELL_STAGED_SUFFIX = ".new"

#: Overrides the install directory. Exists so a smoke run -- or a test -- can
#: exercise the whole fetch without writing into the developer's ``~/.local/bin``,
#: which on a real machine holds the live ``mcc-server`` shim.
DESKTOP_SHELL_DIR_ENV = "MCC_DESKTOP_SHELL_DIR"

#: Overrides where the release assets are fetched from. The same family of
#: override as :data:`DESKTOP_SHELL_DIR_ENV`, and it exists for the same
#: reason: proving the staging path end to end means serving a release feed,
#: and pointing that proof at the real GitHub would make the proof depend on
#: the internet and would put load on a release page for a test. The digest
#: checks are *not* relaxed when it is set -- a loopback feed has to publish a
#: ``SHA256SUMS-desktop-shell.txt`` that agrees with the pin below, exactly as
#: the real release does.
DESKTOP_SHELL_BASE_URL_ENV = "MCC_DESKTOP_SHELL_BASE_URL"

#: ``off`` disables the shell entirely: ``auto`` (the default) lets it lead the
#: window chain. Read from the environment rather than declared as a ``Settings``
#: field because it is a launch-time switch for a *separate process* from the
#: server, and the dashboard cannot change a decision already taken.
DESKTOP_SHELL_ENABLED_ENV = "DESKTOP_SHELL"

#: Seconds for one HTTP read. Two are made: the sums file and the archive.
DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS = 60.0

#: ``(sys.platform, normalized arch) -> (asset name, sha256 of the archive)``.
#:
#: **The Windows installer is deliberately absent from this table.** Since
#: 6.45.0 ``shell-release.yml`` also attaches
#: ``MyClaudeCode-Setup-windows-x86_64.exe``, and it appears in the checksum
#: file this module parses -- but it is delivery *path B*, the thing a human
#: downloads from the release page. Path A is this module, and it wants the
#: archive: a ``setup.exe`` would have to be *run*, per-user, with a Start Menu
#: shortcut and an Apps & Features entry as side effects, to place a file this
#: code already knows how to place itself. Two installers writing the same
#: binary is how you get two of them. The line is simply ignored; nothing here
#: needs to change when it moves.
#:
#: The four targets ``shell-release.yml`` builds. ``linux/aarch64`` is
#: deliberately absent: no runner builds it (see the workflow's matrix), so
#: claiming it here would mean a 404 on a machine that has a working fallback.
_RELEASES: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "x86_64"): (
        "MyClaudeCode-linux-x86_64.tar.gz",
        "68a1a78403d7d3a7ed44fc3ecd079d9d29fcf3fe7b98f3987af2f5966dd428ff",
    ),
    ("darwin", "x86_64"): (
        "MyClaudeCode-macos-x86_64.tar.gz",
        "a669fa648ca3aab21bf5729efe4d0a677086d9527df6f8283d3172abbe96b0f4",
    ),
    ("darwin", "aarch64"): (
        "MyClaudeCode-macos-aarch64.tar.gz",
        "e64d696fed17d76fed90d09d23fce5fd110402ea9986a779ec96b1472dc5f424",
    ),
    ("win32", "x86_64"): (
        "MyClaudeCode-windows-x86_64.zip",
        "b9eea0a910614fc28b173179d1a68376c01575cf40566a12ba843e4ac21b52e5",
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


def receipt_path_for(binary: Path) -> Path:
    """Return the receipt that describes ``binary``, beside it.

    Takes the binary rather than a directory because there is more than one
    place a shell can live now: ``~/.local/bin`` (delivery path A), and
    ``%LOCALAPPDATA%/Programs/My Claude Code`` or ``/usr/bin`` when the
    native installer put it there (path B). ``--ensure-shell`` updates
    *whichever one is running*, so every path here is derived from that file.
    """

    return binary.with_name(DESKTOP_SHELL_RECEIPT_FILENAME)


def staged_binary_path(binary: Path) -> Path:
    """Return where a verified replacement for ``binary`` is staged."""

    return binary.with_name(f"{binary.name}{DESKTOP_SHELL_STAGED_SUFFIX}")


def staged_receipt_path(binary: Path) -> Path:
    """Return where the staged binary's receipt waits for the swap."""

    return binary.with_name(
        f"{DESKTOP_SHELL_RECEIPT_FILENAME}{DESKTOP_SHELL_STAGED_SUFFIX}"
    )


def desktop_shell_base_url() -> str:
    """Return the base URL the release assets are fetched from.

    :data:`DESKTOP_SHELL_BASE_URL_ENV` wins when it is set, so a proof can
    serve the feed from loopback. Nothing else about the fetch changes: the
    published checksum file is still downloaded, still parsed, and still has to
    agree with the digest pinned in this module.
    """

    override = os.environ.get(DESKTOP_SHELL_BASE_URL_ENV, "").strip()
    if override:
        return override.rstrip("/")
    return DESKTOP_SHELL_RELEASE_BASE_URL


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


def read_receipt_at(path: Path) -> dict[str, str] | None:
    """Return the receipt at ``path``, or ``None`` when there is not a valid one."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        name: value
        for name, value in data.items()
        if isinstance(name, str) and isinstance(value, str)
    }


def read_receipt() -> dict[str, str] | None:
    """Return the receipt beside the default install, or ``None``."""

    return read_receipt_at(desktop_shell_receipt_path())


def installed_release_tag() -> str | None:
    """Return the tag the installed binary came from, or ``None``."""

    receipt = read_receipt()
    return None if receipt is None else receipt.get("tag")


def installed_release_tag_at(binary: Path) -> str | None:
    """Return the tag ``binary`` came from, per the receipt beside it."""

    receipt = read_receipt_at(receipt_path_for(binary))
    return None if receipt is None else receipt.get("tag")


def binary_matches_pin(binary: Path) -> bool:
    """Return whether ``binary`` is exactly what the pin asks for.

    The same two-part test :func:`is_desktop_shell_installed` makes -- the file
    is there, and the receipt beside it names this build's tag and digest --
    asked of one named path rather than of the default one.
    """

    if not binary.is_file():
        return False
    receipt = read_receipt_at(receipt_path_for(binary))
    if receipt is None or receipt.get("tag") != DESKTOP_SHELL_RELEASE_TAG:
        return False
    expected = release_for(sys.platform, platform.machine())
    return expected is not None and receipt.get("sha256") == expected[1]


def is_desktop_shell_installed() -> bool:
    """Return whether the binary on disk is the one the pin asks for.

    Both halves matter. A binary with no receipt is something we did not put
    there and cannot vouch for; a receipt naming another tag is the signal that
    the pin moved and the next launch must fetch again.
    """

    return binary_matches_pin(desktop_shell_path())


def _write_receipt_at(path: Path, asset: str, digest: str) -> None:
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


def _write_receipt(asset: str, digest: str) -> None:
    _write_receipt_at(desktop_shell_receipt_path(), asset, digest)


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

    sums_url = f"{desktop_shell_base_url()}/{DESKTOP_SHELL_SUMS_ASSET}"
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


def _place_verified_binary(
    destination: Path, *, timeout: float, install: bool
) -> tuple[str, str]:
    """Download the pinned archive and put the executable at ``destination``.

    The one download-verify-extract implementation in this module. Both callers
    reach it: :func:`fetch_desktop_shell`, which installs over whatever is
    there because the caller is about to *launch* the result, and
    :func:`stage_desktop_shell`, which does not, because the file it would be
    writing over is the window asking for the update.

    ``install`` is that difference and nothing else. ``True`` replaces
    ``destination`` (renaming a locked copy aside first); ``False`` leaves the
    extracted file exactly where it was written, which the caller has already
    named as a staging path.

    Returns ``(asset, digest)`` so the caller can write the receipt that goes
    with what it just placed.
    """

    asset, pinned_digest = release_for_current_platform()
    directory = destination.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DesktopShellError(
            f"Could not create the desktop app directory {directory}: {exc}"
        ) from exc

    _confirm_published_digest(asset, pinned_digest, timeout)

    payload = _download(f"{desktop_shell_base_url()}/{asset}", timeout)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != pinned_digest:
        raise DesktopShellError(
            f"Checksum verification failed for {asset}: expected "
            f"{pinned_digest}, got {digest}."
        )

    _sweep_renamed_aside(directory)
    written = (
        destination.with_name(f".{destination.name}.tmp") if install else destination
    )
    with tempfile.TemporaryDirectory(prefix="mcc-desktop-shell-") as scratch:
        archive_path = Path(scratch) / asset
        try:
            archive_path.write_bytes(payload)
            _extract_binary(archive_path, asset, written)
            written.chmod(
                written.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            if install:
                _install_atomically(written, destination)
        except DesktopShellError:
            if install:
                with suppress(OSError):
                    written.unlink(missing_ok=True)
            raise
        except OSError as exc:
            with suppress(OSError):
                written.unlink(missing_ok=True)
            raise DesktopShellError(
                f"Could not install the desktop app at {destination}: {exc}"
            ) from exc

    return asset, pinned_digest


def fetch_desktop_shell(
    *, timeout: float = DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS
) -> Path:
    """Download, verify and install the pinned shell. Returns its path."""

    destination = desktop_shell_dir() / desktop_shell_binary_name()
    asset, pinned_digest = _place_verified_binary(
        destination, timeout=timeout, install=True
    )
    _write_receipt_at(receipt_path_for(destination), asset, pinned_digest)
    return destination


def stage_desktop_shell(
    target: Path | None = None,
    *,
    timeout: float = DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Bring one shell binary up to the pin without ever writing over it.

    This is what ``mcc-desktop --ensure-shell`` runs, and it is the answer to
    BUG-0: until 6.60.0 the pin was enforced by ``ShellWindow.create()`` alone,
    so a user who launched ``MyClaudeCode.exe`` from the Start Menu -- or from
    the Programs-folder install the native installer makes -- kept whichever
    shell they first received, forever. One user ran a 15-release-old window
    for that reason.

    ``target`` is the binary to bring up to date, which the *window* names
    (its own ``current_exe()``): the file that has to change is the one that is
    running, not necessarily the one in ``~/.local/bin``. ``None`` means the
    default install (the tray's copy).

    **A running window is never written over, and the operating system is what
    decides whether one is running.** The verified executable is written beside
    the target and then replaced onto it with ``os.replace``: Windows refuses
    that for a file that is a *running image*, and POSIX makes it safe by
    construction (a running process keeps the inode it started from). So a
    target nothing is running is installed outright, a target something is
    running stays exactly where it is, and the file is left as
    ``target.new`` for the next start of the window to rename in -- the one
    moment nothing holds it open.

    That distinction is not a nicety. Staging unconditionally would make the
    *transition* into this mechanism impossible: the binary a user upgrading
    from before 6.60.0 is running has no swap step in it, so a ``.new`` left
    beside it would sit there forever and the pin still would not reach the
    machine. Nothing may replace a running window; everything else should just
    be updated.

    Returns the document ``--ensure-shell`` prints:
    ``{updated, from_tag, to_tag, staged_path, restart_required}``.
    """

    if not desktop_shell_enabled():
        raise DesktopShellError(
            f"{DESKTOP_SHELL_ENABLED_ENV}=off, so the desktop app is not used."
        )

    binary = (target or desktop_shell_path()).expanduser()
    to_tag = DESKTOP_SHELL_RELEASE_TAG
    from_tag = installed_release_tag_at(binary)
    result: dict[str, object] = {
        "updated": False,
        "from_tag": from_tag,
        "to_tag": to_tag,
        "staged_path": None,
        "restart_required": False,
    }

    if binary_matches_pin(binary):
        # The pin already reached this machine. Costs one JSON read.
        return result

    exists = binary.is_file()
    if not exists:
        asset, digest = _place_verified_binary(binary, timeout=timeout, install=True)
        _write_receipt_at(receipt_path_for(binary), asset, digest)
        result["updated"] = True
        result["staged_path"] = str(binary)
        return result

    staged = staged_binary_path(binary)
    asset, digest = _place_verified_binary(staged, timeout=timeout, install=False)
    if _replace_if_not_running(staged, binary):
        _write_receipt_at(receipt_path_for(binary), asset, digest)
        result["updated"] = True
        result["staged_path"] = str(binary)
        return result

    _write_receipt_at(staged_receipt_path(binary), asset, digest)
    result["updated"] = True
    result["staged_path"] = str(staged)
    result["restart_required"] = True
    return result


def _replace_if_not_running(staged: Path, binary: Path) -> bool:
    """Move ``staged`` onto ``binary``, unless something is running ``binary``.

    Deliberately one ``os.replace`` and no probing. There is no portable way to
    ask "is anybody running this file", and every approximation of one -- a
    process list, a lock file, a pid written somewhere -- is a second answer
    that can disagree with the first. The operating system already knows:

    * **Windows** refuses to replace a file that is mapped as a running image,
      with a sharing violation. That refusal *is* the check, and it is the one
      that cannot be wrong.
    * **POSIX** allows it and it is safe: a running process holds the inode it
      started from, so replacing the directory entry affects only later starts.

    Note what is *not* done here: :func:`_install_atomically`'s rename-aside.
    That exists so the tray can install over a window it is about to launch;
    using it here would move a *running* window's executable out from under it,
    which is exactly what decision Q5 forbids. A refusal is the right answer,
    and the caller turns it into a staged update and a restart.
    """

    try:
        os.replace(staged, binary)
    except OSError:
        return False
    return True


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


def desktop_shell_install_locations() -> tuple[Path, ...]:
    """Every place a desktop app this wheel manages can be installed.

    Two delivery paths, and both are real on this user's machine:

    * **A** -- ``~/.local/bin`` (or :data:`DESKTOP_SHELL_DIR_ENV`), where this
      module puts the binary it downloads;
    * **B** -- the native installer's directory:
      ``%LOCALAPPDATA%/Programs/My Claude Code`` on Windows, ``/usr/bin`` and
      ``/usr/local/bin`` on Linux, ``/Applications`` on macOS.

    Only paths that *exist and carry a receipt we wrote* are returned. That is
    the whole guard against this touching somebody else's file: a receipt is
    proof this code installed the binary beside it, and without one there is
    nothing here to keep up to date.
    """

    candidates: list[Path] = [desktop_shell_path()]
    name = desktop_shell_binary_name()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            candidates.append(Path(local) / "Programs" / "My Claude Code" / name)
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications")
            / f"{DESKTOP_SHELL_BINARY_STEM}.app"
            / "Contents"
            / "MacOS"
            / name
        )
    else:
        candidates.append(Path("/usr/local/bin") / name)
        candidates.append(Path("/usr/bin") / name)

    seen: dict[Path, None] = {}
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        if not resolved.is_file():
            continue
        if read_receipt_at(receipt_path_for(resolved)) is None:
            continue
        seen[resolved] = None
    return tuple(seen)


@dataclass(frozen=True, slots=True)
class ShellAutoUpdate:
    """What one pass of :func:`auto_update_desktop_shells` did."""

    #: Binaries that were replaced outright, because nothing was running them.
    updated: tuple[Path, ...] = ()
    #: Binaries a replacement is staged beside, waiting for the next start.
    staged: tuple[Path, ...] = ()
    #: Why nothing was done, when nothing was.
    skipped: str | None = None
    #: The one sentence to log. Empty when there is nothing worth saying.
    message: str = ""


def auto_update_desktop_shells(
    *,
    enabled: bool = True,
    helper_is_installing: bool = False,
    timeout: float = DESKTOP_SHELL_DOWNLOAD_TIMEOUT_SECONDS,
) -> ShellAutoUpdate:
    """Bring every installed desktop app up to the pinned release.

    This is the answer to "shouldn't we want good behaviour by default?" --
    and to BUG-0's last mile. Until 6.61.0 the pin reached a machine only if
    somebody *ran* something: the tray's window factory, or the window itself
    noticing it was stale. A user who launches ``MyClaudeCode.exe`` from the
    Start Menu and never opens a terminal was left on whatever build they first
    received, which is how one of them ran a fifteen-release-old window while
    their wheel moved on. Updating the server now updates the app too, with
    nothing to run.

    It is deliberately **the same code path** as ``mcc-desktop --ensure-shell``
    (:func:`stage_desktop_shell`), one more caller and not a second mechanism:
    a binary nothing is running is replaced in place, a binary that *is*
    running gets a verified ``.new`` beside it that its own next start renames
    in. Nothing is ever written over a running image.

    Guards, all of them the caller's to supply except the last:

    * ``enabled`` -- ``DESKTOP_SHELL_AUTO_UPDATE``, default on;
    * ``helper_is_installing`` -- never while an update helper is mid-install,
      because that is exactly when the shims and the tool directory are being
      rewritten;
    * ``DESKTOP_SHELL=off`` -- honoured here, not only by the caller;
    * and it does nothing at all when no receipt names a stale binary, which is
      the overwhelmingly common case and costs one JSON read per location.

    Never raises. A machine that is offline, behind a proxy or out of disk gets
    one line and the same attempt on the next server start -- there is no loop
    and no retry, because the next start is the retry.
    """

    if not enabled:
        return ShellAutoUpdate(skipped="disabled")
    if not desktop_shell_enabled():
        return ShellAutoUpdate(skipped=f"{DESKTOP_SHELL_ENABLED_ENV}=off")
    if helper_is_installing:
        return ShellAutoUpdate(skipped="an update helper is installing")

    stale = [
        binary
        for binary in desktop_shell_install_locations()
        if installed_release_tag_at(binary) != DESKTOP_SHELL_RELEASE_TAG
    ]
    if not stale:
        return ShellAutoUpdate(skipped="already at the pin")

    updated: list[Path] = []
    staged: list[Path] = []
    for binary in stale:
        try:
            report = stage_desktop_shell(binary, timeout=timeout)
        except (DesktopShellError, OSError) as exc:
            return ShellAutoUpdate(
                updated=tuple(updated),
                staged=tuple(staged),
                skipped=str(exc),
                message=(
                    f"The desktop app could not be updated to "
                    f"{DESKTOP_SHELL_RELEASE_TAG}: {exc} It is tried again the "
                    f"next time the server starts."
                ),
            )
        if not report.get("updated"):
            continue
        if report.get("restart_required"):
            staged.append(binary)
        else:
            updated.append(binary)

    tag = DESKTOP_SHELL_RELEASE_TAG
    if staged:
        message = f"desktop app {tag} staged; it will be used at the next app start"
    elif updated:
        message = f"desktop app updated to {tag}"
    else:
        message = ""
    return ShellAutoUpdate(
        updated=tuple(updated), staged=tuple(staged), message=message
    )


def desktop_shell_update_report() -> dict[str, object]:
    """Return what is installed, what is pinned, and whether they disagree.

    Three keys, no network, no writes: the receipt beside the default install
    is read and compared with the pin. The dashboard's Update banner and the
    server's startup line both use this, so "the desktop app is stale" is
    decided once rather than by two readers of the same file.

    ``shell_installed_tag`` is ``None`` when nothing is installed, or when
    what is installed has no receipt we wrote. That is deliberately *not* an
    available update: the overwhelming majority of MCC installs have no desktop
    app at all, and a banner offering to update an app they never installed is
    noise. An update is available when a receipt exists and names another tag
    -- which is exactly the case BUG-0 left unattended for fifteen releases.
    """

    installed = installed_release_tag()
    return {
        "shell_installed_tag": installed,
        "shell_pinned_tag": DESKTOP_SHELL_RELEASE_TAG,
        "shell_update_available": (
            installed is not None and installed != DESKTOP_SHELL_RELEASE_TAG
        ),
    }


def desktop_shell_report() -> dict[str, object]:
    """Return what ``--print-status`` says about the shell. Reads only."""

    path = desktop_shell_path()
    ready = is_desktop_shell_installed()
    return {
        "shell_binary": str(path) if ready else None,
        "shell_release_tag": DESKTOP_SHELL_RELEASE_TAG,
        # What the receipt beside the installed binary says, which is the half
        # BUG-0 turned on: the shell compares this with its own compiled-in tag
        # and asks for a staged update when they disagree. ``null`` when there
        # is no receipt to read.
        "shell_installed_tag": installed_release_tag(),
        "shell_ready": ready,
    }
