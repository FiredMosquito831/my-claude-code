"""Request detection utilities for API optimizations.

Detects title generation, safety classifier, suggestion mode, and model-routing
probe requests to enable targeted handling.
"""

from my_claude_code.core.anthropic import (
    Message,
    MessagesRequest,
    extract_text_from_content,
)


def _request_system_text(request_data: MessagesRequest) -> str:
    """Return top-level and inline system text for request-shape detection."""
    parts: list[str] = []
    if request_data.system:
        text = extract_text_from_content(request_data.system)
        if text:
            parts.append(text)
    for message in request_data.messages:
        if message.role != "system":
            continue
        text = extract_text_from_content(message.content)
        if text:
            parts.append(text)
    return "\n".join(parts)


def is_title_generation_request(request_data: MessagesRequest) -> bool:
    """Check if this is a conversation title generation request.

    Title generation requests are detected by a system prompt containing
    title extraction instructions, no tools, and a single user message.

    Matches Claude Code session title prompts (sentence-case title, JSON
    \"title\" field, etc.).
    """
    if request_data.tools:
        return False
    system_text = _request_system_text(request_data).lower()
    if "title" not in system_text:
        return False
    return "sentence-case title" in system_text or (
        "return json" in system_text
        and "field" in system_text
        and ("coding session" in system_text or "this session" in system_text)
    )


def is_safety_classifier_request(request_data: MessagesRequest) -> bool:
    """Return whether this is Claude Code's auto-mode safety classifier prompt."""
    if request_data.tools:
        return False

    system_text = (
        extract_text_from_content(request_data.system) if request_data.system else ""
    )
    messages_text = "".join(
        extract_text_from_content(message.content) for message in request_data.messages
    )
    combined = f"{system_text}\n{messages_text}"
    has_verdict_instruction = "yes</block>" in combined or "no</block>" in combined
    return "<transcript>" in combined and has_verdict_instruction


def is_suggestion_mode_request(request_data: MessagesRequest) -> bool:
    """Check if this is a suggestion mode request.

    Claude Code appends the suggestion instruction as the *final* user turn of
    an otherwise ordinary transcript, so the marker is the tail of the request
    rather than something buried in its history.

    Only that final turn is inspected. Scanning every user message -- which is
    what this did originally -- means a conversation that merely *mentions* the
    marker answers with an empty string instead of a real reply, which is the
    worst failure this module can produce. Measured against 61 real suggestion
    requests: the marker is never earlier than 97.61% into the prompt and is
    always followed by exactly the same 1,363-character instruction block, so
    narrowing to the last turn loses nothing.
    """
    last_user_turn: Message | None = None
    for message in request_data.messages:
        if message.role == "user":
            last_user_turn = message
    if last_user_turn is None:
        return False
    return "[SUGGESTION MODE:" in extract_text_from_content(last_user_turn.content)


# Upper bound on max_tokens for a reachability probe. The harnesses observed in
# the live log send 16; 32 leaves headroom for other probes without ever
# reaching a real conversation, where output budgets are in the thousands.
_PROBE_MAX_TOKENS = 32


def is_model_routing_probe_request(request_data: MessagesRequest) -> bool:
    """Return whether this is a client's model-routing reachability probe.

    Agent harnesses verify the configured endpoint with one tiny non-streaming
    request -- literally "Say OK" -- before a run, so a routing substitution is
    caught cheaply instead of being diagnosed from degraded behaviour. Measured
    in the live log (1,563 requests over four days): a single user turn, no
    system text, no tools, a 6-7 character exact prompt, max_tokens 16,
    stream false.

    The gate is deliberately narrow in every dimension a real conversation
    differs in: any system text, any tool, a second turn, a longer prompt, an
    absent or normal output budget, or streaming all route upstream untouched.
    A user who types "Say OK" into a chat is never caught, because their
    request carries a real max_tokens and usually history.
    """
    if request_data.tools or request_data.system or request_data.stream:
        return False
    if request_data.max_tokens is None or request_data.max_tokens > _PROBE_MAX_TOKENS:
        return False
    messages = request_data.messages
    if len(messages) != 1 or messages[0].role != "user":
        return False
    text = extract_text_from_content(messages[0].content).strip().lower()
    return text in ("say ok", "say ok.")
