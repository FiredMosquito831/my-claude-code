"""Real payload -> real serialiser -> the CLI's real schema.

The test that was missing. Six harness PRs shipped before a generated document
was refused by a real CLI, and every one of them was green, because:

* every fixture was one or two models with hand-set capabilities, so the shape
  that actually broke -- a gateway publishing no parameter list, no context
  window and no price -- had never been serialised inside a test at all; and
* nothing anywhere validated a generated document against a CLI's own schema.
  The nearest test asserted that no limit-shaped key was ``0``; for OpenCode
  the key was *absent*, so it passed vacuously while the document it was
  guarding was one OpenCode refused outright.

So this module does both halves. It serialises the **live-shaped** capture
(``tests/fixtures/live_catalogue.py``) rather than a toy, and validates the
result against the CLI's **published** schema wherever one exists, vendored
under ``tests/fixtures/schemas/`` so the suite never needs the network.

Where no machine-readable schema exists, the required-key list each serialiser
declares in ``CLI_REQUIRED_KEYS`` stands in for one. Those lists were recovered
by reading the shipped binaries; the module docstring beside each says which
version and how.
"""

import json
from importlib import import_module
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from my_claude_code.application.catalogues import (
    SERIALISERS,
    model_entries,
    serialise,
)
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
from tests.fixtures.live_catalogue import live_catalogue_models

SCHEMA_DIR = Path(__file__).parent.parent / "fixtures" / "schemas"

#: Formats whose CLI publishes a JSON Schema MCC can validate against, and the
#: vendored copy of it. Vendored with the fetch date and the CLI version in
#: ``versions.txt`` beside them; the suite must not need the network.
VENDORED_SCHEMAS: dict[str, str] = {
    "opencode": "opencode-config.schema.json",
    "kilo": "opencode-config.schema.json",
    "crush": "crush.schema.json",
    # ``openclaw config schema`` on OpenClaw 2026.10.8 (the CLI generates its
    # own document schema; 2.5 MB, vendored whole on 2026-10-29).
    "openclaw": "openclaw.schema.json",
}

#: Formats validated with the draft-07 validator directly, without
#: ``jsonschema.validate``'s schema-check pass. OpenClaw's generated schema is
#: valid for the vendor's own validator, but some of its ``pattern`` strings
#: use the ECMA-262 ``\p{Cs}`` escape that Python's ``re`` rejects -- and the
#: schema-check pass compiles every pattern in the document before evaluating
#: one. Same schema, same rules; only the compile-all pass is skipped.
FORMATS_WITHOUT_A_SCHEMA_CHECK = frozenset({"openclaw"})

#: The published schemas set ``additionalProperties: false`` at the root, while
#: both shipped binaries tolerate an unknown root key -- OpenCode 1.18.26 and
#: Crush v0.92.0 were each run against a document carrying ``_mcc_defaulted``
#: and neither complained. The record is MCC's own bookkeeping and is stripped
#: before validation rather than dropped from the document, because a reader
#: opening the generated file is the audience it was written for. Kilo is the
#: one CLI that *does* enforce the rule, and its serialiser omits the key
#: outright -- which is why ``kilo`` is absent from this set and its document
#: is validated exactly as it ships.
FORMATS_WITH_A_TOLERATED_ROOT_RECORD = frozenset({"opencode", "crush"})


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _required_keys(format_id: str) -> frozenset[str]:
    module = import_module(f"my_claude_code.application.catalogues.{format_id}")
    declared = getattr(module, "CLI_REQUIRED_KEYS", frozenset())
    assert isinstance(declared, frozenset), format_id
    return declared


@pytest.mark.parametrize("format_id", sorted(VENDORED_SCHEMAS))
def test_every_serialiser_document_validates_against_the_cli_schema(
    format_id: str,
) -> None:
    """The generated document is one the CLI's own schema accepts.

    This is the assertion that would have caught the launch failure: a
    ``limit`` object carrying only ``output`` violates
    ``limit.required = ["context", "output"]``, and OpenCode answers it by
    refusing the **whole document**, not the entry.
    """

    document, _ = serialise(format_id, live_catalogue_models())
    if format_id in FORMATS_WITH_A_TOLERATED_ROOT_RECORD:
        document = {
            key: value for key, value in document.items() if key != DEFAULTED_KEY
        }
    schema = _schema(VENDORED_SCHEMAS[format_id])
    if format_id in FORMATS_WITHOUT_A_SCHEMA_CHECK:
        jsonschema.Draft7Validator(schema).validate(document)
        return
    jsonschema.validate(document, schema)


