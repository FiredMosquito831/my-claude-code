"""Adding an identity for two hosts must not move one byte for the other 39.

6.69.0 gave the OpenCode profiles a declared client identity. The seam it used
-- ``default_headers`` on the SDK client, ``extra_headers`` on the create call
-- is shared by every profile in the family, which is exactly why this file
exists: a seam that is shared is a seam that can leak.

The baseline in ``outbound_identity_baseline.json`` was recorded from a
worktree at ``a6ca3e42`` (v6.68.2, the commit this branch left from), by
building every profile through the same factory and asking the openai SDK to
render one request. Only header *names* are stored: the values include the
SDK's own version, the interpreter's version and the OS, none of which are
properties of this repository and all of which would make the fixture fail on
a runner that is merely different rather than wrong.
"""

import json
from pathlib import Path

import pytest
from openai._models import FinalRequestOptions

from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.opencode_identity import (
    OPENCODE_HEADER_ORDER,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

BASELINE = json.loads(
    (Path(__file__).parent / "outbound_identity_baseline.json").read_text(
        encoding="utf-8"
    )
)

#: The four headers the two OpenCode profiles are allowed to add. ``User-Agent``
#: is not among them: it is a name the SDK already sent, so the identity
#: changes its *value* without changing the set.
ADDED_NAMES = frozenset(
    name.lower() for name in OPENCODE_HEADER_ORDER if name.lower() != "user-agent"
)

IDENTIFIED = frozenset({"opencode", "opencode_go"})


def _header_names(provider_id: str) -> frozenset[str]:
    provider = create_openai_chat_provider(
        provider_id,
        ProviderConfig(
            api_key="sk-contract-key", base_url="https://example.invalid/v1"
        ),
        ProviderRateLimiter(rate_limit=1, rate_window=60),
    )
    request = provider._client._build_request(
        FinalRequestOptions.construct(
            method="post",
            url="/chat/completions",
            json_data={"model": "m", "messages": []},
        )
    )
    return frozenset(name.lower() for name in request.headers)


#: Profiles added after the recording, each mapped to the recorded profile
#: whose header names it must send exactly. The baseline file itself is a
#: frozen recording and is not rewritten; a new profile that declares no
#: identity is held to the set a recorded identity-less profile sends, which
#: is the same strength of check an entry in the file would give it.
ADDED_SINCE_RECORDING: dict[str, str] = {
    # 7.58.0: B.AI, the generic custom-provider profile under its own name.
    "bai": "hypercharm",
}


def _recorded(provider_id: str) -> frozenset[str]:
    return frozenset(BASELINE[ADDED_SINCE_RECORDING.get(provider_id, provider_id)])


def test_the_baseline_still_covers_every_profile() -> None:
    """A profile added since the recording would slip through unchecked."""
    assert set(BASELINE).isdisjoint(ADDED_SINCE_RECORDING)
    assert set(BASELINE) | set(ADDED_SINCE_RECORDING) == set(OPENAI_CHAT_PROFILES)
    for provider_id, recorded_as in ADDED_SINCE_RECORDING.items():
        assert provider_id not in IDENTIFIED
        assert recorded_as in BASELINE
        assert recorded_as not in IDENTIFIED


@pytest.mark.parametrize("provider_id", sorted(OPENAI_CHAT_PROFILES))
def test_no_profile_sends_a_header_it_did_not_declare(provider_id: str) -> None:
    before = _recorded(provider_id)
    after = _header_names(provider_id)
    expected = before | ADDED_NAMES if provider_id in IDENTIFIED else before
    assert after == expected, (
        f"{provider_id} outbound header names changed; only a profile that "
        "declares a client_identity may add one"
    )


@pytest.mark.parametrize(
    "provider_id", sorted(set(OPENAI_CHAT_PROFILES) - set(IDENTIFIED))
)
def test_a_profile_without_an_identity_is_built_exactly_as_before(
    provider_id: str,
) -> None:
    """Not "sends the same headers" -- is handed the same argument.

    The factory used to pass no ``default_headers`` at all. A profile that
    declares nothing must still pass ``None``, not an empty mapping: the two
    are the same on the wire today and would stop being the same the first
    time somebody merges into it.
    """
    assert OPENAI_CHAT_PROFILES[provider_id].client_identity is None
