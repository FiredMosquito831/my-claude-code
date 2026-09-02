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
# The page a routing decision is edited on. Pausing a model is not a limit --
# sending its reader to Limits & Resilience would be a hint that names a page
# with no such control on it, which is worse than no hint at all.
MODEL_CONFIG_PAGE_LABEL = "Model Config"
# The page a credential is added or replaced on. An exhausted balance is not a
# limit and not a routing decision: nothing on Limits & Resilience or Model
# Config tops up an account, and the only action that clears the failure is
# adding or funding a key, which happens here.
PROVIDERS_PAGE_LABEL = "Providers"

# Env var -> the manifest section id whose card owns it. The label is looked up
# from SECTIONS, so it is the same string the page renders.
_SECTION_FOR_ENV_VAR: dict[str, str] = {
    "FALLBACK_FIRST_TOKEN_TIMEOUT": "deadlines",
    "FALLBACK_ATTEMPT_SHARE_FLOOR": "deadlines",
    "FALLBACK_TOTAL_TIMEOUT": "deadlines",
    "FALLBACK_STALL_TIMEOUT": "deadlines",
    "FALLBACK_REASONING_ANSWER_TIMEOUT": "deadlines",
    "RATE_LIMIT_COOLDOWN_SECONDS": "credential_health",
    "MODEL_PAUSED": "models",
    "MODEL_FABLE_PAUSED": "models",
    "MODEL_OPUS_PAUSED": "models",
    "MODEL_SONNET_PAUSED": "models",
    "MODEL_HAIKU_PAUSED": "models",
    "MODEL_VISION_PAUSED": "models",
}

# Manifest section id -> the dashboard page its card is rendered on. A single
# hardcoded page label was correct while every hinted setting lived on one
# page; it stopped being correct the moment a routing switch needed a hint.
_PAGE_FOR_SECTION: dict[str, str] = {
    "deadlines": LIMITS_PAGE_LABEL,
    "credential_health": LIMITS_PAGE_LABEL,
    "models": MODEL_CONFIG_PAGE_LABEL,
}

_CARD_LABELS: dict[str, str] = {
    section.section_id: section.label for section in SECTIONS
}


def card_for(env_var: str) -> str:
    """The manifest's own label for the card that edits ``env_var``."""
    return _CARD_LABELS[_SECTION_FOR_ENV_VAR[env_var]]


def page_for(env_var: str) -> str:
    """The dashboard page the card that edits ``env_var`` is rendered on."""
    return _PAGE_FOR_SECTION[_SECTION_FOR_ENV_VAR[env_var]]


def providers_hint() -> str:
    """The trailing pointer on a failure only a new or funded key can fix.

    Not built from ``_SECTION_FOR_ENV_VAR``: there is no env var to name --
    the operator does not edit a number, they pay a bill or paste a key. Same
    plain-ASCII shape as :func:`limit_hint` so the two read alike wherever a
    client prints them side by side.
    """
    return f" (add or top up a key on the dashboard under {PROVIDERS_PAGE_LABEL})"


def limit_hint(env_var: str) -> str:
    """The trailing hint appended to a message the client will read.

    Plain ASCII on purpose. This text is carried by an SSE error frame, a JSON
    error body, a request-log row and a terminal, and an arrow that renders as
    a replacement character in one of them costs more than it buys.
    """
    return (
        f" ({env_var} -- change it on the dashboard under "
        f"{page_for(env_var)} -> {card_for(env_var)})"
    )
