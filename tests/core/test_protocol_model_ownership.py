"""Protocol models live with the protocol logic that consumes them."""

import subprocess
import sys

from my_claude_code.core.anthropic import (
    MessagesRequest as PublicMessagesRequest,
)
from my_claude_code.core.anthropic import (
    MessagesResponse,
    TokenCountResponse,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.openai_chat_completions import (
    OpenAIChatCompletionRequest as PublicOpenAIChatCompletionRequest,
)
from my_claude_code.core.openai_chat_completions.models import (
    OpenAIChatCompletionRequest,
)
from my_claude_code.core.openai_responses import (
    OpenAIResponsesRequest as PublicOpenAIResponsesRequest,
)
from my_claude_code.core.openai_responses.models import OpenAIResponsesRequest


def test_anthropic_request_model_is_core_owned_and_permissive() -> None:
    request = MessagesRequest.model_validate(
        {
            "model": "provider-model",
            "messages": [{"role": "user", "content": "hello"}],
            "provider_extension": {"enabled": True},
        }
    )

    assert MessagesRequest.__module__ == "my_claude_code.core.anthropic.models"
    assert PublicMessagesRequest is MessagesRequest
    assert request.model_extra == {"provider_extension": {"enabled": True}}


def test_responses_request_model_is_core_owned_and_permissive() -> None:
    request = OpenAIResponsesRequest.model_validate(
        {
            "model": "provider-model",
            "input": "hello",
            "provider_extension": {"enabled": True},
        }
    )

    assert (
        OpenAIResponsesRequest.__module__
        == "my_claude_code.core.openai_responses.models"
    )
    assert PublicOpenAIResponsesRequest is OpenAIResponsesRequest
    assert request.model_extra == {"provider_extension": {"enabled": True}}


def test_chat_completions_request_model_is_core_owned_and_permissive() -> None:
    request = OpenAIChatCompletionRequest.model_validate(
        {
            "model": "provider-model",
            "messages": [{"role": "user", "content": "hello"}],
            "provider_extension": {"enabled": True},
        }
    )

    assert (
        OpenAIChatCompletionRequest.__module__
        == "my_claude_code.core.openai_chat_completions.models"
    )
    assert PublicOpenAIChatCompletionRequest is OpenAIChatCompletionRequest
    assert request.model_extra == {"provider_extension": {"enabled": True}}


def test_anthropic_response_models_are_protocol_owned() -> None:
    assert MessagesResponse.__module__ == "my_claude_code.core.anthropic.models"
    assert TokenCountResponse.__module__ == "my_claude_code.core.anthropic.models"


def test_protocol_facades_are_import_order_independent() -> None:
    import_orders = (
        (
            "my_claude_code.core.anthropic",
            "my_claude_code.core.openai_responses",
        ),
        (
            "my_claude_code.core.openai_responses",
            "my_claude_code.core.anthropic",
        ),
        (
            "my_claude_code.core.openai_chat_completions",
            "my_claude_code.core.openai_responses",
            "my_claude_code.core.anthropic",
        ),
        (
            "my_claude_code.core.anthropic",
            "my_claude_code.core.openai_chat_completions",
        ),
        (
            "my_claude_code.core.openai_chat_completions",
            "my_claude_code.core.anthropic",
        ),
    )

    for modules in import_orders:
        script = "; ".join(f"import {module}" for module in modules)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
