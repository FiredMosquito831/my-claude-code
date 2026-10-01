"""Shrink an outbound image before it is sent, in place, once per attempt.

Where this runs matters more than what it does. It is called from
``application.routing`` on the per-attempt deep copy the router already takes,
beside ``_resolve_image_delivery`` and for the reason that function's docstring
already states: the converter "is handed a message list and nothing else, and
threading a capability lookup down into every dialect module would put model
metadata where none belongs". A resize is the same kind of decision -- it needs
the destination's billing family, which is model metadata -- so it belongs in
the same place. Doing it there also means all four dialect converters are
handed pre-shrunk data for free, and a chain that falls back from one host to
another re-derives the size for each rung, which one up-front mutation could
not.

Each picture is shrunk once per target, not once per rung. The router builds
every rung of a fallback chain up front, and every rung's copy carries the same
screenshots, so until 7.69.3 each rung decoded, resized and re-encoded each of
them again: about 80 ms per image per rung, on the event loop, which held the
server for up to 22 s on a 27-image conversation and froze ``/health``. The
memo below keeps the finished result keyed by the image's content and by every
parameter that shapes it, so the second and later rungs -- and the next turn of
the conversation, which re-sends the same screenshots -- are handed the exact
bytes the first computation produced.

Nothing here raises and nothing here can fail a request. An image the decoder
cannot read is left exactly as the client sent it.
"""

import base64
import hashlib
import sys
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from my_claude_code.core.image_geometry import decode_base64, downscale, raw_dimensions

from .image_tokens import (
    ANTHROPIC_MAX_TOKENS,
    ImageTokenFamily,
    anthropic_fit,
    resolve_family,
)
from .models import MessagesRequest
from .request_modalities import request_image_sources


@dataclass(frozen=True, slots=True)
class ImageResize:
    """One picture that was shrunk, before and after.

    Recorded rather than logged so the request detail can show
    ``1920x1080 -> 1456x819`` and the request row can carry the byte totals.
    Only images that actually changed produce one of these; an image already
    inside the budget is a no-op and says so by being absent.
    """

    index: int
    before_width: int
    before_height: int
    after_width: int
    after_height: int
    before_bytes: int
    after_bytes: int

    @property
    def summary(self) -> str:
        return (
            f"{self.before_width}x{self.before_height}"
            f" -> {self.after_width}x{self.after_height}"
        )


@dataclass(frozen=True, slots=True)
class DownscaleCacheInfo:
    """What the shrink memo holds, for tests and for a perf probe."""

    hits: int
    misses: int
    entries: int
    held_bytes: int
    bound_bytes: int


@dataclass(frozen=True, slots=True)
class _Shrunk:
    """A finished shrink: the bytes that leave, and the numbers that describe it."""

    data: str
    media_type: str
    before_width: int
    before_height: int
    after_width: int
    after_height: int
    before_bytes: int
    after_bytes: int


@dataclass(frozen=True, slots=True)
class _Fits:
    """The image is already inside the budget and leaves exactly as it came."""


_FITS = _Fits()

_Outcome = _Shrunk | _Fits

#: ``(sha256 of the base64 text, max_long_edge, token budget binds,
#: jpeg_quality)`` -- the content, and every input the outcome depends on.
_Key = tuple[bytes, int, bool, int]


def resize_target(
    width: int,
    height: int,
    *,
    max_long_edge: int,
    family: str | ImageTokenFamily,
) -> tuple[int, int]:
    """Return the size this image should leave at, given the destination.

    ``max_long_edge`` of 0 is the operator saying "send what the client sent",
    and it wins over everything below it -- including the family's own token
    budget, which is why the off switch is genuinely off rather than merely
    looser.

    For the Anthropic family, and for a host that publishes no formula (which
    is charged Anthropic's), both the pixel cap *and* the token budget bind,
    and the token one is the tighter of the two on a wide image. For every
    other family the token budget is not a resize rule the host publishes, so
    only the pixel cap applies and the estimate reflects whatever arrives.
    """
    if max_long_edge <= 0 or width <= 0 or height <= 0:
        return (width, height)
    if _token_budget_binds(family):
        return anthropic_fit(
            width, height, max_long_edge=max_long_edge, max_tokens=ANTHROPIC_MAX_TOKENS
        )
    longest = max(width, height)
    if longest <= max_long_edge:
        return (width, height)
    scale = max_long_edge / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def _token_budget_binds(family: str | ImageTokenFamily) -> bool:
    """Whether Anthropic's token budget binds on top of the pixel cap.

    This is the only thing :func:`resize_target` reads from the family, so it
    is also the only thing about the family the shrink memo is keyed by: two
    hosts that resize the same way share one entry, and a host that resizes
    differently can never be handed another's result.
    """
    return resolve_family(family) in (
        ImageTokenFamily.ANTHROPIC,
        ImageTokenFamily.UNKNOWN,
    )


