"""Pydantic models for Google Gemini ``generateContent`` ingress.

Google's REST surface accepts both spellings of every field -- the JSON API
uses ``lowerCamelCase`` and the proto JSON mapping still accepts
``snake_case`` -- and the two SDKs disagree about which they send: the
JavaScript ``@google/genai`` client (which Gemini CLI bundles) emits camelCase,
while ``google-genai`` for Python emits camelCase too but its ``types`` module
round-trips snake_case. Accepting both costs one alias per field and removes an
entire class of "why is my ``maxOutputTokens`` ignored".

Deliberately permissive everywhere else, for the same reason the two OpenAI
ingress models are: an unknown key must produce this adapter's own 400 naming
the field, not a pydantic validation error listing a schema the client's author
never read.
"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class GeminiThinkingConfig(BaseModel):
    """``generationConfig.thinkingConfig``.

    ``thinkingBudget`` is a token count with two reserved values Google
    documents: ``0`` turns thinking off and ``-1`` hands the budget to the
    model ("dynamic thinking"). Both are carried through as *intent*, never as
    a number MCC invents.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    thinking_budget: int | None = Field(
        default=None,
        validation_alias=AliasChoices("thinkingBudget", "thinking_budget"),
    )
    include_thoughts: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("includeThoughts", "include_thoughts"),
    )
    #: Gemini 3's replacement for a token budget: a named rung rather than a
    #: number. Gemini CLI's own ``chat-base-3`` preset sends it, so a surface
    #: that only reads ``thinkingBudget`` would silently lose the CLI's
    #: reasoning intent on every Gemini-3-shaped request.
    thinking_level: str | None = Field(
        default=None,
        validation_alias=AliasChoices("thinkingLevel", "thinking_level"),
    )


class GeminiGenerationConfig(BaseModel):
    """``generationConfig``: the sampling and output knobs."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    max_output_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("maxOutputTokens", "max_output_tokens"),
    )
    temperature: float | None = None
    top_p: float | None = Field(
        default=None, validation_alias=AliasChoices("topP", "top_p")
    )
    top_k: int | None = Field(
        default=None, validation_alias=AliasChoices("topK", "top_k")
    )
    candidate_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("candidateCount", "candidate_count"),
    )
    stop_sequences: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("stopSequences", "stop_sequences"),
    )
    response_mime_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("responseMimeType", "response_mime_type"),
    )
    response_schema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("responseSchema", "response_schema"),
    )
    #: The newer field name for the same thing; ``google-genai`` 1.x sends it
    #: when the caller passes a raw JSON Schema rather than a ``types.Schema``.
    response_json_schema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("responseJsonSchema", "response_json_schema"),
    )
    thinking_config: GeminiThinkingConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("thinkingConfig", "thinking_config"),
    )


class GeminiContent(BaseModel):
    """One entry of ``contents``, or the ``systemInstruction`` block.

    ``role`` is optional because Google's own single-turn form omits it, and
    ``parts`` is a list of open mappings because a part is a union of eight
    shapes and the adapter names the unsupported one in its own 400.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: str | None = None
    parts: list[dict[str, Any]] | None = None


class GeminiFunctionCallingConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    mode: str | None = None
    allowed_function_names: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("allowedFunctionNames", "allowed_function_names"),
    )


class GeminiToolConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    function_calling_config: GeminiFunctionCallingConfig | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "functionCallingConfig", "function_calling_config"
        ),
    )


class GeminiGenerateContentRequest(BaseModel):
    """Permissive subset of Google's ``GenerateContentRequest``.

    ``model`` is not a body field on this surface: it is the ``{model}`` path
    segment, which the route binds here after validation so that everything
    downstream -- the request log, the router, the response envelope -- reads
    one object rather than a body plus a path variable.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    contents: list[GeminiContent] | GeminiContent | str | None = None
    system_instruction: GeminiContent | str | None = Field(
        default=None,
        validation_alias=AliasChoices("systemInstruction", "system_instruction"),
    )
    generation_config: GeminiGenerationConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("generationConfig", "generation_config"),
    )
    tools: list[dict[str, Any]] | None = None
    tool_config: GeminiToolConfig | None = Field(
        default=None, validation_alias=AliasChoices("toolConfig", "tool_config")
    )
    safety_settings: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("safetySettings", "safety_settings"),
    )
    cached_content: str | None = Field(
        default=None,
        validation_alias=AliasChoices("cachedContent", "cached_content"),
    )
    #: Bound from the URL by the route, never parsed out of the body.
    model: str = ""

    def with_model(self, model: str) -> GeminiGenerateContentRequest:
        """Return this request with the path's model bound to it."""

        return self.model_copy(update={"model": model})

    @property
    def content_list(self) -> list[GeminiContent]:
        """Return ``contents`` in its list form, whatever shape arrived.

        Google's own client libraries accept a bare string and a bare
        ``Content`` as shorthand for a single user turn, and both reach the
        REST surface as-is when a caller hand-builds the body.
        """

        value = self.contents
        if value is None:
            return []
        if isinstance(value, str):
            return [GeminiContent(role="user", parts=[{"text": value}])]
        if isinstance(value, GeminiContent):
            return [value]
        return list(value)
