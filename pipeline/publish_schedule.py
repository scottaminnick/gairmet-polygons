"""
pipeline/publish_schedule.py
-------------------------------
WHEN each hazard is asked to build a package, how long it waits for its
NBM cycle, and how a run says which of the three triggers started it.

Deliberately STDLIB-ONLY, and now for three callers rather than one:

  - .github/scripts/resolve_target_cycle.py and should_publish_cycle.py,
    which run BEFORE `pip install` so a run with nothing to publish costs
    seconds;
  - .github/scripts/await_nbm_cycle.py, the poll;
  - scripts/dispatch_workflows.py, which runs on Railway in a minimal
    container that installs nothing at all.

gairmet_cycle.py imports GAIRMET_CYCLE_HOURS back from here so the
issuance hours have exactly one definition.

WHY THE TRIGGER LEFT `schedule:`
--------------------------------
Measured, not suspected. GitHub delays `schedule:` runs, and the delay is
GROWING:

    2026-09-07  the 03Z package's four IFR attempts were scheduled at
                22:15/22:45/23:15/23:45Z and started at
                23:54/00:22/01:01/01:18Z -- ~95 minutes late, every one.
    2026-09-10  up to 4.5 hours late.

Scheduled events are the lowest-priority trigger on the platform and are
explicitly best-effort. Our own four-attempt retry design quadrupled the
scheduled load we were putting into that queue, which very plausibly made
our own delays worse. `workflow_dispatch` is not queued the same way and
starts promptly, so the trigger moved off the platform entirely: a
Railway cron service POSTs a `workflow_dispatch` for both hazards
(scripts/dispatch_workflows.py).

At 4.5 hours late, a package meant to give the forecaster ~4 hours before
the 0245Z issuance arrives after the issuance it exists to seed. No cron
expression fixes that, because the expression is not what is late.

WHAT `schedule:` IS STILL FOR
-----------------------------
One backstop per hazard at BACKSTOP_OFFSET_MINUTES, and nothing else. It
covers exactly one failure: the Railway dispatch did not happen (service
down, token expired, Railway itself late). By then NBM has long since
posted, so the backstop either finds the package already published and
exits in seconds, or publishes it late -- which beats not publishing it.

It is deliberately ONE run, on an uncontended minute. Adding scheduled
runs to work around scheduled-run delay is what got us here.

THE THREE TRIGGERS
------------------
Every run records which one started it (see classify_trigger and
log_nbm_arrival), because "was the package late" and "was the DISPATCH
late" are different questions and the log has to answer them separately:

    railway-cron       the normal path: Railway POSTed workflow_dispatch
    schedule-backstop  the +3:37 safety net fired, so the normal path
                       did not
    manual             somebody pressed the button

A week of `grep NBM-ARRIVAL` should show start_delay_min near zero for
railway-cron. If it does not, the move did not work and this whole design
needs revisiting.

WHY 45 MINUTES BEFORE, AND A 60-MINUTE WINDOW
---------------------------------------------
The forecaster reports NBM 21Z typically lands 2215-2230Z, i.e. +1:15 to
+1:30 after the synoptic hour. Dispatching at +0:45 puts the job in place
~45 minutes early, and a 60-minute poll window carries it to +1:45 --
past the late end of the observed range with margin, without holding a
runner indefinitely.

A cycle later than that is not waited out. The run reports [nbm-not-yet]
and exits; the +3:37 backstop is what picks it up, and by then the answer
is known rather than guessed at.

NO FALLBACK TO AN OLDER CYCLE. The old design, when the target had not
posted, rebuilt the package from the PREVIOUS NBM run -- which is the
package already live, so the publish guard skipped it after a full fetch
and generation. A run now either builds the cycle it came for or builds
nothing.
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

# The workflow file each hazard lives in. Named here because THREE things
# need the mapping now and none of them should hardcode it: the drift
# test, the Railway dispatcher (which POSTs to
# /actions/workflows/{file}/dispatches), and the docs.
HAZARD_WORKFLOWS = {"ifr": "generate_ifr.yml", "mtn_obsc": "generate_mtn_obsc.yml"}

# Minutes after the synoptic hour at which the RAILWAY cron POSTs the
# workflow_dispatch. This is the number the forecaster types into the
# Railway UI; scripts/dispatch_workflows.py prints the cron line for it
# and tests/test_publish_schedule.py checks railway.dispatch.json against
# it.
#
# Not shifted off the quarter hour the way the GitHub backstop is: the
# oversubscription problem is GitHub's scheduled-event queue, and Railway
# runs our own container on our own plan.
DISPATCH_OFFSET_MINUTES = 45

# How long the job waits between .idx probes. Five minutes is well below
# the spread on NBM's arrival and cheap enough to be irrelevant: one HTTP
# HEAD per iteration, nothing downloaded.
POLL_INTERVAL_MINUTES = 5

# How long the job polls, measured from ITS OWN START rather than from
# the synoptic hour. That is the right anchor now: a workflow_dispatch
# starts promptly, so job start IS dispatch time, and the window is a
# statement about how long we are willing to wait for NBM after asking.
# The backstop gets the same 60 minutes from its own later start.
POLL_WINDOW_MINUTES = 60

# The GitHub `schedule:` backstop, at +3:37. Nominally +3:30 -- late
# enough that NBM has certainly posted and the normal path has certainly
# either worked or failed -- moved to :37 because :00/:15/:30/:45 are the
# platform's most oversubscribed scheduled-run slots and this is the one
# trigger still exposed to that queue. It cannot fix the delay; it is
# free, and this run is the one that can least afford to be delayed.
BACKSTOP_OFFSET_MINUTES = 217

# Head-room for everything AFTER the poll finds its cycle: `pip install`,
# generation, and the publish push. Added to the poll window to produce
# the job's timeout-minutes, so a wedged poll cannot sit on GitHub's
# six-hour default. IFR runs 15-20 minutes; MTN OBSC has historically run
# to 30 (see the timeout note in .github/workflows/generate_mtn_obsc.yml).
WORK_ALLOWANCE_MINUTES = {"ifr": 30, "mtn_obsc": 45}

# THE TWO HAZARDS ARE NO LONGER STAGGERED, and that is a finding rather
# than a simplification. The 15-minute gap was there to keep their pushes
# and their NBM fetches from overlapping. Checked directly:
#
#   - They force-push SEPARATE orphan branches (data-ifr, data-mtnobsc),
#     each replacing only its own; the file globs (ifr_f* vs mtn_obsc_f*)
#     are disjoint and each job has its own workspace.
#   - Their `concurrency:` groups are separate, so they never queue
#     behind each other.
#   - Each job runs on its own GitHub-hosted runner with its own IP, so
#     there is no shared connection pool or rate-limit bucket at NOAA.
#   - Only MTN OBSC reads the terrain grid, and it reads it from its own
#     checkout.
#
# And the decisive one: the stagger never actually prevented overlap.
# MTN OBSC runs 30+ minutes against IFR's 15-20, so under the old +1:15
# and +1:30 crons the two were fetching from NOAA at the same time on
# most cycles anyway. It was protecting nothing.
#
# So both hazards are dispatched together and both backstops fire on the
# same minute. scripts/dispatch_workflows.py keeps a --stagger-seconds
# knob at 0 in case that ever stops being true.
HAZARDS = tuple(sorted(HAZARD_WORKFLOWS))

# The three ways a run can start. Order is most-expected first, which is
# also how they should be read in a log.
TRIGGERS = ("railway-cron", "schedule-backstop", "manual")

# When a package should have PUBLISHED, for the viewer's staleness check:
# dispatch, plus the whole poll window, plus the work. Deliberately built
# from the NORMAL path and not from the backstop -- past this point the
# package genuinely IS late, and the +3:37 backstop is a recovery, not a
# second helping of runway. A viewer that stayed quiet until +4:22 would
# be hiding a real failure for two hours to avoid one honest warning.
PUBLISH_WINDOW_CLOSE_MINUTES = (
    DISPATCH_OFFSET_MINUTES + POLL_WINDOW_MINUTES + max(WORK_ALLOWANCE_MINUTES.values())
)


def _require_hazard(hazard: str) -> None:
    if hazard not in HAZARD_WORKFLOWS:
        raise ValueError(
            f"unknown hazard {hazard!r}; expected one of {sorted(HAZARD_WORKFLOWS)}"
        )


def classify_trigger(event_name: str, trigger_input: str | None = None) -> str:
    """
    Which of TRIGGERS started this run, from the two things GitHub tells
    the job: the event name, and the `trigger` workflow_dispatch input.

    The event name WINS for `schedule`, because a scheduled event carries
    no inputs at all -- a `schedule` run claiming to be railway-cron would
    be a contradiction, not a data point.

    Anything else is "manual". Not an error: the input is a free-text
    string anyone with the Run workflow button can type, and an unknown
    value landing verbatim in the instrumentation would let a typo look
    like a measurement.
    """
    if event_name == "schedule":
        return "schedule-backstop"
    if (trigger_input or "").strip().lower() == "railway-cron":
        return "railway-cron"
    return "manual"


def nominal_start_offset_minutes(trigger: str) -> int | None:
    """
    Minutes after the synoptic hour at which a run with this trigger was
    SUPPOSED to start, or None for a manual run, which has no schedule to
    be late against.

    This is what makes start_delay_min meaningful, and start_delay_min is
    how we will know whether moving off `schedule:` actually worked.
    """
    if trigger == "railway-cron":
        return DISPATCH_OFFSET_MINUTES
    if trigger == "schedule-backstop":
        return BACKSTOP_OFFSET_MINUTES
    return None


def job_timeout_minutes(hazard: str) -> int:
    """
    What `timeout-minutes:` on the job should be: the whole poll window
    plus room to install, generate and publish afterwards.

    Deliberately derived rather than written into the YAML by hand. The
    number has to move whenever the window or the allowance does, and a
    timeout that quietly stops covering the window it was sized for looks
    exactly like a timeout that still does.
    """
    _require_hazard(hazard)
    return POLL_WINDOW_MINUTES + WORK_ALLOWANCE_MINUTES[hazard]


def target_nbm_cycle(now: datetime) -> datetime:
    """
    The NBM cycle a run at `now` is trying for: the most recent synoptic
    hour at or before it.

    Unambiguous because even the backstop's window closes at
    BACKSTOP_OFFSET_MINUTES + POLL_WINDOW_MINUTES (+4:37) and the synoptic
    hours are six apart -- a run inside its window can never be closer to
    the NEXT cycle than to its own.

    Resolved ONCE at job start and then carried, so a long poll cannot
    quietly change which cycle the run is chasing.
    """
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = [day.replace(hour=h) for h in GAIRMET_CYCLE_HOURS]
    earlier = [c for c in candidates if c <= now]
    if earlier:
        return earlier[-1]
    return (day - timedelta(days=1)).replace(hour=GAIRMET_CYCLE_HOURS[-1])


def package_for(nbm_cycle: datetime) -> datetime:
    """The G-AIRMET package built from `nbm_cycle` -- the +6h shift, named."""
    return nbm_cycle + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)


def scheduled_start(now: datetime, trigger: str) -> datetime | None:
    """The instant this run's trigger was supposed to fire, or None if manual."""
    offset = nominal_start_offset_minutes(trigger)
    if offset is None:
        return None
    return target_nbm_cycle(now) + timedelta(minutes=offset)


