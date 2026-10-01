"""The shrink memo changes how often an image is resized, never what is sent.

Contract: for every input, what ``downscale_request_images`` writes into the
attempt's copy -- the request as it serialises, the ``ImageResize`` records,
the debug lines, and whether anything raised -- is byte-identical with the memo
cold, with it warm, and without it. "Without it" is the 7.69.2 body, kept
verbatim below as the oracle and built from the same Pillow primitives. The
oracle and the memo's cold pass are two independent computations, so a
nondeterministic encoder would show up here as well.

Every corpus image is driven through a fixed sequence of settings in which
neighbouring steps differ by exactly one input -- the JPEG quality, the billing
family, the pixel cap -- with the memo shared across the whole sequence. If any
of those were missing from the key, a later step would be handed an earlier
step's bytes and the comparison with the oracle would fail.

Pixel sizes are kept just past the budgets that matter (1300x1000 is 1692
Anthropic tokens, over the 1568 budget, and inside the 1568 pixel cap) so that
every family and every cap produces a different answer without the suite
paying for 4K resizes; one real 1920x1080 and one real 900x2000 screenshot
are kept as they are.
"""

import base64
import concurrent.futures
import io
import json
import struct
import zlib
from collections.abc import Callable
from dataclasses import astuple
from typing import Any

import pytest
from PIL import Image, ImageDraw

from my_claude_code.core.anthropic import image_downscale
from my_claude_code.core.anthropic.image_downscale import (
    ImageResize,
    downscale_request_images,
    image_downscale_cache_info,
)
from my_claude_code.core.anthropic.image_tokens import (
    ANTHROPIC_MAX_TOKENS,
    ImageTokenFamily,
    anthropic_fit,
    resolve_family,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.request_modalities import request_image_sources
from my_claude_code.core.image_geometry import decode_base64, downscale, raw_dimensions

# ------------------------------------------------------------------- oracle


def _oracle_resize_target(
    width: int, height: int, *, max_long_edge: int, family: str
) -> tuple[int, int]:
    """``resize_target`` exactly as 7.69.2 shipped it."""
    if max_long_edge <= 0 or width <= 0 or height <= 0:
        return (width, height)
    resolved = resolve_family(family)
    if resolved in (ImageTokenFamily.ANTHROPIC, ImageTokenFamily.UNKNOWN):
        return anthropic_fit(
            width, height, max_long_edge=max_long_edge, max_tokens=ANTHROPIC_MAX_TOKENS
        )
    longest = max(width, height)
    if longest <= max_long_edge:
        return (width, height)
    scale = max_long_edge / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def _oracle_one(
    source: dict[Any, Any],
    data: str,
    *,
    index: int,
    max_long_edge: int,
    family: str,
    jpeg_quality: int,
    debug: list[tuple[Any, ...]],
) -> ImageResize | None:
    """``_downscale_one`` exactly as 7.69.2 shipped it; its log line recorded."""
    raw = decode_base64(data)
    if raw is None:
        return None
    size = raw_dimensions(raw)
    if size is None:
        return None
    width, height = size
    target = _oracle_resize_target(
        width, height, max_long_edge=max_long_edge, family=family
    )
    if target == (width, height):
        return None
    result = downscale(raw, target, jpeg_quality=jpeg_quality)
    if result is None:
        return None
    new_raw, media_type = result
    if len(new_raw) >= len(raw) and jpeg_quality <= 0:
        debug.append(
            (
                "Image downscale grew the payload: {} -> {} bytes at {}x{}",
                len(raw),
                len(new_raw),
                target[0],
                target[1],
            )
        )
    source["data"] = base64.b64encode(new_raw).decode("ascii")
    source["media_type"] = media_type
    return ImageResize(
        index=index,
        before_width=width,
        before_height=height,
        after_width=target[0],
        after_height=target[1],
        before_bytes=len(raw),
        after_bytes=len(new_raw),
    )


def _oracle(
    request: MessagesRequest,
    *,
    max_long_edge: int,
    family: str,
    jpeg_quality: int,
    debug: list[tuple[Any, ...]],
) -> tuple[ImageResize, ...]:
    """``downscale_request_images`` exactly as 7.69.2 shipped it."""
    if max_long_edge <= 0:
        return ()
    resizes: list[ImageResize] = []
    for index, (kind, source) in enumerate(request_image_sources(request)):
        if not isinstance(source, dict):
            continue
        if kind != "image" or source.get("type") != "base64":
            continue
        data = source.get("data")
        if not isinstance(data, str) or not data:
            continue
        resize = _oracle_one(
            source,
            data,
            index=index,
            max_long_edge=max_long_edge,
            family=family,
            jpeg_quality=jpeg_quality,
            debug=debug,
        )
        if resize is not None:
            resizes.append(resize)
    return tuple(resizes)


# ------------------------------------------------------------------- corpus


def _b64(image: Image.Image, image_format: str, **save: Any) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _screenshot(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """Text-and-code shaped content: what the encoders really see."""
    image = Image.new("RGB", (width, height), (30, 30, 36))
    draw = ImageDraw.Draw(image)
    for row, top in enumerate(range(8, height - 12, 18)):
        indent = 12 + (row * 37) % 160
        length = (row * 211) % max(1, width - indent - 12)
        draw.rectangle(
            (indent, top, indent + length, top + 9),
            fill=(120 + row % 120, 180 - row % 90, 90 + (row * 7) % 160),
        )
    gradient = Image.linear_gradient("L").resize((width, height))
    image = Image.blend(image, Image.merge("RGB", (gradient,) * 3), 0.15)
    if mode == "RGBA":
        alpha = Image.linear_gradient("L").rotate(90).resize((width, height))
        image = image.convert("RGBA")
        image.putalpha(alpha)
        return image
    return image.convert(mode)


def _palette_with_transparency() -> str:
    image = _screenshot(1300, 1000).convert("P", palette=Image.Palette.ADAPTIVE)
    return _b64(image, "PNG", transparency=0)


def _animated_gif() -> str:
    first = _screenshot(1300, 1000).convert("P", palette=Image.Palette.ADAPTIVE)
    second = (
        _screenshot(1300, 1000).rotate(180).convert("P", palette=Image.Palette.ADAPTIVE)
    )
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second])
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _exif_rotated_jpeg() -> str:
    exif = Image.Exif()
    exif[0x0112] = 6
    return _b64(_screenshot(1300, 1000), "JPEG", quality=88, exif=exif.tobytes())


