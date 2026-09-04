"""
tests/test_publish_schedule.py
---------------------------------
The publish schedule, and the three places that have to agree about it:
pipeline/publish_schedule.py, the workflow crons, and the viewer's
staleness check.

The old crons fired at +0:20, before the matching NBM run was posted, so
every run fell back to the previous NBM cycle and rebuilt the G-AIRMET
cycle that was already current -- the 15Z first guess did not appear until
15:20Z, after the 1445Z issuance it exists to seed. Runs now start at
+1:15 and retry to +3:00.

The schedule is expressed once, in Python, and the YAML and the JS are
checked against it. Hand-maintaining four cron lines per hazard across two
files is how the offsets that roll into the next hour (and, for MTN OBSC
after 21Z, into the next DAY) get quietly wrong.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.publish_schedule import (  # noqa: E402
    ATTEMPTS_PER_CYCLE,
    ATTEMPT_INTERVAL_MINUTES,
    FIRST_ATTEMPT_OFFSET_MINUTES,
    GAIRMET_CYCLE_HOURS,
    HAZARD_STAGGER_MINUTES,
    NBM_LEAD_TIME_OFFSET_HOURS,
    PUBLISH_WINDOW_CLOSE_MINUTES,
    attempt_number,
    attempt_offsets,
    cron_entries,
    is_final_attempt,
    target_nbm_cycle,
)

WORKFLOWS = {"ifr": "generate_ifr.yml", "mtn_obsc": "generate_mtn_obsc.yml"}


def _crons(workflow_name):
    spec = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow_name).read_text())
    on = spec[next(k for k in spec if str(k) in ("on", "True"))]
    return [entry["cron"] for entry in on["schedule"]]


# --- the schedule itself ---------------------------------------------------

def test_the_first_attempt_is_late_enough_to_be_worth_making():
    """
    The whole point of the change. +0:20 was before NBM posted, so every
    scheduled run rebuilt the already-current cycle.
    """
    assert FIRST_ATTEMPT_OFFSET_MINUTES == 75, "the base run is meant to be +1:15"
    assert FIRST_ATTEMPT_OFFSET_MINUTES > 20


def test_retries_cover_the_window_without_a_sleep_loop():
    assert ATTEMPTS_PER_CYCLE == 4
    assert ATTEMPT_INTERVAL_MINUTES == 30
    assert attempt_offsets("ifr") == [75, 105, 135, 165]
    assert attempt_offsets("mtn_obsc") == [90, 120, 150, 180]
    assert PUBLISH_WINDOW_CLOSE_MINUTES == 180, "the window is meant to close at +3:00"


def test_the_hazards_stay_staggered_at_every_attempt():
    """
    They force-push separate data branches and MTN OBSC fetches ten NBM
    fields per hour against IFR's four. A stagger that held only for the
    first attempt would put them back on top of each other for the
    retries, which is where a slow run is most likely.
    """
    ifr, mtn = attempt_offsets("ifr"), attempt_offsets("mtn_obsc")
    gaps = {m - i for i, m in zip(ifr, mtn)}
    assert gaps == {15}, f"the stagger is not a constant 15 minutes: {sorted(gaps)}"
    assert HAZARD_STAGGER_MINUTES["ifr"] < HAZARD_STAGGER_MINUTES["mtn_obsc"], "IFR runs first"


def test_attempts_never_reach_the_next_synoptic_hour():
    """
    target_nbm_cycle() takes the most recent synoptic hour, which is only
    unambiguous while every attempt lands inside its own six-hour slot.
    """
    assert max(attempt_offsets("mtn_obsc")) < 6 * 60


@pytest.mark.parametrize("hazard", sorted(WORKFLOWS))
def test_the_workflow_crons_match_the_schedule(hazard):
    """
    The offsets past +2:00 roll into later hours, and MTN OBSC's +3:00
    attempt after 21Z rolls into hour 0 of the NEXT day. That is the part
    that gets silently wrong when four cron lines are edited by hand.
    """
    assert _crons(WORKFLOWS[hazard]) == cron_entries(hazard)


def test_the_old_pre_nbm_crons_are_gone():
    for workflow in WORKFLOWS.values():
        crons = _crons(workflow)
        for stale in ("20 3,9,15,21 * * *", "35 3,9,15,21 * * *"):
            assert stale not in crons, f"{workflow} still fires before NBM posts: {stale}"


# --- which attempt is this -------------------------------------------------

@pytest.mark.parametrize("now,hazard,target_hour,attempt,final", [
    ("2026-09-04T04:15", "ifr", 3, 1, False),
    ("2026-09-04T04:30", "mtn_obsc", 3, 1, False),
    ("2026-09-04T05:45", "ifr", 3, 4, True),
    ("2026-09-04T06:00", "mtn_obsc", 3, 4, True),
    ("2026-09-04T22:15", "ifr", 21, 1, False),
    ("2026-09-04T23:45", "ifr", 21, 4, True),
    # MTN OBSC's final attempt at the 21Z cycle lands after midnight.
    ("2026-09-05T00:00", "mtn_obsc", 21, 4, True),
])
def test_attempts_are_identified_including_across_midnight(now, hazard, target_hour, attempt, final):
    when = datetime.fromisoformat(now)
    assert target_nbm_cycle(when).hour == target_hour
    assert attempt_number(when, hazard) == attempt
    assert is_final_attempt(when, hazard) is final


def test_the_midnight_attempt_targets_yesterdays_cycle():
    """A date bug here would make the 21Z package look like a 03Z miss."""
    when = datetime(2026, 9, 5, 0, 0)
    target = target_nbm_cycle(when)
    assert (target.year, target.month, target.day, target.hour) == (2026, 9, 4, 21)


def test_a_manual_run_is_not_a_numbered_attempt():
    """
    workflow_dispatch fires whenever a person presses the button.
    Reporting it as "attempt 3 of 4" would be invented, and treating it as
    FINAL would raise a missed-cycle alarm for a cycle the schedule is
    still going to try for.
    """
    off_slot = datetime(2026, 9, 4, 4, 40)
    assert attempt_number(off_slot, "ifr") is None
    assert is_final_attempt(off_slot, "ifr") is False


def test_a_late_start_still_counts_as_its_attempt():
    """Actions does not fire crons punctually; minutes of delay are routine."""
    assert attempt_number(datetime(2026, 9, 4, 4, 19), "ifr") == 1
    assert attempt_number(datetime(2026, 9, 4, 4, 26), "ifr") is None, "tolerance is not open-ended"


def test_every_scheduled_cron_maps_back_to_the_attempt_it_encodes():
    """
    Round trip: each cron line, fired at its own minute, must be
    recognised as the attempt that generated it. Catches an hour-rollover
    error that a forward-only check would agree with.
    """
    for hazard in WORKFLOWS:
        for index, cron in enumerate(cron_entries(hazard), start=1):
            minute, hours = cron.split()[0], cron.split()[1]
            for hour in (int(h) for h in hours.split(",")):
                # Sunday 2026-09-06 is arbitrary; any date works, but the
                # hour-0 crons must be read as the PREVIOUS day's cycle.
                when = datetime(2026, 9, 6, hour, int(minute))
                assert attempt_number(when, hazard) == index, (
                    f"{hazard} cron {cron!r} at {when} reads as attempt "
                    f"{attempt_number(when, hazard)}, not {index}"
                )


# --- the viewer has to agree ----------------------------------------------

def test_the_viewer_uses_the_same_numbers_as_the_scheduler():
    """
    The staleness check decides when a cycle is late, and it does that
    from a copy of these constants in JavaScript. If they drift, the panel
    either cries stale during a normal publish window or stays quiet
    through a genuinely missed cycle.
    """
    js = (REPO_ROOT / "webapp" / "static" / "map.js").read_text()

    hours = re.search(r"const CYCLE_HOURS = \[([^\]]+)\]", js)
    assert hours, "map.js no longer declares CYCLE_HOURS"
    assert [int(h) for h in hours.group(1).split(",")] == GAIRMET_CYCLE_HOURS

    window = re.search(r"const PUBLISH_WINDOW_CLOSE_MINUTES = (\d+)", js)
    assert window, "map.js still uses the old publish-grace constant"
    assert int(window.group(1)) == PUBLISH_WINDOW_CLOSE_MINUTES

    lead = re.search(r"const NBM_LEAD_OFFSET_HOURS = (\d+)", js)
    assert lead, "map.js does not account for the NBM lead offset"
    assert int(lead.group(1)) == NBM_LEAD_TIME_OFFSET_HOURS == 6


def test_the_viewer_no_longer_compares_the_cycle_to_the_wall_clock():
    """
    The app normally holds a cycle AHEAD of now (a 15Z package at 10:15Z),
    so the old grace-based check would call a healthy state stale.
    """
    js = (REPO_ROOT / "webapp" / "static" / "map.js").read_text()
    assert "PUBLISH_GRACE_MINUTES" not in js, "the old 90-minute grace is still there"
    assert "expectedGairmetCycle" in js


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
