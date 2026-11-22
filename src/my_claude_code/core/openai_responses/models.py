"""Pydantic models for OpenAI Responses-compatible ingress."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class OpenAIResponsesRequest(BaseModel):
    """Permissive subset of the OpenAI Responses API request shape."""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any = None
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    parallel_tool_calls: bool | None = None
    stream: bool | None = True
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    metadata: dict[str, Any] | None = None
    reasoning: dict[str, Any] | None = None
    previous_response_id: str | None = None
    store: bool | None = None
    text: dict[str, Any] | None = None
    service_tier: str | None = None
    truncation: Any = None
    include: list[str] | None = None
    prompt_cache_key: str | None = None
    client_metadata: dict[str, Any] | None = None
    user: str | None = None
    safety_identifier: str | None = None
    background: bool | None = None
    prompt_cache_retention: Any = None
