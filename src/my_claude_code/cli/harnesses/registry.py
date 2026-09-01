"""Bind a registered harness to the shared launcher behaviour.

Every launcher used to restate its own binary name, display name and install
hint as module constants. They are registry fields now, so ``mcc-help``, the
dashboard card and the exit-127 message a user actually sees cannot disagree
about what to install.

The never-install rule is enforced here, in exactly one place:
:func:`resolve_harness_binary` looks the executable up with ``shutil.which``
and, when it is absent, prints the vendor's own install line and exits 127. MCC
does not fetch, download or run a package manager for a third-party CLI --
bundling agents broke installs once, and the installer's own contract test
(``test_installers_never_install_a_third_party_cli``) now encodes the rule for
the shell side.
"""

import shutil
import sys

from my_claude_code.cli.launchers.common import resolve_client_binary
from my_claude_code.config.harnesses import HarnessSpec, harness_spec


def spec_for(harness_id: str) -> HarnessSpec:
    """Return the registered spec for one harness id."""

    return harness_spec(harness_id)


def install_hint(spec: HarnessSpec, platform: str | None = None) -> str:
    """Return the install line to print when the harness binary is missing."""

    return spec.install_hint_for(platform or sys.platform)


def resolve_harness_binary(spec: HarnessSpec) -> str:
    """Resolve a harness executable, or exit 127 with the vendor's own hint.

    Aliases are consulted only after the canonical name misses, so the error
    message and the exit code stay exactly what they were for every harness
    that declares none.
    """

    if spec.binary_aliases and shutil.which(spec.binary) is None:
        for alias in spec.binary_aliases:
            resolved = shutil.which(alias)
            if resolved is not None:
                return resolved
    return resolve_client_binary(
        binary_name=spec.binary,
        display_name=spec.display_name,
        install_hint=install_hint(spec),
    )
