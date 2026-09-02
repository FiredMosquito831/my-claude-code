"""Rules every harness catalogue serialiser must obey, checked statically.

The guard below is the test that stops ``"context_window": 200000`` coming
back. It is deliberately a source scan rather than a behavioural assertion: a
hard-coded limit is invisible in output whenever the ladder happens to have no
answer, which is exactly the case it was hiding in before.
"""

import ast
import re
from importlib import import_module
from pathlib import Path

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import (
    SERIALISERS,
    model_entries,
    serialise,
)
from my_claude_code.application.catalogues.base import (
    DEFAULTED_KEY,
    starting_model,
    visible_entries,
)
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.config.harnesses import catalogue_specs
from my_claude_code.core.reasoning import ReasoningEffort

SERIALISER_PACKAGE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "application"
    / "catalogues"
)

#: A key shaped like a limit. Any large integer literal bound to one of these
#: outside ``CLI_DOCUMENTED_DEFAULTS`` is a number MCC invented.
LIMIT_KEY_PATTERN = re.compile(r"context|window|limit|max.*token|output", re.IGNORECASE)

LITERAL_FLOOR = 1024

ALLOWED_DEFAULTS_DICT = "CLI_DOCUMENTED_DEFAULTS"


def _serialiser_modules() -> list[Path]:
    return sorted(
        path for path in SERIALISER_PACKAGE.glob("*.py") if path.name != "__init__.py"
    )


def _documented_default_nodes(tree: ast.Module) -> set[int]:
    """Return the ids of every node inside a ``CLI_DOCUMENTED_DEFAULTS`` value."""

    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = {node.target.id}
            value = node.value
        else:
            continue
        if ALLOWED_DEFAULTS_DICT not in names or value is None:
            continue
        for inner in ast.walk(value):
            allowed.add(id(inner))
    return allowed


def test_no_serialiser_hard_codes_a_limit() -> None:
    offenders: list[str] = []
    for path in _serialiser_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _documented_default_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                if not LIMIT_KEY_PATTERN.search(key.value):
                    continue
                if not isinstance(value, ast.Constant):
                    continue
                if not isinstance(value.value, int) or isinstance(value.value, bool):
                    continue
                if value.value < LITERAL_FLOOR or id(value) in allowed:
                    continue
                offenders.append(
                    f"{path.name}:{value.lineno} {key.value}={value.value}"
                )

    assert offenders == [], (
        "A limit-shaped key was assigned a literal outside "
        f"{ALLOWED_DEFAULTS_DICT}: {offenders}"
    )


def test_every_registered_catalogue_format_has_a_serialiser() -> None:
    formats = {
        spec.catalogue.format_id
        for spec in catalogue_specs()
        if spec.catalogue is not None
    }
    assert formats <= set(SERIALISERS)


#: Formats whose CLI publishes no per-model context window, so a resolved
#: context length has nowhere honest to go. Named rather than skipped: the
#: list is the record of which agents cannot show a context gauge for an
#: MCC-routed model, and adding to it should require saying why.
#:
#: ``gemini_cli`` -- Gemini CLI 0.49.0's ``tokenLimit(model)`` is a hardcoded
#: switch over Google's own model ids returning a hardcoded 1,048,576 default,
#: and no settings key overrides it.
FORMATS_WITHOUT_A_CONTEXT_FIELD = frozenset({"gemini_cli"})


