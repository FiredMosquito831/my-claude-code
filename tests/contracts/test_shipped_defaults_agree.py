"""Every shipped default is one number, written in three places that agree.

A setting's default is stated in three places a user can read: the code
(``constants.py`` / the ``Settings`` field), the template the install
instructions hand them (``.env.example``), and the admin form (the manifest).
Nothing pinned the first two together except two hand-curated lists in
``test_architecture_contracts.py`` covering twelve keys between them, and by
6.67.0 **thirty-eight** non-secret keys had drifted: the template recommended
``HTTP_READ_TIMEOUT=300`` while a server started without a ``.env`` used 120,
``FALLBACK_STALL_TIMEOUT=180`` while the code used 0, and so on. An operator
who copied the template got one proxy, an operator who did not got another,
and the release notes for neither ever said so.

The thirty-eighth is why this test asks the manifest which fields are secret
instead of matching their names: the survey that found the other thirty-seven
skipped every key containing "TOKEN", so ``FALLBACK_FIRST_TOKEN_TIMEOUT``
(template 180, code 0) hid behind a rule meant for credentials. This test
found it on its first honest run.

6.68.0 reconciled all thirty-eight and this test is what keeps them
reconciled. The manifest leg is
``test_every_manifest_default_matches_the_settings_default``
(``tests/config/test_admin_limits.py``); this is the template leg.
"""

from pathlib import Path
from typing import Any

import pytest

from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _shipped_template() -> dict[str, str]:
    """The assignments in ``.env.example``, comments and blanks dropped."""

    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    shipped: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier() or not key.isupper():
            continue
        shipped[key] = value.strip()
    return shipped


def _aliases(name: str, field: Any) -> list[str]:
    alias = field.validation_alias
    if alias is None:
        # pydantic-settings upper-cases the field name when no alias is given,
        # which is how HOST and PORT are read.
        return [name.upper()]
    if isinstance(alias, str):
        return [alias]
    return [
        choice for choice in getattr(alias, "choices", []) if isinstance(choice, str)
    ]


def _code_defaults() -> dict[str, Any]:
    """Every env name a ``Settings`` field answers to, and its code default."""

    defaults: dict[str, Any] = {}
    for name, field in Settings.model_fields.items():
        for alias in _aliases(name, field):
            defaults[alias] = field.default
    return defaults


#: Fields the manifest itself declares to be credentials. Asked of the manifest
#: rather than matched against the key's name, because a name match on "TOKEN"
#: silently excludes MAX_OUTPUT_TOKENS_UNKNOWN_DEFAULT and
#: FALLBACK_FIRST_TOKEN_TIMEOUT -- neither of which is a credential, and the
#: second of which had drifted (the template said 180, the code said 0) behind
#: exactly that hole while this test reported green.
_MANIFEST_SECRETS = frozenset(
    field.key for field in FIELDS if field.field_type == "secret" or field.secret
)

#: Suffixes and prefixes that make a value the operator's own rather than a
#: shipped default: an endpoint, an outbound proxy, a model chain, a spend
#: choice. The template ships these blank or as a placeholder on purpose.
_PERSONAL_SHAPES = ("_BASE_URL", "_PROXY", "_FALLBACKS", "_PAUSED", "_ROTATION")


def _is_personal(key: str) -> bool:
    if key in _MANIFEST_SECRETS:
        return True
    if any(key.endswith(shape) for shape in _PERSONAL_SHAPES):
        return True
    # Model chains and the per-profile reasoning choices: the user's own
    # routing and spend, never a shipped default.
    return key.startswith("MODEL") or key.startswith("REASONING_")


#: The only keys allowed to differ, each with the reason it must. Every entry
#: is a promise that the difference is deliberate; the test fails when a listed
#: key starts agreeing, so the list cannot rot into a pile of stale excuses.
#:
#: It is EMPTY, and 6.68.0 shipped it empty on purpose: the thirty-eight keys
#: that disagreed were reconciled rather than excused. An entry here is a
#: shipped default a user reads two different numbers for, so adding one needs
#: a reason that survives being written down next to the key.
DELIBERATE_DIFFERENCES: dict[str, str] = {}


def _agree(shipped: str, default: Any) -> bool:
    """Is the template text the same value as the code default?"""

    text = shipped.strip().strip('"').strip("'")
    if default is None:
        return text == ""
    if isinstance(default, bool):
        return text.lower() == ("true" if default else "false")
    if isinstance(default, int | float):
        try:
            return float(text) == float(default)
        except ValueError:
            return False
    if isinstance(default, (list, tuple)):
        return text == ",".join(str(item) for item in default)
    # Enums (ReasoningPreference and friends) compare by their value.
    return text == str(getattr(default, "value", default))


_TEMPLATE = _shipped_template()
_DEFAULTS = _code_defaults()
_CHECKED = sorted(
    key for key in _TEMPLATE if key in _DEFAULTS and not _is_personal(key)
)


def test_the_template_covers_a_real_share_of_the_settings() -> None:
    """Guards the guard: a parser that silently matched nothing would pass."""

    assert len(_CHECKED) > 100, (
        f"only {len(_CHECKED)} keys were compared; the .env.example parser or "
        "the Settings alias walk has stopped working"
    )


@pytest.mark.parametrize("key", _CHECKED)
def test_the_template_ships_the_code_default(key: str) -> None:
    """A template that recommends a value the server does not use is a lie."""

    agree = _agree(_TEMPLATE[key], _DEFAULTS[key])
    reason = DELIBERATE_DIFFERENCES.get(key)
    if reason is None:
        assert agree, (
            f"{key} ships {_TEMPLATE[key]!r} in .env.example but the code "
            f"default is {_DEFAULTS[key]!r}. Reconcile them -- or, if the "
            "difference is deliberate, record it in DELIBERATE_DIFFERENCES "
            "with the reason."
        )
        return
    assert not agree, (
        f"{key} now matches the code default -- remove it from "
        f"DELIBERATE_DIFFERENCES ({reason})"
    )


@pytest.mark.parametrize("key", sorted(DELIBERATE_DIFFERENCES))
def test_every_recorded_difference_is_still_a_real_setting(key: str) -> None:
    """A reason recorded for a key nobody reads any more is worse than none."""

    assert key in _DEFAULTS, f"{key} is no longer a Settings alias"
