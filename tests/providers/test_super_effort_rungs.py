"""`max` is what a client asks for; which word that is belongs to the host.

Some hosts publish rungs above MCC's own top one. The installed
`@openai/codex` **0.154.0** binary's serde enum spells
`none|minimal|low|medium|high|xhigh|max|ultra|persistent` -- read out of
`codex.exe`'s strings on 2026-09-14; the binary was never run.

Decision Q7 (2026-09-13) is that `ReasoningEffort` does **not** grow members
for those. A rung in that enum is something a client may ask for, and no client
asks for `ultra`: what a client asks for is `max`, "the most this model will
do". So the translation lives in the dialect, per model, built from what the
host itself named.

What that fixes as well as adds: the eight-word Codex vocabulary used to fall
into `learned_effort_values`'s positional spread -- six rungs scattered across
eight unknown words by index -- which mapped `high` onto `xhigh` and `xhigh`
onto `max`. A vocabulary that contains MCC's own words verbatim was being read
as if it contained none of them.
"""

import pytest

from my_claude_code.config.reasoning_enum import (
    KNOWN_EFFORT_WORDS,
    SUPER_EFFORT_WORDS,
    parse_effort_enum,
)
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.providers.openai_chat.learned_dialect import (
    learned_effort_values,
    learned_named_effort_reasoning,
)

#: Exactly what the installed binary spells, in its own order.
CODEX_0_154_0 = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "persistent",
)


def _mapping(words: tuple[str, ...]) -> dict[str, str]:
    return {rung.value: word for rung, word in learned_effort_values(words)}


class TestTheCodexVocabulary:
    def test_every_mcc_rung_maps_to_itself_and_max_becomes_ultra(self) -> None:
        """The whole feature, on the vocabulary it was read from."""

        scale = tuple(word for word in CODEX_0_154_0 if word != "none")

        assert _mapping(scale) == {
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "ultra",
        }

    def test_before_this_the_same_vocabulary_mis_ranked_the_middle(self) -> None:
        """The positional spread's answer, computed here so the regression is
        visible rather than described: it is what eight unknown words get, and
        it is wrong for eight words six of which are MCC's own."""

        scale = tuple(word for word in CODEX_0_154_0 if word != "none")
        rungs = tuple(ReasoningEffort)
        count = len(scale)
        spread = {
            rung.value: scale[min(count - 1, index * count // len(rungs))]
            for index, rung in enumerate(rungs)
        }

        assert spread["high"] == "xhigh"
        assert spread["xhigh"] == "max"
        assert _mapping(scale)["high"] == "high"
        assert _mapping(scale)["xhigh"] == "xhigh"

    def test_the_off_word_is_still_the_off_word(self) -> None:
        """`none` leaves the scale and becomes `disabled_value`, unchanged."""

        encoder = learned_named_effort_reasoning(CODEX_0_154_0)

        assert encoder is not None
        assert encoder.disabled_value == "none"
        assert {rung.value: word for rung, word in encoder.efforts}["max"] == "ultra"


class TestWhichSuperRungWins:
    def test_ultra_wins_over_persistent(self) -> None:
        """`ultra` is unambiguously more effort; `persistent` reads as a mode,
        and a client asking for the most effort has not asked to change modes."""

        assert (
            _mapping(
                (
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                    "persistent",
                )
            )["max"]
            == "ultra"
        )

    def test_persistent_is_used_only_when_it_is_the_top(self) -> None:
        assert _mapping(("low", "medium", "high", "max", "persistent"))["max"] == (
            "persistent"
        )

    def test_a_super_rung_that_is_not_on_top_is_ignored(self) -> None:
        """It is not the answer to "the most this model will do", and MCC has
        no other use for it -- so the vocabulary falls back to the ranking it
        had before, rather than being given a wrong one."""

        mapping = _mapping(("low", "persistent", "medium", "high", "max"))

        assert mapping["max"] == "max"

    def test_a_host_that_names_neither_is_unchanged(self) -> None:
        assert _mapping(("low", "medium", "high", "max")) == {
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
            "max": "max",
        }

    def test_a_host_with_its_own_words_still_gets_the_spread(self) -> None:
        """No shared scale, so the order the host named is the only ranking on
        offer -- exactly as before."""

        assert _mapping(("brief", "detailed")) == {
            "minimal": "brief",
            "low": "brief",
            "medium": "brief",
            "high": "detailed",
            "xhigh": "detailed",
            "max": "detailed",
        }

    def test_a_super_rung_beside_words_of_the_hosts_own_is_not_a_ladder(self) -> None:
        """One familiar word in a foreign vocabulary does not make it MCC's
        scale with a top on it."""

        mapping = _mapping(("brief", "detailed", "ultra"))

        assert mapping["max"] == "ultra"  # the spread's own answer, not the ladder's
        assert mapping["minimal"] == "brief"


class TestTheVocabularyItself:
    def test_the_super_words_are_the_two_the_binary_spells(self) -> None:
        assert SUPER_EFFORT_WORDS == ("ultra", "persistent")

    def test_they_make_a_list_credible_as_an_effort_vocabulary(self) -> None:
        """`parse_effort_enum` scores a candidate list by how many of its words
        it recognises. A host that answers a 400 with Codex's own enum has to
        be recognised as naming one."""

        assert {"ultra", "persistent"} <= KNOWN_EFFORT_WORDS

    @pytest.mark.parametrize(
        "message",
        [
            "invalid value: expected one of minimal, low, medium, high, "
            "xhigh, max, ultra, persistent",
            "'bogus' is not one of ['low', 'medium', 'high', 'max', 'ultra']",
        ],
    )
    def test_a_rejection_naming_them_is_parsed(self, message: str) -> None:
        words = parse_effort_enum(message, sent="bogus")

        assert "ultra" in words
        assert "bogus" not in words

    def test_mcc_gained_no_rungs(self) -> None:
        """Decision Q7, pinned. The enum is what a client may ask for."""

        assert [effort.value for effort in ReasoningEffort] == [
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