def poll_deadline(job_start: datetime) -> datetime:
    """When the job stops waiting: POLL_WINDOW_MINUTES after it started."""
    return job_start + timedelta(minutes=POLL_WINDOW_MINUTES)


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
    return package_for(target_nbm_cycle(cutoff))


def _cron_line(offset_minutes: int) -> str:
    hours = sorted({(h + offset_minutes // 60) % 24 for h in GAIRMET_CYCLE_HOURS})
    return f"{offset_minutes % 60} {','.join(str(h) for h in hours)} * * *"


def dispatch_cron_entry() -> str:
    """
    The cron line the forecaster sets on the Railway service. Generated
    here so the value in railway.dispatch.json, the one in this module,
    and the one in the docs cannot disagree.
    """
    return _cron_line(DISPATCH_OFFSET_MINUTES)


def cron_entries(hazard: str) -> list[str]:
    """
    The 5-field cron lines a hazard's workflow declares: exactly ONE now,
    the +3:37 backstop.

    An offset past +1:00 rolls into later hours, and +3:37 after 21Z rolls
    into hour 0 of the next day -- which this one does, so the rollover is
    live rather than theoretical. Generated rather than hand-written for
    that reason; tests/test_publish_schedule.py round-trips each line back
    to the offset and cycle it encodes.
    """
    _require_hazard(hazard)
    return [_cron_line(BACKSTOP_OFFSET_MINUTES)]


def log_nbm_arrival(
    target: datetime,
    hazard: str,
    trigger: str,
    job_start: datetime,
    now: datetime | None = None,
    posted: bool = True,
    polled: bool = False,
) -> dict:
    """
    One machine-readable line per run recording which trigger started it,
    how late that start was, and when the cycle it came for showed up.

    THE POINT, RESTATED. The old scheme labelled every line
    `attempt=N/4`, which described a retry ladder that no longer exists
    and answered none of the questions that matter now. What matters now:

        trigger           railway-cron / schedule-backstop / manual.
                          If schedule-backstop lines start appearing
                          regularly, the Railway service is broken.
        start_delay_min   job start minus the trigger's nominal time.
                          THE number that says whether moving off
                          `schedule:` worked. It was ~95 minutes on
                          2026-09-07 and up to 4.5 hours by 2026-09-10;
                          on a workflow_dispatch it should be ~0.
        poll_wait_min     how long we then waited for NBM.
        arrival_min       when the cycle appeared, from its synoptic
                          hour. This is NBM's latency and nothing else.

    arrival_measured says whether arrival_min is a real measurement or
    only an upper bound. It is a measurement when the poll actually
    watched the cycle appear (at least one probe found nothing first). If
    the data was already there on the first probe, all we know is that it
    arrived at or before then -- and conflating those two is precisely
    how a platform delay came to look like an NBM delay.

    A single greppable prefix and flat key=value pairs, because the thing
    that will actually read these is `grep NBM-ARRIVAL` over downloaded
    job logs, not a log pipeline this project does not have.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    nominal = scheduled_start(job_start, trigger)

    record = {
        "hazard": hazard,
        "trigger": trigger,
        "target": f"{target:%Y-%m-%dT%H:%M:%S}Z",
        "package": f"{package_for(target):%Y-%m-%dT%H:%M:%S}Z",
        "posted": "yes" if posted else "no",
        "job_start": f"{job_start:%Y-%m-%dT%H:%M:%S}Z",
        # "-" for a manual run: there is no schedule for it to be late
        # against, and a zero would read as "started on time".
        "start_delay_min": (
            f"{(job_start - nominal).total_seconds() / 60:.0f}" if nominal else "-"
        ),
        "detected": f"{now:%Y-%m-%dT%H:%M:%S}Z",
        "poll_wait_min": f"{(now - job_start).total_seconds() / 60:.0f}",
        "arrival_min": f"{(now - target).total_seconds() / 60:.0f}" if posted else "-",
        "arrival_measured": "yes" if (posted and polled) else "no",
    }
    print("NBM-ARRIVAL " + " ".join(f"{k}={v}" for k, v in record.items()))
    return record


def warn_nbm_not_posted(target: datetime, hazard: str, trigger: str) -> None:
    """
    The [nbm-not-yet] case: the poll window closed and the cycle never
    appeared, so this run builds nothing.

    WARNING on stderr because it is not routine and it is not a no-op the
    way an already-published skip is: the package this run existed to
    produce does not exist yet. It is recoverable -- the +3:37 backstop
    will try again -- and the message says so, because an alarm that
    overstates itself gets ignored.
    """
    backstop = f"+{BACKSTOP_OFFSET_MINUTES // 60}:{BACKSTOP_OFFSET_MINUTES % 60:02d}"
    following = (
        f"The {backstop} scheduled backstop will try again for this cycle."
        if trigger != "schedule-backstop"
        else "This WAS the backstop, so nothing else is scheduled for this cycle."
    )
    print(
        f"WARNING [nbm-not-yet]: NBM {target:%Y-%m-%d %H}Z did not post within the "
        f"{POLL_WINDOW_MINUTES}-minute poll window (hazard={hazard}, trigger={trigger}). "
        f"Nothing was built -- this run does NOT fall back to an older cycle, because "
        f"rebuilding the package that is already live costs a full fetch and generation to "
        f"produce something the publish guard then skips. {following} If this repeats, NBM's "
        f"real arrival is later than the window assumes -- see the arrival_min field on the "
        f"NBM-ARRIVAL lines.",
        file=sys.stderr,
    )
