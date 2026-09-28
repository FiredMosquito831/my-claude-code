"""A child process that installs the token-host block the way the race children do.

Started by ``tests/test_no_live_token_hosts.py``, by path, with the suite's own
interpreter. Its first statements are the three every script under
``tests/providers/oauth_race_children/`` opens with: put the worktree root on
``sys.path``, import the block, install it. Only then does it ask for
``platform.claude.com``, and it asks only when the block reports itself
installed, so a broken install can never turn into a real lookup.

Prints one JSON line: whether the block is installed, what happened to the
lookup (``refused`` / ``reached`` / ``not-installed``) and the hosts the block
refused in this process.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.token_host_block import install_token_host_block

install_token_host_block()


def main() -> int:
    import json
    import socket

    from tests.support.token_host_block import (
        HermeticityViolation,
        block_is_installed,
        refused_hosts,
    )

    installed = block_is_installed()
    outcome = "not-installed"
    if installed:
        try:
            socket.getaddrinfo("platform.claude.com", 443)
        except HermeticityViolation:
            outcome = "refused"
        else:
            outcome = "reached"
    print(
        json.dumps(
            {
                "installed": installed,
                "outcome": outcome,
                "refused": list(refused_hosts()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
