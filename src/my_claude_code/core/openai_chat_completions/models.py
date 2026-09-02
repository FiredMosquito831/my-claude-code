"""Pydantic models for OpenAI Chat Completions-compatible ingress."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ChatCompletionMessage(BaseModel):
    """One entry of the ``messages`` array.

    Deliberately permissive: ``role`` is a plain string rather than a Literal
    so that an unknown role produces this adapter's own 400 naming the role,
    not a pydantic validation error listing every accepted value. ``content``
    is ``Any`` because the same field is a string, a list of parts, or ``null``
    (on an assistant turn that only called tools) depending on the role.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    #: The de-facto field OpenAI-compatible clients use to carry thinking back
    #: into a follow-up turn. MCC emits it on the way out, so it accepts it on
    #: the way in.
    reasoning_content: str | None = None


class ChatCompletionStreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_usage: bool | None = None


class OpenAIChatCompletionRequest(BaseModel):
    """Permissive subset of the OpenAI Chat Completions request shape."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatCompletionMessage]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    n: int | None = None
    stream: bool | None = None
    stream_options: ChatCompletionStreamOptions | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    reasoning_effort: str | None = None
    response_format: dict[str, Any] | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    metadata: dict[str, Any] | None = None
    user: str | None = None

    @property
    def wants_usage(self) -> bool:
        """Whether a streaming client asked for the trailing usage chunk."""
        options = self.stream_options
        return options is not None and bool(options.include_usage)
