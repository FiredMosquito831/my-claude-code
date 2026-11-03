"""RANGE — the custom time window, proved three ways.

``specs/INVESTIGATION-CUSTOM-TIME-WINDOWS.md`` §6, PR-CW-1/2/3, and its
binding USER DECISIONS block. Split from ``test_admin_static_jsdom.py`` only so
this release's proofs read as one document; everything here runs the real
``index.html``, ``admin.js`` and ``admin.css`` in jsdom through
``admin_jsdom_harness.mjs``.

What is asserted, and why each one exists:

1. **THE EQUALITY BASELINE** (the user's hard rule 2) — a baseline captured
   from the BASE tree (``6eb5551b``) by the same scenario with the same frozen
   clock, compared against this branch's run: every one of the five existing
   period values produces the byte-identical request it produced at 8.8.0, on
   the Analytics toolbar AND in the export dialog. A mutation that moves an
   existing period fails this.

2. **THE BOUNDARY TABLE** — with the browser's zone pinned, the exact epoch
   pair for every shape, including **October 2026 in UTC+2 being 745 hours**
   (not 744: the month starts at +03:00 and ends at +02:00), an end month
   included whole, a DST crossing both ways, and a leap day. This is the test
   the investigation's own first measurement would have failed.

3. **THE THREE SENTENCES, THE WARNING, AND THE SAVED VIEW** — each fires on its
   own fixture and stays silent on the cases it is not about.
"""

import json
import os
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import _run

BASELINE = Path(__file__).with_name("..") / "fixtures" / "range_windows_dom_8_8_0.json"

#: The instant the clock is frozen at for every run that compares URLs. It is
#: an epoch SECONDS value with a fractional part, so a helper that rounds `now`
#: before subtracting is caught: `now - 3600` and `floor(now) - 3600` differ by
#: the fraction, and `.toFixed(0)` of the DIFFERENCE is what 8.8.0 wrote.
FROZEN_NOW_MS = 1_793_551_232_000

#: The zone every boundary assertion is made in — the user's own (`GTB
#: Standard Time` / `Europe/Bucharest`: +02:00 standard, +03:00 in summer,
#: transitions the last Sunday of March at 03:00 local and the last Sunday of
#: October at 04:00 local). Pinned through `TZ`, which Node's `Intl` honours on
#: every platform, so the table below is a statement about a zone rather than
#: about whichever machine CI happens to run on.
ZONE = "Europe/Bucharest"

#: CI must not pass a suite it never ran (`MCC_CI=1` makes a missing node or a
#: missing jsdom a failure inside ``_run``).
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


def _run_pinned(**env_extra) -> dict:
    """One harness run with the zone and the clock pinned."""
    return _run(TZ=ZONE, MCC_JSDOM_NOW_MS=str(FROZEN_NOW_MS), **env_extra)


@pytest.fixture(scope="module")
def range_run() -> dict:
    return _run_pinned(MCC_JSDOM_SCENARIO="range_windows")