def _truncated_png() -> str:
    buffer = io.BytesIO()
    _screenshot(1300, 1000).save(buffer, format="PNG")
    raw = buffer.getvalue()
    return base64.b64encode(raw[: len(raw) // 3]).decode("ascii")


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def _header_past_the_decode_guard() -> str:
    """A PNG whose header claims 9000x9000 pixels: refused before decoding.

    81 MP is past MCC's 80 MP guard and still under Pillow's own bomb warning,
    so the refusal under test is ours. Only the header exists; nothing decodes.
    """
    header = struct.pack(">IIBBBBB", 9000, 9000, 8, 0, 0, 0, 0)
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", b"\x00" * 16)
    )
    return base64.b64encode(raw).decode("ascii")


CORPUS: dict[str, Callable[[], str]] = {
    "png-rgb-1300x1000": lambda: _b64(_screenshot(1300, 1000), "PNG"),
    "png-rgb-real-1920x1080": lambda: _b64(_screenshot(1920, 1080), "PNG"),
    "png-tall-real-screenshot-900x2000": lambda: _b64(_screenshot(900, 2000), "PNG"),
    "png-rgba": lambda: _b64(_screenshot(1300, 1000, "RGBA"), "PNG"),
    "png-palette-transparency": _palette_with_transparency,
    "png-grey-L": lambda: _b64(_screenshot(1300, 1000, "L"), "PNG"),
    "png-grey-LA": lambda: _b64(_screenshot(1300, 1000, "RGBA").convert("LA"), "PNG"),
    "png-16bit-I16": lambda: _b64(
        _screenshot(1300, 1000, "L").convert("I").convert("I;16"), "PNG"
    ),
    "png-tiny-1x1": lambda: _b64(Image.new("RGB", (1, 1), (1, 2, 3)), "PNG"),
    "png-exactly-anthropic-fit-1456x819": lambda: _b64(_screenshot(1456, 819), "PNG"),
    "png-exactly-pixel-cap-1568x882": lambda: _b64(_screenshot(1568, 882), "PNG"),
    "png-one-over-the-cap-1569x882": lambda: _b64(_screenshot(1569, 882), "PNG"),
    "jpeg-rgb-q95": lambda: _b64(_screenshot(1300, 1000), "JPEG", quality=95),
    "jpeg-low-quality-grows-on-reencode": lambda: _b64(
        _screenshot(1300, 1000), "JPEG", quality=20
    ),
    "jpeg-cmyk": lambda: _b64(_screenshot(1300, 1000, "CMYK"), "JPEG", quality=90),
    "jpeg-exif-rotated": _exif_rotated_jpeg,
    "webp-rgb-lossy": lambda: _b64(_screenshot(1300, 1000), "WEBP", quality=80),
    "webp-rgba-lossless": lambda: _b64(
        _screenshot(1300, 1000, "RGBA"), "WEBP", lossless=True
    ),
    "gif-animated": _animated_gif,
    "bmp-rgb": lambda: _b64(_screenshot(1300, 1000), "BMP"),
    "corrupt-not-an-image": lambda: base64.b64encode(b"not a picture" * 50).decode(
        "ascii"
    ),
    "corrupt-truncated-png": _truncated_png,
    "corrupt-past-the-decode-guard": _header_past_the_decode_guard,
    "corrupt-bad-base64-padding": lambda: "abc",
    "corrupt-non-ascii": lambda: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABé",
}

#: Inputs nothing can read at all. A truncated file is not in here: its header
#: still tells the size, so "already fits" is a real, memoised answer for it.
UNREADABLE = {
    "corrupt-not-an-image",
    "corrupt-past-the-decode-guard",
    "corrupt-bad-base64-padding",
    "corrupt-non-ascii",
}

#: Neighbouring steps differ in exactly one input. Every billing family the
#: code declares appears, plus spellings that resolve onto one of them.
STEPS: tuple[tuple[int, str, int], ...] = (
    *((1568, family.value, 0) for family in ImageTokenFamily),
    (1568, " Anthropic ", 0),
    (1568, "a-custom-provider-family", 0),
    (1568, "anthropic", 85),
    (1568, "openai_patch", 85),
    (1000, "openai_patch", 85),
    (1000, "openai_patch", 0),
    (1000, "anthropic", 0),
    (1, "anthropic", 0),
    (0, "anthropic", 0),
)


def _request(*datas: str, nested: bool = False) -> MessagesRequest:
    """One user turn with each image; optionally each again in a tool result."""
    content: list[dict[str, Any]] = [{"type": "text", "text": "what is this"}]
    content += [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        }
        for data in datas
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    if nested:
        messages += [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "shot.png"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": data,
                                },
                            }
                            for data in datas
                        ],
                    }
                ],
            },
        ]
    return MessagesRequest.model_validate(
        {"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": messages}
    )


