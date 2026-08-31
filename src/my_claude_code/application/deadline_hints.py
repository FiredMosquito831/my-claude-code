"""The sentence a limit adds to the error the client actually reads.

A request that MCC itself ended -- a deadline elapsed, a pool benched -- used
to arrive at Claude Code as a bare statement of fact: "produced no output
within 180s". True, and useless. The reader's next question is always the same
one ("where do I change that?") and the answer was three files and a search
away, which in practice meant the number stayed as it was.

So every such message now carries the env var that set the limit and the place
on the dashboard that edits it. The card labels are read out of the admin
manifest rather than written here, because a card renamed on the page and left
alone in an error string is a worse lie than no hint at all: the reader goes
looking for something that does not exist. ``tests/contracts`` pins the page
label against the shipped ``admin.js`` for the same reason.

Nothing in here decides anything. It formats.
"""

from my_claude_code.config.admin.manifest import SECTIONS

# The dashboard page every card below lives on. `admin.js` holds the nav label
# itself; `tests/contracts/test_deadline_hint_labels.py` pins the two equal so
# renaming one without the other fails a check rather than a user's search.
LIMITS_PAGE_LABEL = "Limits & Resilience"

# Env var -> the manifest section id whose card owns it. The label is looked up
# from SECTIONS, so it is the same string the page renders.
_SECTION_FOR_ENV_VAR: dict[str, str] = {
    "FALLBACK_FIRST_TOKEN_TIMEOUT": "deadlines",
    "FALLBACK_ATTEMPT_SHARE_FLOOR": "deadlines",
    "FALLBACK_TOTAL_TIMEOUT": "deadlines",
    "FALLBACK_STALL_TIMEOUT": "deadlines",
    "FALLBACK_REASONING_ANSWER_TIMEOUT": "deadlines",
    "RATE_LIMIT_COOLDOWN_SECONDS": "credential_health",
}

_CARD_LABELS: dict[str, str] = {
    section.section_id: section.label for section in SECTIONS
}


def card_for(env_var: str) -> str:
    """The manifest's own label for the card that edits ``env_var``."""
    return _CARD_LABELS[_SECTION_FOR_ENV_VAR[env_var]]


def limit_hint(env_var: str) -> str:
    """The trailing hint appended to a message the client will read.

    Plain ASCII on purpose. This text is carried by an SSE error frame, a JSON
    error body, a request-log row and a terminal, and an arrow that renders as
    a replacement character in one of them costs more than it buys.
    """
    return (
        f" ({env_var} -- change it on the dashboard under "
        f"{LIMITS_PAGE_LABEL} -> {card_for(env_var)})"
    )
