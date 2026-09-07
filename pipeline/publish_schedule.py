"""
pipeline/publish_schedule.py
-------------------------------
WHEN each hazard goes looking for its NBM cycle, how long it keeps
looking, and how to tell a run's own timing apart from the platform's.

Split out from gairmet_cycle.py and deliberately STDLIB-ONLY: the
publish guard (.github/scripts/should_publish_cycle.py) and the poller
(.github/scripts/await_nbm_cycle.py) both need these numbers, and both
run in workflow steps that execute BEFORE `pip install` -- that is what
makes a non-publishing run cost seconds instead of minutes.
gairmet_cycle.py imports GAIRMET_CYCLE_HOURS back from here so the
issuance hours have exactly one definition.

WHY THIS IS ONE POLLING JOB AND NOT FOUR CRON ATTEMPTS
------------------------------------------------------
It used to be four. The crons for the 03Z package were +1:15, +1:45,
+2:15 and +2:45 (22:15, 22:45, 23:15, 23:45Z); on 2026-09-07 GitHub
actually STARTED them at 23:54, 00:22, 01:01 and 01:18Z. A consistent
~95 minute queue delay on every one.

That delay is the platform, not the cron expression. Scheduled Actions
runs are best-effort and queued at low priority, so no amount of
tightening the schedule can recover it. Two things followed from it:

  - The 03Z package published at ~23:54Z instead of ~22:45Z, leaving the
    forecaster 2h45m before the 0245Z issuance rather than the ~4h the
    schedule was designed for.
  - Each attempt ran 15-20 minutes because the publish decision came
    AFTER the NBM fetch and polygon generation. Three of every four did
    the full job and threw it away: roughly nine hours of runner time a
    day to publish four packages.

A single job that polls fixes both directions of the problem. Started on
time, it picks NBM up within POLL_INTERVAL_MINUTES of its arrival
instead of waiting for the next half-hourly attempt. Started 95 minutes
late, the data is already there and it proceeds immediately -- no worse
than the old first attempt would have been. The schedule no longer has
to predict when GitHub will feel like running it.

Sleeping in a job used to be the thing this file argued against, on the
grounds that it burns billable minutes holding a runner. That argument
turned on the OLD cost model, where every attempt paid for a full fetch
and generation up front. It doesn't survive the reordering: the poll
loop is an HTTP HEAD every five minutes, and the run now decides whether
it would publish AT ALL before installing dependencies or touching NBM
data (see .github/scripts/await_nbm_cycle.py, and the pre-flight guard
step in the generate workflows). A run that has nothing to publish exits
in seconds, which is what makes the polling affordable.

WHAT THIS COSTS
---------------
Worth being honest about, because the poll CAN sleep for a long time. The
three cases, per hazard per cycle:

  - Nothing to publish (the cycle is already live). Seconds. The run
    never installs dependencies or fetches NBM.
  - GitHub starts the job late, as it did all four times on 2026-09-07.
    NBM is already posted, so zero poll: just the work, 15-20 minutes for
    IFR and up to ~30 for MTN OBSC.
  - GitHub starts the job on time and NBM lands at +1:20. About 37
    minutes of polling for IFR, then the work.

So the realistic daily total is at or below the ~9 hours the four-attempt
ladder was spending, and it publishes on time instead of an hour late.
The pathological case -- NBM never posts, both hazards poll to the
deadline -- is about 5 hours for the day, and it is bounded: that is what
POLL_DEADLINE_OFFSET_MINUTES and the derived job_timeout_minutes() are
for.

WHY +0:45 AND WHY :43/:58
-------------------------
The forecaster reports NBM 21Z typically lands 2215-2230Z, i.e. +1:15 to
+1:30 after the synoptic hour. Starting the poll ~45 minutes ahead of
that means the job is already sitting there when the data appears, with
margin for an early cycle, and only a handful of poll iterations wasted
when it isn't.

The exact minutes are deliberately NOT :00/:15/:30/:45. Those are the
most oversubscribed slots on the platform -- everybody's cron lands on
them -- and an off-beat minute measurably reduces queue delay. It does
not fix the delay (nothing here can), it is just free. :43 and :58 keep
the 15-minute IFR/MTN OBSC stagger while avoiding both the quarter-hour
slots and the multiples of five clustered around them.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

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

# The NBM forecast hour the poller probes for. The SMALLEST any hazard
# will actually need: G-AIRMET valid-time offset 0h maps to NBM hour 6
# (the lead offset above). If that message isn't posted, none of the
# longer lead times are either. gairmet_cycle.py derives the same number
# from FORECAST_HOURS[0]; tests/test_publish_schedule.py pins them
# together so this copy cannot drift.
PROBE_FORECAST_HOUR = NBM_LEAD_TIME_OFFSET_HOURS

# Minutes after the synoptic hour at which IFR's job is SCHEDULED to
# start polling. Nominally +0:45, shifted to :43 to stay off the
# quarter-hour slots -- see the module docstring. What time the job
# actually starts is GitHub's decision, not this number's, which is the
# entire reason the job polls.
POLL_START_OFFSET_MINUTES = 43

# How long the job waits between .idx probes. Five minutes is well below
# the spread on NBM's arrival and cheap enough to be irrelevant: one HTTP
# HEAD per iteration.
POLL_INTERVAL_MINUTES = 5

# Wall clock, measured from the SYNOPTIC HOUR rather than from job start,
# after which the job stops waiting and exits successfully. Anchored to
# the synoptic hour on purpose: a job GitHub starts 95 minutes late must
# not get 95 extra minutes of runway and drift into the next cycle's
# territory. It is the same instant for both hazards for the same reason.
#
# +3:30 is the old attempt window's +3:00 plus the half hour the last
# attempt implicitly had to actually run in. Past it, the cycle is being
# missed rather than delayed, and waiting longer only delays the honest
# report of that.
POLL_DEADLINE_OFFSET_MINUTES = 210

# The two hazards force-push their own data branches. Staggering them
# keeps the pushes from colliding; 15 minutes is the existing gap and is
# kept. MTN OBSC second, because it is the slower of the two (ten real
# NBM fields per forecast hour against IFR's four).
HAZARD_STAGGER_MINUTES = {"ifr": 0, "mtn_obsc": 15}

# Head-room for the actual work, once the poll has found its cycle. Added
# to the longest possible poll to produce the job's timeout-minutes, so a
# wedged poll cannot sit on GitHub's six-hour default. IFR runs 15-20
# minutes; MTN OBSC has historically run to 30 (see the timeout note in
# .github/workflows/generate_mtn_obsc.yml).
GENERATION_ALLOWANCE_MINUTES = {"ifr": 25, "mtn_obsc": 40}

# The last moment any hazard is still trying for a given cycle. The
# viewer's staleness check uses this: a cycle is only late once the
# window for publishing it has been and gone. Now simply the poll
# deadline -- with one job per hazard there is no later attempt behind it.
PUBLISH_WINDOW_CLOSE_MINUTES = POLL_DEADLINE_OFFSET_MINUTES


def _require_hazard(hazard: str) -> None:
    if hazard not in HAZARD_STAGGER_MINUTES:
        raise ValueError(
            f"unknown hazard {hazard!r}; expected one of {sorted(HAZARD_STAGGER_MINUTES)}"
        )


def poll_start_offset_minutes(hazard: str) -> int:
    """Minutes after the synoptic hour at which `hazard`'s job is scheduled."""
    _require_hazard(hazard)
    return POLL_START_OFFSET_MINUTES + HAZARD_STAGGER_MINUTES[hazard]


