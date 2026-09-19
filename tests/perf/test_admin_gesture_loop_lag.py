"""The long admin gestures do not hold the event loop.

Three gestures were measured holding it on the reporting machine's
configuration -- 52 KB of managed ``.env``, 475 set keys, 995 visibility
patterns, 222 providers in the models.dev index:

===================  ==========  ==========
gesture              before      after
===================  ==========  ==========
Pause one route      2092 ms     see below
Resume one route     2273 ms     see below
Refresh models       2755 ms     see below
===================  ==========  ==========

None of it was the render, which is 2.7 ms. It was ``dotenv`` parsing the same
unchanged files six times per click, and ``_flatten_index`` rebuilding the same
unchanged models.dev table once per provider.

**The measurement is wall clock, never an iteration count.** A heartbeat asks
for 20 ms and reports how much later than that it woke; a bound on that number
is a statement about whether one coroutine blocked every other one, which is
the defect. Iteration counts measure the runner.

The bounds are generous against the measured figures, because a shared CI
runner cannot be asked how fast it is -- but they are an order of magnitude
below the numbers above, so the defect cannot come back unnoticed.
"""

import asyncio
import time
from pathlib import Path

import pytest

from my_claude_code.config.admin import persistence, sources
from my_claude_code.config.admin.manifest import FIELDS

#: The bar the spec sets for a Pause/Resume click: the loop back inside
#: 300 ms. Asserted at 400 ms to leave a loaded runner room without leaving
#: room for a two-second hold.
MAX_LOOP_GAP_MS = 400.0

#: How many visibility patterns the reporting machine carries. The line is
#: ~35 KB on its own and is most of what makes the file expensive to parse.
VISIBILITY_PATTERNS = 995


class _Heartbeat:
    """A 20 ms tick, and the gaps it did not get."""

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _Heartbeat:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0.05)
        self.gaps.clear()
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self._stop.set()
        if self._task is not None:
            await self._task
        return False

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            self.gaps.append((now - last - 0.02) * 1000.0)
            last = now

    @property
    def max_gap(self) -> float:
        return max(self.gaps) if self.gaps else 0.0


@pytest.fixture
def reporting_machine_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed file shaped like the one the gestures were measured on."""

    config_dir = tmp_path / ".mcc"
    config_dir.mkdir()
    monkeypatch.setenv("MCC_CONFIG_DIR", str(config_dir))
    sources.clear_env_parse_cache()

    values: dict[str, str] = {}
    for field in FIELDS:
        if field.field_type == "bool":
            values[field.key] = "true"
        elif field.secret:
            values[field.key] = f"sk-{field.key.lower()}-0123456789abcdef"
        elif field.default:
            values[field.key] = field.default
    deny = ",".join(
        f"provider-{n // 20}/model-name-{n}-*" for n in range(VISIBILITY_PATTERNS)
    )
    if "MODEL_VISIBILITY_DENY" in {field.key for field in FIELDS}:
        values["MODEL_VISIBILITY_DENY"] = deny
    managed = config_dir / ".env"
    managed.write_text(
        persistence.render_env_file(values, preserved={}), encoding="utf-8"
    )
    assert managed.stat().st_size > 20_000, "the fixture must be a large file"
    return managed


def _pause(paused: bool) -> dict[str, str]:
    return {"MODEL_OPUS_PAUSED": "anthropic/claude-opus-4-1" if paused else ""}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "paused"),
    [("pause", True), ("resume", False)],
)
async def test_a_pause_or_resume_does_not_hold_the_loop(
    reporting_machine_config: Path, label: str, paused: bool
) -> None:
    """One key written, on a file this size, without stalling anything else.

    This is the whole of a Pause click's server-side cost bar the provider
    generation swap, which 6.35.1 measured at under a millisecond.
    """

    async with _Heartbeat() as beat:
        for _ in range(3):
            prepared = persistence.prepare_admin_update(_pause(paused))
            assert prepared.valid, prepared.errors
            persistence.commit_prepared_admin_update(prepared)
            await asyncio.sleep(0.02)
    assert beat.max_gap < MAX_LOOP_GAP_MS, (
        f"{label} held the loop for {beat.max_gap:.0f} ms"
    )


@pytest.mark.asyncio
async def test_the_managed_layers_are_not_reparsed_once_per_read(
    reporting_machine_config: Path,
) -> None:
    """The mechanism behind the bound above, asserted as a ratio not a time.

    The first read of a content pays for the parse; every read after it, until
    the content changes, must be very much cheaper. A ratio survives a slow
    runner in a way a millisecond bound does not.
    """

    sources.clear_env_parse_cache()
    start = time.perf_counter()
    first = sources.dotenv_values_from_file(reporting_machine_config)
    cold = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(5):
        again = sources.dotenv_values_from_file(reporting_machine_config)
    warm = (time.perf_counter() - start) / 5

    assert again == first
    assert warm < cold / 4, f"cold {cold * 1000:.1f} ms, warm {warm * 1000:.1f} ms"


@pytest.mark.asyncio
async def test_refreshing_the_catalogue_does_not_reflatten_per_provider(
    tmp_path: Path,
) -> None:
    """F16's mechanism: one flatten per index, not one per provider.

    The flatten does not depend on how many models the caller brought -- it is
    the index that is walked -- so a sweep of twenty providers used to pay for
    twenty identical flattenings of one unchanged dictionary. Measured as a
    ratio, for the same reason as above.
    """

    from my_claude_code.providers.runtime import models_dev

    index = {
        f"provider-{p}": {
            "models": {
                f"model-{p}-{m}": {
                    "limit": {"context": 128_000},
                    "cost": {"input": 1.0, "output": 2.0},
                    "modalities": {"input": ["text", "image"]},
                }
                for m in range(60)
            }
        }
        for p in range(60)
    }
    models_dev.reset_models_dev_payload_cache()

    infos = tuple(
        models_dev.ProviderModelInfo(model_id=f"model-0-{m}") for m in range(50)
    )
    start = time.perf_counter()
    first = models_dev.enrich_model_infos(infos, index, "provider-0")
    cold = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(5):
        again = models_dev.enrich_model_infos(infos, index, "provider-0")
    warm = (time.perf_counter() - start) / 5

    assert again == first
    assert warm < cold / 4, f"cold {cold * 1000:.1f} ms, warm {warm * 1000:.1f} ms"

    # A different index is a different object and is flattened again, so a
    # refreshed models.dev cache reaches the catalogue rather than being
    # answered from a table built out of the payload it replaced.
    other = {
        "provider-0": {"models": {"model-0-0": {"limit": {"context": 999}, "cost": {}}}}
    }
    enriched = models_dev.enrich_model_infos(
        (models_dev.ProviderModelInfo(model_id="model-0-0"),), other, "provider-0"
    )
    assert enriched[0].context_length == 999
