"""A settings save can never wipe the settings file (7.69.7).

The reported hazard (INVESTIGATION-PAUSE-UNDER-LOAD.md, H1): on Windows a
second process replacing the managed ``.env`` made 18 of 351 reads come back
empty, and a save rebuilds the file from what it read -- so a one-key pause
save at that moment wrote the file back with that one key. These tests inject
the failure deterministically (a read or a replace refused N times) and prove:

* a busy read is retried on the documented schedule and then refuses the save
  with ``SettingsFileBusyError``, before anything is written;
* a refused save leaves ``.env`` byte-identical (sha256) and no staging file;
* the file as it was is kept beside it as ``.env.previous`` before every
  replace -- only from a successful, non-empty read;
* a normal save writes exactly the bytes it wrote before this change, over a
  corpus of realistic files (comments, quoting, CRLF, BOM, unknown and
  duplicate keys, a 50 KB file of ~1,000 patterns);
* the display read still reads a busy file as empty (it writes nothing).

The real two-process version is ``test_settings_save_two_process.py``.
"""

import hashlib
import os
import sys
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pytest
from loguru import logger

from my_claude_code.config.admin import env_io, persistence, sources
from my_claude_code.config.admin.env_io import (
    RETRY_DELAYS_SECONDS,
    SettingsFileBusyError,
)

FABLE_A = "nvidia_nim/vendor/fable-a"
FABLE_B = "nvidia_nim/vendor/fable-b"
FABLE_C = "nvidia_nim/vendor/fable-c"

#: Keys these tests read or write. Cleared from the process environment so a
#: developer shell (or the isolation shell, which sets PORT) cannot lock them.
KEYS_USED = (
    "MODEL_FABLE",
    "MODEL_FABLE_FALLBACKS",
    "MODEL_FABLE_PAUSED",
    "MODEL_SONNET",
    "MODEL_SONNET_FALLBACKS",
    "MODEL_SONNET_PAUSED",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "LOG_LEVEL",
    "HTTP_READ_TIMEOUT",
    "MODEL_VISIBILITY_DENY",
    "MODEL_VISIBILITY_ALLOW",
    "MCC_SMOKE_TARGETS",
    "MCC_ENV_FILE",
    "PORT",
    "HOST",
)

#: A managed file shaped like a real one: choices on two rails with pauses,
#: two provider keys (fake values of the real prefix shape), a visibility list
#: and an entry no admin field owns, which a save must carry over verbatim.
SEED = (
    "# Managed by My Claude Code /admin.\n"
    "# Edit in the server UI when possible.\n"
    "\n"
    "DEEPSEEK_API_KEY=sk-test-fake-0001\n"
    "GROQ_API_KEY=gsk_test_fake_0002\n"
    f"MODEL_FABLE={FABLE_A}\n"
    f"MODEL_FABLE_FALLBACKS={FABLE_B},{FABLE_C}\n"
    f"MODEL_FABLE_PAUSED={FABLE_B}\n"
    f"MODEL_SONNET={FABLE_C}\n"
    f"MODEL_SONNET_FALLBACKS={FABLE_A}\n"
    f"MODEL_SONNET_PAUSED={FABLE_A}\n"
    "LOG_LEVEL=INFO\n"
    "MODEL_VISIBILITY_DENY=*:free,*-preview\n"
    "MCC_SMOKE_TARGETS=fable,opus\n"
)

#: Every key the seed sets that a pause save on Fable must carry over.
SENTINELS = (
    "DEEPSEEK_API_KEY=sk-test-fake-0001",
    "GROQ_API_KEY=gsk_test_fake_0002",
    f"MODEL_FABLE={FABLE_A}",
    f"MODEL_FABLE_FALLBACKS={FABLE_B},{FABLE_C}",
    f"MODEL_SONNET={FABLE_C}",
    f"MODEL_SONNET_PAUSED={FABLE_A}",
    "LOG_LEVEL=INFO",
    "MCC_SMOKE_TARGETS=fable,opus",
)

PAUSE_C = {"MODEL_FABLE_PAUSED": f"{FABLE_B},{FABLE_C}"}

_REAL_READ = env_io._attempt_read
_REAL_REPLACE = env_io._attempt_replace


