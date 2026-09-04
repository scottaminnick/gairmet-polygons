"""
pipeline/publish_schedule.py
-------------------------------
WHEN each hazard tries to publish a cycle, and how to tell which attempt
a given run is.

Split out from gairmet_cycle.py and deliberately STDLIB-ONLY: the publish
guard (.github/scripts/should_publish_cycle.py) needs these numbers and
runs in a step that must not depend on requests/numpy/the GRIB stack.
gairmet_cycle.py imports GAIRMET_CYCLE_HOURS back from here so the
issuance hours have exactly one definition.

WHY THE SCHEDULE MOVED
----------------------
The crons used to fire at :20 and :35 past each synoptic hour. At +0:20
the matching NBM run is not posted yet, so find_latest_gairmet_cycle()
fell back to the PREVIOUS NBM cycle and, with the +6h lead offset,
rebuilt the G-AIRMET cycle that was already current. The practical cost:
the 15Z first guess did not appear until 15:20Z -- thirty-five minutes
AFTER the 1445Z issuance it is meant to seed, which is the opposite of a
first guess.

Runs now start at +1:15 and retry every 30 minutes to +3:00. Retries are
separate cron entries rather than a sleep/poll loop inside one job: a
job that sleeps burns billable minutes waiting, holds a runner, and
turns a missed NBM into a timeout instead of a clean no-op. An early
retry that finds only the older NBM run rebuilds the already-published
cycle and should_publish_cycle.py skips it -- correct, and cheap.

WHAT AN ATTEMPT COSTS WHEN IT IS TOO EARLY
------------------------------------------
Nothing is published, but the run still fetches and processes a full
cycle before the guard sees it. That is the trade for not sleeping, and
it is why the attempts stop at +3:00 rather than continuing until the
next synoptic hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Real G-AIRMET issuance hours (UTC), per NWSI 10-811 section 7.2
# ("around 0245, 0845, 1445 and 2045 UTC"). Defined HERE rather than in
# gairmet_cycle.py so this module stays stdlib-only; gairmet_cycle
# imports it from here.
GAIRMET_CYCLE_HOURS = [3, 9, 15, 21]

# A G-AIRMET package is built from the NBM cycle six hours before it, so
# a healthy site holds a cycle AHEAD of the wall clock. Defined here with
# the rest of the schedule -- it is what turns "which NBM cycle" into
# "which package", and the viewer's staleness check and the publish guard
# both need it without the fetch stack. gairmet_cycle re-exports it under
# its original name; the VALUE is unchanged and deliberately stays 6.
NBM_LEAD_TIME_OFFSET_HOURS = 6

# First attempt at +1:15 after the synoptic hour, then every 30 minutes,
# four attempts, so the last IFR attempt is +2:45 and the last MTN OBSC
# attempt +3:00.
FIRST_ATTEMPT_OFFSET_MINUTES = 75
ATTEMPT_INTERVAL_MINUTES = 30
ATTEMPTS_PER_CYCLE = 4

# The two hazards force-push their own data branches. Staggering them
# keeps the pushes from colliding; 15 minutes is the existing gap
# (the old crons were :20 and :35) and is kept.
HAZARD_STAGGER_MINUTES = {"ifr": 0, "mtn_obsc": 15}

# The last moment any hazard is still trying for a given cycle. The
# viewer's staleness check uses this: a cycle is only late once every
# attempt at it has been and gone.
PUBLISH_WINDOW_CLOSE_MINUTES = (
    FIRST_ATTEMPT_OFFSET_MINUTES
    + (ATTEMPTS_PER_CYCLE - 1) * ATTEMPT_INTERVAL_MINUTES
    + max(HAZARD_STAGGER_MINUTES.values())
)


def attempt_offsets(hazard: str) -> list[int]:
    """Minutes after the synoptic hour at which `hazard` runs."""
    if hazard not in HAZARD_STAGGER_MINUTES:
        raise ValueError(f"unknown hazard {hazard!r}; expected one of {sorted(HAZARD_STAGGER_MINUTES)}")
    stagger = HAZARD_STAGGER_MINUTES[hazard]
    return [
        FIRST_ATTEMPT_OFFSET_MINUTES + stagger + i * ATTEMPT_INTERVAL_MINUTES
        for i in range(ATTEMPTS_PER_CYCLE)
    ]


def target_nbm_cycle(now: datetime) -> datetime:
    """
    The NBM cycle a run at `now` is trying for: the most recent synoptic
    hour at or before it.

    Unambiguous because every attempt lands within +3:00 of its synoptic
    hour and the hours are six apart -- an attempt can never be closer to
    the NEXT cycle than to its own.
    """
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [day.replace(hour=h) for h in GAIRMET_CYCLE_HOURS]
    earlier = [c for c in candidates if c <= now]
    if earlier:
        return earlier[-1]
    return (day - timedelta(days=1)).replace(hour=GAIRMET_CYCLE_HOURS[-1])


def attempt_number(now: datetime, hazard: str, tolerance_minutes: int = 10) -> int | None:
    """
    Which attempt (1-based) a run at `now` is, or None if it does not line
    up with any scheduled slot -- a manual workflow_dispatch, or a
    scheduled run that GitHub started late.

    The tolerance exists because Actions does not fire crons punctually;
    delays of several minutes are routine under load. None is a real
    answer, not an error: a manual run is simply not "attempt 3 of 4", and
    nothing downstream should pretend otherwise.
    """
    minutes_in = (now - target_nbm_cycle(now)).total_seconds() / 60.0
    for index, offset in enumerate(attempt_offsets(hazard), start=1):
        if offset <= minutes_in <= offset + tolerance_minutes:
            return index
    return None


def is_final_attempt(now: datetime, hazard: str, **kwargs) -> bool:
    """
    True only for the LAST scheduled attempt at a cycle. A manual run is
    not a final attempt -- it has no next attempt to compare against, and
    treating it as final would raise an alarm for a cycle the schedule is
    still going to try for.
    """
    return attempt_number(now, hazard, **kwargs) == ATTEMPTS_PER_CYCLE


def cron_entries(hazard: str) -> list[str]:
    """
    The 5-field cron lines this schedule implies, so the workflow YAML can
    be checked against it rather than hand-maintained beside it. Offsets
    past +2:00 roll into later hours (and, for MTN OBSC's +3:00 attempt
    after 21Z, into hour 0 of the next day), which is exactly the part
    that is easy to get wrong by hand.
    """
    entries = []
    for offset in attempt_offsets(hazard):
        hours = sorted({(h + offset // 60) % 24 for h in GAIRMET_CYCLE_HOURS})
        entries.append(f"{offset % 60} {','.join(str(h) for h in hours)} * * *")
    return entries
