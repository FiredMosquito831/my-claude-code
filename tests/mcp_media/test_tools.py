"""The media tools are a pure function of ``GET /v1/media/models`` (7.99.0).

User decisions 3 and 4 of 2026-10-18 (``specs/PR-MCP-SERVER-SPEC.md`` §3.2):
one tool per job for a rail that has a model, ``media_status`` always, every
field any of the rail's models takes, labelled with which take, need or
refuse it -- in a plain JSON Schema subset (risk R9) and byte-stable (R1).
"""

import ast
import copy
import inspect
import json
import re
from pathlib import Path
from typing import Any

from my_claude_code.config.mcp_media import (
    MCP_MEDIA_FREE_TOOLS,
    MCP_MEDIA_SPENDING_TOOLS,
)
from my_claude_code.mcp_media import tools
from my_claude_code.mcp_media.tools import (
    derive_tools,
    tool_definitions,
    tool_list_bytes,
)

A = "fake_a/pic-one"
B = "fake_b/pic-two"
V = "fake_v/clip"


def _field(
    *,
    accepted: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    refused: tuple[str, ...] = (),
    not_forwarded: tuple[str, ...] = (),
    not_known: tuple[str, ...] = (),
) -> dict[str, list[str]]:
    return {
        "accepted_by": list(accepted) + [r for r in required if r not in accepted],
        "required_by": list(required),
        "refused_by": list(refused),
        "not_forwarded_by": list(not_forwarded),
        "not_known_for": list(not_known),
    }


