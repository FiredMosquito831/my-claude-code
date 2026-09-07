"""Header-only geometry, its memo, and the round trip of a downscale."""

import base64
import io

import pytest
from PIL import Image

from my_claude_code.core.image_geometry import (
    downscale,
    image_dimensions,
    image_geometry_cache_info,
    raw_dimensions,
    reset_image_geometry_cache,
)


def encode(image: Image.Image, image_format: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def png(width: int, height: int, mode: str = "RGB") -> bytes:
    return encode(Image.new(mode, (width, height), "red" if mode == "RGB" else None))


@pytest.mark.parametrize(
    "image_format",
    ["PNG", "JPEG", "WEBP", "GIF"],
)
def test_dimensions_read_without_decoding_pixels(image_format):
    raw = encode(Image.new("RGB", (321, 123), "blue"), image_format)
    assert raw_dimensions(raw) == (321, 123)


def test_dimensions_none_for_garbage():
    assert raw_dimensions(b"not an image at all") is None
    assert image_dimensions("!!!! not base64 and not an image") is None


def test_dimensions_memoised_for_repeated_image():
    reset_image_geometry_cache()
    data = base64.b64encode(png(640, 480)).decode("ascii")
    assert image_dimensions(data) == (640, 480)
    assert image_dimensions(data) == (640, 480)
    hits, misses, _ = image_geometry_cache_info()
    assert (hits, misses) == (1, 1)


def test_unreadable_image_is_memoised_as_unknown():
    """The failure is cached too, or a broken upload is re-parsed every turn."""
    reset_image_geometry_cache()
    data = base64.b64encode(b"still not an image").decode("ascii")
    assert image_dimensions(data) is None
    assert image_dimensions(data) is None
    hits, misses, _ = image_geometry_cache_info()
    assert (hits, misses) == (1, 1)


def test_downscale_round_trip_preserves_format_and_aspect():
    raw = png(1920, 1080)
    result = downscale(raw, (1456, 819))
    assert result is not None
    smaller, media_type = result
    assert media_type == "image/png"
    with Image.open(io.BytesIO(smaller)) as image:
        assert image.size == (1456, 819)
        assert image.format == "PNG"


def test_downscale_preserves_alpha():
    source = Image.new("RGBA", (2000, 1000), (255, 0, 0, 128))
    result = downscale(encode(source), (1000, 500))
    assert result is not None
    smaller, media_type = result
    assert media_type == "image/png"
    with Image.open(io.BytesIO(smaller)) as image:
        assert image.mode == "RGBA"
        pixel = image.getpixel((10, 10))
        assert isinstance(pixel, tuple)
        assert pixel[3] == 128


def test_downscale_keeps_alpha_even_when_jpeg_is_requested():
    """Quality is an opt-in for size, not permission to flatten transparency."""
    source = Image.new("RGBA", (2000, 1000), (0, 255, 0, 64))
    result = downscale(encode(source), (1000, 500), jpeg_quality=85)
    assert result is not None
    smaller, media_type = result
    assert media_type == "image/png"
    with Image.open(io.BytesIO(smaller)) as image:
        assert image.mode == "RGBA"


def test_downscale_reencodes_to_jpeg_when_asked_and_there_is_no_alpha():
    result = downscale(png(2000, 1000), (1000, 500), jpeg_quality=85)
    assert result is not None
    smaller, media_type = result
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(smaller)) as image:
        assert image.format == "JPEG"
        assert image.size == (1000, 500)


def test_downscale_returns_none_for_something_it_cannot_read():
    assert downscale(b"not an image", (10, 10)) is None
