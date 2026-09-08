"""The two things that made building ``Settings`` cost seconds.

Neither is a micro-optimisation. Ten of the twenty-three seconds a cold start
took on the reporter's machine were spent here, before the first log line -- and
``mcc-desktop --print-status`` pays the same bill, which is why merely deciding
whether to start a server took the desktop app ten seconds on every launch.

Both are asserted behaviourally rather than by timing: a wall-clock assertion in
a test suite is a flake, and what actually needs pinning is the *work*, not the
milliseconds.
"""

from pathlib import Path

import pytest

from my_claude_code.config import env_files
from my_claude_code.config.env_files import (
    clear_env_file_cache,
    env_file_value,
)
from my_claude_code.config.settings import Settings


def _write_env(path: Path, keys: int) -> Path:
    lines = ["HOST=127.0.0.1", "PORT=8391"]
    lines += [f"MCC_SCRATCH_PAD_{index:03d}=" + "z" * 40 for index in range(keys)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_dotenv_source_stops_at_the_fields_the_model_declares() -> None:
    """``dotenv_filtering="only_existing"`` -- the whole ten seconds, one key.

    pydantic-settings' default is to fold every variable in the file into the
    model so that ``extra="allow"`` can see them, and working out which ones no
    field claims means walking every field for every variable: O(variables x
    fields), 99,376 annotation inspections on this user's configuration. The
    result was then discarded by ``extra="ignore"`` two lines later.
    """

    assert Settings.model_config.get("dotenv_filtering") == "only_existing"
    assert Settings.model_config.get("extra") == "ignore"


def test_a_dotenv_file_is_parsed_once_per_revision_of_it(tmp_path, monkeypatch) -> None:
    """``env_file_override`` is called for every credential key of every provider.

    Each call used to re-parse the whole file -- read, tokenise, then copy
    ``os.environ`` once per value for ``${VAR}`` interpolation. About 35ms a
    call on a 47 KB file, hundreds of calls on a 57-provider configuration:
    most of the thirteen seconds that showed up in the log as a slow run of
    "ProviderRateLimiter initialized" lines.
    """

    clear_env_file_cache()
    path = _write_env(tmp_path / ".env", keys=50)
    parses: list[Path] = []
    real = env_files.dotenv_values

    def counting(target, *args, **kwargs):
        parses.append(Path(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(env_files, "dotenv_values", counting)
    for _ in range(20):
        assert env_file_value(path, "HOST") == "127.0.0.1"

    assert len(parses) == 1


def test_a_rewritten_dotenv_file_is_re_read(tmp_path) -> None:
    """The dashboard rewrites this file while the server runs.

    A cache keyed on the path alone would answer with the value the user just
    changed away from, which is a far worse defect than the cost it saves.
    """

    clear_env_file_cache()
    path = tmp_path / ".env"
    path.write_text("PORT=8391\n", encoding="utf-8")
    assert env_file_value(path, "PORT") == "8391"

    # A different size as well as a different mtime: a test can write twice
    # inside one filesystem timestamp tick, and size is what catches that.
    path.write_text("PORT=9000\nEXTRA=1\n", encoding="utf-8")
    assert env_file_value(path, "PORT") == "9000"


@pytest.mark.parametrize("keys", [10, 400])
def test_settings_reads_the_declared_fields_whatever_the_file_size(
    tmp_path, monkeypatch, keys
) -> None:
    """The filtering must not drop a value the model does actually declare."""

    path = _write_env(tmp_path / ".env", keys=keys)
    monkeypatch.setattr(
        Settings,
        "model_config",
        {**Settings.model_config, "env_file": str(path)},
    )
    settings = Settings()
    assert settings.port == 8391
    assert settings.host == "127.0.0.1"


def test_the_models_dev_ladder_answers_a_repeated_question_from_a_memo(
    tmp_path, monkeypatch
) -> None:
    """5.7 seconds of a start, and the log storm around it.

    The ladder is pure with respect to the catalogue file, and the catalogue
    file already keys every index cache underneath it -- so a second identical
    question can only ever produce the same answer. Before 6.59.0 it was
    recomputed for every model of every provider, four price fields at a time,
    each cross-provider miss logging a paragraph.
    """

    from my_claude_code.providers.runtime import models_dev

    cache = tmp_path / "models-dev.json"
    cache.write_text("{}", encoding="utf-8")
    models_dev._field_answer_cache.clear()
    calls: list[str] = []
    real = models_dev._resolve_model_field_tiered

    def counting(field, provider_id, model_id, path):
        calls.append(model_id)
        return real(field, provider_id, model_id, path)

    monkeypatch.setattr(models_dev, "_resolve_model_field_tiered", counting)
    for _ in range(25):
        models_dev.model_context_length_tiered("openai", "gpt-4o", cache)
    assert calls == ["gpt-4o"]

    # A different model is a different question.
    models_dev.model_context_length_tiered("openai", "gpt-4o-mini", cache)
    assert calls == ["gpt-4o", "gpt-4o-mini"]
