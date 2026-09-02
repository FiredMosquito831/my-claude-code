"""Identifier helpers for OpenAI Chat Completions payloads."""

import uuid


def new_chat_completion_id() -> str:
    """Return an id in the shape every OpenAI client expects to parse."""
    return f"chatcmpl-{uuid.uuid4().hex}"


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"