def test_the_openclaw_document_carries_no_root_key_its_schema_refuses() -> None:
    """OpenClaw sets ``additionalProperties: false`` at two levels.

    Nothing of MCC's bookkeeping can live in the file -- not ``_mcc_defaulted``
    at the root, not a provider-level note -- so the record reaches the
    launcher and the card through ``GET /admin/api/catalogue-models`` instead.
    The negative half below is what proves the schema would have caught it,
    rather than that the key happens to be absent.
    """

    document, defaulted = serialise("openclaw", live_catalogue_models())

    assert defaulted.by_model, "the fixture must exercise the defaulted record"
    assert DEFAULTED_KEY not in document
    assert DEFAULTED_KEY not in repr(document)

    extra_root = {**document, DEFAULTED_KEY: {"a": ["b"]}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(_schema("openclaw.schema.json")).validate(extra_root)

    block = document["models"]["providers"]["mcc"]
    block["invented"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(_schema("openclaw.schema.json")).validate(document)
    del block["invented"]

    entry = block["models"][0]
    entry["invented"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft7Validator(_schema("openclaw.schema.json")).validate(document)


def test_the_kilo_document_carries_no_root_key_its_validator_refuses() -> None:
    """Kilo runs an excess-key check against the root before decoding.

    CONFIRMED against 7.5.9: with ``_mcc_defaulted`` at the root, the document
    at ``$XDG_CONFIG_HOME/kilo/kilo.json`` produces
    ``Error: Configuration is invalid ... Unrecognized key: _mcc_defaulted``,
    exit 1, zero models -- and through ``KILO_CONFIG`` it is worse: that load
    path swallows the failure, so the provider simply vanishes with no error
    at all. Which is exactly what a user saw.
    """

    document, defaulted = serialise("kilo", live_catalogue_models())

    assert defaulted.by_model, "the fixture must exercise the defaulted record"
    assert DEFAULTED_KEY not in document
    # ...and it is genuinely the same document otherwise.
    opencode, _ = serialise("opencode", live_catalogue_models())
    assert document == {
        key: value for key, value in opencode.items() if key != DEFAULTED_KEY
    }


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_a_live_shaped_payload_serialises_for_every_format(format_id: str) -> None:
    """Every format survives the real spread of unknowns, not a toy fixture."""

    models = live_catalogue_models()
    document, _defaulted = serialise(format_id, models)
    entries = model_entries(format_id, document)

    assert entries, format_id
    required = _required_keys(format_id)
    for entry in entries:
        assert isinstance(entry, dict), format_id
        missing = required - set(entry)
        assert not missing, f"{format_id} entry omits required {sorted(missing)}"


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_every_serialiser_declares_the_keys_its_cli_requires(format_id: str) -> None:
    """``CLI_REQUIRED_KEYS`` is not optional decoration.

    It is the machine-readable form of "what this CLI refuses a document
    without", recovered from a shipped binary or a published schema. A new
    serialiser that does not state it cannot be held to it, and the two that
    drifted drifted precisely where nothing in the repo recorded the rule.
    """

    module = import_module(f"my_claude_code.application.catalogues.{format_id}")
    assert hasattr(module, "CLI_REQUIRED_KEYS"), format_id
    assert isinstance(module.CLI_REQUIRED_KEYS, frozenset), format_id


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_batch_refs_are_absent_from_harness_catalogues(format_id: str) -> None:
    """A ``:batch`` ref is a pricing tier, not a second interactive model.

    They stay in ``/v1/models`` and are excluded from every picker. On the
    captured payload that is 106 of 270 variant records.
    """

    models = live_catalogue_models()
    assert any(":batch" in model.provider_model_ref for model in models)

    document, _ = serialise(format_id, models)
    assert ":batch" not in json.dumps(document), format_id


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_no_catalogue_lists_a_claude_code_only_prefix(format_id: str) -> None:
    """The ``claude-3-freecc-no-thinking/`` prefix means nothing to these CLIs.

    Where the normal variant exists the twin is dropped; where it does not --
    a model whose provider reports it cannot think -- the twin is re-projected
    onto the plain ref and states ``reasoning: false`` in the CLI's own words.
    Either way the prefix never reaches a picker.
    """

    document, _ = serialise(format_id, live_catalogue_models())
    assert "claude-3-freecc-no-thinking" not in json.dumps(document), format_id
