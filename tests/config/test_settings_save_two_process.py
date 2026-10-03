"""The two-process lab, in the suite (7.69.7).

INVESTIGATION-PAUSE-UNDER-LOAD.md measured it on NTFS: with a second process
replacing the managed ``.env`` in a loop, 18 of 351 reads came back empty, and
with a second process reading it, 91 of 15,727 replaces failed. A save that
read "empty" rewrote the file without every other setting.

Here the same second process is real: ``tests/support/
settings_file_adversary_child.py`` holds the file with no sharing, replaces it
in a loop, or reads it in a loop, while this process runs the real save path
(``prepare_admin_update`` + ``commit_prepared_admin_update``, what every
dashboard save runs) for pause, resume and an ordinary setting. Every save
either writes every setting the file holds, or is refused with the file
byte-identical to before it.

Those three tests are **Windows-only**: on POSIX a second process blocks
neither a read nor a replace, so they would pass without exercising anything
-- Linux CI proves nothing for them. The mocked twin at the bottom runs the
same driver everywhere with the refusals injected, and
``test_settings_save_safety.py`` pins each step deterministically.
"""

import hashlib
import os
import random
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from my_claude_code.config.admin import env_io, persistence, sources
from my_claude_code.config.admin.env_io import SettingsFileBusyError

HELPER = (
    Path(__file__).resolve().parents[1] / "support" / "settings_file_adversary_child.py"
)

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "Windows sharing violations: on POSIX a second process blocks neither a "
        "read nor a replace, so this would prove nothing; the mocked twin below "
        "covers the same driver on every platform"
    ),
)

FABLE_A = "nvidia_nim/vendor/fable-a"
FABLE_B = "nvidia_nim/vendor/fable-b"

SEED = (
    "DEEPSEEK_API_KEY=sk-test-fake-0001\n"
    "GROQ_API_KEY=gsk_test_fake_0002\n"
    f"MODEL_FABLE={FABLE_A}\n"
    f"MODEL_FABLE_FALLBACKS={FABLE_B}\n"
    f"MODEL_SONNET={FABLE_B}\n"
    f"MODEL_SONNET_PAUSED={FABLE_B}\n"
    "MODEL_VISIBILITY_DENY=*:free,*-preview\n"
    "LOG_LEVEL=INFO\n"
    "MCC_SMOKE_TARGETS=fable,opus\n"
)

#: Lines every save must carry over (the saves below change only the Fable
#: pause list and LOG_LEVEL).
SENTINELS = (
    "DEEPSEEK_API_KEY=sk-test-fake-0001",
    "GROQ_API_KEY=gsk_test_fake_0002",
    f"MODEL_FABLE={FABLE_A}",
    f"MODEL_FABLE_FALLBACKS={FABLE_B}",
    f"MODEL_SONNET={FABLE_B}",
    f"MODEL_SONNET_PAUSED={FABLE_B}",
    "MODEL_VISIBILITY_DENY=*:free,*-preview",
    "MCC_SMOKE_TARGETS=fable,opus",
)

#: What the replacing adversary publishes: the seed plus a line no save can
#: render (a comment), so its bytes are told apart from anything a save wrote.
ADVERSARY_CONTENT = (SEED + "# written by the adversary\n").encode("utf-8")

#: Pause, resume, and an ordinary setting both ways.
UPDATES = (
    {"MODEL_FABLE_PAUSED": FABLE_B},
    {"MODEL_FABLE_PAUSED": ""},
    {"LOG_LEVEL": "DEBUG"},
    {"LOG_LEVEL": "INFO"},
)

SAVES = 200


@dataclass
class Tally:
    saved: int = 0
    refused: int = 0
    lossy: int = 0
    refused_identical: int = 0
    refused_adversary_wrote: int = 0
    refused_changed_by_us: int = 0


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / ".mcc"
    config.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config))
    monkeypatch.chdir(tmp_path)
    for key in (
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "MODEL_FABLE",
        "MODEL_FABLE_FALLBACKS",
        "MODEL_FABLE_PAUSED",
        "MODEL_SONNET",
        "MODEL_SONNET_PAUSED",
        "MODEL_VISIBILITY_DENY",
        "LOG_LEVEL",
        "MCC_SMOKE_TARGETS",
        "MCC_ENV_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    sources.clear_env_parse_cache()
    path = config / ".env"
    path.write_bytes(SEED.encode("utf-8"))
    return path


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every file text a commit rendered -- what it wrote, or tried to."""

    texts: list[str] = []
    real = persistence.render_env_pair

    def capture(
        values: Mapping[str, str], *, preserved: Mapping[str, str] | None = None
    ) -> tuple[str, str]:
        plain, masked = real(values, preserved=preserved)
        texts.append(plain)
        return plain, masked

    monkeypatch.setattr(persistence, "render_env_pair", capture)
    return texts


def _bytes_when_readable(path: Path) -> bytes:
    """The file's bytes, waiting out a sharing violation (test side only)."""

    deadline = time.monotonic() + 10.0
    while True:
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.001)


