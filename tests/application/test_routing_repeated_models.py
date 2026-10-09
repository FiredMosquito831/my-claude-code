"""A model listed twice in a chain is tried twice, in its places (7.82.0).

User decisions (2026-10-06, binding): "allow the same model from the same
provider to repeat how-many-ever times it is put in the fallback chain"; a
repeat gets no special handling -- "we have a rate limit timer so it is good we
should respect it"; "don't wrongfully alter good existing arch". So the only
thing that changed is that the router stops dropping the second listing. The
executor below it was always index-based: each listing is its own attempt,
judged when it is reached by the same pause, bench, cooldown step-over and
retry rules as any other entry. These tests pin both halves -- the repeat is
kept on every chain the router builds, and a chain without a repeat resolves
exactly as the 7.81.1 algorithm resolved it.

The full provider stack under a repeat (same-key retries, key rotation, key
block, bench, chat and media alike) is in
``tests/contracts/test_media_chat_parity.py``.
"""

import asyncio
import random
from collections.abc import AsyncIterator

import pytest

from my_claude_code.application.execution import ProviderExecutor, RouteAttemptRecord
from my_claude_code.application.media.rails import (
    MediaRouter,
    configured_media_model_refs,
    rail_refs,
)
from my_claude_code.application.media.request import MediaRail, MediaRequest
from my_claude_code.application.routing import ModelRouter, RouteDiversion
from my_claude_code.config.harness_tiers import HarnessTierOverride, HarnessTiers
from my_claude_code.config.media_surfaces import MEDIA_OPERATION_IMAGE_GENERATE
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import ReasoningPolicy

A = "open_router/a"
B = "groq/b"
C = "cerebras/c"
PRIMARY = "nvidia_nim/primary"
VISION = "open_router/sees"
VISION_2 = "cerebras/sees-too"

_IMAGE_BLOCK: dict[str, object] = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
}


@pytest.fixture
def settings() -> Settings:
    settings = Settings()
    settings.model = PRIMARY
    for tier in ("mythos", "fable", "opus", "sonnet", "haiku"):
        setattr(settings, f"model_{tier}", None)
        setattr(settings, f"model_{tier}_fallbacks", None)
        setattr(settings, f"model_{tier}_paused", None)
    settings.model_fallbacks = None
    settings.model_paused = None
    settings.model_vision = None
    settings.model_vision_fallbacks = None
    settings.model_vision_paused = None
    settings.vision_adapter_mode = "route"
    settings.reasoning_policy = ReasoningPreference.CLIENT
    for tier in ("fable", "opus", "sonnet", "haiku"):
        setattr(settings, f"reasoning_{tier}", ReasoningPreference.INHERIT)
    return settings


def _request(
    model: str = "claude-opus-4", *, image: bool = False, stream: bool = True
) -> MessagesRequest:
    content: list[dict[str, object]] = [{"type": "text", "text": "hello"}]
    if image:
        content.append(_IMAGE_BLOCK)
    return MessagesRequest.model_validate(
        {
            "model": model,
            "stream": stream,
            "messages": [{"role": "user", "content": content}],
        }
    )


def _router(settings: Settings, tiers: HarnessTiers | None = None, **kwargs):
    table = tiers if tiers is not None else HarnessTiers()
    return ModelRouter(settings, harness_tiers=lambda: table, **kwargs)


def _refs(router: ModelRouter, request: MessagesRequest, harness=None):
    return router.resolve_messages_plan(request, harness=harness).model_refs()


# ------------------------------------------------------------ the chains --


def test_a_tier_tries_a_model_at_every_place_it_is_listed(settings) -> None:
    settings.model_opus = A
    settings.model_opus_fallbacks = f"{B},{A},{B}"

    assert _refs(_router(settings), _request("claude-opus-4")) == (A, B, A, B)


def test_the_default_route_keeps_its_primary_listed_again(settings) -> None:
    settings.model_fallbacks = f"{PRIMARY},{B},{PRIMARY}"

    assert _refs(_router(settings), _request("claude-sonnet-4")) == (
        PRIMARY,
        PRIMARY,
        B,
        PRIMARY,
    )


def test_a_model_listed_only_twice_is_two_attempts(settings) -> None:
    settings.model_fallbacks = PRIMARY

    assert _refs(_router(settings), _request()) == (PRIMARY, PRIMARY)