def job_timeout_minutes(hazard: str) -> int:
    """
    What `timeout-minutes:` on the job should be: the longest the poll can
    possibly run (its scheduled start to the deadline) plus room to do the
    work afterwards.

    Deliberately derived rather than written into the YAML by hand. The
    number has to move whenever the deadline or the start does, and a
    timeout that quietly stops covering the window it was sized for looks
    exactly like a timeout that still does.
    """
    longest_poll = POLL_DEADLINE_OFFSET_MINUTES - poll_start_offset_minutes(hazard)
    return longest_poll + GENERATION_ALLOWANCE_MINUTES[hazard]


def target_nbm_cycle(now: datetime) -> datetime:
    """
    The NBM cycle a run at `now` is trying for: the most recent synoptic
    hour at or before it.

    Unambiguous because the whole poll window closes at
    POLL_DEADLINE_OFFSET_MINUTES (+3:30) and the synoptic hours are six
    apart -- a run inside its window can never be closer to the NEXT
    cycle than to its own. A run GitHub starts so late that it has
    crossed into the next synoptic hour genuinely IS working on that next
    cycle, and this returns it.
    """
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [day.replace(hour=h) for h in GAIRMET_CYCLE_HOURS]
    earlier = [c for c in candidates if c <= now]
    if earlier:
        return earlier[-1]
    return (day - timedelta(days=1)).replace(hour=GAIRMET_CYCLE_HOURS[-1])


