"""A fallback chain shrinks each screenshot once, not once per rung.

This is the regression guard for the loop hold behind the 7.69.2 listener
loss (``specs/INVESTIGATION-REQUEST-DEADLOCK.md`` F3, decision 6). The router
builds every rung of a fallback chain up front, on the event loop, and until
7.69.3 each rung's deep copy decoded, LANCZOS-resized and re-encoded every
image again. The production log measured about 80 ms per image per rung; a
27-image Claude Code conversation on a 14-rung chain held the loop for 8-11 s
per turn, and the worst single hold was 22.4 s.

Measured on the real product (2026-10-01, Windows 11, scratch servers from the
7.69.2 tag and from this change, a fake SSE upstream, a 13-rung chain of fake
models, one conversation re-sending 27 PNG screenshots of 900x2000 on three
turns, ``/health`` polled every 100 ms on a fresh connection each time):

======  ==========================  ==========================
turn    longest /health wait 7.69.2 longest /health wait 7.69.3
======  ==========================  ==========================
1       88.8 s                      10.7 s (the one cold rung)
2       86.6 s                      0.9 s
3       88.2 s                      0.9 s
======  ==========================  ==========================

The 39 request bodies the upstream received were byte-identical between the
two servers (sha256 per rung). This test on the same machine, before and after
(``time.perf_counter``): uncached build 3.96 s, cold 0.70 s, warm 0.06 s.

What is pinned here is the mechanism, at a size a CI runner can afford: 20
images on a 12-rung chain whose rungs use two resize policies. The resize
work must run once per distinct (image, policy) pair -- 40, not 240 -- the next
turn of the same conversation must run none, and the loop hold of the chain
build must fall with it. Hold ratios, not absolute times, are asserted: a
shared runner cannot be asked how fast it is, but it can be asked whether the
cached build does a fraction of the uncached one's blocking work.
"""

import asyncio
import base64
import io
import time
from collections.abc import Callable
from typing import Any

import pytest
from PIL import Image, ImageDraw

from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import image_downscale
from my_claude_code.core.anthropic.image_downscale import image_downscale_cache_info
from my_claude_code.core.anthropic.models import MessagesRequest

IMAGES = 20
#: Seven rungs where Anthropic's token budget binds (anthropic, and hosts that
#: publish no formula), five where only the pixel cap does.
CHAIN = (
    "anthropic/claude-a",
    "nvidia_nim/m1",
    "openai/m2",
    "opencode/m3",
    "gemini/m4",
    "nous_portal/m5",
    "deepseek/m6",
    "open_router/m7",
    "qwencloud/m8",
    "anthropic_oauth/m9",
    "azure_openai/m10",
    "groq/m11",
)
POLICIES = 2


class _Heartbeat:
    """A 10 ms tick, and the longest gap it did not get."""

    def __init__(self) -> None:
        self.max_gap_ms = 0.0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _Heartbeat:
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        await asyncio.sleep(0.03)
        self._stop.set()
        if self._task is not None:
            await self._task
        return False

    async def _run(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            self.max_gap_ms = max(self.max_gap_ms, (now - last - 0.01) * 1000.0)
            last = now


def _screenshot(seed: int) -> str:
    image = Image.new("RGB", (480, 360), (24, 24, 30))
    draw = ImageDraw.Draw(image)
    for row, top in enumerate(range(6, 350, 12)):
        left = 10 + (row * 13) % 90
        draw.rectangle(
            (left, top, left + 20 + (row * (seed + 1) * 37) % 360, top + 8),
            fill=((row * 11 + seed) % 255, 170, (seed * 29) % 255),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _request(datas: list[str]) -> MessagesRequest:
    """One conversation turn: every screenshot, as freshly parsed text."""
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-5",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what changed"},
                        *(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "".join(list(data)),
                                },
                            }
                            for data in datas
                        ),
                    ],
                }
            ],
        }
    )


@pytest.fixture
def router() -> ModelRouter:
    settings = Settings()
    settings.model = CHAIN[0]
    settings.model_fallbacks = ",".join(CHAIN[1:])
    for attr in ("model_fable", "model_opus", "model_sonnet", "model_haiku"):
        setattr(settings, attr, None)
    for attr in (
        "model_fable_fallbacks",
        "model_opus_fallbacks",
        "model_sonnet_fallbacks",
        "model_haiku_fallbacks",
        "model_vision",
    ):
        setattr(settings, attr, None)
    settings.reasoning_policy = ReasoningPreference.CLIENT
    for attr in (
        "reasoning_fable",
        "reasoning_opus",
        "reasoning_sonnet",
        "reasoning_haiku",
    ):
        setattr(settings, attr, ReasoningPreference.INHERIT)
    settings.image_max_long_edge = 320
    settings.image_jpeg_quality = 0
    settings.tool_result_image_delivery = "attach"
    return ModelRouter(settings)


@pytest.fixture
def resizes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Every real resize the downscaler asks Pillow for."""
    calls: list[tuple[int, int]] = []
    real: Callable[..., Any] = image_downscale.downscale

    def counting(raw: bytes, target: tuple[int, int], **kwargs: Any) -> Any:
        calls.append(target)
        return real(raw, target, **kwargs)

    monkeypatch.setattr(image_downscale, "downscale", counting)
    return calls


async def _build_chain(router: ModelRouter, request: MessagesRequest) -> float:
    """Force every rung on the loop, as ``application/execution.py`` does."""
    async with _Heartbeat() as beat:
        plan = router.resolve_messages_plan(request)
        attempts = tuple(plan.attempts)
        await asyncio.sleep(0)
    assert len(attempts) == len(CHAIN)
    assert all(len(attempt.image_resizes) == IMAGES for attempt in attempts)
    return beat.max_gap_ms


class _NoMemo:
    """The 7.69.2 behaviour: every lookup misses and nothing is kept."""

    def get(self, _key: Any) -> None:
        return None

    def put(self, _key: Any, _outcome: Any) -> None:
        return None

    def settle(self, _keys: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_twelve_rung_chain_shrinks_each_screenshot_once(
    router: ModelRouter,
    resizes: list[tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screenshots = [_screenshot(seed) for seed in range(IMAGES)]

    with monkeypatch.context() as patch:
        patch.setattr(image_downscale, "_MEMO", _NoMemo())
        uncached_hold = await _build_chain(router, _request(screenshots))
    uncached_resizes = len(resizes)

    resizes.clear()
    cold_hold = await _build_chain(router, _request(screenshots))
    cold_resizes = len(resizes)

    resizes.clear()
    warm_hold = await _build_chain(router, _request(screenshots))
    warm_resizes = len(resizes)

    # The work: once per rung before, once per (image, policy) now, and the
    # next turn of the same conversation does none.
    assert uncached_resizes == IMAGES * len(CHAIN)
    assert cold_resizes == IMAGES * POLICIES
    assert warm_resizes == 0
    info = image_downscale_cache_info()
    assert info.entries == IMAGES * POLICIES
    assert info.held_bytes <= info.bound_bytes

    # The hold follows the work.
    assert cold_hold < uncached_hold * 0.5, (
        f"cold {cold_hold:.0f} ms vs uncached {uncached_hold:.0f} ms"
    )
    assert warm_hold < uncached_hold * 0.25, (
        f"warm {warm_hold:.0f} ms vs uncached {uncached_hold:.0f} ms"
    )
