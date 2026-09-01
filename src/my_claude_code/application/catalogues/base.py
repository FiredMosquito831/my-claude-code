"""Shared rules every harness catalogue serialiser obeys.

**Unknown stays unknown.** This is the load-bearing rule of the whole layer.
The resolution ladder distinguishes three states per capability -- unknown,
known-and-absent, known-and-present -- and a serialiser that collapses the
first two destroys the only information that made the record worth building.
Concretely:

* Where the CLI's schema makes a field **optional**, a ``None`` from the ladder
  means *omit the key entirely*. Never write ``0``, and never write ``null``
  where the CLI would read it as a real value.
* Where the CLI's schema makes a field **required**, use *that CLI's own
  documented default* -- never a number MCC invented, and never another CLI's
  default -- and record the substitution through :class:`DefaultedFields` so it
  reaches the generated file, the launcher's stderr summary and the Coding
  agents dashboard card. A reader must be able to tell which numbers are the
  CLI's guess from which are their provider's answer.

Each serialiser module declares its CLI's defaults in a module-level dict named
exactly ``CLI_DOCUMENTED_DEFAULTS``. That name is not decoration:
``tests/application/test_serialiser_contract.py::test_no_serialiser_hard_codes_a_limit``
AST-scans this package and fails on any large integer literal bound to a
context/limit/token-shaped key outside that dict. It is the test that stops
``200000`` coming back.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.core.reasoning import ReasoningEffort

#: Key carrying the defaulted-field record inside a generated catalogue.
DEFAULTED_KEY = "_mcc_defaulted"

#: MCC's effort ladder, weakest first. Serialisers walk this to find the
#: nearest rung in their own CLI's vocabulary.
EFFORT_ORDER: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.MINIMAL,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)


@dataclass
class DefaultedFields:
    """Which fields a serialiser had to fill in from the CLI's own defaults."""

    by_model: dict[str, list[str]] = field(default_factory=dict)

    def record(self, model_key: str, field_name: str) -> None:
        """Note that ``field_name`` for ``model_key`` is the CLI's guess."""

        names = self.by_model.setdefault(model_key, [])
        if field_name not in names:
            names.append(field_name)

    def as_document(self) -> dict[str, list[str]]:
        """Return the record in the shape written into the catalogue file."""

        return {key: list(value) for key, value in sorted(self.by_model.items())}

    def summary_lines(self) -> list[str]:
        """Return one human-readable line per model that needed a default."""

        return [
            f"{model_key}: {', '.join(names)}"
            for model_key, names in sorted(self.by_model.items())
        ]

    @property
    def model_count(self) -> int:
        """Return how many models carry at least one defaulted field."""

        return len(self.by_model)


def visible_entries(models: Iterable[CatalogueModel]) -> list[CatalogueModel]:
    """Drop the no-thinking variant of any ref whose normal variant is present.

    The two ids exist so Claude Code's client-side heuristic can be used to
    turn thinking off. Every other CLI picks the model from a list, so
    offering both would double the list for no gain; the no-thinking entry is
    kept only when it is the *only* way to reach that ref.
    """

    entries = list(models)
    normal_refs = {
        entry.provider_model_ref for entry in entries if not entry.force_no_thinking
    }
    return [
        entry
        for entry in entries
        if not (entry.force_no_thinking and entry.provider_model_ref in normal_refs)
    ]


def clamp_efforts(
    reasoning: ModelReasoningCapability | None,
    cli_vocabulary: Sequence[str],
    mapping: Mapping[ReasoningEffort, str],
) -> tuple[list[str], bool]:
    """Intersect a model's effort vocabulary with one CLI's own rungs.

    Returns ``(rungs, unknown)``. ``rungs`` is in the CLI's own order and never
    contains a rung the model does not support -- Codex's ``xhigh`` disappears
    for a model that has never claimed it. ``unknown`` is True only in the
    genuinely-unknown case, where the caller must fall back to the CLI's
    documented default and record that it did.

    The rules, in the order they are applied:

    1. ``can_reason is False`` -- the model does not reason. No rungs, known.
    2. ``supported_efforts`` non-empty -- map each effort onto the nearest rung
       this CLI actually has, drop what has no counterpart, emit in CLI order.
    3. ``supported_efforts`` empty, or ``supports_effort_control is False`` --
       the model reasons but exposes no knob. No rungs, known.
    4. ``supported_efforts is None`` -- unknown; the caller decides.
    """

    if reasoning is None:
        return [], True
    if reasoning.can_reason is False:
        return [], False
    if reasoning.supports_effort_control is False:
        return [], False
    efforts = reasoning.supported_efforts
    if efforts is None:
        if reasoning.can_reason is True and reasoning.supports_effort_control is None:
            # Known to reason, nothing published about how it is steered.
            return [], True
        return [], True
    if not efforts:
        return [], False

    chosen: set[str] = set()
    for effort in efforts:
        rung = _nearest_rung(effort, cli_vocabulary, mapping)
        if rung is not None:
            chosen.add(rung)
    return [rung for rung in cli_vocabulary if rung in chosen], False


def _nearest_rung(
    effort: ReasoningEffort,
    cli_vocabulary: Sequence[str],
    mapping: Mapping[ReasoningEffort, str],
) -> str | None:
    direct = mapping.get(effort)
    if direct is not None and direct in cli_vocabulary:
        return direct
    if effort not in EFFORT_ORDER:
        return None
    index = EFFORT_ORDER.index(effort)
    # Walk outwards from the model's own rung, weaker side first: a CLI that
    # cannot express "max" should be told "high", not "low".
    for distance in range(1, len(EFFORT_ORDER)):
        for candidate_index in (index - distance, index + distance):
            if not 0 <= candidate_index < len(EFFORT_ORDER):
                continue
            candidate = mapping.get(EFFORT_ORDER[candidate_index])
            if candidate is not None and candidate in cli_vocabulary:
                return candidate
    return None


def reasoning_is_mandatory(reasoning: ModelReasoningCapability | None) -> bool:
    """Return whether thinking is known to be impossible to turn off."""

    return reasoning is not None and reasoning.mandatory is True


def can_reason(reasoning: ModelReasoningCapability | None) -> bool | None:
    """Return the model's known reasoning support, or ``None`` when unknown."""

    return None if reasoning is None else reasoning.can_reason