def downscale_request_images(
    request: MessagesRequest,
    *,
    max_long_edge: int,
    family: str | ImageTokenFamily,
    jpeg_quality: int = 0,
) -> tuple[ImageResize, ...]:
    """Shrink every oversized image on *request*, in place, and report what moved.

    The request must already be the router's per-attempt deep copy: this writes
    through to the ``source`` dicts it finds, which is the whole point -- there
    is no second place an outbound image is materialised, so editing the source
    is what makes every dialect see the smaller picture.
    """
    if max_long_edge <= 0:
        return ()
    resizes: list[ImageResize] = []
    seen: list[_Key] = []
    try:
        for index, (kind, source) in enumerate(request_image_sources(request)):
            if not isinstance(source, dict):
                continue
            # A document is a PDF, not a raster the geometry code can resize,
            # and a URL-referenced image has no bytes here to shrink.
            if kind != "image" or source.get("type") != "base64":
                continue
            data = source.get("data")
            if not isinstance(data, str) or not data:
                continue
            resize = _downscale_one(
                source,
                data,
                index=index,
                max_long_edge=max_long_edge,
                family=family,
                jpeg_quality=jpeg_quality,
                seen=seen,
            )
            if resize is not None:
                resizes.append(resize)
    finally:
        if seen:
            _MEMO.settle(seen)
    return tuple(resizes)


def _downscale_one(
    source: dict[Any, Any],
    data: str,
    *,
    index: int,
    max_long_edge: int,
    family: str | ImageTokenFamily,
    jpeg_quality: int,
    seen: list[_Key],
) -> ImageResize | None:
    key = _memo_key(
        data,
        max_long_edge=max_long_edge,
        family=family,
        jpeg_quality=jpeg_quality,
    )
    outcome = None
    if key is not None:
        seen.append(key)
        outcome = _MEMO.get(key)
    if outcome is None:
        # Computed outside every lock: a decode and a LANCZOS pass must never
        # make another caller wait for the memo.
        outcome = _shrink(
            data,
            max_long_edge=max_long_edge,
            family=family,
            jpeg_quality=jpeg_quality,
        )
        if outcome is None:
            return None
        if key is not None:
            _MEMO.put(key, outcome)
    return _apply(outcome, source, index=index, jpeg_quality=jpeg_quality)


def _shrink(
    data: str,
    *,
    max_long_edge: int,
    family: str | ImageTokenFamily,
    jpeg_quality: int,
) -> _Outcome | None:
    """Do the work once: decode, measure, resize, re-encode.

    ``None`` is every failure -- undecodable, unreadable, past the decode
    guard, a resize that raised. Those are never memoised: they are recomputed
    on every rung exactly as they always were, so their log lines and their
    cost are unchanged, and the image is sent untouched.
    """
    raw = decode_base64(data)
    if raw is None:
        return None
    size = raw_dimensions(raw)
    if size is None:
        # Unreadable, oversized past the decode guard, or a codec Pillow does
        # not know. Sent untouched: a request must never fail because its
        # screenshot was odd.
        return None
    width, height = size
    target = resize_target(width, height, max_long_edge=max_long_edge, family=family)
    if target == (width, height):
        return _FITS
    result = downscale(raw, target, jpeg_quality=jpeg_quality)
    if result is None:
        return None
    new_raw, media_type = result
    return _Shrunk(
        data=base64.b64encode(new_raw).decode("ascii"),
        media_type=media_type,
        before_width=width,
        before_height=height,
        after_width=target[0],
        after_height=target[1],
        before_bytes=len(raw),
        after_bytes=len(new_raw),
    )


def _apply(
    outcome: _Outcome,
    source: dict[Any, Any],
    *,
    index: int,
    jpeg_quality: int,
) -> ImageResize | None:
    """Write one outcome into this attempt's copy, the same way every time."""
    if not isinstance(outcome, _Shrunk):
        return None
    if outcome.after_bytes >= outcome.before_bytes and jpeg_quality <= 0:
        # A re-encoded PNG can come out larger than the original a better
        # encoder produced, even at fewer pixels. The smaller *picture* is
        # still the point when the destination bills per pixel, so this is not
        # a refusal -- but it is worth one debug line when it happens.
        logger.debug(
            "Image downscale grew the payload: {} -> {} bytes at {}x{}",
            outcome.before_bytes,
            outcome.after_bytes,
            outcome.after_width,
            outcome.after_height,
        )
    source["data"] = outcome.data
    source["media_type"] = outcome.media_type
    return ImageResize(
        index=index,
        before_width=outcome.before_width,
        before_height=outcome.before_height,
        after_width=outcome.after_width,
        after_height=outcome.after_height,
        before_bytes=outcome.before_bytes,
        after_bytes=outcome.after_bytes,
    )


