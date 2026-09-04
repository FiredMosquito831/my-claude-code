"""Every UI control the docs tell you to press must actually exist.

Part IX has carried "docs drift is unguarded" as a known gap for a long time,
and it bit for real in 4.40.x: the in-dashboard Guide and ``docs/USAGE.md`` both
still said "paste a key, press Validate, then Apply and select it as active"
after the Providers page had been rebuilt around Configure, a key pool, and
Refresh models. A walkthrough that names a button which no longer exists is
worse than no walkthrough, because it is read by someone who does not yet know
the app and cannot tell the difference between "I misread this" and "this lies".

This does not try to check prose for accuracy — nothing can. It checks the one
mechanical thing that goes stale first and silently: control names.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO_ROOT / "src/my_claude_code/api/admin_static"

# Control labels the user-facing docs instruct the reader to press or look for.
# Add to this when the docs start naming a new control; the point is that a
# rename in the UI then fails here instead of quietly making the docs wrong.
DOCUMENTED_CONTROLS: tuple[str, ...] = (
    "Search providers",
    "Only configured",
    "Configure",
    "Add key",
    "Remove",
    "Rotation",
    "Refresh models",
    "Test connection",
    "Validate",
    "Apply",
    "Manage keys",
    # Model Config: the guide and the README tell you to press these.
    "Add fallback",
    "Vision adapter",
    # Analytics: the docs send the reader to these by name to explain why the
    # windowed totals stop rising.
    "All time",
    "Clear log",
    "Export",
)


def _ui_surface() -> tuple[str, str]:
    """Return (script, markup-without-the-Guide).

    The in-dashboard Guide lives inside ``index.html``, so searching that file
    wholesale makes this check circular: it finds every label in the Guide's own
    prose and passes whether or not the control still exists. The Guide view is
    therefore cut out.
    """
    script = (ADMIN_STATIC / "admin.js").read_text(encoding="utf-8")
    markup = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

    start = markup.index('<section id="view-guide"')
    end = markup.index("</section>", markup.index('id="guide-keys"'))
    chrome = markup[:start] + markup[end:]
    assert '<section id="view-guide"' not in chrome

    return script, chrome


def _defines_control(label: str) -> bool:
    """True when ``label`` is used *as a control label*, not merely mentioned.

    A plain substring search over the whole file is too loose to be a guard. It
    matches prose as readily as a button: renaming the two real "Refresh models"
    buttons still left the hint string "No discovered models. Refresh models or
    enter a custom slug.", so a substring check passed a UI that no longer had
    the button. Both weaknesses in this test were found by breaking it on
    purpose, which is the only way to know a guard works.
    """
    script, chrome = _ui_surface()

    quoted_in_script = any(
        f"{quote}{label}{quote}" in script for quote in ('"', "'", "`")
    )
    tag_text_in_markup = f">{label}<" in " ".join(chrome.split())
    return quoted_in_script or tag_text_in_markup


@pytest.mark.parametrize("label", DOCUMENTED_CONTROLS)
def test_documented_control_exists_in_the_dashboard(label: str) -> None:
    assert _defines_control(label), (
        f"The docs name a control {label!r} that the dashboard no longer "
        "defines. Either restore it, or update docs/USAGE.md, the in-dashboard "
        "Guide and the README together."
    )


def test_guide_and_usage_describe_the_current_provider_flow() -> None:
    """Pin the specific claims that were wrong, so they cannot regress.

    Keys added through the pool apply immediately, and there is no "active
    provider" to select -- the model ref on Model Config decides who serves a
    request. Both of those were stated incorrectly before 4.40.2.
    """
    usage = (REPO_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    guide = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

    for name, text in (("USAGE.md", usage), ("the Guide", guide)):
        assert "Configure" in text, f"{name} does not mention Configure"
        assert "Refresh models" in text, f"{name} does not mention Refresh models"

    # The two concrete falsehoods that shipped.
    assert "select it as active" not in guide
    assert "**Select** that provider as the active one" not in usage


def test_readme_points_at_configure_rather_than_a_removed_path() -> None:
    """The README used to route the reader to "Providers -> Manage keys"."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Providers → Manage keys" not in readme
    assert "**Configure**" in readme
    assert "Refresh models" in readme