def test_a_tier_alias_keeps_the_repeat_of_its_global_chain(settings) -> None:
    settings.model_fallbacks = f"{A},{PRIMARY}"

    router = _router(settings)

    assert _refs(router, _request("mcc/best")) == (PRIMARY, A, PRIMARY)
    assert _refs(router, _request("mcc/best"), harness="opencode") == (
        PRIMARY,
        A,
        PRIMARY,
    )


def test_an_agent_override_keeps_its_repeats(settings) -> None:
    tiers = HarnessTiers(
        harnesses={
            "opencode": {
                "best": HarnessTierOverride(model=A, fallbacks=(B, A, B)),
                "cheap": HarnessTierOverride(fallbacks=(PRIMARY,)),
            }
        }
    )
    router = _router(settings, tiers)

    assert _refs(router, _request("mcc/best"), harness="opencode") == (A, B, A, B)
    # The middle state: the global primary, then the agent's own fallbacks.
    assert _refs(router, _request("mcc/cheap"), harness="opencode") == (
        PRIMARY,
        PRIMARY,
    )


def test_an_unknown_provider_is_skipped_at_every_listing(settings) -> None:
    settings.model_fallbacks = f"not_a_provider/x,{A},not_a_provider/x,{A}"

    assert _refs(_router(settings), _request()) == (PRIMARY, A, A)


def _sight(blind: set[str]):
    def lookup(provider_id: str, model_id: str) -> bool | None:
        return False if f"{provider_id}/{model_id}" in blind else None

    return lookup


def test_the_vision_chain_keeps_its_repeats_when_it_takes_a_request(
    settings,
) -> None:
    settings.model_fallbacks = f"{B},{C}"
    settings.model_vision = VISION
    settings.model_vision_fallbacks = f"{VISION_2},{VISION}"
    router = _router(settings, vision_lookup=_sight({PRIMARY}))

    plan = router.resolve_messages_plan(_request(image=True))

    # The adapter's chain leads, repeat and all; then the route's own sighted
    # entries the adapter does not already carry -- the splice rule is
    # unchanged.
    assert plan.model_refs() == (VISION, VISION_2, VISION, B, C)
    assert plan.diversion is RouteDiversion.VISION


def test_describe_mode_hands_the_vision_chain_over_with_its_repeats(
    settings,
) -> None:
    settings.model_vision = VISION
    settings.model_vision_fallbacks = f"{VISION},{VISION_2}"
    settings.vision_adapter_mode = "describe"
    router = _router(settings, vision_lookup=_sight({PRIMARY}))

    chain = router.vision_describe_chain(_request(image=True))

    assert [entry.provider_model_ref for entry in chain] == [
        VISION,
        VISION,
        VISION_2,
    ]


def test_a_known_blind_vision_entry_is_dropped_at_every_listing(settings) -> None:
    settings.model_vision = VISION
    settings.model_vision_fallbacks = f"{B},{VISION_2},{B}"
    router = _router(settings, vision_lookup=_sight({PRIMARY, B}))

    plan = router.resolve_messages_plan(_request(image=True))

    assert plan.model_refs() == (VISION, VISION_2)


def test_a_pause_names_a_model_so_every_listing_is_paused(settings) -> None:
    settings.model_fallbacks = f"{A},{PRIMARY}"
    settings.model_paused = PRIMARY

    plan = _router(settings).resolve_messages_plan(_request())

    assert plan.model_refs() == (PRIMARY, A, PRIMARY)
    assert plan.paused_refs == frozenset({PRIMARY})


# -------------------------------------------- equality: no repeat, no change --

_POOL = (
    PRIMARY,
    A,
    B,
    C,
    VISION,
    VISION_2,
    "groq/moonshotai/kimi-k2",
    "open_router/x-ai/grok-5",
    "not_a_provider/x",
)