@pytest.fixture(scope="module")
def range_run_written(range_run: dict) -> dict:
    # Writing the baseline is a deliberate, one-off act: point
    # MCC_JSDOM_RANGE_BASELINE_OUT at a path and run this module against the
    # BASE tree's admin_static (STATIC_DIR replaced). Nothing in CI sets it.
    out = os.environ.get("MCC_JSDOM_RANGE_BASELINE_OUT")
    if out:
        Path(out).write_text(
            json.dumps(
                {
                    "base_commit": "6eb5551b",
                    "written_by": "BASE tree 6eb5551b (v8.8.0), scenario range_windows",
                    "frozen_now_ms": FROZEN_NOW_MS,
                    "zone": ZONE,
                    "toolbar": range_run["toolbar"],
                    "export": range_run["export"],
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return range_run


# --------------------------------------------------------------------------
# 0. the run itself
# --------------------------------------------------------------------------


def test_the_scenario_runs_clean(range_run) -> None:
    assert range_run["fatal"] is None
    assert range_run["scenarioFatal"] is None
    assert range_run["scriptErrors"] == []
    # And it really is the branch: the two custom shapes exist.
    assert range_run["baseTree"] is False
    assert range_run["timezone"] == ZONE
    assert range_run["frozenNowMs"] == FROZEN_NOW_MS


def test_the_comparison_is_not_vacuous(range_run) -> None:
    """The equality test below would also pass if nothing had been captured."""

    toolbar = range_run["toolbar"]
    assert sorted(toolbar) == ["2592000", "3600", "604800", "86400", "all"]
    for value, calls in toolbar.items():
        assert calls, value
        # Nine windowed routes per full load, every one of them carrying (or
        # deliberately not carrying) a bound.
        assert len(calls) == 9, (value, [entry["path"] for entry in calls])

    # And a bound goes where a bound belongs: every windowed route carries the
    # `since` its period resolves to, none carries an `until`, and the all-time
    # counters (`/admin/api/requests/lifetime`) carry neither -- they take no
    # window at all, by design (`admin_routes.py:5188`).
    def windowed(value):
        return [
            entry
            for entry in toolbar[value]
            if entry["path"] != "/admin/api/requests/lifetime"
        ]

    for value in ("3600", "86400", "604800", "2592000"):
        assert len(windowed(value)) == 8, value
        assert all("since" in entry for entry in windowed(value)), value
        assert not any("until" in entry for entry in toolbar[value]), value
    assert not any("since" in entry for entry in toolbar["all"])


# --------------------------------------------------------------------------
# 1. THE EQUALITY BASELINE — byte-identical where untouched
# --------------------------------------------------------------------------


def test_every_existing_period_sends_the_base_trees_requests(range_run_written) -> None:
    """TODAY MUST SEND BYTE-IDENTICAL REQUESTS. This is that proof.

    The baseline is the same scenario run against the BASE tree ``6eb5551b``
    (v8.8.0) with the same fixtures, the same zone and the same frozen clock.
    Every captured entry is ``{path, since?, until?, window?, shape?}``: the
    route asked for and the window-bearing parameters it carried, and nothing
    else -- a diff of whole URLs would mostly be a diff of the filter boxes the
    scenario left alone, which says nothing about this release.

    If this fails, a mutation has moved an existing period. That is the one
    outcome the user's governing constraint calls a blocker.
    """

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline["base_commit"] == "6eb5551b"
    assert "6eb5551b" in baseline["written_by"]
    assert baseline["frozen_now_ms"] == FROZEN_NOW_MS
    assert baseline["zone"] == ZONE

    assert range_run_written["toolbar"] == baseline["toolbar"], (
        "the Analytics toolbar's requests for one of the five existing periods "
        "are not what the base tree sent"
    )
    assert range_run_written["export"] == baseline["export"], (
        "the export dialog's requests for one of its existing periods are not "
        "what the base tree sent"
    )


def test_the_export_still_sends_no_bound_for_an_empty_custom_pair(
    range_run_written,
) -> None:
    """`Custom…` with both boxes empty must send neither bound, as it always did.

    This is the export's one legacy custom value, and the release that unifies
    the two controls is exactly the one that could accidentally start sending
    `since=undefined` or a resolved default.
    """

    assert range_run_written["export"]["custom"] == {}
    assert range_run_written["export"]["all"] == {}
    # And the four fixed ones still carry a `since` and no `until`.
    for value in ("3600", "86400", "604800", "2592000"):
        entry = range_run_written["export"][value]
        assert set(entry) == {"since"}, (value, entry)


def test_the_custom_inputs_are_invisible_on_every_existing_period(
    range_run_written,
) -> None:
    """The two new shapes add no visible control on any of the five values.

    The base tree has no such elements at all (the scenario reports ``null``
    there); on this branch they exist and are hidden for every fixed period, so
    the page a reader sees for "last 7d" is the page they saw before.
    """

    for value, hidden in range_run_written["customHidden"].items():
        assert hidden == {"custom": True, "range": True}, value


# --------------------------------------------------------------------------
# 2. THE BOUNDARY TABLE
# --------------------------------------------------------------------------
#
# Every number is an epoch SECOND, computed by the browser itself (Node 24's
# `Intl` implements the same IANA database Chromium, Edge and Firefox use) with
# the zone pinned to Europe/Bucharest. The two months around a transition are
# the point:
#
#   2026-03  span 743 h   (spring: 03:00 -> 04:00 on the last Sunday)
#   2026-10  span 745 h   (autumn: 04:00 -> 03:00 on the last Sunday)
#
# October is the case that proves the rule. It starts at +03:00 and ends at
# +02:00, because the clock goes back INSIDE it. Any helper that computes one
# offset and reuses it for both ends is wrong by an hour on exactly the months
# a reader is most likely to look at -- and the error is invisible, it just
# shifts a month boundary by 3,600 s of traffic. The investigation's own first
# measurement made that mistake; this table is why the test exists.

#: (input, since, until) as the browser resolves them.
MONTHS = [
    ("2026-09", 1788210000, 1790802000),
    ("2026-10", 1790802000, 1793484000),
    ("2026-11", 1793484000, 1796076000),
    ("2026-01", 1767218400, 1769896800),
    ("2026-03", 1772316000, 1774990800),
    ("2026-12", 1796076000, 1798754400),
]

HOURS = 3600


def test_the_regions_offset_is_what_this_table_assumes(range_run) -> None:
    """If the pinned zone did not take, every number below would be wrong for a
    reason that has nothing to do with the helper."""

    assert range_run["timezone"] == ZONE


@pytest.mark.parametrize(("window", "since", "until"), MONTHS)
def test_a_calendar_month_resolves_each_boundary_separately(
    range_run, window, since, until
) -> None:
    """The exact epoch pair the URL carries for one month, from the real control."""

    got = range_run["shapes"][f"month:{window}"]
    assert got["since"] == str(since), window
    assert got["until"] == str(until), window


def test_october_2026_is_745_hours_long(range_run) -> None:
    """THE CASE THAT PROVES THE RULE: an autumn month is one hour too long.

    1 October 2026 starts at UTC+03:00 and 1 November 2026 starts at UTC+02:00,
    because the clock goes back on the last Sunday of October. The month is
    therefore 745 hours, not 744 — and a helper that computed one offset and
    reused it would produce exactly 744 and look right.
    """

    month = range_run["shapes"]["month:2026-10"]
    span = int(month["until"]) - int(month["since"])
    assert span == 745 * HOURS
    assert span != 744 * HOURS
    # The two ends really do carry different offsets.
    assert int(month["since"]) == 1790802000  # 2026-09-30T21:00Z, +03:00
    assert int(month["until"]) == 1793484000  # 2026-10-31T22:00Z, +02:00


def test_march_2026_is_743_hours_long(range_run) -> None:
    """The other direction: the spring month loses an hour."""

    month = range_run["shapes"]["month:2026-03"]
    span = int(month["until"]) - int(month["since"])
    assert span == 743 * HOURS
    # Its two epochs are left to the helper, per §6: pinning a literal here
    # would hard-code one zone's transition rule into a test about arithmetic.
    assert int(month["since"]) < int(month["until"])


def test_a_plain_month_is_720_hours(range_run) -> None:
    """Neither end near a transition: a September is exactly 720 hours."""

    for window in ("2026-09", "2026-11"):
        month = range_run["shapes"][f"month:{window}"]
        assert int(month["until"]) - int(month["since"]) == 720 * HOURS, window


def test_a_calendar_month_is_not_duration_arithmetic(range_run) -> None:
    """THE NEGATIVE: a fixed duration is NOT how a month resolves.

    A mutation that replaces the two-boundary computation with one offset plus a
    duration is the specific defect this whole table exists to catch, and it is
    caught by the PLAIN months, where the two differ:

      September is 720 h, so adding October's 745 h to its start overruns.
      March is 743 h, so adding a nominal 30 days (720 h) lands short.

    (October's own 745 h is what the duration arithmetic would be tuned to, and
    it happens to match there -- which is exactly why one month proving the rule
    is not enough.)
    """

    september = range_run["shapes"]["month:2026-09"]
    assert int(september["since"]) + 745 * HOURS != int(september["until"])
    march = range_run["shapes"]["month:2026-03"]
    assert int(march["since"]) + 30 * 86400 != int(march["until"])
    # And the same rule in the other direction: a month that IS 745 h would be
    # missed by 744 h of duration arithmetic.
    october = range_run["shapes"]["month:2026-10"]
    assert int(october["since"]) + 744 * HOURS != int(october["until"])


def test_the_end_month_of_a_span_is_included_whole(range_run) -> None:
    """`months: 2026-09 .. 2026-11` ends at 1 December, not at 1 November.

    "Through November" has to mean all of November, which is why the end bound
    is the first instant of the month AFTER it and never `lastDay 23:59:59`.
    """

    span = range_run["shapes"]["months:2026-09..2026-11"]
    assert int(span["since"]) == 1788210000  # 2026-09-01 local
    assert int(span["until"]) == 1796076000  # 2026-12-01 local
    # And it is exactly the union of the three months, to the second.
    total = sum(
        int(range_run["shapes"][f"month:{window}"]["until"])
        - int(range_run["shapes"][f"month:{window}"]["since"])
        for window in ("2026-09", "2026-10", "2026-11")
    )
    assert int(span["until"]) - int(span["since"]) == total


def test_a_one_month_span_is_the_same_window_as_that_month(range_run) -> None:
    """`months: 2026-10 .. 2026-10` is October, to the second.

    The one-month case runs through the SAME arithmetic as a span rather than a
    second implementation, and this is what says so.
    """

    span = range_run["shapes"]["months:2026-10..2026-10"]
    month = range_run["shapes"]["month:2026-10"]
    assert span == {"since": month["since"], "until": month["until"]}


def test_a_whole_year_is_8760_hours_and_a_leap_year_is_8784(range_run) -> None:
    """Both DST shifts inside a year cancel; a leap year adds its day."""

    year = range_run["shapes"]["year:2026"]
    assert int(year["since"]) == 1767218400  # 2025-12-31T22:00Z, +02:00
    assert int(year["until"]) == 1798754400  # 2026-12-31T22:00Z, +02:00
    assert int(year["until"]) - int(year["since"]) == 8760 * HOURS

    leap = range_run["shapes"]["year:2024"]
    assert int(leap["until"]) - int(leap["since"]) == 8784 * HOURS
    # The leap day itself is inside it: 29 February 2024 local exists.
    feb = range_run["shapes"]["month:2028-02"]
    assert int(feb["until"]) - int(feb["since"]) == 696 * HOURS  # a 29-day Feb


def test_a_backwards_range_is_no_window_at_all(range_run) -> None:
    """November .. September is an unfinished pick, not a query with since>until."""

    assert range_run["shapes"]["months:backwards"] == {"since": None, "until": None}


def test_the_exact_times_pair_is_read_as_local(range_run) -> None:
    """`datetime-local` carries no zone, so the engine parses it as LOCAL.

    That is what the export's custom range has always done, and the shape has
    to keep it: 2026-10-01T00:00 local is 1790802000 here, not the 1790812800
    a UTC reading would give.
    """

    exact = range_run["shapes"]["exact:october"]
    assert int(exact["since"]) == 1790802000
    assert int(exact["until"]) == 1793484000
    # The UTC misreading is three hours away and would be a different window.
    assert int(exact["since"]) != 1790812800


def test_the_relative_shape_is_a_duration_not_a_calendar(range_run) -> None:
    """ "Last N hours/days/weeks" is `now - N*unit`, with NO upper bound.

    It deliberately includes the current partial hour, exactly as the five fixed
    periods do, and across a DST change it spans 71 or 73 local clock-hours --
    which is correct, and is the difference between this shape and the
    calendar one.
    """

    now = FROZEN_NOW_MS / 1000
    cases = {
        "7 days": 7 * 86400,
        "3 weeks": 3 * 604800,
        "36 hours": 36 * 3600,
        "1 day": 86400,
    }
    for key, seconds in cases.items():
        got = range_run["shapes"][f"relative:{key}"]
        assert got["until"] is None, key
        assert int(got["since"]) == int(f"{now - seconds:.0f}"), key


def test_the_same_shape_is_offered_on_both_controls(range_run) -> None:
    """USER DECISIONS Q7: ONE period vocabulary.

    The export's two new values reach the same function, and each shape's
    control is visible on exactly the value that asks for it.
    """

    shapes = range_run["shapes"]
    assert shapes["relativeHidden"] == {"custom": False, "range": True}
    assert shapes["calendarHidden"] == {"custom": True, "range": False}
    # Each shape shows exactly its own boxes.
    for shape, expected in (
        (
            "month",
            {"month": False, "from": True, "to": True, "year": True, "exact": True},
        ),
        (
            "months",
            {"month": True, "from": False, "to": False, "year": True, "exact": True},
        ),
        (
            "year",
            {"month": True, "from": True, "to": True, "year": False, "exact": True},
        ),
        (
            "exact",
            {"month": True, "from": True, "to": True, "year": True, "exact": False},
        ),
    ):
        assert shapes["shapeVisibility"][shape] == expected, shape


# --------------------------------------------------------------------------
# 3. THE LABEL (§5 rule 7)
# --------------------------------------------------------------------------


def test_a_custom_window_is_named_by_its_bounds_and_marked_local(range_run) -> None:
    """A month or a year must not read as "all stored rows", and must say whose
    calendar was meant — the label is the only place a shared screenshot says."""

    assert range_run["shapes"]["monthLabel"].startswith("Window: February 2028, local")
    assert range_run["shapes"]["yearLabel"].startswith("Window: 2028, local")


def test_a_relative_window_is_labelled_by_its_phrase_not_by_dates(range_run) -> None:
    """USER DECISIONS Q4: a saved "last 7 days" keeps meaning the last 7 days.

    A label carrying today's resolved dates would read as a frozen snapshot the
    moment it was saved, which is the failure this ordering prevents.
    """

    label = range_run["shapes"]["relativeLabel"]
    assert label.startswith("Window: last 1 day")
    # No resolved date leaked into it.
    assert "2026" not in label


# --------------------------------------------------------------------------
# 4. THE THREE SENTENCES (PR-CW-2)
# --------------------------------------------------------------------------


def test_the_hour_snap_gap_says_so_in_one_clause(range_run) -> None:
    """Sentence 1: the cards count whole UTC hours; the table is exact.

    The measured case is October 2026 — 211,992 rows in the table against
    212,317 on the cards, +325 — and the clause names that difference rather
    than leaving two different totals unexplained.
    """

    note = range_run["hourSnap"]["october"]
    assert "whole UTC hours" in note
    assert "325 more requests than the table here" in note
    assert "the table is exact" in note
    # A wider overshoot says "hours", not "1 hour".
    assert "last 2 hours count" in range_run["hourSnap"]["twoHours"]


def test_the_hour_snap_note_stays_silent_when_the_bounds_agree(range_run) -> None:
    """Three cases where there is nothing to explain, and it says nothing."""

    assert range_run["hourSnap"]["exact"] == ""
    assert range_run["hourSnap"]["allTime"] == ""
    assert range_run["hourSnap"]["noWindow"] == ""


def test_the_retention_gap_fires_without_being_at_the_cap(range_run) -> None:
    """Sentence 2: the rollup outlives the rows, so the cards count requests the
    table cannot show. Measured: 700,005 rows against 738,165 rolled, +38,160 —
    and the note used to fire only at the cap, which a pruned table is almost
    never exactly at."""

    assert range_run["retention"]["outrun"] == 38160
    # The two agreeing is not news.
    assert range_run["retention"]["agrees"] is None
    # Nor is a handful of rows, which is a race between two requests.
    assert range_run["retention"]["tiny"] is None
    # And with no all-time payload there is nothing to compare.
    assert range_run["retention"]["noLifetime"] is None


def test_an_empty_custom_window_says_which_kind_of_empty_it_is(range_run) -> None:
    """Sentence 3: "not measured" is not "nothing in it".

    Two different causes, both drawn by the product's own row renderer:
    a window after the oldest kept row (retention), and a window before uptime
    tracking began (coverage). Both say "not measured"; neither says "no
    requests match", which would read as a fault in the filter.
    """

    assert range_run["shapes"]["emptyYearText"].startswith(
        "This window is not measured"
    )
    assert "oldest request kept" in range_run["shapes"]["emptyYearText"]
    assert range_run["shapes"]["emptyCoverageBranch"].startswith(
        "This window is not measured"
    )
    assert "uptime tracking" in range_run["shapes"]["emptyCoverageBranch"]
    # The fixed periods keep their old words: this sentence is the custom
    # window's, not everyone's.
    assert (
        range_run["shapes"]["emptyAllTimeText"]
        == "No requests match the current filters."
    )


# --------------------------------------------------------------------------
# 5. THE WIDE-SEARCH WARNING (PR-CW-3)
# --------------------------------------------------------------------------


def test_a_wide_search_states_the_measured_cost_before_it_runs(range_run) -> None:
    """The line names §4's measurements and nothing else.

    A month is quoted as a RANGE ("a minute to two") because the two months
    measured took 55 s and 131 s for a 1.3x difference in matched rows. Quoting
    one figure for "a month" would be a number this codebase has had to
    retract before.
    """

    thirty = range_run["cost30d"]
    assert "30 days took about 38 seconds" in thirty
    assert "a month a minute to two" in thirty
    assert "press Enter" in thirty
    assert "type again or reload" in thirty

    all_time = range_run["costAllTime"]
    assert "a year took about five minutes" in all_time
    assert "all time a little longer" in all_time


def test_the_warning_never_narrows_refuses_or_truncates(range_run) -> None:
    """It is advice. The search still runs, over the same window, when Enter is
    pressed — no bound, no refusal, no cut result (USER DECISIONS Q5)."""

    assert range_run["searchStillRuns"] is True
    assert range_run["searchWindowMatchesLoad"] is True


def test_a_narrow_window_gets_no_warning(range_run) -> None:
    """An hour costs about a second and there is nothing to say about it."""

    assert range_run["costHour"] == ""


def test_a_held_search_is_not_priced(range_run) -> None:
    """A saved search waiting for Run is paying nothing yet, and it has its own
    notice. Pricing it would be telling a reader about a cost they are not
    incurring."""

    assert range_run["costWhenHeld"] == ""
    assert range_run["costAfterHeldCleared"] != ""


# --------------------------------------------------------------------------
# 6. THE SAVED VIEW
# --------------------------------------------------------------------------


def test_a_relative_window_is_saved_as_its_shape_not_as_an_epoch(range_run) -> None:
    """USER DECISIONS Q4: "last 7 days" saved means the last 7 days when it runs.

    The stored object names the shape and its inputs. A resolved epoch in
    localStorage would turn the view into a snapshot that reads as broken a week
    later — and the window part of what was written carries no epoch at all.
    """

    saved = range_run["savedRelative"]["windowCustom"]
    assert saved["value"] == "custom-relative"
    assert saved["amount"] == "14"
    assert saved["unit"] == "days"
    # A chosen shape, not a computed instant: every field is an input value,
    # and none of them is long enough to be an epoch second.
    for key, value in saved.items():
        if value is None:
            continue
        assert not (str(value).isdigit() and len(str(value)) >= 9), (key, value)


def test_an_absolute_window_stays_absolute_across_a_reload(range_run) -> None:
    """The other half: a month is fixed by its own inputs and restores to the
    same window, to the second."""

    saved = range_run["savedCalendar"]["windowCustom"]
    assert saved["value"] == "custom-calendar"
    assert saved["shape"] == "month"
    assert saved["month"] == "2026-10"
    restored = range_run["restored"]
    assert restored["window"] == "custom-calendar"
    assert restored["shape"] == "month"
    assert restored["month"] == "2026-10"
    assert restored["rangeHidden"] is False
    # And the window it resolves to is October, unchanged.
    assert restored["params"]["since"] == "1790802000"
    assert restored["params"]["until"] == "1793484000"


def test_a_view_saved_before_this_release_restores_exactly_as_it_did(range_run) -> None:
    """A saved view written by 8.8.0 has no custom inputs at all.

    It must restore with no exception, no blank select and no visible custom
    control — the SELECT-BLANK class of defect this project has already paid
    for once. The fixed period it names still sends the same request.
    """

    restored = range_run["restoredPreRange"]
    assert restored["window"] == "604800"
    assert restored["provider"] == "nous_portal"
    assert restored["customHidden"] is True
    assert restored["rangeHidden"] is True
    assert restored["params"]["since"] is not None
    assert restored["params"]["until"] is None