def test_all_three_docs_explain_that_stored_rows_are_capped() -> None:
    """The gap that produced a real bug report.

    A user watched their request count sit at ~50,000 and their per-model token
    usage stop moving, and concluded the dashboard was broken. It was not: the
    cap was doing exactly what it says, and nothing anywhere told them that the
    figures derived from those rows are a rolling window. Whichever doc they
    reach for has to say so.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    usage = (REPO_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    guide = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

    for name, text in (("README", readme), ("USAGE.md", usage), ("the Guide", guide)):
        assert "REQUEST_LOG_MAX_ROWS" in text, f"{name} never names the cap"
        assert "All time" in text, f"{name} does not explain All time"
        lowered = text.lower()
        assert "rolling window" in lowered or "roll over" in lowered, (
            f"{name} does not say the windowed figures stop rising at the cap"
        )


def test_docs_say_search_covers_reasoning_and_tool_calls() -> None:
    """Search silently covered only the prompt and reply until v4.46.0.

    Reasoning and tool calls are the majority of a real log, so a reader who
    assumes search sees everything will conclude requests are missing rather
    than that search was narrow. Whichever doc they reach for has to say what
    is actually searched.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    usage = (REPO_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    guide = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

    for name, text in (("README", readme), ("USAGE.md", usage), ("the Guide", guide)):
        lowered = text.lower()
        assert "reasoning" in lowered, f"{name} does not say reasoning is searched"
        assert "tool call" in lowered, f"{name} does not say tool calls are searched"


def test_the_search_box_says_what_it_searches() -> None:
    markup = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")
    box = markup[markup.index('id="reqFilterSearch"') :][:600]
    assert "reasoning" in box and "tool calls" in box, (
        "The search placeholder must state that it covers reasoning and tool "
        "calls; a narrower-looking box makes people distrust the results."
    )


def test_all_three_docs_say_a_silent_model_is_a_failure() -> None:
    """The behaviour that made a configured fallback chain useless.

    A model that accepted a request and then produced nothing held it until the
    transport read timeout, so the chain fired minutes after the client had
    given up. Whichever doc a reader reaches for has to say that silence counts
    and that a deadline bounds it, or the setting looks like an oddity rather
    than the thing that makes failover work.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    usage = (REPO_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    guide = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

    for name, text in (("README", readme), ("USAGE.md", usage), ("the Guide", guide)):
        lowered = text.lower()
        assert "first-token deadline" in lowered or "first token" in lowered, (
            f"{name} never mentions the first-token deadline"
        )
        assert "silent" in lowered or "goes quiet" in lowered, (
            f"{name} does not say a silent model counts as a failure"
        )
        assert "bench" in lowered, f"{name} does not explain ejecting a bad model"


def test_docs_say_a_blank_limit_falls_back_to_its_default() -> None:
    """Clearing a field used to stop the server from starting."""
    usage = (REPO_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    guide = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

    for name, text in (("USAGE.md", usage), ("the Guide", guide)):
        lowered = text.lower()
        assert "blank" in lowered, f"{name} never mentions a blank value"
        assert "default" in lowered, f"{name} does not say blank means the default"


# ---------------------------------------------------------------------------
# The Anthropic subscription disclaimer
#
# This one is not a label check. ``anthropic_oauth`` uses a credential in a way
# Anthropic's published terms forbid, and the only thing standing between a
# user and an account-level consequence is that the docs say so. A future edit
# that trims the warning for brevity, or renames the doc without updating the
# links, would leave the provider shipped and the disclaimer gone -- which is
# exactly the drift every other guard in this file exists to stop.
# ---------------------------------------------------------------------------


def test_the_subscription_disclaimer_exists_and_says_it_is_not_permitted() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    doc = repo_root / "docs" / "ANTHROPIC-SUBSCRIPTION.md"

    assert doc.is_file(), (
        "docs/ANTHROPIC-SUBSCRIPTION.md is the disclaimer for anthropic_oauth"
    )
    text = doc.read_text(encoding="utf-8")

    # The claim itself, and the misconception it exists to correct.
    assert "does not permit" in text
    assert "cc_entrypoint=cli" in text
    assert "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE" in text
    # The supported alternative must stay named, or the warning has no exit.
    assert "ANTHROPIC_API_KEY" in text
    # The primary source, so a reader can check rather than trust us.
    assert "code.claude.com/docs/en/legal-and-compliance" in text


def test_the_docs_that_offer_the_provider_link_the_disclaimer() -> None:
    """Anywhere anthropic_oauth is offered must point at the warning."""
    repo_root = Path(__file__).resolve().parents[2]

    for relative in ("README.md", "docs/USAGE.md"):
        text = (repo_root / relative).read_text(encoding="utf-8")
        if "anthropic_oauth" not in text and "Claude subscription" not in text:
            continue
        assert "ANTHROPIC-SUBSCRIPTION.md" in text, (
            f"{relative} offers the subscription provider without linking the disclaimer"
        )


def test_the_guide_warns_before_it_explains() -> None:
    """The in-dashboard Guide carries the warning, not just the repo docs."""
    repo_root = Path(__file__).resolve().parents[2]
    html = (repo_root / "src/my_claude_code/api/admin_static/index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="guide-claude-models"' in html
    assert "not permitted by" in html
    # The marker MCC reads, and the gate it feeds. Until 6.45.4 this asserted the
    # literal ``cc_entrypoint=cli``, which is the marker Claude Code's *CLI*
    # writes -- and the Guide grew a sentence around it saying an Agent SDK
    # script is refused. The gate has admitted ``sdk-cli``, ``sdk-py`` and
    # ``sdk-ts`` since 6.36.0, so the assertion was pinning the wrong half of
    # the sentence in place. Assert the marker and the admitted set instead.
    assert "cc_entrypoint" in html
    for entrypoint in ("cli-bg", "sdk-cli", "sdk-py", "sdk-ts"):
        assert entrypoint in html, (
            f"the Guide names the entrypoint gate without listing {entrypoint}"
        )
    # Rendered as a warning, not as ordinary prose.
    assert "guide-note-warn" in html