def _schema(model: str, properties: dict[str, Any]) -> dict[str, Any]:
    """A published per-model schema, keywords and all (what R9 keeps out)."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "const": model,
                "x-mcc": {"state": "stated", "source": "rail", "since": None},
            },
            **properties,
        },
        "required": ["prompt"],
        "x-mcc": {"model": model, "ladder": []},
    }


def _catalogue(
    image_fields: dict[str, Any] | None = None,
    image_schemas: dict[str, dict[str, Any]] | None = None,
    *,
    image_models: tuple[str, ...] = (A, B),
    video_models: tuple[str, ...] = (),
) -> dict[str, Any]:
    fields = image_fields or {
        "prompt": _field(required=image_models),
        "size": _field(not_known=image_models),
    }
    schemas = image_schemas or {
        ref: _schema(ref, {"prompt": {"type": "string"}, "size": {"type": "string"}})
        for ref in image_models
    }
    data = [
        {
            "id": ref,
            "object": "media.model",
            "operations": {
                "image_generate": {
                    "endpoint": "/v1/images/generations",
                    "request_schema": schemas[ref],
                }
            },
        }
        for ref in image_models
    ] + [
        {
            "id": ref,
            "object": "media.model",
            "operations": {
                "video_create": {
                    "endpoint": "/v1/videos",
                    "request_schema": _schema(ref, {"prompt": {"type": "string"}}),
                }
            },
        }
        for ref in video_models
    ]
    return {
        "object": "list",
        "data": data,
        "rails": [
            {
                "rail": "image",
                "label": "Image",
                "models": list(image_models),
                "operations": {
                    "image_generate": {
                        "endpoint": "/v1/images/generations",
                        "models": list(image_models),
                        "not_served_by": [],
                        "fields": fields,
                    },
                    "image_edit": {
                        "endpoint": "/v1/images/edits",
                        "models": [],
                        "not_served_by": [],
                        "fields": {},
                    },
                },
            },
            {
                "rail": "tts",
                "label": "Speech",
                "models": [],
                "operations": {
                    "speech": {
                        "endpoint": "/v1/audio/speech",
                        "models": [],
                        "not_served_by": [],
                        "fields": {},
                    }
                },
            },
            {
                "rail": "video",
                "label": "Video",
                "models": list(video_models),
                "operations": {
                    "video_create": {
                        "endpoint": "/v1/videos",
                        "models": list(video_models),
                        "not_served_by": [],
                        "fields": {
                            "prompt": _field(required=video_models),
                            "mode": _field(required=video_models),
                        },
                    }
                },
            },
        ],
    }


def _tool(catalogue: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [t.definition for t in derive_tools(catalogue) if t.name == name]
    return dict(found)


def _properties(catalogue: dict[str, Any], name: str = "generate_image") -> dict:
    return _tool(catalogue, name)["inputSchema"]["properties"]


def _wire(catalogue: dict[str, Any] | None) -> str:
    return json.dumps(
        tool_definitions(derive_tools(catalogue)),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_tool_list_is_a_pure_function_of_the_catalogue() -> None:
    catalogue = _catalogue(video_models=(V,))
    untouched = copy.deepcopy(catalogue)

    first = _wire(catalogue)
    second = _wire(copy.deepcopy(catalogue))

    assert first == second
    # The same bytes from an equal catalogue, and the argument is not changed.
    assert catalogue == untouched
    assert tool_list_bytes(derive_tools(catalogue)) == len(first.encode("utf-8"))


def test_tools_follow_the_catalogue_order_media_status_first() -> None:
    names = [t.name for t in derive_tools(_catalogue(video_models=(V,)))]
    assert names == ["media_status", "generate_image", "create_video", "get_video"]


def test_no_model_name_branch() -> None:
    """IV.6: no comparison puts a model name beside a literal; no vendor spelled."""

    source = Path(inspect.getfile(tools)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def names(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                found.add(child.id)
            elif isinstance(child, ast.Attribute):
                found.add(child.attr)
        return found

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            mentions_model = any(
                "model" in name.lower() for part in parts for name in names(part)
            )
            has_literal = any(
                isinstance(part, ast.Constant) and isinstance(part.value, str)
                for part in parts
            )
            if mentions_model and has_literal:
                offenders.append(ast.unparse(node))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"startswith", "endswith"}
            and any("model" in name.lower() for name in names(node.func.value))
        ):
            offenders.append(ast.unparse(node))
    assert offenders == []
    lowered = source.lower()
    for vendor in ("agnes", "gpt-", "dall-e", "sora", "veo", "imagen", "flux"):
        assert vendor not in lowered, vendor


def test_field_refused_by_every_model_is_not_offered() -> None:
    catalogue = _catalogue(
        {
            "prompt": _field(required=(A, B)),
            "everyone_refuses": _field(refused=(A, B)),
            "nobody_is_sent": _field(not_forwarded=(A, B)),
            "refused_and_not_sent": _field(refused=(A,), not_forwarded=(B,)),
            "one_refuses": _field(refused=(A,), not_known=(B,)),
        }
    )
    properties = _properties(catalogue)
    assert "everyone_refuses" not in properties
    assert "nobody_is_sent" not in properties
    assert "refused_and_not_sent" not in properties
    # Refused by one, not known for the other: offered, and labelled.
    assert properties["one_refuses"]["description"] == (
        f"refused by {A}; not known for {B}"
    )


def test_unknown_is_described_not_enforced() -> None:
    """IV.16: a field nobody spoke about is offered, never forced or hidden."""

    catalogue = _catalogue(
        {"prompt": _field(required=(A, B)), "size": _field(not_known=(A, B))}
    )
    tool = _tool(catalogue, "generate_image")
    size = tool["inputSchema"]["properties"]["size"]
    assert size == {"type": "string", "description": f"not known for {A}, {B}"}
    assert "size" not in tool["inputSchema"]["required"]


def test_required_only_when_every_serving_model_requires() -> None:
    catalogue = _catalogue(
        {
            "prompt": _field(required=(A, B)),
            "aspect": _field(required=(A,), not_known=(B,)),
        }
    )
    tool = _tool(catalogue, "generate_image")
    assert tool["inputSchema"]["required"] == ["prompt"]
    assert tool["inputSchema"]["properties"]["aspect"]["description"] == (
        f"required by {A}; not known for {B}"
    )


def test_enum_only_when_every_serving_model_states_the_same_values() -> None:
    def schemas(a_values: list[str], b_values: list[str] | None) -> dict:
        b_size: dict[str, Any] = {"type": "string"}
        if b_values is not None:
            b_size["enum"] = b_values
        return {
            A: _schema(A, {"size": {"type": "string", "enum": a_values}}),
            B: _schema(B, {"size": b_size}),
        }

    fields = {
        "prompt": _field(required=(A, B)),
        "size": _field(accepted=(A, B)),
    }
    same = _properties(_catalogue(fields, schemas(["1x1", "2x2"], ["1x1", "2x2"])))
    assert same["size"]["enum"] == ["1x1", "2x2"]

    differs = _properties(_catalogue(fields, schemas(["1x1"], None)))
    assert "enum" not in differs["size"]
    assert differs["size"]["description"].endswith("values stated: 1x1")


def test_empty_rail_has_no_tool() -> None:
    names = [t.name for t in derive_tools(_catalogue())]
    # The Speech rail and the image edit operation have no model: no tool.
    assert names == ["media_status", "generate_image"]
    assert "get_video" not in names
    # No catalogue at all (MCC not answering, nothing saved): status alone.
    assert [t.name for t in derive_tools(None)] == ["media_status"]


#: The JSON Schema keywords a tool's input schema may use (risk R9).
PLAIN_KEYWORDS = frozenset(
    {"type", "enum", "items", "properties", "required", "description"}
)


def _keywords(node: object) -> set[str]:
    """Every keyword in a schema; property names are not keywords."""

    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            if key == "properties" and isinstance(value, dict):
                for prop in value.values():
                    found |= _keywords(prop)
            else:
                found |= _keywords(value)
    elif isinstance(node, list):
        for item in node:
            found |= _keywords(item)
    return found


def test_no_xmcc_dates_or_not_in_tool_schemas() -> None:
    """R9: a model API reading the schema sees a plain subset only."""

    schemas = {
        ref: _schema(
            ref,
            {
                "prompt": {"type": "string"},
                "size": {
                    "type": ["string", "object"],
                    "format": "binary",
                    "x-mcc": {"state": "stated", "since": "2026-10-18"},
                },
                "style": {"not": {}, "x-mcc": {"state": "refused (stated)"}},
                "seed": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            },
        )
        for ref in (A, B)
    }
    fields = {
        "prompt": _field(required=(A, B)),
        "size": _field(accepted=(A,), not_known=(B,)),
        "style": _field(refused=(A,), not_known=(B,)),
        "seed": _field(not_known=(A, B)),
    }
    for tool in derive_tools(_catalogue(fields, schemas, video_models=(V,))):
        assert _keywords(tool.definition["inputSchema"]) <= PLAIN_KEYWORDS, tool.name
        text = json.dumps(tool.definition)
        assert "x-mcc" not in text
        assert re.search(r"\d{4}-\d{2}-\d{2}", text) is None
    # A list of types becomes its first: "string".
    assert _properties(_catalogue(fields, schemas))["size"]["type"] == "string"


def test_spending_tools_are_not_read_only() -> None:
    """Decision 8: an app that asks before a tool asks before every paid one."""

    for tool in derive_tools(_catalogue(video_models=(V,))):
        hints = tool.definition["annotations"]
        if tool.name in MCP_MEDIA_SPENDING_TOOLS:
            assert hints["readOnlyHint"] is False, tool.name
            assert hints["destructiveHint"] is False, tool.name
            assert hints["openWorldHint"] is True, tool.name
        else:
            assert tool.name in MCP_MEDIA_FREE_TOOLS
            assert hints["readOnlyHint"] is True, tool.name


def test_file_fields_are_local_paths_and_server_owned_fields_are_not_offered() -> None:
    catalogue = _catalogue(
        {
            "prompt": _field(required=(A, B)),
            "stream": _field(not_forwarded=(A,), not_known=(B,)),
            "response_format": _field(not_known=(A, B)),
            "partial_images": _field(not_known=(A, B)),
            "user": _field(not_known=(A, B)),
        }
    )
    properties = _properties(catalogue)
    for owned in ("stream", "response_format", "partial_images", "user"):
        assert owned not in properties
    assert properties["model"]["enum"] == [A, B]
    assert "save_to" in properties

    video = _tool(_catalogue(video_models=(V,)), "create_video")
    assert video["inputSchema"]["required"] == ["prompt", "mode"]
