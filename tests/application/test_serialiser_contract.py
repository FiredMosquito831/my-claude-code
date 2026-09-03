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
from typing import Any

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
from my_claude_code.config.harness_attribution import with_harness_id
from my_claude_code.config.harnesses import (
    HARNESS_SPECS,
    HARNESSES_WITHOUT_ATTRIBUTION_HEADER,
    MCC_HARNESS_ID_SENTINEL,
    catalogue_specs,
    harness_spec,
)
from my_claude_code.core.client_fingerprint import HARNESS_HEADER
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


# --------------------------------------------------------- harness attribution
#
# MCC's launchers tell the proxy which coding agent they started, with the
# ``x-mcc-harness`` request header. For a harness configured from a *document*
# the header lives in the generated document, and the tests below are the
# record of which documents that is and which it deliberately is not.

#: Catalogue formats whose CLI publishes somewhere to put a custom request
#: header, with the key it publishes. The key is the CLI's own spelling and is
#: restated here rather than imported, because a serialiser that silently
#: renamed it would still pass a test that read the name back out of it.
FORMATS_WITH_AN_ATTRIBUTION_HEADER: dict[str, tuple[str, ...]] = {
    # ``provider.mcc.options`` is handed to ``@ai-sdk/anthropic``'s
    # ``createAnthropic``, which merges ``headers`` into every request. This
    # is the key PR #258 removed an ``Authorization`` override from; what
    # goes back is a label, not a credential.
    "opencode": ("provider", "mcc", "options", "headers"),
    "kilo": ("provider", "mcc", "options", "headers"),
    "commandcode": ("provider", "mcc", "headers"),
    # ``providers.<id>.extra_headers``, "Additional HTTP headers to send with
    # requests" in Crush's own schema -- tests/fixtures/schemas/crush.schema.json.
    "crush": ("providers", "mcc", "extra_headers"),
    # Inside ``settings``: Cline discards the whole document on an unknown
    # *root* key. See ``config/harness_cline.strip_mcc_keys``.
    "cline": ("providers", "openai-compatible", "settings", "headers"),
}

#: Qwen Code's is a sixth, and it is not in the table above because its header
#: map is per *model* -- ``modelProviders.anthropic[].generationConfig
#: .customHeaders`` -- so there is no one path to state. It has a test of its
#: own below.
QWEN_FORMAT = "qwen"

#: Everything else, derived rather than listed, so a catalogue format added
#: later lands in exactly one of these three sets and cannot quietly land in
#: none. Four of these have a hook elsewhere -- Codex takes a ``-c``
#: assignment, Gemini CLI and Claude Code an environment variable, Pi an
#: argument to ``registerProvider`` in its bundled extension -- and the rest
#: have no hook at all, which ``HARNESSES_WITHOUT_ATTRIBUTION_HEADER`` in
#: ``config/harnesses.py`` records with the reason per harness.
FORMATS_WITHOUT_AN_ATTRIBUTION_HEADER = (
    frozenset(SERIALISERS) - set(FORMATS_WITH_AN_ATTRIBUTION_HEADER) - {QWEN_FORMAT}
)


def _attribution_model() -> CatalogueModel:
    return CatalogueModel(
        gateway_id="anthropic/openrouter/sonnet",
        provider_model_ref="openrouter/sonnet",
        display_name="openrouter/sonnet",
        context_length=131072,
        max_output_tokens=16384,
    )


def _at_path(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = document
    for key in path:
        assert isinstance(node, dict), path
        assert key in node, f"missing {key} of {path}"
        node = node[key]
    return node


def _header_values(node: object) -> list[str]:
    """Return every value bound to the attribution header, at any depth."""

    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() == HARNESS_HEADER:
                found.append(str(value))
            found.extend(_header_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_header_values(item))
    return found


@pytest.mark.parametrize("format_id", sorted(FORMATS_WITH_AN_ATTRIBUTION_HEADER))
def test_a_header_capable_format_writes_the_sentinel_where_its_cli_reads_it(
    format_id: str,
) -> None:
    """The header is at the CLI's own key, and its value is not yet an id.

    Not an id, because it cannot be one here: ``build_opencode_catalogue``
    serves ``opencode``, ``opencode2`` and ``kilo``, and a pure function of the
    model records cannot tell them apart. The caller resolves the sentinel.
    """

    document, _ = serialise(format_id, [_attribution_model()])

    headers = _at_path(document, FORMATS_WITH_AN_ATTRIBUTION_HEADER[format_id])
    assert headers == {HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL}
    # The version companion is explicitly out of scope: MCC never probes a
    # harness binary for its version, and a version it does not have is worse
    # than none.
    assert "x-mcc-harness-version" not in str(document)


def test_qwen_hangs_its_headers_off_every_models_generation_config() -> None:
    """Qwen Code's header map is per model, not per provider."""

    document, _ = serialise(QWEN_FORMAT, [_attribution_model()])

    entries = model_entries(QWEN_FORMAT, document)
    assert entries
    for entry in entries:
        assert entry["generationConfig"]["customHeaders"] == {
            HARNESS_HEADER: MCC_HARNESS_ID_SENTINEL
        }


@pytest.mark.parametrize("format_id", sorted(FORMATS_WITHOUT_AN_ATTRIBUTION_HEADER))
def test_a_format_with_no_documented_header_hook_gains_no_header(
    format_id: str,
) -> None:
    """No hook, no header -- and no sentinel smuggled in somewhere else either."""

    document, _ = serialise(format_id, [_attribution_model()])

    assert _header_values(document) == []
    assert MCC_HARNESS_ID_SENTINEL not in str(document)


def test_the_harnesses_with_no_hook_are_named_in_the_registry() -> None:
    """The 'no header' decision is data, so it can be checked rather than trusted."""

    ids = {spec.id for spec in HARNESS_SPECS}
    assert ids >= HARNESSES_WITHOUT_ATTRIBUTION_HEADER

    for harness_id in HARNESSES_WITHOUT_ATTRIBUTION_HEADER:
        catalogue = harness_spec(harness_id).catalogue
        if catalogue is None:
            # Goose and Antigravity have no generated document at all.
            continue
        document, _ = serialise(catalogue.format_id, [_attribution_model()])
        assert _header_values(document) == [], harness_id


def test_every_harness_that_does_get_a_header_resolves_to_its_registry_id() -> None:
    """The value on disk is the registry id, spelled the way the log keys on it."""

    for harness_id in (
        "opencode",
        "opencode2",
        "kilo",
        "commandcode_cli",
        "qwen_code",
        "crush",
        "cline_cli",
    ):
        spec = harness_spec(harness_id)
        assert spec.catalogue is not None
        document, _ = serialise(spec.catalogue.format_id, [_attribution_model()])
        resolved = with_harness_id(document, spec.id)
        values = _header_values(resolved)
        assert values and set(values) == {spec.id}, harness_id
        assert MCC_HARNESS_ID_SENTINEL not in str(resolved)
        # The sentinel's own shape, so a *partially* substituted document --
        # one branch resolved and another missed -- fails here too.
        assert "{{" not in str(resolved)
