"""Neutral Admin manifest spec types shared by manifest generators."""

from dataclasses import dataclass
from typing import Literal

FieldType = Literal[
    "text",
    "secret",
    "number",
    "boolean",
    "model",
    "optional_model",
    "model_chain",
    "select",
    "textarea",
    "oauth_login",
]


@dataclass(frozen=True, slots=True)
class ConfigSectionSpec:
    """A group of config fields rendered together in the admin UI."""

    section_id: str
    label: str
    description: str
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class ConfigOptionSpec:
    """A persisted option value and its user-facing label."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ConfigFieldSpec:
    """Typed metadata for one env-backed admin setting."""

    key: str
    label: str
    section_id: str
    field_type: FieldType = "text"
    settings_attr: str | None = None
    # Provider this field belongs to, for fields in the ``providers`` section.
    # Empty for everything else, so the Admin UI can group a provider's key,
    # base URL, proxy and rotation fields together instead of listing them flat.
    provider: str = ""
    default: str = ""
    options: tuple[str | ConfigOptionSpec, ...] = ()
    secret: bool = False
    advanced: bool = False
    restart_required: bool = False
    session_sensitive: bool = False
    description: str = ""
    # Inclusive bounds for a numeric field, published so the browser can refuse
    # a value before it is saved instead of the server clamping it afterwards.
    minimum: float | None = None
    maximum: float | None = None
    # Human form of the numeric range, e.g. "0 to 3600 (0 waits indefinitely)".
    # Published so the browser can show the bound as a hint beside the input
    # instead of appending it to the end of the help text.
    range_hint: str = ""
    # Whether changing this field can alter a provider client or its
    # catalogue. Defaults True: an unknown or newly added key rebuilds the
    # provider generation and re-sweeps discovery, which is slow but correct.
    # Only a field the provider layer demonstrably never reads sets this
    # False -- see ``update_affects_providers``.
    affects_providers: bool = True
