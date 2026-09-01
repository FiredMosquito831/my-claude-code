"""Rules every harness catalogue serialiser must obey, checked statically.

The guard below is the test that stops ``"context_window": 200000`` coming
back. It is deliberately a source scan rather than a behavioural assertion: a
hard-coded limit is invisible in output whenever the ladder happens to have no
answer, which is exactly the case it was hiding in before.
"""

import ast
import re
from pathlib import Path

import pytest

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.catalogues import SERIALISERS, serialise
from my_claude_code.application.catalogues.base import DEFAULTED_KEY
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

    assert document["models"], format_id
    serialised = str(document)
    assert "131072" in serialised
    if defaulted.by_model:
        assert DEFAULTED_KEY in document


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_a_fully_unknown_model_never_produces_a_zero_limit(format_id: str) -> None:
    model = CatalogueModel(
        gateway_id="anthropic/custom_acme/silent",
        provider_model_ref="custom_acme/silent",
        display_name="custom_acme/silent",
    )

    document, defaulted = serialise(format_id, [model])

    entry = document["models"][0]
    for key, value in entry.items():
        if LIMIT_KEY_PATTERN.search(key) and isinstance(value, int):
            assert value > 0, f"{format_id}.{key} was zeroed rather than defaulted"
    assert defaulted.model_count == 1
