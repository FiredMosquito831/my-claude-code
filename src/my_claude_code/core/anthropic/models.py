"""Pydantic models for the Anthropic Messages protocol."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class _AnthropicBlockBase(BaseModel):
    """Pass through protocol extensions such as ``cache_control``."""

    model_config = ConfigDict(extra="allow")


class ContentBlockText(_AnthropicBlockBase):
    type: Literal["text"]
    text: str


class ContentBlockImage(_AnthropicBlockBase):
    type: Literal["image"]
    source: dict[str, Any]


class ContentBlockDocument(_AnthropicBlockBase):
    """Anthropic document block (e.g. PDF files via the Files API)."""

    type: Literal["document"]
    source: dict[str, Any]


class ContentBlockToolUse(_AnthropicBlockBase):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ContentBlockToolResult(_AnthropicBlockBase):
    type: Literal["tool_result"]
    tool_use_id: str
    # Anthropic documents ``content`` as optional: a tool that returned nothing
    # is answered with no content at all. Absent stays absent -- every dump
    # that reaches a wire leaves ``None`` out, and every reader of this field
    # already treats a missing value as the empty result.
    content: str | list[Any] | dict[str, Any] | None = None


class ContentBlockThinking(_AnthropicBlockBase):
    type: Literal["thinking"]
    thinking: str
    signature: str | None = None


class ContentBlockRedactedThinking(_AnthropicBlockBase):
    type: Literal["redacted_thinking"]
    data: str


class ContentBlockServerToolUse(_AnthropicBlockBase):
    """Anthropic server-side tool invocation (e.g. ``web_search``, ``web_fetch``)."""

    type: Literal["server_tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ContentBlockWebSearchToolResult(_AnthropicBlockBase):
    type: Literal["web_search_tool_result"]
    tool_use_id: str
    content: Any


class ContentBlockWebFetchToolResult(_AnthropicBlockBase):
    type: Literal["web_fetch_tool_result"]
    tool_use_id: str
    content: Any


class ContentBlockToolAddition(_AnthropicBlockBase):
    """A tool offered from this point of the conversation onward.

    Anthropic's ``mid-conversation-tool-changes`` beta, which Claude Code
    2.1.304 sends inside a ``role: "system"`` message whenever its tool set
    changes (an MCP server connecting late, a resumed session launched with
    other tools). ``tool`` is a ``tool_reference`` naming a tool, a
    ``tool_definition`` carrying one by value, or an MCP reference; it is kept
    as the client sent it, and :mod:`.tool_changes` folds it into ``tools``
    before any provider sees the request.
    """

    type: Literal["tool_addition"]
    tool: dict[str, Any]


class ContentBlockToolRemoval(_AnthropicBlockBase):
    """A tool no longer offered from this point of the conversation onward."""

    type: Literal["tool_removal"]
    tool: dict[str, Any]


class SystemContent(_AnthropicBlockBase):
    type: Literal["text"]
    text: str


#: One block of a message's content, chosen by its ``type``. Tagged rather
#: than tried member by member: every member already declares a distinct
#: ``Literal`` type, so an accepted block parses into exactly the model it
#: always did, and an unknown one is ONE error that names the tag
#: (``Input tag 'x' found using 'type' does not match any of the expected
#: tags``) instead of one error per member. That sentence is also the one
#: Claude Code reads to fall back by itself when a block type is refused.
ContentBlock = Annotated[
    ContentBlockText
    | ContentBlockImage
    | ContentBlockDocument
    | ContentBlockToolUse
    | ContentBlockToolResult
    | ContentBlockThinking
    | ContentBlockRedactedThinking
    | ContentBlockServerToolUse
    | ContentBlockWebSearchToolResult
    | ContentBlockWebFetchToolResult
    | ContentBlockToolAddition
    | ContentBlockToolRemoval,
    Field(discriminator="type"),
]


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str | list[ContentBlock]
    reasoning_content: str | None = None


class Tool(_AnthropicBlockBase):
    name: str
    type: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None


class ThinkingConfig(BaseModel):
    enabled: bool | None = True
    type: str | None = None
    budget_tokens: int | None = None
    #: How the client asked Anthropic to show the thinking it does --
    #: ``"summarized"``, ``"omitted"`` or ``"updates"`` (Claude Code picks one
    #: from its own ``showThinkingSummaries``). Until 7.102.5 it was not
    #: declared, so it was dropped at parse time and never reached Anthropic.
    #: Excluded from every dump: the request log, the other dialects and every
    #: body that dumps this object stay byte-identical. Read explicitly, on the
    #: one path that forwards it (``build_anthropic_messages_body``). ``Any``,
    #: not ``str``: a value the client got wrong is ignored there, as it was
    #: before, rather than refused here.
    display: Any = Field(default=None, exclude=True)


@dataclass(frozen=True, slots=True)
class PromptCacheAsk:
    """The prompt-cache lifetime the operator asked for on one request (7.104.0).

    ``minutes`` is what was asked (1-60), before any host's list is consulted;
    ``source`` names the setting that decided it (``PROMPT_CACHE_TTL_OPUS``,
    ``PROMPT_CACHE_TTL``). Resolved by the door from the route, because the
    route decides the tier, and adapted per attempt by the provider, because
    the host decides the lifetimes.
    """

    minutes: int
    source: str


class MessagesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    #: The operator's prompt-cache lifetime for this request (7.104.0), or
    #: ``None`` -- the state of every request while nothing is set, which
    #: leaves every body exactly as before. A private attribute, not a field:
    #: no client body can set it, no dump ever carries it, and routing's
    #: per-rung ``model_copy(deep=True)`` copies it to every attempt.
    _prompt_cache_ask: PromptCacheAsk | None = PrivateAttr(default=None)

    model: str
    original_model: str | None = Field(default=None, exclude=True)
    resolved_provider_model: str | None = Field(default=None, exclude=True)
    max_tokens: int | None = None
    messages: list[Message]
    system: str | list[SystemContent] | None = None
    stop_sequences: list[str] | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    metadata: dict[str, Any] | None = None
    tools: list[Tool] | None = None
    tool_choice: dict[str, Any] | None = None
    thinking: ThinkingConfig | None = None
    context_management: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    extra_body: dict[str, Any] | None = None
    betas: list[str] | None = Field(default=None, exclude=True)

    @property
    def prompt_cache_ask(self) -> PromptCacheAsk | None:
        """What the operator asked a host to keep this prompt cached for."""
        return self._prompt_cache_ask

    def set_prompt_cache_ask(self, ask: PromptCacheAsk | None) -> None:
        """Stamp (or clear) the operator's lifetime; see ``_prompt_cache_ask``."""
        self._prompt_cache_ask = ask


class TokenCountRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    original_model: str | None = Field(default=None, exclude=True)
    resolved_provider_model: str | None = Field(default=None, exclude=True)
    messages: list[Message]
    system: str | list[SystemContent] | None = None
    tools: list[Tool] | None = None
    thinking: ThinkingConfig | None = None
    tool_choice: dict[str, Any] | None = None
    context_management: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    betas: list[str] | None = Field(default=None, exclude=True)


class TokenCountResponse(BaseModel):
    input_tokens: int


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class MessagesResponse(BaseModel):
    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: list[
        ContentBlockText
        | ContentBlockToolUse
        | ContentBlockThinking
        | ContentBlockRedactedThinking
        | dict[str, Any]
    ]
    type: Literal["message"] = "message"
    stop_reason: (
        Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"] | None
    ) = None
    stop_sequence: str | None = None
    usage: Usage
