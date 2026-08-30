"""Every env var Settings reads is settable from the dashboard.

A setting with no admin field is invisible: the only way to change it is to
hand-edit ``~/.fcc/.env``, which the dashboard then rewrites. Four fields had
drifted out of the manifests this way before this test existed
(``WEBSEARCH_DIGEST_CHARS``, ``WEBSEARCH_DIGEST_CONTENT_CHARS``,
``WEBSEARCH_DIGEST_ANSWER``, ``ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE``).
"""

from my_claude_code.config.admin.manifest import env_keys
from my_claude_code.config.settings import Settings

# A nested settings model is not an env var of its own: ``nim`` is populated
# from ``NIM_*`` keys, each of which carries its own manifest field. The group
# name itself can never appear in a manifest, so it is exempt by name rather
# than by a rule that would also excuse a genuinely missing scalar.
NESTED_SETTINGS_GROUPS = frozenset({"NIM"})

# Where a new key belongs, so the failure message can say it instead of
# leaving the author to find out.
_WHERE_TO_ADD = {
    "WEBSEARCH": "config/admin/websearch_manifest.py",
    "WEB_SEARCH": "config/admin/websearch_manifest.py",
    "SEARXNG": "config/admin/websearch_manifest.py",
}


def _env_key(name: str) -> str:
    """Return the env var a Settings field is populated from."""

    field = Settings.model_fields[name]
    alias = field.validation_alias
    return str(alias) if alias else name.upper()


def _suggested_manifest(key: str) -> str:
    for prefix, module in _WHERE_TO_ADD.items():
        if key.startswith(prefix):
            return module
    return (
        "config/admin/provider_manifest.py if it belongs to a provider card, "
        "otherwise config/admin/manifest.py"
    )


def test_every_setting_is_reachable_from_the_dashboard() -> None:
    exposed = env_keys()
    missing = sorted(
        key
        for key in (_env_key(name) for name in Settings.model_fields)
        if key not in exposed and key not in NESTED_SETTINGS_GROUPS
    )
    assert not missing, "\n".join(
        f"{key} has no admin field -- add a ConfigFieldSpec to {_suggested_manifest(key)}"
        for key in missing
    )


def test_the_four_recovered_fields_stay_exposed() -> None:
    """Names the fields this test was written for, so removing one fails loudly.

    The general check above would also catch it, but only as a diff; this one
    says which field and therefore which regression.
    """
    exposed = env_keys()
    for key in (
        "WEBSEARCH_DIGEST_CHARS",
        "WEBSEARCH_DIGEST_CONTENT_CHARS",
        "WEBSEARCH_DIGEST_ANSWER",
        "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE",
    ):
        assert key in exposed, f"{key} was removed from the admin manifests"


def test_the_nested_group_allow_list_only_covers_real_groups() -> None:
    """An allow-list is an escape hatch, so it must not outlive its reason."""

    for group in NESTED_SETTINGS_GROUPS:
        name = group.lower()
        assert name in Settings.model_fields, f"{group} is no longer a Settings field"
        assert hasattr(Settings.model_fields[name].annotation, "model_fields"), (
            f"{group} is no longer a nested settings model and needs a real field"
        )
