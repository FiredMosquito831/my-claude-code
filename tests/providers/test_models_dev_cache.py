"""One parse of the models.dev cache file, shared by every index shape.

The file is 4.9 MB on a real install and ``json.loads`` of it measured 147 ms.
Ten index shapes are built from it and each one used to read and parse the
whole file for itself, on the event loop, in the first requests after every
restart -- measured loop gaps of 336 ms on the TTFT path and 1,108 ms during
finalisation.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.models_dev import (
    prewarm_models_dev_indexes,
    read_models_dev_cache,
)


def _write_cache(path: Path, *, model: str = "acme-1", price: float = 1.0) -> None:
    payload: dict[str, Any] = {
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "index": {
            "acme": {
                "id": "acme",
                "models": {
                    model: {
                        "id": model,
                        "limit": {"context": 128000, "output": 8192},
                        "reasoning": True,
                        "tool_call": True,
                        "modalities": {"input": ["text", "image"]},
                        "cost": {"input": price, "output": price * 2},
                    }
                },
            }
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def counted_loads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real = json.loads

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(models_dev.json, "loads", counting)
    return calls


def test_payload_parsed_once_per_generation(
    tmp_path: Path, counted_loads: list[int]
) -> None:
    """However many index shapes are built, the file is parsed once."""

    cache = tmp_path / "models-dev.json"
    _write_cache(cache)

    assert prewarm_models_dev_indexes(cache) is True

    assert counted_loads[0] == 1


def test_every_read_shares_the_one_parse(
    tmp_path: Path, counted_loads: list[int]
) -> None:
    cache = tmp_path / "models-dev.json"
    _write_cache(cache)

    first = read_models_dev_cache(cache)
    for _ in range(20):
        read_models_dev_cache(cache)

    assert first is not None
    assert counted_loads[0] == 1


def test_a_rewritten_cache_is_parsed_again(
    tmp_path: Path, counted_loads: list[int]
) -> None:
    """The memo is keyed on the file's generation, not on its path."""

    cache = tmp_path / "models-dev.json"
    _write_cache(cache, price=1.0)
    first = read_models_dev_cache(cache)
    assert first is not None
    assert first.index["acme"]["models"]["acme-1"]["cost"]["input"] == 1.0

    _write_cache(cache, price=9.0)
    models_dev.reset_models_dev_payload_cache()
    second = read_models_dev_cache(cache)

    assert second is not None
    assert second.index["acme"]["models"]["acme-1"]["cost"]["input"] == 9.0
    assert counted_loads[0] == 2


def test_freshness_is_recomputed_even_though_the_payload_is_shared(
    tmp_path: Path,
) -> None:
    """``fresh`` is a statement about the clock, so it is never memoized."""

    cache = tmp_path / "models-dev.json"
    _write_cache(cache)

    first = read_models_dev_cache(cache)
    assert first is not None
    assert first.fresh is True

    # Same bytes, same generation, a new record object each time.
    second = read_models_dev_cache(cache)
    assert second is not None
    assert second is not first
    assert second.index is first.index


def test_prewarm_reports_when_there_is_nothing_to_build(tmp_path: Path) -> None:
    assert prewarm_models_dev_indexes(tmp_path / "absent.json") is False


def test_a_missing_or_corrupt_cache_is_still_none(tmp_path: Path) -> None:
    corrupt = tmp_path / "models-dev.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert read_models_dev_cache(corrupt) is None
    assert read_models_dev_cache(tmp_path / "absent.json") is None
