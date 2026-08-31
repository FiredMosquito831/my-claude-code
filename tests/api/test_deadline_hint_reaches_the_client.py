"""The hint is only worth anything if Claude Code actually receives it.

Every other test of this feature asserts the string a helper builds. This one
starts at a provider that says nothing, lets the executor's own first-token
deadline end it, and reads what comes back out of ``/v1/messages`` -- through
the failure mapping, the wire-error builder and ``redact_sensitive_error_text``
-- because that is the path where a hint gets truncated, escaped or eaten.
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config.settings import Settings
from tests.api.support import create_test_app

EXPECTED_HINT = (
    "(FALLBACK_FIRST_TOKEN_TIMEOUT -- change it on the dashboard under "
    "Limits & Resilience -> Deadlines)"
)


class SilentProvider:
    """Accepts the request and produces nothing, which is the failure shape."""

    def __init__(self) -> None:
        self.preflight_stream = MagicMock()

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(
        self, _request: object, **_kwargs: Any
    ) -> AsyncIterator[str]:
        await anyio.sleep(30)
        yield "event: message_stop\ndata: {}\n\n"


def _settings() -> Settings:
    # Only the first-token deadline is set: everything else stays at the
    # shipped zero, so this also proves one knob is enough to get a bounded
    # failure back out of an all-zero install.
    # Env values arrive as strings and the model coerces them, which a
    # precisely-typed kwargs call cannot express.
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "FALLBACK_FIRST_TOKEN_TIMEOUT": "0.05",
    }
    return Settings(**kwargs)


def _payload(*, stream: bool) -> dict[str, object]:
    return {
        "model": "nvidia_nim/test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 32,
        "stream": stream,
    }


@pytest.mark.parametrize("stream", [True, False])
def test_a_deadline_error_reaches_the_client_naming_its_own_knob(
    stream: bool,
) -> None:
    app = create_test_app(_settings())
    provider = SilentProvider()

    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post("/v1/messages", json=_payload(stream=stream))

    assert response.status_code == 504
    message = response.json()["error"]["message"]

    assert "produced no output within" in message
    assert EXPECTED_HINT in message
    # Trailing, so the sentence still reads as a statement of fact first.
    assert message.endswith(EXPECTED_HINT)


def test_the_hint_survives_the_redaction_pass_intact() -> None:
    """Redaction rewrites anything shaped like a credential; this is not one.

    Asserted on the wire rather than on the helper, because the redaction runs
    inside the error builder and nowhere the unit test can see.
    """
    app = create_test_app(_settings())
    provider = SilentProvider()

    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post("/v1/messages", json=_payload(stream=False))

    message = response.json()["error"]["message"]
    assert "<redacted>" not in message
    assert "FALLBACK_FIRST_TOKEN_TIMEOUT" in message
    # ASCII the whole way: no transport in this path has to guess an encoding.
    message.encode("ascii")