def scheduled_start(now: datetime, hazard: str) -> datetime:
    """
    The instant this run's cron was SUPPOSED to fire, so a run can
    measure how late the platform started it. `now` is normally job
    start; the cycle it belongs to is read from it the same way
    everything else here reads it.
    """
    return target_nbm_cycle(now) + timedelta(minutes=poll_start_offset_minutes(hazard))


def poll_deadline(now: datetime) -> datetime:
    """The instant the job must stop waiting for `now`'s target cycle."""
    return target_nbm_cycle(now) + timedelta(minutes=POLL_DEADLINE_OFFSET_MINUTES)


def expected_gairmet_cycle(now: datetime) -> datetime:
    """
    The newest G-AIRMET package that should have PUBLISHED by `now`: the
    last NBM synoptic hour whose publish window has fully closed, shifted
    by the lead offset to the package built from it.

    The Python side of the viewer's staleness check
    (expectedGairmetCycle() in webapp/static/map.js). Mirrored rather
    than shared because the check runs in the browser, with nothing to
    call; tests/test_publish_schedule.py pins the constants the JS copy
    uses against the ones here, and exercises this against frozen clocks.

    NOT a comparison against the wall clock: with a +6h lead the app
    normally holds a cycle in the FUTURE (the 15Z package at 10:15Z), so
    comparing the loaded cycle to `now` would call every healthy state
    stale.
    """
    cutoff = now - timedelta(minutes=PUBLISH_WINDOW_CLOSE_MINUTES)
    return target_nbm_cycle(cutoff) + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)