def _old_parse(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    refs: list[str] = []
    for candidate in raw.split(","):
        model_ref = candidate.strip()
        if model_ref and model_ref not in refs:
            refs.append(model_ref)
    return tuple(refs)


def _old_chain(head: tuple[str, ...], raw: str | None) -> tuple[str, ...]:
    """The 7.81.1 router's walk: one ``seen`` set, unknown providers skipped."""

    known = get_provider_registry().all_descriptors()
    seen: set[str] = set()
    out: list[str] = []
    for ref in (*head, *_old_parse(raw)):
        if not ref or ref in seen:
            continue
        seen.add(ref)
        if ref.split("/", 1)[0] not in known:
            continue
        out.append(ref)
    return tuple(out)


def _spelled(refs: list[str], rng: random.Random) -> str:
    return ",".join(rng.choice(("", " ")) + ref + rng.choice(("", " ")) for ref in refs)


def test_a_chain_without_a_repeat_resolves_exactly_as_before(settings) -> None:
    """Equality, 200 generated configurations per surface.

    Route chains, tier aliases (global and per agent) and the vision adapter's
    chain, each against the 7.81.1 algorithm kept verbatim above. No listing
    repeats in any of them, so nothing may differ.
    """

    rng = random.Random(782)
    known_pool = [ref for ref in _POOL if not ref.startswith("not_a_provider")]
    for _ in range(200):
        primary = rng.choice(known_pool)
        rest = [ref for ref in _POOL if ref != primary]
        fallbacks = rng.sample(rest, rng.randint(0, len(rest)))
        raw = _spelled(fallbacks, rng)
        settings.model_opus = primary
        settings.model_opus_fallbacks = raw
        settings.model_fallbacks = raw
        settings.model = primary
        router = _router(settings)

        expected = _old_chain((primary,), raw)
        assert _refs(router, _request("claude-opus-4")) == expected, raw
        assert _refs(router, _request("claude-haiku-4")) == expected, raw
        assert _refs(router, _request("mcc/best")) == expected, raw

        override = HarnessTiers(
            harnesses={
                "crush": {
                    "good": HarnessTierOverride(
                        model=primary, fallbacks=tuple(_old_parse(raw))
                    )
                }
            }
        )
        assert (
            _refs(_router(settings, override), _request("mcc/good"), harness="crush")
            == expected
        ), raw

        settings.model_vision = primary
        settings.model_vision_fallbacks = raw
        vision = [
            entry.provider_model_ref
            for entry in _router(settings)._vision_adapter_chain(
                _router(settings).resolve_chain("claude-opus-4")[0]
            )
        ]
        assert tuple(vision) == expected, raw
        settings.model_vision = None
        settings.model_vision_fallbacks = None


# ------------------------------------- the executor walks every listing --


class _Upstream:
    """One fake provider whose answers are scripted call by call."""

    def __init__(self, name: str, script: list[str]) -> None:
        self.name = name
        self.script = list(script)
        self.calls = 0
        self.cooldown_seconds = 0.0

    def throttle_remaining(self, model: str | None = None) -> float:
        return self.cooldown_seconds

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(self, request, *, reasoning: ReasoningPolicy) -> None:
        return None

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.calls += 1
        TRAIL.append(self.name)
        answer = self.script.pop(0) if self.script else "ok"
        if answer == "500":
            raise ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                status_code=500,
                message="upstream said no",
                retryable=True,
            )
        if answer == "429":
            # What a real pool does after a 429 spent its keys: the provider
            # is now in a rate-limit cooldown the executor can read.
            self.cooldown_seconds = 30.0
            raise ExecutionFailure(
                kind=FailureKind.RATE_LIMIT,
                status_code=429,
                message="slow down",
                retryable=True,
            )
        yield (
            "event: content_block_delta\ndata: "
            '{"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"ok"}}\n\n'
        )


TRAIL: list[str] = []


def _walk(
    settings: Settings, upstreams: dict[str, _Upstream]
) -> tuple[list[str], list[tuple[int, str, str, str | None]], Exception | None]:
    TRAIL.clear()
    plan = _router(settings).resolve_messages_plan(_request())
    executor = ProviderExecutor(
        lambda provider_id: upstreams[provider_id],
        token_counter=lambda _messages, _system, _tools: 1,
    )
    records: list[RouteAttemptRecord] = []
    error: list[Exception] = []

    async def run() -> None:
        try:
            stream = executor.stream(
                plan,
                wire_api="messages",
                raw_log_label="X",
                raw_log_payload={},
                request_id="repeat",
                on_attempt_result=records.append,
            )
            async for _chunk in stream:
                pass
        except Exception as exc:
            error.append(exc)

    asyncio.run(run())
    rows = [(r.attempt, r.model_ref, str(r.outcome), r.error_kind) for r in records]
    return list(TRAIL), rows, (error[0] if error else None)


def test_a_b_a_tries_a_then_b_then_a_again_with_one_row_each(settings) -> None:
    settings.model = A
    settings.model_fallbacks = f"{B},{A}"
    upstreams = {
        "open_router": _Upstream("A", ["500", "ok"]),
        "groq": _Upstream("B", ["500"]),
    }

    trail, rows, error = _walk(settings, upstreams)

    assert error is None
    assert trail == ["A", "B", "A"]
    assert rows == [
        (0, A, "failed", "upstream"),
        (1, B, "failed", "upstream"),
        (2, A, "succeeded", None),
    ]