class _RecordingLogger:
    def __init__(self) -> None:
        self.debug_calls: list[tuple[Any, ...]] = []

    def debug(self, *args: Any) -> None:
        self.debug_calls.append(args)


Outcome = tuple[bytes, tuple[tuple[Any, ...], ...], str | None]


def _outcome(
    run: Callable[[MessagesRequest], tuple[ImageResize, ...]],
    request: MessagesRequest,
) -> Outcome:
    """What one path produced: the wire bytes, the records, any exception."""
    try:
        resizes = run(request)
    except Exception as exc:  # the contract is that neither path raises
        return (b"", (), f"{type(exc).__name__}: {exc}")
    wire = json.dumps(request.model_dump(mode="json"), separators=(",", ":"))
    return (wire.encode("utf-8"), tuple(astuple(r) for r in resizes), None)


def _expected(
    datas: tuple[str, ...], step: tuple[int, str, int], *, nested: bool = False
) -> tuple[Outcome, list[tuple[Any, ...]]]:
    max_long_edge, family, quality = step
    log: list[tuple[Any, ...]] = []
    outcome = _outcome(
        lambda request: _oracle(
            request,
            max_long_edge=max_long_edge,
            family=family,
            jpeg_quality=quality,
            debug=log,
        ),
        _request(*datas, nested=nested),
    )
    return outcome, log