def _memo_key(
    data: str,
    *,
    max_long_edge: int,
    family: str | ImageTokenFamily,
    jpeg_quality: int,
) -> _Key | None:
    """Return the memo key for one image, or ``None`` to bypass the memo.

    Base64 is ASCII by definition, and the decoder refuses anything else, so a
    non-ASCII string can only ever fail; it skips the memo and fails exactly
    as it always did. Hashing the text rather than the decoded bytes is what
    lets a hit skip the base64 decode as well as the resize.
    """
    if not data.isascii():
        return None
    digest = hashlib.sha256(data.encode("ascii")).digest()
    return (digest, max_long_edge, _token_budget_binds(family), jpeg_quality)


class _ShrinkMemo:
    """Finished shrinks by content, bounded by the bytes they hold.

    The bound is not a number anyone chose. The memo holds at most what the
    largest single request it has served needed at once -- the shrunk copies
    of that request's images, for every target its chain asked for -- because
    that is what makes every rung after the first a hit. Memory therefore
    follows the largest request, never the amount of traffic: a long-running
    server that has seen ten thousand screenshots holds no more than one
    request's worth of them.

    Consecutive calls over the same set of images are one request (its rungs);
    a call over a different set starts the next one. Entries the current
    request has not touched are evicted least recently used first whenever the
    total passes the bound, so the next turn of the busiest conversation keeps
    finding its screenshots, and a conversation nobody is using any more ages
    out.

    One lock guards the bookkeeping only. The decode and the resize happen
    outside it -- two callers that miss on the same image at the same moment
    both compute it, get identical bytes, and the first insert wins.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[_Key, _Outcome] = OrderedDict()
        self._sizes: dict[_Key, int] = {}
        self._held = 0
        self._bound = 0
        self._request_sources: frozenset[bytes] = frozenset()
        self._request_keys: set[_Key] = set()
        self._hits = 0
        self._misses = 0

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._sizes.clear()
            self._held = 0
            self._bound = 0
            self._request_sources = frozenset()
            self._request_keys = set()
            self._hits = 0
            self._misses = 0

    def info(self) -> DownscaleCacheInfo:
        with self._lock:
            return DownscaleCacheInfo(
                hits=self._hits,
                misses=self._misses,
                entries=len(self._entries),
                held_bytes=self._held,
                bound_bytes=self._bound,
            )

    def get(self, key: _Key) -> _Outcome | None:
        with self._lock:
            outcome = self._entries.get(key)
            if outcome is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return outcome

    def put(self, key: _Key, outcome: _Outcome) -> None:
        size = _entry_size(key, outcome)
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return
            self._entries[key] = outcome
            self._sizes[key] = size
            self._held += size

    def settle(self, keys: Sequence[_Key]) -> None:
        """Record one call's images, raise the bound to fit them, evict past it."""
        sources = frozenset(key[0] for key in keys)
        with self._lock:
            if sources != self._request_sources:
                self._request_sources = sources
                self._request_keys = set()
            self._request_keys.update(keys)
            needed = sum(self._sizes.get(key, 0) for key in self._request_keys)
            self._bound = max(self._bound, needed)
            if self._held <= self._bound:
                return
            for key in list(self._entries):
                if self._held <= self._bound:
                    break
                if key in self._request_keys:
                    continue
                del self._entries[key]
                self._held -= self._sizes.pop(key)


def _entry_size(key: _Key, outcome: _Outcome) -> int:
    """The bytes one entry keeps alive, measured rather than guessed."""
    size = sys.getsizeof(key) + sys.getsizeof(key[0]) + sys.getsizeof(outcome)
    if isinstance(outcome, _Shrunk):
        size += sys.getsizeof(outcome.data) + sys.getsizeof(outcome.media_type)
    return size


_MEMO = _ShrinkMemo()


def reset_image_downscale_cache() -> None:
    """Forget every memoised shrink and the bound. For tests."""
    _MEMO.reset()


def image_downscale_cache_info() -> DownscaleCacheInfo:
    """Return the shrink memo's counters, size and bound."""
    return _MEMO.info()