def _sha(path: Path) -> str:
    # ``open`` rather than ``read_bytes``: a test below makes ``read_bytes``
    # refuse the file to stand for the display read.
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / ".mcc"
    config.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config))
    # The repo layer is ``./.env``; there is none in an empty directory.
    monkeypatch.chdir(tmp_path)
    for key in KEYS_USED:
        monkeypatch.delenv(key, raising=False)
    sources.clear_env_parse_cache()
    return config


@pytest.fixture
def managed(config_dir: Path) -> Path:
    path = config_dir / ".env"
    path.write_bytes(SEED.encode("utf-8"))
    return path


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every wait instead of sleeping it."""

    recorded: list[float] = []
    monkeypatch.setattr(env_io, "_sleep", recorded.append)
    return recorded


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _failing_reads(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    failures: int,
    error: Callable[[], OSError] = lambda: PermissionError(13, "Permission denied"),
) -> list[int]:
    """Refuse the first ``failures`` reads of ``target``; count every attempt."""

    attempts = [0]

    def read(path: Path) -> tuple[bytes, int]:
        if _same(path, target):
            attempts[0] += 1
            if attempts[0] <= failures:
                raise error()
        return _REAL_READ(path)

    monkeypatch.setattr(env_io, "_attempt_read", read)
    return attempts


def _failing_replaces(
    monkeypatch: pytest.MonkeyPatch, target: Path, failures: int
) -> list[int]:
    """Refuse the first ``failures`` replaces onto ``target`` like Windows does."""

    attempts = [0]

    def replace(source: Path, destination: Path) -> None:
        if _same(destination, target):
            attempts[0] += 1
            if attempts[0] <= failures:
                raise PermissionError(13, "Access is denied", str(destination))
        _REAL_REPLACE(source, destination)

    monkeypatch.setattr(env_io, "_attempt_replace", replace)
    return attempts


def _save(updates: dict[str, str]) -> dict[str, object]:
    prepared = persistence.prepare_admin_update(updates)
    assert prepared.valid, prepared.errors
    return persistence.commit_prepared_admin_update(prepared)


def _staging_files(config_dir: Path) -> list[str]:
    return sorted(p.name for p in config_dir.iterdir() if p.name.endswith(".tmp"))


# --------------------------------------------------------------- the reader


def test_the_schedule_is_seven_attempts_over_315_ms() -> None:
    """The numbers the docstring derives from the measurement, pinned."""

    assert len(RETRY_DELAYS_SECONDS) + 1 == 7
    assert RETRY_DELAYS_SECONDS[0] == pytest.approx(0.005)
    assert sum(RETRY_DELAYS_SECONDS) == pytest.approx(0.315)
    assert all(
        later == pytest.approx(2 * earlier)
        for earlier, later in pairwise(RETRY_DELAYS_SECONDS)
    )


def test_a_busy_read_is_retried_and_then_reads_the_file(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _failing_reads(monkeypatch, managed, failures=3)

    raw = env_io.read_settings_bytes(managed)

    assert raw == SEED.encode("utf-8")
    assert attempts[0] == 4
    assert waits == list(RETRY_DELAYS_SECONDS[:3])


def test_a_read_busy_on_every_attempt_refuses_after_315_ms(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _failing_reads(monkeypatch, managed, failures=99)

    with pytest.raises(SettingsFileBusyError) as refused:
        env_io.read_settings_bytes(managed)

    assert attempts[0] == 7
    assert waits == list(RETRY_DELAYS_SECONDS)
    message = str(refused.value)
    assert message.startswith("Not saved: the settings file was busy")
    assert "nothing was changed. Try again." in message
    assert refused.value.path == managed


def test_any_other_read_error_is_refused_too_never_read_as_empty(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_reads(
        monkeypatch, managed, failures=99, error=lambda: OSError(5, "I/O error")
    )

    with pytest.raises(SettingsFileBusyError, match="could not be read"):
        env_io.read_settings_bytes(managed)


def test_a_file_that_does_not_exist_yet_reads_as_a_fresh_install(
    config_dir: Path, waits: list[float]
) -> None:
    assert env_io.read_settings_bytes(config_dir / ".env") is None
    assert waits == []


def test_a_directory_named_like_the_file_is_no_file_and_never_refuses(
    managed: Path, waits: list[float], tmp_path: Path
) -> None:
    """A virtualenv called ``.env`` where the server runs is not a settings file.

    The repo layer is ``./.env``. The lenient read skipped a directory there
    (``is_file``); the strict one must too, or every save would be refused.
    """

    (tmp_path / ".env").mkdir()
    (tmp_path / ".env" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")

    assert env_io.read_settings_bytes(tmp_path / ".env") is None
    _save(PAUSE_C)

    assert waits == []
    with open(managed, encoding="utf-8") as handle:
        assert f"MODEL_FABLE_PAUSED={FABLE_B},{FABLE_C}" in handle.read()


def test_a_file_seen_with_settings_that_goes_missing_is_refused_once(
    config_dir: Path, waits: list[float]
) -> None:
    """Missing in the middle of someone else's delete-and-rename is not empty.

    Refused once; if it was deleted on purpose the next save starts a new one.
    """

    path = config_dir / ".env"
    env_io.note_settings_read(path, b"LOG_LEVEL=INFO\n")

    with pytest.raises(SettingsFileBusyError, match="missing"):
        env_io.read_settings_bytes(path)
    assert waits == list(RETRY_DELAYS_SECONDS)
    assert env_io.read_settings_bytes(path) is None


def test_a_file_seen_with_settings_that_reads_empty_is_refused_once(
    managed: Path, waits: list[float]
) -> None:
    assert env_io.read_settings_bytes(managed) is not None
    managed.write_bytes(b"")

    with pytest.raises(SettingsFileBusyError, match="emptied"):
        env_io.read_settings_bytes(managed)
    assert env_io.read_settings_bytes(managed) == b""


def test_a_read_that_changes_size_underneath_is_retried(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [0]

    def torn(path: Path) -> tuple[bytes, int]:
        raw, size = _REAL_READ(path)
        calls[0] += 1
        return (raw[:10], size) if calls[0] <= 2 else (raw, size)

    monkeypatch.setattr(env_io, "_attempt_read", torn)

    assert env_io.read_settings_bytes(managed) == SEED.encode("utf-8")
    assert waits == list(RETRY_DELAYS_SECONDS[:2])


def test_the_display_read_still_reads_a_busy_file_as_empty(
    managed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only paths are not made to fail hard (the dashboard must load)."""

    real = Path.read_bytes

    def busy(self: Path) -> bytes:
        if _same(self, managed):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", busy)

    assert sources.dotenv_values_from_file(managed) == {}


