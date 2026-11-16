"""Shared non-mutating option mapping and additive metadata helpers for adapters."""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from my_claude_code.core.websearch.models import WebSearchRequestOptions

TIME_RANGE_TO_BRAVE: dict[str, str] = {
    "day": "pd",
    "week": "pw",
    "month": "pm",
    "year": "py",
}
TIME_RANGE_TO_DDGS: dict[str, str] = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}
TIME_RANGE_TO_TBS: dict[str, str] = {
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}
TIME_RANGE_TO_SEARCHAPI: dict[str, str] = {
    "day": "last_day",
    "week": "last_week",
    "month": "last_month",
    "year": "last_year",
}
TIME_RANGE_TO_SEARXNG: dict[str, str] = {
    "day": "day",
    "week": "month",
    "month": "month",
    "year": "year",
}
TIME_RANGE_DAYS: dict[str, int] = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}

DEPTH_TO_EXA: dict[str, str] = {
    "fast": "fast",
    "basic": "auto",
    "deep": "deep",
}
DEPTH_TO_TAVILY: dict[str, str] = {
    "fast": "fast",
    "basic": "basic",
    "deep": "advanced",
}
DEPTH_TO_LINKUP: dict[str, str] = {
    "fast": "fast",
    "basic": "standard",
    "deep": "deep",
}
DEPTH_TO_PARALLEL: dict[str, str] = {
    "fast": "turbo",
    "basic": "basic",
    "deep": "advanced",
}
DEPTH_TO_PERPLEXITY: dict[str, str] = {
    "fast": "low",
    "basic": "medium",
    "deep": "high",
}


def text_str(value: Any) -> str:
    """Coerce optional string value to ``str``, returning empty string when absent."""
    return value if isinstance(value, str) else ""


def opt_str(value: Any) -> str | None:
    """Return non-empty string or ``None``."""
    return value if isinstance(value, str) and value else None


def opt_float(value: Any) -> float | None:
    """Coerce int/float or numeric string into finite float, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            number = float(stripped)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def opt_seconds_to_ms(value: Any) -> float | None:
    """Convert provider duration reported in seconds into milliseconds."""
    seconds = opt_float(value)
    if seconds is None:
        return None
    return round(seconds * 1000.0, 3)


def opt_str_tuple(value: Any) -> tuple[str, ...] | None:
    """Return tuple of non-empty string entries or ``None`` when absent/empty."""
    if not isinstance(value, list):
        return None
    entries = tuple(part for part in value if isinstance(part, str) and part)
    return entries or None


def opt_float_tuple(value: Any) -> tuple[float, ...] | None:
    """Return tuple of finite float entries or ``None`` when absent/empty."""
    if not isinstance(value, list):
        return None
    numbers: list[float] = []
    for entry in value:
        parsed = opt_float(entry)
        if parsed is None:
            continue
        numbers.append(parsed)
    return tuple(numbers) or None


def opt_images(value: Any) -> tuple[str | dict[str, Any], ...] | None:
    """Return normalized top-level image entries or ``None`` when absent/empty."""
    if not isinstance(value, list):
        return None
    cleaned: list[str | dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, str) and entry:
            cleaned.append(entry)
        elif isinstance(entry, dict) and entry:
            cleaned.append(dict(entry))
    return tuple(cleaned) or None


def time_range_start_iso(time_range: str | None) -> str | None:
    """Convert ``day|week|month|year`` relative window into UTC ``YYYY-MM-DD``."""
    if not time_range:
        return None
    days = TIME_RANGE_DAYS.get(time_range)
    if days is None:
        return None
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def iso_to_mmddyyyy(value: str | None) -> str | None:
    """Convert ISO ``YYYY-MM-DD[...]`` string to ``MM/DD/YYYY`` for Perplexity."""
    if not value:
        return None
    head = value[:10]
    parts = head.split("-")
    if len(parts) != 3:
        return None
    year, month, day = parts
    if not (len(year) == 4 and len(month) == 2 and len(day) == 2):
        return None
    return f"{month}/{day}/{year}"


def effective_option(
    request_options: WebSearchRequestOptions | None,
    attr: str,
) -> Any:
    """Read one override field from optional ``WebSearchRequestOptions``."""
    if request_options is None:
        return None
    return getattr(request_options, attr, None)
