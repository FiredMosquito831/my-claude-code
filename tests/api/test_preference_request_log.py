"""Three facts about preferences that live outside the routing seam itself.

What the request log records, what an older build does with a file written by
this one, and that the derived Models-page cache notices a preference change.
"""

from my_claude_code.api.models_page_cache import capability_half_key
from my_claude_code.application.routing import (
    ModelRouter,
    RoutedMessagesRequest,
)
from my_claude_code.config import model_overrides as store
from my_claude_code.config.model_overrides import (
    ALLOWED_OVERRIDE_PARAMETERS,
    MAX_OUTPUT_TOKENS_OVERRIDE,
    REASONING_PREFERENCE_OVERRIDE,
    ModelParameterOverrides,
)
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.model_visibility import ModelVisibility

MODEL = "nvidia_nim/minimaxai/minimax-m3"


def routed(preferences=None) -> RoutedMessagesRequest:
    settings = Settings()
    settings.model = MODEL
    settings.model_fable = None
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    settings.reasoning_policy = ReasoningPreference.OFF
    settings.reasoning_fable = ReasoningPreference.INHERIT
    settings.reasoning_opus = ReasoningPreference.INHERIT
    settings.reasoning_sonnet = ReasoningPreference.INHERIT
    settings.reasoning_haiku = ReasoningPreference.INHERIT
    router = ModelRouter(
        settings,
        output_limit_lookup=lambda _p, _m: 200_000,
        model_preferences=(None if preferences is None else (lambda: preferences)),
    )
    return router.resolve_messages_request(
        MessagesRequest(
            model="claude-sonnet-4",
            messages=[Message(role="user", content="hi")],
        )
    )


def table(**row):
    return ModelParameterOverrides.from_document(
        {"providers": {}, "models": {MODEL: row}}
    )


# --------------------------------------------------------------------------- #
# The request log
# --------------------------------------------------------------------------- #


def test_a_preference_that_decided_the_policy_is_recorded_on_the_routed_request():
    """The ``params`` bag reads ``preference_sources`` and nothing else, so
    this is where the fact has to be true."""

    assert routed(
        table(**{REASONING_PREFERENCE_OVERRIDE: "high"})
    ).preference_sources == ((REASONING_PREFERENCE_OVERRIDE, "model"),)


def test_no_preference_writes_no_params_key():
    """Absence is the finding, exactly as it is for ``output_widened_from``."""

    assert routed().preference_sources == ()
    assert routed(ModelParameterOverrides()).preference_sources == ()


def test_an_old_row_without_the_key_still_renders():
    """The renderer branches on presence, so a default is never invented.

    Asserted at the source: the routed request's default is the empty tuple,
    which writes no key, which is what every row stored before this release
    looks like.
    """

    assert (
        RoutedMessagesRequest.__dataclass_fields__["preference_sources"].default == ()
    )


# --------------------------------------------------------------------------- #
# Downgrade (decision Q4: documented, not prevented)
# --------------------------------------------------------------------------- #


def test_an_older_build_reading_this_file_ignores_the_new_keys(monkeypatch):
    """An MCC below 7.25.0 has neither key in either allow-list.

    Its parser therefore logs "is not a known request parameter" and drops
    them, which is safe: the preference simply stops applying. Reproduced by
    restoring the 7.24.0 allow-lists rather than by importing an old build, so
    the guarantee is checked wherever the suite runs.
    """

    monkeypatch.setattr(
        store, "NON_BODY_OVERRIDE_PARAMETERS", frozenset({"response_surface"})
    )

    parsed = ModelParameterOverrides.from_document(
        {
            "providers": {},
            "models": {
                MODEL: {
                    REASONING_PREFERENCE_OVERRIDE: "max",
                    MAX_OUTPUT_TOKENS_OVERRIDE: 4096,
                    "temperature": 0.5,
                }
            },
        }
    )

    assert parsed.models[MODEL] == {"temperature": 0.5}
    # And the hazard the release notes name: the old build's own save would
    # write that row back WITHOUT the preferences.
    assert REASONING_PREFERENCE_OVERRIDE not in parsed.as_document()["models"][MODEL]


def test_the_body_allow_list_is_unchanged_by_this_release():
    """The nine sampling parameters are exactly the nine they were."""

    assert (
        frozenset(
            {
                "frequency_penalty",
                "min_p",
                "presence_penalty",
                "repetition_penalty",
                "seed",
                "stop",
                "temperature",
                "top_k",
                "top_p",
            }
        )
        == ALLOWED_OVERRIDE_PARAMETERS
    )


# --------------------------------------------------------------------------- #
# The 6.77.0 derived cache
# --------------------------------------------------------------------------- #


def test_a_preference_change_invalidates_the_capability_half():
    """No change to ``models_page_cache`` is needed, and this proves it: the
    key already hashes ``as_document()`` through a canonical serialiser."""

    visibility = ModelVisibility(allow=(), deny=())
    before = capability_half_key([], [], visibility, ModelParameterOverrides())
    after = capability_half_key(
        [], [], visibility, table(**{REASONING_PREFERENCE_OVERRIDE: "high"})
    )
    other = capability_half_key(
        [], [], visibility, table(**{REASONING_PREFERENCE_OVERRIDE: "low"})
    )

    assert before != after
    assert after != other
