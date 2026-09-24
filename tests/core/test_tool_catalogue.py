"""The fingerprint of a tools array: what counts as the same tool, and the same array."""

import hashlib
import json

from my_claude_code.core.anthropic.models import Tool
from my_claude_code.core.tool_catalogue import (
    TOOL_SHA_BYTES,
    ToolFingerprinter,
    canonical_tool_definition,
    fingerprint_tools,
    split_member_shas,
)

VIDEO_SCALE = r"^(?:[1-9]\d{0,4}|-[12]):(?:[1-9]\d{0,4}|-[12])(?![\s\S])"


def _tool(name: str, **extra) -> dict:
    tool = {
        "name": name,
        "description": f"The {name} tool.",
        "input_schema": {
            "type": "object",
            "properties": {"videoScale": {"type": "string", "pattern": VIDEO_SCALE}},
        },
    }
    tool.update(extra)
    return tool


class TestTheDefinition:
    def test_key_order_does_not_change_the_bytes(self) -> None:
        forward = {"name": "A", "description": "d", "input_schema": {"b": 1, "a": 2}}
        backward = {"input_schema": {"a": 2, "b": 1}, "description": "d", "name": "A"}

        assert canonical_tool_definition(forward) == canonical_tool_definition(backward)

    def test_it_is_compact_sorted_json_as_written(self) -> None:
        tool = {"name": "Ünïcode", "input_schema": {"z": 1, "a": "é"}}

        definition = canonical_tool_definition(tool)

        assert definition == '{"input_schema":{"a":"é","z":1},"name":"Ünïcode"}'

    def test_cache_control_is_not_part_of_the_tool(self) -> None:
        """Claude Code marks whichever tool is last; position is not identity."""

        plain = _tool("Bash")
        marked = _tool("Bash", cache_control={"type": "ephemeral"})

        assert canonical_tool_definition(plain) == canonical_tool_definition(marked)

    def test_a_changed_pattern_is_a_different_tool(self) -> None:
        before = _tool("Rec")
        after = _tool("Rec")
        after["input_schema"]["properties"]["videoScale"]["pattern"] = "^x$"

        assert canonical_tool_definition(before) != canonical_tool_definition(after)

    def test_the_pydantic_model_and_the_dict_agree(self) -> None:
        """The request log sees ``Tool`` models; a dict must hash the same."""

        raw = _tool("Bash", cache_control={"type": "ephemeral"})

        assert canonical_tool_definition(
            Tool.model_validate(raw)
        ) == canonical_tool_definition(raw)

    def test_something_that_is_not_a_tool_has_no_definition(self) -> None:
        assert canonical_tool_definition("Bash") is None
        assert canonical_tool_definition(None) is None


class TestTheCatalogue:
    def test_no_tools_is_no_catalogue(self) -> None:
        assert fingerprint_tools([]) is None
        assert fingerprint_tools(["not a tool"]) is None

    def test_the_hash_is_of_the_member_hashes_in_order(self) -> None:
        tools = [_tool("A"), _tool("B")]

        catalogue = fingerprint_tools(tools)

        assert catalogue is not None
        expected = [
            hashlib.sha256((canonical_tool_definition(tool) or "").encode()).digest()
            for tool in tools
        ]
        assert [member.sha for member in catalogue.members] == expected
        assert catalogue.member_shas == b"".join(expected)
        assert catalogue.sha == hashlib.sha256(b"".join(expected)).digest()
        assert len(catalogue.sha) == TOOL_SHA_BYTES

    def test_order_is_part_of_the_array(self) -> None:
        """Order is what the upstream sees and what a prompt cache keys on."""

        first = fingerprint_tools([_tool("A"), _tool("B")])
        second = fingerprint_tools([_tool("B"), _tool("A")])

        assert first is not None and second is not None
        assert first.sha != second.sha
        assert {m.sha for m in first.members} == {m.sha for m in second.members}

    def test_the_same_array_twice_is_the_same_hash(self) -> None:
        tools = [Tool.model_validate(_tool(name)) for name in ("A", "B", "C")]

        first = fingerprint_tools(tools)
        second = fingerprint_tools([Tool.model_validate(_tool(n)) for n in "ABC"])

        assert first is not None and second is not None
        assert first.sha == second.sha

    def test_members_keep_their_names_and_definitions(self) -> None:
        catalogue = fingerprint_tools([_tool("Bash"), {"input_schema": {}}])

        assert catalogue is not None
        assert [member.name for member in catalogue.members] == ["Bash", ""]
        assert json.loads(catalogue.members[0].definition)["name"] == "Bash"

    def test_the_client_objects_are_not_changed(self) -> None:
        raw = _tool("Bash", cache_control={"type": "ephemeral"})
        snapshot = json.dumps(raw, sort_keys=True)

        fingerprint_tools([raw])

        assert json.dumps(raw, sort_keys=True) == snapshot

    def test_member_hashes_split_back(self) -> None:
        catalogue = fingerprint_tools([_tool(name) for name in "ABCD"])

        assert catalogue is not None
        assert split_member_shas(catalogue.member_shas) == [
            member.sha for member in catalogue.members
        ]


class TestTheWriterMemo:
    """The writer recognises an unchanged tool by ``==`` instead of re-serialising it."""

    def test_it_always_agrees_with_a_fresh_fingerprint(self) -> None:
        fingerprinter = ToolFingerprinter()
        sessions = [
            [_tool("A"), _tool("B"), _tool("C")],
            [_tool("A"), _tool("B")],
            [_tool("B"), _tool("A"), _tool("C")],
            [_tool("A"), _tool("B", cache_control={"type": "ephemeral"})],
        ]
        changed = _tool("B")
        changed["description"] = "B, version two"
        sessions.append([_tool("A"), changed])
        sessions.append([_tool("A"), _tool("B")])

        for tools in sessions:
            models = [Tool.model_validate(tool) for tool in tools]
            remembered = fingerprinter.fingerprint(models)
            fresh = fingerprint_tools(models)
            assert remembered is not None and fresh is not None
            assert remembered.sha == fresh.sha
            assert remembered.members == fresh.members

    def test_an_unchanged_tool_is_not_serialised_again(self, monkeypatch) -> None:
        from my_claude_code.core import tool_catalogue

        fingerprinter = ToolFingerprinter()
        fingerprinter.fingerprint([Tool.model_validate(_tool(n)) for n in "ABC"])
        calls: list[str] = []
        real = tool_catalogue.canonical_tool_definition

        def counting(tool):
            calls.append(tool.name)
            return real(tool)

        monkeypatch.setattr(tool_catalogue, "canonical_tool_definition", counting)
        changed = _tool("C")
        changed["description"] = "changed"
        fingerprinter.fingerprint(
            [
                Tool.model_validate(_tool("A")),
                Tool.model_validate(_tool("B")),
                Tool.model_validate(changed),
            ]
        )

        assert calls == ["C"]

    def test_it_forgets_the_oldest_name_beyond_its_capacity(self) -> None:
        fingerprinter = ToolFingerprinter(capacity=2)
        fingerprinter.fingerprint([_tool("A"), _tool("B"), _tool("C")])

        assert list(fingerprinter._recent) == ["B", "C"]