#: Formats whose CLI refuses unknown root keys, so the generated document
#: cannot carry MCC's own record of what it had to guess. Kilo CLI is the only
#: one: its validator runs an excess-key check against the root document and
#: throws ``Unrecognized key: _mcc_defaulted``. The record still reaches the
#: launcher's summary and the dashboard card, which read it from
#: ``GET /admin/api/catalogue-models`` rather than from the file.
FORMATS_WITHOUT_A_DEFAULTED_RECORD = frozenset({"kilo"})


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_custom_provider_with_only_tier_five_data_still_serialises(
    format_id: str,
) -> None:
    """A custom provider can never reach a models.dev bucket of its own.

    Everything it knows arrives from the OpenRouter reference rung, which
    resolves limits and reasoning but publishes no prices. The document must
    still be valid, carry the resolved numbers, and record the price default
    rather than presenting a zero as a fact.
    """

    model = CatalogueModel(
        gateway_id="anthropic/custom_acme/llama-4",
        provider_model_ref="custom_acme/llama-4",
        display_name="custom_acme/llama-4",
        context_length=131072,
        max_output_tokens=16384,
        reasoning=ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supported_efforts=frozenset({ReasoningEffort.MEDIUM}),
        ),
    )

    document, defaulted = serialise(format_id, [model])

    assert model_entries(format_id, document), format_id
    serialised = str(document)
    # Every format carries at least one of the two resolved limits. Which one
    # is a fact about that CLI's schema, not about the ladder: Gemini CLI has
    # no context-window field at all -- its ``tokenLimit(model)`` is a
    # hardcoded switch over Google's own model ids with a hardcoded default,
    # and no settings key overrides it -- so the output ceiling is the only
    # resolved number it has a home for.
    assert "131072" in serialised or "16384" in serialised, format_id
    if format_id not in FORMATS_WITHOUT_A_CONTEXT_FIELD:
        assert "131072" in serialised, format_id
    if defaulted.by_model and format_id not in FORMATS_WITHOUT_A_DEFAULTED_RECORD:
        assert DEFAULTED_KEY in document


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_a_fully_unknown_model_never_produces_a_zero_limit(format_id: str) -> None:
    model = CatalogueModel(
        gateway_id="anthropic/custom_acme/silent",
        provider_model_ref="custom_acme/silent",
        display_name="custom_acme/silent",
    )

    document, defaulted = serialise(format_id, [model])

    entry = model_entries(format_id, document)[0]
    for key, value in _flatten(entry):
        if key.startswith("cost"):
            # A price of zero is a real, defensible CLI default -- free is a
            # number a model can genuinely cost. A *limit* of zero never is.
            continue
        if not LIMIT_KEY_PATTERN.search(key) or not isinstance(value, int):
            continue
        if value == 0 and _cli_documented_defaults(format_id).get(key) == 0:
            # A zero the CLI itself documents as its unknown marker, declared
            # in that serialiser's own CLI_DOCUMENTED_DEFAULTS. Kimi Code is
            # the case: ``max_context_size`` is a required int and
            # ``compute_max_completion_tokens`` branches on ``<= 0`` to fall
            # back to its own budget, so zero is the CLI's own way of saying
            # "nobody published one" and any positive number would be a
            # context window MCC invented. The exemption is derived from the
            # declared dict rather than listed here, so a stray zero anywhere
            # else in any format still fails this guard.
            continue
        assert value > 0, f"{format_id}.{key} was zeroed rather than defaulted"
    assert defaulted.model_count == 1


def _cli_documented_defaults(format_id: str) -> dict[str, object]:
    """Return one serialiser's declared CLI defaults, or an empty mapping."""

    module = import_module(f"my_claude_code.application.catalogues.{format_id}")
    declared = getattr(module, ALLOWED_DEFAULTS_DICT, {})
    return dict(declared) if isinstance(declared, dict) else {}


def _flatten(entry: object, prefix: str = "") -> list[tuple[str, object]]:
    """Return every leaf in one model entry, however deeply the CLI nests it.

    Codex keeps its limits at the top level of an entry and OpenCode puts them
    inside ``limit``; a guard that only looked one level down would pass for
    OpenCode by never reaching the field it exists to check.
    """

    pairs: list[tuple[str, object]] = []
    if isinstance(entry, dict):
        for key, value in entry.items():
            name = f"{prefix}{key}"
            if isinstance(value, dict):
                pairs.extend(_flatten(value, f"{name}."))
            else:
                pairs.append((name, value))
    return pairs


def test_a_surviving_no_thinking_twin_is_re_projected_onto_its_plain_ref() -> None:
    """``visible_entries`` is where the prefix stops.

    A twin only survives the collapse when the normal variant was never
    emitted -- which happens exactly when the provider reported the model
    cannot think. That statement is carried through as ``can_reason=False``
    rather than dropped, so a serialiser writes the CLI's own "reasoning off"
    spelling instead of recording a substitution for something MCC was told.
    """

    twin = CatalogueModel(
        gateway_id="claude-3-freecc-no-thinking/open_router/plain",
        provider_model_ref="open_router/plain",
        display_name="open_router/plain (no thinking)",
        force_no_thinking=True,
    )

    entries = visible_entries([twin])

    assert len(entries) == 1
    assert entries[0].gateway_id == "anthropic/open_router/plain"
    assert entries[0].force_no_thinking is False
    assert entries[0].reasoning == ModelReasoningCapability(can_reason=False)


def test_a_batch_ref_never_reaches_a_picker_but_is_not_rewritten() -> None:
    """``:batch`` is a pricing tier, not a second interactive model."""

    interactive = CatalogueModel(
        gateway_id="anthropic/open_router/m",
        provider_model_ref="open_router/m",
        display_name="open_router/m",
    )
    batched = CatalogueModel(
        gateway_id="anthropic/open_router/m:batch",
        provider_model_ref="open_router/m:batch",
        display_name="open_router/m:batch",
    )

    assert visible_entries([interactive, batched]) == [interactive]


def test_the_starting_model_is_the_configured_route_then_the_first_paid_one() -> None:
    free = CatalogueModel(
        gateway_id="anthropic/open_router/free-one",
        provider_model_ref="open_router/free-one:free",
        display_name="open_router/free-one:free",
    )
    paid = CatalogueModel(
        gateway_id="anthropic/open_router/paid",
        provider_model_ref="open_router/paid",
        display_name="open_router/paid",
    )
    configured = CatalogueModel(
        gateway_id="anthropic/open_router/configured",
        provider_model_ref="open_router/configured:free",
        display_name="open_router/configured:free",
        is_primary_route=True,
    )

    assert starting_model([free, paid, configured]) is configured
    assert starting_model([free, paid]) is paid
    assert starting_model([free]) is free
    assert starting_model([]) is None
