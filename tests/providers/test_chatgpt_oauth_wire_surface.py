"""Every provider that records a wire body records which surface it was.

`providers/chatgpt_oauth/provider.py` has recorded the outbound body since
6.74.0 -- `params.wire` carries `model`, `reasoning.{effort,summary}`,
`include`, `parallel_tool_calls`, `store` and `stream`, confirmed on a live
row. It passed no `surface=`, unlike
`providers/openai_chat/responses_transport.py:284-292`, so
`params.wire.surface` -- read by `core/export.py:1115-1118` and rendered as
"Wire surface" -- was empty for this provider alone.

There is nothing to *resolve* here: the Codex backend serves exactly one
endpoint, `POST <base>/codex/responses`, for every model. So the source is
`DEFAULT` and not `REGISTRY`, which would claim a published fact that was never
consulted.
"""

import ast
from pathlib import Path

from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.core.wire_capture import (
    _WIRE_TRACE,
    WireTrace,
    record_wire_request,
)
from my_claude_code.providers.chatgpt_oauth import provider as provider_module
from my_claude_code.providers.chatgpt_oauth.provider import WIRE_SURFACE

_SOURCE = Path(provider_module.__file__ or "").read_text(encoding="utf-8")


def test_the_label_is_the_vocabulary_the_rest_of_the_stack_uses() -> None:
    """`"responses (default)"`, built from the enums rather than typed out."""

    expected = (
        f"{ResponseSurface.RESPONSES.value} ({ResponseSurfaceSource.DEFAULT.value})"
    )

    assert expected == WIRE_SURFACE
    assert WIRE_SURFACE == "responses (default)"


def test_the_recorded_body_carries_the_surface() -> None:
    """What the capture stores, through the real collector."""

    trace = WireTrace()
    token = _WIRE_TRACE.set(trace)
    try:
        record_wire_request(
            {
                "model": "gpt-5.6-luna",
                "stream": True,
                "reasoning": {"effort": "high", "summary": "auto"},
            },
            surface=WIRE_SURFACE,
        )
    finally:
        _WIRE_TRACE.reset(token)

    (recorded,) = trace.requests.values()
    assert recorded.params["surface"] == "responses (default)"
    assert recorded.params["surface"], "an empty surface is the defect"
    assert recorded.params["model"] == "gpt-5.6-luna"


def test_the_provider_passes_it_at_its_own_commit_boundary() -> None:
    """The call site, not a stand-in for it.

    Reading the source rather than running a live stream: the alternative is a
    full OAuth transport double for one keyword, and what has to be true is
    that *this* `record_wire_request` -- the one inside the retry loop, at the
    commit boundary -- names the surface. A second call site added later
    without it would fail here.
    """

    tree = ast.parse(_SOURCE)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "record_wire_request"
    ]
    assert calls, "the provider no longer records a wire body at all"
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "surface" in keywords, (
            f"record_wire_request at line {call.lineno} records no surface"
        )