def _actual(
    datas: tuple[str, ...],
    step: tuple[int, str, int],
    monkeypatch: pytest.MonkeyPatch,
    *,
    nested: bool = False,
) -> tuple[Outcome, list[tuple[Any, ...]]]:
    max_long_edge, family, quality = step
    recorder = _RecordingLogger()
    monkeypatch.setattr(image_downscale, "logger", recorder)
    outcome = _outcome(
        lambda request: downscale_request_images(
            request,
            max_long_edge=max_long_edge,
            family=family,
            jpeg_quality=quality,
        ),
        _request(*datas, nested=nested),
    )
    return outcome, recorder.debug_calls


def _assert_same(
    expected: tuple[Outcome, list[tuple[Any, ...]]],
    actual: tuple[Outcome, list[tuple[Any, ...]]],
    where: str,
) -> None:
    (wire, records, raised), log = expected
    (wire_now, records_now, raised_now), log_now = actual
    assert raised_now == raised, f"exception differs at {where}"
    assert records_now == records, f"ImageResize records differ at {where}"
    assert wire_now == wire, f"outbound request bytes differ at {where}"
    assert log_now == log, f"debug lines differ at {where}"


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_memo_cold_and_warm_match_the_uncached_body(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = CORPUS[name]()
    oracle = {step: _expected((data,), step) for step in STEPS}
    for step in STEPS:  # cold: the first time each key is asked for
        _assert_same(oracle[step], _actual((data,), step, monkeypatch), f"{step} cold")
    cold = image_downscale_cache_info()
    for step in STEPS:  # warm: every memoised outcome is now a hit
        _assert_same(oracle[step], _actual((data,), step, monkeypatch), f"{step} warm")
    warm = image_downscale_cache_info()

    assert warm.entries == cold.entries
    # In the cold pass every miss either created an entry or was a failure,
    # which is recomputed and never stored. The warm pass repeats only those.
    assert warm.misses - cold.misses == cold.misses - cold.entries
    if name in UNREADABLE:
        assert cold.entries == 0
        assert warm.hits == 0
    else:
        assert cold.entries > 0
        assert warm.hits > cold.hits


def test_the_corpus_exercises_every_outcome() -> None:
    """Guard the corpus itself: shrinks, already-fits and failures all occur."""
    shrunk = untouched = 0
    for name in sorted(CORPUS):
        data = CORPUS[name]()
        request = _request(data)
        resizes = downscale_request_images(
            request, max_long_edge=1568, family="anthropic", jpeg_quality=0
        )
        if resizes:
            shrunk += 1
        else:
            untouched += 1
            assert request_image_sources(request)[0][1] == {
                "type": "base64",
                "media_type": "image/png",
                "data": data,
            }
    assert shrunk >= 14
    assert untouched >= len(UNREADABLE) + 3


def test_images_interleaved_across_requests_never_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One memo, many images and settings, nested copies: no entry answers another."""
    names = (
        "png-rgb-1300x1000",
        "png-rgba",
        "jpeg-rgb-q95",
        "webp-rgb-lossy",
        "png-tall-real-screenshot-900x2000",
    )
    datas = tuple(CORPUS[name]() for name in names)
    steps = ((1568, "anthropic", 0), (1568, "openai_patch", 85), (1000, "gemini", 0))
    for step in steps:
        together = _expected(datas, step, nested=True)
        alone = [_expected((data,), step) for data in datas]
        for _round in range(2):
            _assert_same(
                together,
                _actual(datas, step, monkeypatch, nested=True),
                f"{step} all five images",
            )
            for position, data in enumerate(datas):
                _assert_same(
                    alone[position],
                    _actual((data,), step, monkeypatch),
                    f"{step} {names[position]} alone",
                )


def test_threads_sharing_the_memo_get_the_uncached_bytes() -> None:
    """The memo is reachable from the loop and from worker threads alike."""
    names = (
        "png-rgb-1300x1000",
        "png-tall-real-screenshot-900x2000",
        "jpeg-rgb-q95",
        "webp-rgb-lossy",
    )
    datas = [CORPUS[name]() for name in names]
    step = (1568, "anthropic", 0)
    expected = [_expected((data,), step)[0] for data in datas]

    def one(position: int) -> Outcome:
        return _outcome(
            lambda request: downscale_request_images(
                request, max_long_edge=1568, family="anthropic", jpeg_quality=0
            ),
            _request(datas[position % len(datas)]),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(48)))
    for position, result in enumerate(results):
        assert result == expected[position % len(datas)]
    info = image_downscale_cache_info()
    assert info.held_bytes <= info.bound_bytes
