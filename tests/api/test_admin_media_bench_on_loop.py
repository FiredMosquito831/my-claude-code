"""The media models readout asks the bench on the event loop (7.72.1).

``GET /admin/api/media/models`` builds its page on a worker thread. Reading a
bench is not read-only -- ``RouteHealthRegistry.why`` asks ``is_ejected``,
which clears a bench whose time is up, outcome window included -- and the
media requests that record failures into the same books, counting that window
as they do, run on the event loop. So the readout is taken on the loop and only
the rest of the page goes to the worker, and the page says exactly what it said
before. The registry the books live in is also built once per settings key, no
matter how many threads ask for it first.
"""

import asyncio
import contextlib
import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from my_claude_code.application.media import executor
from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

TOGETHER_IMAGE = "together/black-forest-labs/FLUX.1-schnell"
DEEPINFRA_IMAGE = "deepinfra/stabilityai/sdxl-turbo"
GROQ_TTS = "groq/playai-tts"
GROQ_ASR = "groq/whisper-large-v3"
#: A custom provider removed after the rail named it: a ref whose provider is
#: unknown to the page.
UNKNOWN_VIDEO = "custom_ghost/video-model"


def _settings(monkeypatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path))
    registry = get_provider_registry()
    registry.add(
        display_name="Ghost",
        base_url="https://ghost.example/v1",
        api_keys=("sk-ghost-aaaa1111",),
        media_operations=["video"],
    )
    try:
        return _validated()
    finally:
        registry.remove("custom_ghost")


def _validated() -> Settings:
    return Settings.model_validate(
        {
            "TOGETHER_API_KEY": "tg-" + "b" * 40,
            "GROQ_API_KEY": "gsk_" + "c" * 40,
            "PROVIDER_RETRY_ATTEMPTS": "1",
            "MODEL_IMAGE": TOGETHER_IMAGE,
            "MODEL_IMAGE_FALLBACKS": DEEPINFRA_IMAGE,
            "MODEL_TTS": GROQ_TTS,
            "MODEL_ASR": GROQ_ASR,
            "MODEL_VIDEO": UNKNOWN_VIDEO,
            "FALLBACK_BEHAVIOR": "consecutive",
            "FALLBACK_EJECT_AFTER_FAILURES": "1",
            "FALLBACK_EJECT_SECONDS": "60",
            "FALLBACK_BENCH_ENABLED": "true",
        }
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _benched_state(settings: Settings) -> RouteHealthRegistry:
    """One bench expired, one still running, one ref healthy, one unknown.

    On a clock the test owns, so two readouts taken a moment apart cannot
    differ by a rounding step of ``remaining_seconds``.
    """

    registry = media_route_health_registry(settings)
    clock = _Clock()
    registry.now = clock
    clock.now = 1000.0
    registry.record_failure(TOGETHER_IMAGE, failure_kind="upstream", status_code=500)
    clock.now = 1050.0
    registry.record_failure(GROQ_TTS, failure_kind="upstream", status_code=500)
    registry.record_success(GROQ_ASR)
    clock.now = 1100.0  # TOGETHER_IMAGE's minute is up; GROQ_TTS has 10 s left
    return registry


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _get(settings: Settings) -> dict:
    client = TestClient(create_test_app(settings), client=("127.0.0.1", 50000))
    response = client.get("/admin/api/media/models")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_bench_is_never_asked_from_a_worker_thread(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    registry = _benched_state(settings)
    asked: list[tuple[str, str, bool]] = []
    original_is_ejected = RouteHealthRegistry.is_ejected
    original_why = RouteHealthRegistry.why

    def spy_is_ejected(self: RouteHealthRegistry, model_ref: str) -> bool:
        if self is registry:
            asked.append(("is_ejected", model_ref, _on_event_loop()))
        return original_is_ejected(self, model_ref)

    def spy_why(self: RouteHealthRegistry, model_ref: str):
        if self is registry:
            asked.append(("why", model_ref, _on_event_loop()))
        return original_why(self, model_ref)

    monkeypatch.setattr(RouteHealthRegistry, "is_ejected", spy_is_ejected)
    monkeypatch.setattr(RouteHealthRegistry, "why", spy_why)

    _get(settings)

    refs = {ref for _name, ref, _loop in asked}
    assert refs == {TOGETHER_IMAGE, DEEPINFRA_IMAGE, GROQ_TTS, GROQ_ASR, UNKNOWN_VIDEO}
    off_loop = [(name, ref) for name, ref, on_loop in asked if not on_loop]
    assert off_loop == [], f"asked from a worker thread: {off_loop}"


def test_the_page_says_exactly_what_it_said_before(monkeypatch, tmp_path) -> None:
    """The route against the payload built in one piece, as before 7.72.1.

    ``media_models_payload(settings)`` with no readout takes the bench where it
    stands, inside the payload, which is the pre-7.72.1 computation; the route
    takes it on the loop first. Same state, same clock: same page.
    """

    from my_claude_code.api.admin_media_routes import media_models_payload

    settings = _settings(monkeypatch, tmp_path)
    _benched_state(settings)

    served = _get(settings)
    in_one_piece = json.loads(json.dumps(media_models_payload(settings)))

    assert served == in_one_piece
    health = {row["model_ref"]: row["health"] for row in served["models"]}
    assert health[TOGETHER_IMAGE] == {"benched": False}  # expired, and cleared
    assert health[GROQ_TTS]["benched"] is True
    assert health[GROQ_TTS]["remaining_seconds"] == 10.0
    assert health[GROQ_TTS]["reason"].startswith("benched:")
    assert health[GROQ_ASR] == {"benched": False}
    assert health[DEEPINFRA_IMAGE] == {"benched": False}
    unknown = [row for row in served["models"] if row["model_ref"] == UNKNOWN_VIDEO]
    assert unknown[0]["provider_state"] == "unknown"
    assert unknown[0]["health"] == {"benched": False}
    assert served["bench_enabled"] is True


def test_two_first_callers_on_two_threads_share_one_registry(
    monkeypatch, tmp_path
) -> None:
    """Both callers inside the constructor at once: one registry comes out.

    A lookup and a store would let the second store orphan the first
    registry, along with anything a request had recorded into it.
    """

    settings = _settings(monkeypatch, tmp_path)
    inside = threading.Barrier(2, timeout=5.0)
    built: list[int] = []
    original = executor.RouteHealthRegistry

    def both_inside(*args, **kwargs) -> RouteHealthRegistry:
        built.append(threading.get_ident())
        with contextlib.suppress(threading.BrokenBarrierError):
            inside.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "RouteHealthRegistry", both_inside)
    got: list[RouteHealthRegistry | None] = [None, None]

    def first_caller(slot: int) -> None:
        got[slot] = media_route_health_registry(settings)

    threads = [threading.Thread(target=first_caller, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert len(built) == 2, "the interleaving did not happen: both must construct"
    assert got[0] is not None and got[0] is got[1]
    assert media_route_health_registry(settings) is got[0]
