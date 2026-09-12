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
from dataclasses import dataclass, field
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


#: The three characters that make a pattern a glob, per :mod:`fnmatch`. A
#: pattern containing none of them matches exactly one string -- itself -- so
#: ``fnmatchcase(candidate, pattern)`` and ``candidate == pattern`` are the
#: same test, and the second is a hash lookup instead of a translated regex.
_GLOB_CHARACTERS = "*?["


def is_glob(pattern: str) -> bool:
    """Whether ``pattern`` can match anything other than itself."""

    return any(character in pattern for character in _GLOB_CHARACTERS)


@dataclass(frozen=True, slots=True)
class ModelVisibility:
    """Hide-only allow/deny filter over provider-prefixed model refs."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    # Derived once per filter, from the two lists above, and never part of the
    # value: `compare=False` keeps equality and hashing the pair of tuples they
    # have always been, so nothing that stores or compares a ModelVisibility
    # changes behaviour.
    #
    # Why they exist: a real installation reaches a thousand patterns, and
    # almost all of them are exact refs written by the per-model ticks rather
    # than globs a person typed. Measured on one such configuration -- 1,160
    # refs, 996 deny patterns of which **995 are exact and one is a glob** --
    # the Models page spent 5.88 s in `fnmatchcase`: 1.33 s in `is_visible`,
    # 3.24 s in `hiding_pattern` and 1.32 s in `visibility_payload`. That is
    # 1.16 M translated-regex matches to answer a question that is a set
    # lookup for 99.9% of them.
    _allow_exact: frozenset[str] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _allow_globs: tuple[str, ...] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _deny_exact: frozenset[str] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _deny_globs: tuple[str, ...] = field(
        init=False, repr=False, compare=False, hash=False
    )
    #: Where each deny pattern sits in the declared list, and the globs with
    #: their positions -- both only so ``first_deny_match`` can answer "which
    #: pattern, in order" without walking the list.
    _deny_index: dict[str, int] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _deny_globs_indexed: tuple[tuple[int, str], ...] = field(
        init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        # `object.__setattr__` because the dataclass is frozen: these are
        # computed *from* the fields, at construction, and never again.
        for name, patterns in (("allow", self.allow), ("deny", self.deny)):
            object.__setattr__(
                self,
                f"_{name}_exact",
                frozenset(p for p in patterns if not is_glob(p)),
            )
            object.__setattr__(
                self,
                f"_{name}_globs",
                tuple(p for p in patterns if is_glob(p)),
            )
        object.__setattr__(
            self,
            "_deny_index",
            {pattern: index for index, pattern in enumerate(self.deny)},
        )
        object.__setattr__(
            self,
            "_deny_globs_indexed",
            tuple(
                (index, pattern)
                for index, pattern in enumerate(self.deny)
                if is_glob(pattern)
            ),
        )

    @classmethod
    def from_raw(cls, allow: str | None, deny: str | None) -> Self:
        """Build a filter from the two comma-separated env values."""

        return cls(parse_model_patterns(allow), parse_model_patterns(deny))

    def _matches_allow(self, candidate: str) -> bool:
        """Whether the allow list lets ``candidate`` through.

        Exactly ``any(fnmatchcase(candidate, p) for p in self.allow)``, with
        the wildcard-free patterns answered by set membership. Callers must
        have case-folded and stripped ``candidate`` already.
        """

        if candidate in self._allow_exact:
            return True
        return any(fnmatchcase(candidate, pattern) for pattern in self._allow_globs)

    def _matches_deny(self, candidate: str) -> bool:
        """Whether the deny list hides ``candidate``. Same contract as above."""

        if candidate in self._deny_exact:
            return True
        return any(fnmatchcase(candidate, pattern) for pattern in self._deny_globs)

    def first_deny_match(self, candidate: str, *, ignoring: str = "") -> str:
        """The first deny pattern matching ``candidate``, in declaration order.

        *Which* pattern matched is part of the answer the Models page gives --
        a row reads "Hidden by nous_portal/\\*" and that has to name the first
        pattern the list declares, not merely some pattern that matches. So the
        order is preserved exactly; only the search is.

        A wildcard-free pattern matches exactly one candidate, so of the
        hundreds of exact patterns a real deny list holds, at most one can be
        the answer and its position is known from an index built once. The
        globs are walked in order, and the walk stops as soon as it passes that
        position -- everything after it is later in the list than a pattern
        that already matched. On the configuration this was measured against
        that is one comparison instead of nine hundred.

        ``ignoring`` skips one pattern, which is how the page distinguishes a
        row hidden by its own tick from a row hidden by somebody's glob.
        ``candidate`` must already be stripped and case-folded.
        """

        own_index = self._deny_index.get(candidate)
        if candidate == ignoring:
            own_index = None
        for index, pattern in self._deny_globs_indexed:
            if own_index is not None and index > own_index:
                break
            if pattern == ignoring:
                continue
            if fnmatchcase(candidate, pattern):
                return pattern
        if own_index is not None:
            return candidate
        return ""

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
        if self.allow and not self._matches_allow(candidate):
            return False
        return not self._matches_deny(candidate)

    def visible(self, model_refs: Iterable[str]) -> Iterator[str]:
        """Yield only the refs that should be listed, in the order given."""

        return (model_ref for model_ref in model_refs if self.is_visible(model_ref))