def _drive(managed: Path, rendered: list[str], saves: int) -> Tally:
    """Run the real save path ``saves`` times and classify every outcome."""

    tally = Tally()
    for index in range(saves):
        update = UPDATES[index % len(UPDATES)]
        before = _bytes_when_readable(managed)
        written_before = len(rendered)
        try:
            prepared = persistence.prepare_admin_update(update)
            assert prepared.valid, prepared.errors
            persistence.commit_prepared_admin_update(prepared)
        except SettingsFileBusyError:
            tally.refused += 1
            after = _bytes_when_readable(managed)
            if after == before:
                tally.refused_identical += 1
            elif after == ADVERSARY_CONTENT:
                tally.refused_adversary_wrote += 1
            else:
                tally.refused_changed_by_us += 1
            continue
        tally.saved += 1
        assert len(rendered) == written_before + 1
        if any(line not in rendered[-1] for line in SENTINELS):
            tally.lossy += 1
    return tally


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path} never appeared")
        time.sleep(0.02)


@pytest.fixture
def adversary(tmp_path: Path) -> Iterator[Callable[..., Path]]:
    """Start the helper in a mode; stop it (by its own stop file) afterwards."""

    children: list[tuple[subprocess.Popen[bytes], Path]] = []

    def start(mode: str, path: Path, *extra: Path) -> Path:
        ready = tmp_path / f"{mode}.ready"
        stop = tmp_path / f"{mode}.stop"
        child = subprocess.Popen(
            [
                sys.executable,
                str(HELPER),
                mode,
                str(path),
                *map(str, extra),
                str(ready),
                str(stop),
            ]
        )
        children.append((child, stop))
        _wait_for(ready)
        return stop

    yield start
    for child, stop in children:
        stop.write_text("stop\n", encoding="utf-8")
        child.wait(timeout=30)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ------------------------------------------------------- real, Windows only


@WINDOWS_ONLY
def test_a_save_while_another_process_holds_the_file_is_refused(
    managed: Path, rendered: list[str], adversary: Callable[..., Path]
) -> None:
    before = _sha(managed.read_bytes())
    stop = adversary("hold", managed)

    for update in (UPDATES[0], UPDATES[2]):
        started = time.monotonic()
        with pytest.raises(SettingsFileBusyError, match="was busy"):
            persistence.prepare_admin_update(update)
        # Seven attempts, 315 ms of waiting: refused promptly, not hung.
        assert time.monotonic() - started < 5.0

    stop.write_text("stop\n", encoding="utf-8")
    _bytes_when_readable(managed)
    time.sleep(0.2)
    assert _sha(managed.read_bytes()) == before
    assert sorted(p.name for p in managed.parent.iterdir()) == [".env"]

    # Released: the very same saves go through, and keep the copy.
    tally = _drive(managed, rendered, len(UPDATES))
    assert tally.saved == len(UPDATES) and tally.lossy == 0
    assert (managed.parent / ".env.previous").is_file()


@WINDOWS_ONLY
def test_saves_racing_a_process_that_replaces_the_file_never_lose_a_setting(
    managed: Path,
    rendered: list[str],
    adversary: Callable[..., Path],
    tmp_path: Path,
) -> None:
    content = tmp_path / "adversary-content"
    content.write_bytes(ADVERSARY_CONTENT)
    adversary("replace", managed, content)

    tally = _drive(managed, rendered, SAVES)

    print(f"replace race: {tally}")
    assert tally.lossy == 0
    assert tally.refused_changed_by_us == 0
    assert tally.saved + tally.refused == SAVES


@WINDOWS_ONLY
def test_saves_racing_a_process_that_reads_the_file_never_lose_a_setting(
    managed: Path, rendered: list[str], adversary: Callable[..., Path]
) -> None:
    adversary("read", managed)

    tally = _drive(managed, rendered, SAVES)

    print(f"read race: {tally}")
    assert tally.lossy == 0
    # Nothing else writes the file here, so a refusal must be byte-identical.
    assert tally.refused == tally.refused_identical
    assert tally.saved + tally.refused == SAVES


# ------------------------------------------------- the mocked twin, everywhere


def test_the_same_race_with_the_refusals_injected(
    managed: Path, rendered: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver above, with Windows' refusals injected on a fixed schedule.

    Streaks of 1-9 refused attempts on reads and replaces of the settings
    file: the short ones are retried through, the long ones (seven or more,
    the whole schedule) refuse the save. Every save is still either complete
    or refused with the file unchanged.
    """

    pick = random.Random(7697)
    streak = {"read": 0, "replace": 0}
    real_read = env_io._attempt_read
    real_replace = env_io._attempt_replace

    def refused_now(kind: str) -> bool:
        if streak[kind] == 0 and pick.random() < 0.04:
            streak[kind] = pick.randint(1, 9)
        if streak[kind]:
            streak[kind] -= 1
            return True
        return False

    def read(path: Path) -> tuple[bytes, int]:
        if path.name == ".env" and refused_now("read"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_read(path)

    def replace(source: Path, target: Path) -> None:
        if target.name == ".env" and refused_now("replace"):
            raise PermissionError(13, "Access is denied", str(target))
        real_replace(source, target)

    monkeypatch.setattr(env_io, "_attempt_read", read)
    monkeypatch.setattr(env_io, "_attempt_replace", replace)
    monkeypatch.setattr(env_io, "_sleep", lambda _seconds: None)

    tally = _drive(managed, rendered, SAVES)

    print(f"injected race: {tally}")
    assert tally.lossy == 0
    assert tally.refused == tally.refused_identical
    # The schedule must exercise both outcomes, or the test proves nothing.
    assert tally.saved > 0
    assert tally.refused > 0
    assert os.path.exists(managed.parent / ".env.previous")
