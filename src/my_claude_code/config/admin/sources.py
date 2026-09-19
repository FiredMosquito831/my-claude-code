"""Admin config source loading and source precedence.

**The parse cache.** ``dotenv`` is a line-by-line tokeniser written in Python.
On the reporting machine's managed file -- 52 KB, 475 set keys -- one parse
costs **50 ms**, and the template costs another **58 ms**. A single pause click
paid for six of them (``load_value_state`` three times, plus the managed file
again for the target values, the repo file, and the unmanaged read on each of
the two renders), which is where the measured 2.1 s of held event loop came
from: not the render, which is 2.7 ms, and not pydantic, which is 12 ms.

So a parse is memoised **on the bytes it parsed**, never on a timestamp. The
file is still read on every call -- ``read_bytes`` of 52 KB is 0.1 ms and a
16-byte digest of it is 40 us -- so a file that changed by any means, from any
process, at any resolution of any filesystem clock, parses again. There is no
staleness window to reason about, because there is no window: same bytes, same
answer, and ``dotenv_values`` is a pure function of its input.
"""

import hashlib
import os
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

from my_claude_code.config.env_files import (
    explicit_env_path as configured_explicit_env_path,
)
from my_claude_code.config.env_files import (
    repo_env_path as configured_repo_env_path,
)
from my_claude_code.config.env_files import (
    settings_env_files,
)
from my_claude_code.config.env_template import load_env_template_or_empty

from .manifest import FIELDS

SourceType = Literal[
    "default",
    "template",
    "repo_env",
    "managed_env",
    "explicit_env_file",
    "process",
]


def repo_env_path() -> Path:
    """Return the repo-local env path."""

    return configured_repo_env_path()


def explicit_env_path() -> Path | None:
    """Return the explicit MCC_ENV_FILE path, when configured."""

    return configured_explicit_env_path(os.environ)


def configured_env_files() -> tuple[tuple[SourceType, Path], ...]:
    """Return dotenv files in low-to-high precedence order."""

    source_names: tuple[SourceType, ...] = (
        "repo_env",
        "managed_env",
        "explicit_env_file",
    )
    return tuple(zip(source_names, settings_env_files(), strict=False))


#: Parsed layers, keyed on a digest of exactly the bytes (or characters) that
#: produced them. Small and bounded: an install has three env files and one
#: template, and a handful of historic contents of each is all that is ever
#: worth keeping.
_PARSE_CACHE: dict[tuple[str, bytes], dict[str, str]] = {}

#: How many parses to keep. Well above "every layer of one install, before and
#: after a save", well below anything that could be called a leak.
_PARSE_CACHE_MAX = 32


def _cached_parse(
    kind: str, material: bytes, parse: Callable[[], dict[str, str]]
) -> dict[str, str]:
    """Return ``parse()``'s answer for these exact bytes, computing it once.

    ``material`` is the whole input, not a description of it, so a cache hit is
    a statement about content rather than about a clock.
    """

    key = (kind, hashlib.blake2b(material, digest_size=16).digest())
    hit = _PARSE_CACHE.get(key)
    if hit is None:
        hit = parse()
        if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[key] = hit
    # A copy, always: two callers mutate what they are given (the managed
    # layer is the base a save edits), and a shared dict would let one caller's
    # edit reach the next caller's read.
    return dict(hit)


def clear_env_parse_cache() -> None:
    """Forget every memoised parse. Tests, and anything that wants a cold read."""

    _PARSE_CACHE.clear()


def dotenv_values_from_text(text: str) -> dict[str, str]:
    """Parse dotenv text into string values."""

    def parse() -> dict[str, str]:
        values = dotenv_values(stream=StringIO(text))
        return {key: "" if value is None else value for key, value in values.items()}

    return _cached_parse("text", text.encode("utf-8", "surrogatepass"), parse)


def template_values() -> dict[str, str]:
    """Return .env.example values plus manifest defaults for newer fields.

    Read-only display state. This is what a field falls back to when nothing
    sets it, so the dashboard can show the effective value of a field nobody
    has ever touched. It must never seed a write: materialising these into the
    managed file turns every default into a stored user choice, which is how a
    default that later changed could never reach an existing install.
    """

    values = dotenv_values_from_text(load_env_template_or_empty())
    for field in FIELDS:
        values.setdefault(field.key, field.default)
    return values


def dotenv_values_from_file(path: Path) -> dict[str, str]:
    """Return dotenv values from a file, or an empty mapping when absent."""

    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
    except OSError:
        # Unreadable is not the same as absent, but neither is it values: the
        # pre-cache call would have raised out of ``dotenv_values`` here, and
        # nothing downstream was written to expect that either.
        return {}

    def parse() -> dict[str, str]:
        # Parsed from the bytes that were hashed, not from a second read of the
        # path: a cache entry must describe the content it is keyed on, and a
        # file rewritten between the two reads would otherwise file the new
        # answer under the old digest for the life of the process. The decode
        # is what ``io.open(path, encoding="utf-8")`` does inside
        # ``dotenv_values(path)``, newline translation included.
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        values = dotenv_values(stream=StringIO(text))
        return {key: "" if value is None else value for key, value in values.items()}

    return _cached_parse(str(path), raw, parse)


def is_locked_source(source: SourceType) -> bool:
    """Return whether an admin value source must not be overwritten."""

    return source in {"process", "explicit_env_file"}