def cron_entries(hazard: str) -> list[str]:
    """
    The 5-field cron line this schedule implies -- ONE per hazard now,
    where there used to be four -- so the workflow YAML can be checked
    against it rather than hand-maintained beside it.

    An offset past +1:00 rolls into the next hour, and past 21Z into hour
    0 of the next day. Today's offsets (+0:43 and +0:58) do not roll, but
    the arithmetic stays because the offsets are meant to be tunable from
    the arrival distribution the NBM-ARRIVAL lines are collecting, and
    that is exactly the edit that gets silently wrong by hand.
    """
    offset = poll_start_offset_minutes(hazard)
    hours = sorted({(h + offset // 60) % 24 for h in GAIRMET_CYCLE_HOURS})
    return [f"{offset % 60} {','.join(str(h) for h in hours)} * * *"]


def log_nbm_arrival(
    found_cycle: datetime | None,
    hazard: str | None = None,
    now: datetime | None = None,
    job_start: datetime | None = None,
    polled: bool = False,
) -> dict:
    """
    One machine-readable line per run recording what this run WANTED,
    what it GOT, when the platform let it start looking, and when it
    actually saw the data.

    THE POINT. Two completely different latencies were being conflated in
    a single "delta_min" number: how late GITHUB started the job, and how
    late NBM posted the cycle. On 2026-09-07 those were ~95 minutes and
    ~0 minutes respectively, and the log could not tell them apart --
    which matters, because they need different fixes (one is unfixable
    platform behaviour to be absorbed; the other is a schedule constant
    to be tuned). They are now separate fields:

        queue_delay_min   job_start - scheduled cron time. The platform.
        poll_wait_min     detection - job_start. How long we sat waiting.
        arrival_min       detection - the target synoptic hour. NBM.

    arrival_measured says whether arrival_min is a real measurement or
    only an upper bound. It is a measurement when the poll actually
    watched the cycle appear (at least one probe found nothing first). If
    the data was already there on the very first probe, all we know is
    that it arrived at or before then -- which is the common case when
    the platform starts us late, and reporting it as a measurement is how
    the two numbers got conflated in the first place.

    A single greppable prefix and flat key=value pairs, because the thing
    that will actually read these is `grep NBM-ARRIVAL` over downloaded
    job logs, not a log pipeline this project does not have.

    on_target=no is NOT a failure by itself in a manual run, but for a
    scheduled one it now means the poll ran to its deadline without the
    cycle appearing -- there is no later attempt behind it, so it warns.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    job_start = job_start or now
    target = target_nbm_cycle(job_start)
    on_target = found_cycle is not None and found_cycle >= target

    record = {
        "hazard": hazard or "-",
        "target": f"{target:%Y-%m-%dT%H:%M:%S}Z",
        "found": f"{found_cycle:%Y-%m-%dT%H:%M:%S}Z" if found_cycle else "none",
        "on_target": "yes" if on_target else "no",
        "job_start": f"{job_start:%Y-%m-%dT%H:%M:%S}Z",
        "detected": f"{now:%Y-%m-%dT%H:%M:%S}Z",
        # How late GitHub started us. Nothing in this repo can change it;
        # the schedule has to absorb it. Meaningless without a hazard,
        # since the scheduled minute is per-hazard.
        "queue_delay_min": (
            f"{(job_start - scheduled_start(job_start, hazard)).total_seconds() / 60:.0f}"
            if hazard else "-"
        ),
        # How long the poll loop waited. Runner minutes spent sleeping.
        "poll_wait_min": f"{(now - job_start).total_seconds() / 60:.0f}",
        # How late the cycle itself was, measured from its synoptic hour.
        "arrival_min": f"{(now - target).total_seconds() / 60:.0f}",
        "arrival_measured": "yes" if (polled and on_target) else "no",
        "behind_min": f"{(target - found_cycle).total_seconds() / 60:.0f}" if found_cycle else "-",
    }
    print("NBM-ARRIVAL " + " ".join(f"{k}={v}" for k, v in record.items()))
    return record


def warn_deadline_missed(target: datetime, found_cycle: datetime | None, hazard: str) -> None:
    """
    The one thing that has to look different from a routine no-op: the
    poll ran all the way to its deadline and the cycle never appeared.

    Kept at WARNING and on stderr, matching the classification
    should_publish_cycle.py emits, because there is no later attempt --
    the G-AIRMET cycle this run existed to seed is being MISSED, not
    delayed.
    """
    fallback = f"{found_cycle:%Y-%m-%d %H}Z" if found_cycle else "nothing at all"
    print(
        f"WARNING [nbm-not-yet]: polled for NBM {target:%Y-%m-%d %H}Z until the "
        f"+{POLL_DEADLINE_OFFSET_MINUTES // 60}:{POLL_DEADLINE_OFFSET_MINUTES % 60:02d} "
        f"deadline and it never posted (hazard={hazard}); falling back to {fallback}. "
        f"No further run is scheduled for this cycle, so the G-AIRMET package seeded by "
        f"NBM {target:%H}Z is being MISSED, not merely delayed. If this repeats, the "
        f"deadline is too early for NBM's real arrival -- see the NBM-ARRIVAL lines.",
        file=sys.stderr,
    )
