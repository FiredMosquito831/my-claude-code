"""Which `provider/model` refs are *shown* in catalogues and pickers.

A gateway can publish hundreds of models -- `nous_portal` alone lists 343 --
and every one of them lands in `/v1/models` and in the Admin model pickers.
This is the one place that decides which of them are worth showing.

Two glob lists, both matched against the full ref (`nvidia_nim/openai/gpt-oss`),
both case-insensitive:

* an **allow** list, where empty means "allow everything"; a non-empty list
  makes visibility opt-in;
* a **deny** list, applied *after* allow, which wins.

An explicit model pick is just an exact-match pattern, so one mechanism serves
both "tick this model" and "write a glob" -- a UI that lets the user pick
models writes exact refs into the same two lists.

**Hide only.** Nothing here may affect routing. A model named in `MODEL`,
`MODEL_OPUS` or a `MODEL_*_FALLBACKS` chain still resolves and still serves
requests while hidden. That was a deliberate choice: a visibility filter that
silently broke a working fallback chain would be far worse than a chain entry
that is invisible but alive, because the breakage would surface as an outage
somewhere unrelated to the setting that caused it.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Self

MODEL_PATTERN_SEPARATOR = ","


def parse_model_patterns(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated glob list into unique, case-folded patterns.

    Blank and whitespace-only entries are dropped rather than rejected: a
    trailing comma or a list typed across two lines is a formatting accident,
    not a configuration error, and an empty pattern would otherwise match
    nothing while looking like it matched everything.
    """

    patterns: list[str] = []
    for candidate in (raw or "").split(MODEL_PATTERN_SEPARATOR):
        pattern = candidate.strip().casefold()
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return tuple(patterns)


@dataclass(frozen=True, slots=True)
class ModelVisibility:
    """Hide-only allow/deny filter over provider-prefixed model refs."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, allow: str | None, deny: str | None) -> Self:
        """Build a filter from the two comma-separated env values."""

        return cls(parse_model_patterns(allow), parse_model_patterns(deny))

    @property
    def hides_anything(self) -> bool:
        """Whether this filter can hide a model at all."""

        return bool(self.allow or self.deny)

    def is_visible(self, model_ref: str) -> bool:
        """Whether `model_ref` should be listed."""

        # `fnmatchcase` on pre-folded strings rather than `fnmatch`: `fnmatch`
        # runs both sides through `os.path.normcase`, which on Windows also
        # rewrites `/` as `\` -- so the same pattern would behave differently
        # per platform on refs that are built out of slashes.
        candidate = model_ref.strip().casefold()
        if self.allow and not any(
            fnmatchcase(candidate, pattern) for pattern in self.allow
        ):
            return False
        return not any(fnmatchcase(candidate, pattern) for pattern in self.deny)

    def visible(self, model_refs: Iterable[str]) -> Iterator[str]:
        """Yield only the refs that should be listed, in the order given."""

        return (model_ref for model_ref in model_refs if self.is_visible(model_ref))
