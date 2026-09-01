"""Atomic TOML writes, and the credential substitution Kimi Code needs.

``config/atomic_json.py`` is the mechanism every generated harness config used
until now, because every one of them was JSON. Kimi Code's ``config.toml`` is
not, and its loader (``kimi_cli.config.load_config``) picks the parser off the
file's suffix -- ``.toml`` goes to ``tomlkit``, ``.json`` to ``json`` -- so a
document named ``config.toml`` has to *be* TOML.

**Why an emitter rather than a library.** ``tomllib`` reads TOML and does not
write it, and the third-party writers are a dependency this package does not
otherwise need. What MCC emits is also far narrower than TOML: a mapping whose
leaves are strings, integers, floats, booleans and lists of those. The
:func:`toml_document_bytes` below covers exactly that and raises on anything
else, which is the behaviour worth having -- a silently mis-encoded config is
a model picker that is empty for reasons nobody can see.

**Why no merge writer.** The other half of this module's job in an earlier
draft was to merge MCC's tables into the user's own ``config.toml`` the way
``config/harness_config_merge.py`` merges one key into Command Code's
``providers.json``. It is not needed: ``kimi --config-file PATH`` names the
config document outright, so MCC owns a file of its own under ``~/.fcc`` and
the user's ``~/.kimi/config.toml`` is never read, written or backed up. A TOML
merge writer that preserved comments and layout byte-for-byte would have been
the single most delicate thing in this package, and the flag makes it
unnecessary.

The bytes are deterministic and LF-terminated for the same reason the JSON
writer's are: the write-if-changed comparison is a byte compare, and a
platform-dependent line ending would make every launch rewrite the file.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from my_claude_code.config.atomic_json import FCC_TEMP_SUFFIX
from my_claude_code.config.harnesses import (
    KIMI_API_KEY_SENTINEL,
    KIMI_BASE_URL_SENTINEL,
)

#: Keys that may be written bare. Everything else is quoted, which is most of
#: what MCC emits: a model key like ``mcc/openrouter/anthropic/claude-sonnet``
#: carries slashes and would not parse bare.
_BARE_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def toml_document_bytes(data: Mapping[str, object]) -> bytes:
    """Return the exact bytes :func:`write_toml_document_atomically` would write."""

    lines: list[str] = []
    _emit_table(lines, (), data)
    text = "\n".join(lines)
    return (text + "\n" if text else "").encode("utf-8")


def write_toml_document_atomically(path: Path, data: Mapping[str, object]) -> None:
    """Write ``data`` as TOML to ``path``, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + FCC_TEMP_SUFFIX)
    try:
        # Bytes, not text, so Windows does not translate "\n" to CRLF and
        # defeat the content compare below.
        tmp_path.write_bytes(toml_document_bytes(data))
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def write_toml_document_atomically_if_changed(
    path: Path, data: Mapping[str, object]
) -> bool:
    """Write ``data`` only when it differs from what is already on disk."""

    content = toml_document_bytes(data)
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    write_toml_document_atomically(path, data)
    return True


def with_kimi_credentials(
    document: Mapping[str, object], *, proxy_root_url: str, api_key: str
) -> dict[str, object]:
    """Return the Kimi document with its two placeholders resolved.

    Both values are the caller's to supply: the serialiser is a pure function
    of the model records and knows neither the port this install listens on
    nor the proxy token. Nothing else in the document is touched, and a
    document whose placeholders are already resolved is returned unchanged --
    which is what makes calling this twice harmless.
    """

    resolved = _deep_copy_mapping(document)
    providers = resolved.get("providers")
    if not isinstance(providers, Mapping):
        return resolved
    rewritten: dict[str, object] = {}
    for name, value in providers.items():
        entry: dict[str, object] = (
            {str(key): item for key, item in value.items()}
            if isinstance(value, Mapping)
            else {}
        )
        if not entry:
            rewritten[str(name)] = value
            continue
        if entry.get("base_url") == KIMI_BASE_URL_SENTINEL:
            entry["base_url"] = kimi_base_url(proxy_root_url)
        if entry.get("api_key") == KIMI_API_KEY_SENTINEL:
            entry["api_key"] = api_key
        rewritten[str(name)] = entry
    resolved["providers"] = rewritten
    return resolved


def kimi_base_url(proxy_root_url: str) -> str:
    """Return the base URL Kimi Code's Anthropic client wants: the proxy *root*.

    This is the opposite of what the OpenCode family and Command Code need, and
    the difference is the SDK underneath rather than anything about MCC.
    ``kosong.contrib.chat_provider.anthropic.Anthropic`` hands ``base_url``
    straight to ``anthropic.AsyncAnthropic``, the *official* Anthropic SDK,
    whose message route is already ``/v1/messages`` -- so a base URL ending in
    ``/v1`` produces ``POST /v1/v1/messages`` and a 404 with no hint as to
    which half is wrong. The other harnesses go through Vercel's
    ``@ai-sdk/anthropic``, which appends a bare ``/messages`` and therefore
    does want the ``/v1``.

    Measured, not reasoned: a launch against a scratch proxy logged exactly
    that ``POST /v1/v1/messages 404``, which is why any trailing ``/v1`` is
    stripped here rather than appended.
    """

    stripped = proxy_root_url.rstrip("/")
    return stripped.removesuffix("/v1").rstrip("/")


def _deep_copy_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _deep_copy(item) for key, item in value.items()}


def _deep_copy(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _deep_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_copy(item) for item in value]
    return value


def _emit_table(
    lines: list[str], path: tuple[str, ...], table: Mapping[str, object]
) -> None:
    """Emit one table: its scalar keys first, then each sub-table in turn.

    Scalars before sub-tables is not cosmetic. In TOML every key/value pair
    after a ``[header]`` belongs to that header, so a scalar written after a
    nested table would silently land inside it.
    """

    scalars: list[tuple[str, object]] = []
    tables: list[tuple[str, Mapping[str, object]]] = []
    for key, item in table.items():
        if isinstance(item, Mapping):
            tables.append((str(key), {str(k): v for k, v in item.items()}))
        else:
            scalars.append((str(key), item))

    if path and (scalars or not tables):
        if lines:
            lines.append("")
        lines.append(f"[{'.'.join(_format_key(part) for part in path)}]")
    for key, item in scalars:
        lines.append(f"{_format_key(key)} = {_format_value(item)}")
    for key, nested in tables:
        _emit_table(lines, (*path, key), nested)


def _format_key(key: object) -> str:
    text = str(key)
    if text and all(char in _BARE_KEY_CHARS for char in text):
        return text
    return _format_string(text)


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _format_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} as TOML")


def _format_string(value: str) -> str:
    out = []
    for char in value:
        escape = _ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'