def test_a_listing_still_cooling_down_is_stepped_over_when_a_model_remains(
    settings,
) -> None:
    """The existing rule, unchanged: a >= 5 s cooldown is not waited for."""

    settings.model = A
    settings.model_fallbacks = f"{A},{B}"
    upstreams = {
        "open_router": _Upstream("A", ["429"]),
        "groq": _Upstream("B", ["ok"]),
    }

    trail, rows, error = _walk(settings, upstreams)

    assert error is None
    assert trail == ["A", "B"]
    assert rows == [
        (0, A, "failed", "rate_limit"),
        (1, A, "skipped", "cooldown"),
        (2, B, "succeeded", None),
    ]


def test_the_last_listing_is_tried_even_while_it_cools_down(settings) -> None:
    """Never the last candidate: a skipped chain with nothing behind it is an
    outage, so the existing rule tries it and lets its own limiter wait."""

    settings.model = A
    settings.model_fallbacks = A
    upstreams = {"open_router": _Upstream("A", ["429", "ok"])}

    trail, rows, error = _walk(settings, upstreams)

    assert error is None
    assert trail == ["A", "A"]
    assert rows == [
        (0, A, "failed", "rate_limit"),
        (1, A, "succeeded", None),
    ]


def test_a_paused_model_is_skipped_at_every_listing(settings) -> None:
    settings.model = A
    settings.model_fallbacks = f"{B},{A}"
    settings.model_paused = A
    upstreams = {
        "open_router": _Upstream("A", []),
        "groq": _Upstream("B", ["ok"]),
    }

    trail, rows, error = _walk(settings, upstreams)

    assert error is None
    assert trail == ["B"]
    assert rows == [
        (0, A, "skipped", "paused"),
        (1, B, "succeeded", None),
        (2, A, "skipped", "paused"),
    ]


# ------------------------------------------------------------ media rails --

IMG_A = "deepinfra/img-a"
IMG_B = "together/img-b"


def _media_request(model: str = "") -> MediaRequest:
    return MediaRequest(
        operation=MEDIA_OPERATION_IMAGE_GENERATE,
        rail=MediaRail.IMAGE,
        model=model,
        body={"prompt": "p"},
    )


def test_a_media_rail_tries_a_model_at_every_place_it_is_listed(settings) -> None:
    settings.model_image = IMG_A
    settings.model_image_fallbacks = f"{IMG_B},{IMG_A},{IMG_B}"

    plan = MediaRouter(settings).plan(_media_request())

    assert plan.model_refs() == (IMG_A, IMG_B, IMG_A, IMG_B)
    assert rail_refs(settings, MediaRail.IMAGE) == (IMG_A, IMG_B, IMG_A, IMG_B)
    # The "every media model" list is still a set.
    assert configured_media_model_refs(settings) == (IMG_A, IMG_B)


def test_a_media_ref_named_directly_is_still_one_attempt(settings) -> None:
    settings.model_image = IMG_A
    settings.model_image_fallbacks = f"{IMG_A},{IMG_A}"

    plan = MediaRouter(settings).plan(_media_request(IMG_B))

    assert plan.model_refs() == (IMG_B,)


def test_a_media_rail_without_a_repeat_resolves_exactly_as_before(settings) -> None:
    rng = random.Random(7820)
    pool = [IMG_A, IMG_B, "openai/gpt-image-1", "deepinfra/flux", "together/sdxl"]
    known = get_provider_registry().all_descriptors()
    for rail in MediaRail:
        model_attr = f"model_{rail.value}"
        chain_attr = f"{model_attr}_fallbacks"
        for _ in range(50):
            primary = rng.choice([*pool, ""])
            rest = [ref for ref in pool if ref != primary]
            raw = _spelled(rng.sample(rest, rng.randint(0, len(rest))), rng)
            setattr(settings, model_attr, primary or None)
            setattr(settings, chain_attr, raw)
            old_refs = tuple(
                ref for ref in (primary, *_old_parse(raw)) if ref
            )  # 7.81.1 de-duplicated these; nothing repeats here
            assert rail_refs(settings, rail) == old_refs, raw
            usable = tuple(ref for ref in old_refs if ref.split("/", 1)[0] in known)
            if usable:
                request = MediaRequest(
                    operation=MEDIA_OPERATION_IMAGE_GENERATE,
                    rail=rail,
                    model="",
                    body={},
                )
                assert MediaRouter(settings).plan(request).model_refs() == usable
        setattr(settings, model_attr, None)
        setattr(settings, chain_attr, None)