# --------------------------------------------------------- the reported wipe


def test_the_reported_one_key_wipe_cannot_happen(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1, deterministic: the file unreadable while a pause is saved."""

    before = _sha(managed)
    _failing_reads(monkeypatch, managed, failures=99)
    real_read_bytes = Path.read_bytes

    def busy(self: Path) -> bytes:
        if _same(self, managed):
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", busy)

    # What the lenient read builds -- what every save built before 7.69.7:
    # the one key, and nothing else of the eleven the file holds.
    lenient = persistence.prepare_admin_update(PAUSE_C, strict=False)
    assert sorted(lenient.target_values) == ["MODEL_FABLE_PAUSED"]

    with pytest.raises(SettingsFileBusyError):
        persistence.prepare_admin_update(PAUSE_C)

    assert _sha(managed) == before
    assert not (managed.parent / ".env.previous").exists()
    assert _staging_files(managed.parent) == []


@pytest.mark.parametrize("failing_read", range(1, 9))
def test_one_failed_read_anywhere_in_a_save_never_loses_a_setting(
    managed: Path,
    waits: list[float],
    monkeypatch: pytest.MonkeyPatch,
    failing_read: int,
) -> None:
    """A moment of contention that hits exactly one read of the file.

    Whichever read of the save it lands on -- the value state, the managed
    values, the validation pass, the restart check, the commit -- that read is
    retried and the save writes every setting. Counted across both readers,
    so a read that went back to the lenient path would read empty here and
    the save would drop the file's settings.
    """

    count = [0]
    real_read_bytes = Path.read_bytes

    def tick(path: Path) -> None:
        if _same(path, managed):
            count[0] += 1
            if count[0] == failing_read:
                raise PermissionError(13, "Permission denied", str(path))

    def read(path: Path) -> tuple[bytes, int]:
        tick(path)
        return _REAL_READ(path)

    def read_bytes(self: Path) -> bytes:
        tick(self)
        return real_read_bytes(self)

    monkeypatch.setattr(env_io, "_attempt_read", read)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    _save(PAUSE_C)

    with open(managed, encoding="utf-8") as handle:
        text = handle.read()
    for line in SENTINELS:
        assert line in text
    assert f"MODEL_FABLE_PAUSED={FABLE_B},{FABLE_C}" in text


def test_a_commit_whose_own_read_fails_writes_nothing(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _sha(managed)
    prepared = persistence.prepare_admin_update(PAUSE_C)
    assert prepared.valid
    _failing_reads(monkeypatch, managed, failures=99)

    with pytest.raises(SettingsFileBusyError):
        persistence.commit_prepared_admin_update(prepared)

    assert _sha(managed) == before
    assert not (managed.parent / ".env.previous").exists()
    assert _staging_files(managed.parent) == []


def test_a_replace_refused_on_every_attempt_leaves_the_file_byte_identical(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _sha(managed)
    prepared = persistence.prepare_admin_update(PAUSE_C)
    attempts = _failing_replaces(monkeypatch, managed, failures=99)

    with pytest.raises(SettingsFileBusyError, match="was busy"):
        persistence.commit_prepared_admin_update(prepared)

    assert attempts[0] == 7
    assert _sha(managed) == before
    assert _staging_files(managed.parent) == []
    # The copy was kept before the replace was tried; it is the file as it is.
    assert (managed.parent / ".env.previous").read_bytes() == managed.read_bytes()


def test_a_replace_refused_a_few_times_then_lands(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_replaces(monkeypatch, managed, failures=3)

    _save(PAUSE_C)

    text = managed.read_text(encoding="utf-8")
    assert f"MODEL_FABLE_PAUSED={FABLE_B},{FABLE_C}" in text
    for line in SENTINELS:
        assert line in text
    assert waits == list(RETRY_DELAYS_SECONDS[:3])
    assert _staging_files(managed.parent) == []


def test_staging_cleanup_on_the_failure_path_never_raises(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undeletable staging file must not turn the refusal into a 500."""

    prepared = persistence.prepare_admin_update(PAUSE_C)
    _failing_replaces(monkeypatch, managed, failures=99)
    real_unlink = Path.unlink

    def stuck(self: Path, missing_ok: bool = False) -> None:
        if self.name == ".env.tmp":
            raise PermissionError(13, "Access is denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)

    with pytest.raises(SettingsFileBusyError):
        persistence.commit_prepared_admin_update(prepared)


# ------------------------------------------------------- the previous copy


def test_every_save_keeps_the_file_as_it_was_beside_it(
    managed: Path, waits: list[float]
) -> None:
    previous = managed.parent / ".env.previous"

    _save(PAUSE_C)
    assert previous.read_bytes() == SEED.encode("utf-8")

    after_first = managed.read_bytes()
    _save({"LOG_LEVEL": "DEBUG"})
    assert previous.read_bytes() == after_first
    assert _staging_files(managed.parent) == []


def test_a_fresh_install_has_no_previous_copy(
    config_dir: Path, waits: list[float]
) -> None:
    _save({"LOG_LEVEL": "DEBUG"})

    assert (config_dir / ".env").is_file()
    assert not (config_dir / ".env.previous").exists()


def test_an_empty_file_never_replaces_the_previous_copy(
    config_dir: Path, waits: list[float]
) -> None:
    previous = config_dir / ".env.previous"
    previous.write_bytes(SEED.encode("utf-8"))
    (config_dir / ".env").write_bytes(b"   \n")

    _save({"LOG_LEVEL": "DEBUG"})

    assert previous.read_bytes() == SEED.encode("utf-8")


def test_a_previous_copy_that_cannot_be_kept_refuses_the_save(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _sha(managed)
    prepared = persistence.prepare_admin_update(PAUSE_C)
    _failing_replaces(monkeypatch, managed.parent / ".env.previous", failures=99)

    with pytest.raises(SettingsFileBusyError):
        persistence.commit_prepared_admin_update(prepared)

    assert _sha(managed) == before
    assert _staging_files(managed.parent) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_previous_copy_is_no_wider_than_the_settings_file(
    managed: Path, waits: list[float]
) -> None:
    managed.chmod(0o600)

    _save(PAUSE_C)

    assert (managed.parent / ".env.previous").stat().st_mode & 0o077 == 0


def test_the_previous_copy_never_reaches_a_response_or_the_log(
    managed: Path, waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}", level="DEBUG")
    try:
        response = _save(PAUSE_C)
        prepared = persistence.prepare_admin_update({"LOG_LEVEL": "DEBUG"})
        _failing_replaces(monkeypatch, managed, failures=99)
        with pytest.raises(SettingsFileBusyError) as refused:
            persistence.commit_prepared_admin_update(prepared)
    finally:
        logger.remove(sink)

    assert ".env.previous" not in repr(response)
    for text in (repr(response), str(refused.value), *lines):
        assert "sk-test-fake-0001" not in text
        assert "gsk_test_fake_0002" not in text
    assert any("SETTINGS SAVE REFUSED" in line for line in lines)


# ------------------------------------------------- C4: the same bytes as before


def _visibility(count: int) -> str:
    """``count`` hide patterns of a realistic length: 1,000 of them is ~50 KB."""

    return ",".join(
        f"vendor-{index % 37}/model-family-{index:04d}-instruct-preview:free"
        for index in range(count)
    )


def _corpus() -> dict[str, bytes]:
    """Realistic managed files, as bytes, every shape a hand edit produces."""

    base_lines = SEED.splitlines()
    commented = (
        "# my own notes\n\n# keys\n"
        + SEED
        + "\n\n# trailing comment\n# MODEL_OPUS=commented/out\n"
    )
    quoting = SEED + (
        'MCC_SMOKE_NOTE="two words # not a comment"\n'
        "MCC_SMOKE_SINGLE='single $quoted'\n"
        'MCC_SMOKE_ESCAPED="a \\"quoted\\" word = and $dollar"\n'
        "export MCC_SMOKE_EXPORTED=yes\n"
        "HTTP_READ_TIMEOUT = 120\n"
    )
    unknown = SEED + (
        "SOME_OTHER_TOOL_KEY=value-the-manifest-does-not-know\n"
        "NIM=legacy-alias-value\n"
        "lowercase_key=x\n"
    )
    duplicates = SEED + "LOG_LEVEL=WARNING\nMCC_SMOKE_TARGETS=second\n"
    big = SEED + f"MODEL_VISIBILITY_ALLOW={_visibility(1000)}\n"
    return {
        "lf": SEED.encode("utf-8"),
        "crlf": "\r\n".join(base_lines).encode("utf-8") + b"\r\n",
        "bom": b"\xef\xbb\xbf" + SEED.encode("utf-8"),
        "comments-and-blank-lines": commented.encode("utf-8"),
        "quoting": quoting.encode("utf-8"),
        "unknown-keys": unknown.encode("utf-8"),
        "duplicate-keys": duplicates.encode("utf-8"),
        "50kb-1000-patterns": big.encode("utf-8"),
        "crlf-50kb": big.replace("\n", "\r\n").encode("utf-8"),
    }


def _reference_bytes(prepared: persistence.PreparedAdminUpdate, scratch: Path) -> bytes:
    """What a save wrote before 7.69.7, composed from the same public parts.

    The old commit was: render the target values with the unmanaged entries
    read back from the file, then ``write_text(..., encoding="utf-8")``.
    """

    plain, _masked = persistence.render_env_pair(
        prepared.target_values,
        preserved=persistence.unmanaged_env_values(prepared.path),
    )
    scratch.write_text(plain, encoding="utf-8")
    return scratch.read_bytes()


@pytest.mark.parametrize("case", sorted(_corpus()))
@pytest.mark.parametrize(
    "update",
    [PAUSE_C, {"LOG_LEVEL": "DEBUG"}],
    ids=["pause", "ordinary-setting"],
)
def test_a_normal_save_writes_exactly_the_bytes_it_always_did(
    config_dir: Path, waits: list[float], case: str, update: dict[str, str]
) -> None:
    original = _corpus()[case]
    managed = config_dir / ".env"
    managed.write_bytes(original)

    prepared = persistence.prepare_admin_update(update)
    assert prepared.valid, prepared.errors
    expected = _reference_bytes(prepared, config_dir.parent / f"expected-{case}")

    persistence.commit_prepared_admin_update(prepared)

    assert managed.read_bytes() == expected
    # And the previous copy is the original, byte for byte, BOM and CRLF too.
    assert (config_dir / ".env.previous").read_bytes() == original
    assert waits == []
