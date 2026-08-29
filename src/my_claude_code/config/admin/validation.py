"""Settings-backed Admin config validation."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from my_claude_code.config.limits import describe_range, range_for
from my_claude_code.config.settings import Settings

from .manifest import FIELDS, field_input_key


def validate_values(values: Mapping[str, str]) -> tuple[bool, list[str]]:
    """Validate proposed env values against the Settings model."""

    settings, errors = settings_from_values(values)
    return settings is not None, errors


def settings_from_values(
    values: Mapping[str, str],
) -> tuple[Settings | None, list[str]]:
    """Build the prospective Settings snapshot without reading dotenv files.

    A key that is absent from ``values`` is left out of the kwargs entirely so
    Settings supplies its own default -- which is exactly what the running
    server does when it loads a file that has no line for it. Passing ``""``
    instead would push an empty string into every ``bool`` and ``int`` field
    that has no blank validator and fail validation on settings nobody touched.
    """

    kwargs: dict[str, Any] = {"_env_file": None}
    for field in FIELDS:
        input_key = field_input_key(field)
        if input_key is None:
            continue
        if field.key in values:
            kwargs[input_key] = values[field.key]

    out_of_range = range_errors(values)
    if out_of_range:
        # Report the range rather than letting Settings clamp: a form that
        # silently changes what was typed teaches the user nothing.
        return None, out_of_range

    try:
        return Settings(**kwargs), []
    except ValidationError as exc:
        return None, format_validation_errors(exc)


def range_errors(values: Mapping[str, str]) -> list[str]:
    """Return one message per numeric field set outside its usable range."""

    errors: list[str] = []
    for field in FIELDS:
        limit = range_for(field.settings_attr)
        if limit is None:
            continue
        raw = str(values.get(field.key, "")).strip()
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError:
            continue
        if not limit.contains(number):
            errors.append(f"{field.key}: accepts {describe_range(limit)}.")
    return errors


def format_validation_errors(exc: ValidationError) -> list[str]:
    """Return user-readable validation errors from a Pydantic exception."""

    errors: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "Invalid value"))
        errors.append(f"{loc}: {message}" if loc else message)
    return errors
