"""
tests/test_publish_schedule.py
---------------------------------
The publish schedule, and the three places that have to agree about it:
pipeline/publish_schedule.py, the workflow crons, and the viewer's
staleness check.

WHAT CHANGED, AND WHY THESE TESTS DID. The schedule used to be four retry
crons per hazard, +1:15 to +3:00, each one doing a full NBM fetch and
polygon generation before finding out whether it had anything to publish.
On 2026-09-07 GitHub started all four of the 03Z package's IFR attempts
~95 minutes late -- the platform's scheduled-run queue delay, which no
cron expression can tighten away. It is now ONE polling job per hazard
that waits for its cycle, and the schedule module has to express a start
offset, a poll interval and a deadline rather than a ladder of attempts.

The schedule is expressed once, in Python, and the YAML and the JS are
checked against it. That is not bureaucracy: the offsets are meant to be
retuned from the NBM-ARRIVAL measurements, and every retune moves a cron
line, a timeout, and a browser constant that all have to move together.
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
    GAIRMET_CYCLE_HOURS,
    GENERATION_ALLOWANCE_MINUTES,
    HAZARD_STAGGER_MINUTES,
    NBM_LEAD_TIME_OFFSET_HOURS,
    POLL_DEADLINE_OFFSET_MINUTES,
    POLL_INTERVAL_MINUTES,
    POLL_START_OFFSET_MINUTES,
    PROBE_FORECAST_HOUR,
    PUBLISH_WINDOW_CLOSE_MINUTES,
    cron_entries,
    expected_gairmet_cycle,
    job_timeout_minutes,
    log_nbm_arrival,
    poll_deadline,
    poll_start_offset_minutes,
    scheduled_start,
    target_nbm_cycle,
)

WORKFLOWS = {"ifr": "generate_ifr.yml", "mtn_obsc": "generate_mtn_obsc.yml"}


def _workflow(workflow_name):
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow_name).read_text())


def _crons(workflow_name):
    spec = _workflow(workflow_name)
    on = spec[next(k for k in spec if str(k) in ("on", "True"))]
    return [entry["cron"] for entry in on["schedule"]]


# --- the schedule itself ---------------------------------------------------

def test_there_is_one_polling_job_per_hazard_not_four_attempts():
    """
    The headline change. Four cron attempts could not beat a ~95 minute
    queue delay, because every one of them inherited it; a job that waits
    absorbs the delay from either direction.
    """
    for hazard in WORKFLOWS:
        assert len(cron_entries(hazard)) == 1, f"{hazard} still has a ladder of attempts"


def test_the_poll_starts_before_nbm_is_due_and_off_the_busy_minutes():
    """
    Nominally +0:45: the forecaster reports NBM 21Z lands 2215-2230Z, so
    the job wants to be sitting there ~45 minutes ahead of that with room
    for an early cycle.

    And NOT on :00/:15/:30/:45, the platform's most oversubscribed slots.
    That does not fix the queue delay -- nothing here can -- but it is
    free, so there is no reason to pay it.
    """
    assert 30 <= POLL_START_OFFSET_MINUTES <= 60, "the poll no longer starts around +0:45"
    for hazard in WORKFLOWS:
        minute = poll_start_offset_minutes(hazard) % 60
        assert minute % 15 != 0, f"{hazard} fires on a quarter hour ({minute})"
        assert minute % 5 != 0, f"{hazard} fires on a multiple of five ({minute})"


def test_the_deadline_is_shared_and_late_enough_to_be_worth_waiting_for():
    assert POLL_DEADLINE_OFFSET_MINUTES == 210, "the deadline is meant to be +3:30"
    assert PUBLISH_WINDOW_CLOSE_MINUTES == POLL_DEADLINE_OFFSET_MINUTES, (
        "the viewer's staleness anchor has come loose from the deadline it means"
    )
    # Anchored to the synoptic hour, not to job start: a job GitHub
    # starts 95 minutes late must not get 95 extra minutes of runway.
    for hazard in WORKFLOWS:
        assert poll_deadline(datetime(2026, 9, 4, 4, 0)) == datetime(2026, 9, 4, 6, 30)
    assert POLL_DEADLINE_OFFSET_MINUTES > max(
        poll_start_offset_minutes(h) for h in WORKFLOWS
    ), "the deadline is not after the last hazard's start"


def test_the_poll_interval_is_fine_grained_enough_to_be_the_point():
    """
    The old ladder's resolution was 30 minutes, so a cycle landing at
    +1:16 waited until +1:45. Five minutes is what turns "eventually" into
    "within minutes of arrival".
    """
    assert POLL_INTERVAL_MINUTES == 5
    assert POLL_INTERVAL_MINUTES < 30


def test_the_hazards_stay_staggered():
    """
    They force-push separate data branches and MTN OBSC fetches ten NBM
    fields per hour against IFR's four.
    """
    gap = poll_start_offset_minutes("mtn_obsc") - poll_start_offset_minutes("ifr")
    assert gap == 15, f"the stagger is no longer 15 minutes: {gap}"
    assert HAZARD_STAGGER_MINUTES["ifr"] < HAZARD_STAGGER_MINUTES["mtn_obsc"], "IFR runs first"


def test_the_window_never_reaches_the_next_synoptic_hour():
    """
    target_nbm_cycle() takes the most recent synoptic hour, which is only
    unambiguous while the whole window lands inside its own six-hour slot.
    """
    assert POLL_DEADLINE_OFFSET_MINUTES < 6 * 60


def test_an_unknown_hazard_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        poll_start_offset_minutes("turbulence")


def test_the_probe_hour_matches_the_smallest_hour_any_hazard_needs():
    """
    The poller runs before `pip install` and so cannot import
    gairmet_cycle (that module reaches requests at import time), which is
    why publish_schedule carries the derived probe hour rather than
    computing it from FORECAST_HOURS. This is what stops the copy
    drifting.

    FORECAST_HOURS is read out of the source rather than imported, for
    the same reason: importing gairmet_cycle here would make this test
    file itself need requests, which CI deliberately does not install
    (see tests/test_workflow_dependencies.py).
    """
    import ast

    tree = ast.parse((REPO_ROOT / "pipeline" / "gairmet_cycle.py").read_text())
    forecast_hours = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "FORECAST_HOURS" for t in node.targets)
    )
    assert PROBE_FORECAST_HOUR == forecast_hours[0] + NBM_LEAD_TIME_OFFSET_HOURS == 6


# --- the workflows have to agree ------------------------------------------

@pytest.mark.parametrize("hazard", sorted(WORKFLOWS))
def test_the_workflow_crons_match_the_schedule(hazard):
    assert _crons(WORKFLOWS[hazard]) == cron_entries(hazard)


@pytest.mark.parametrize("hazard", sorted(WORKFLOWS))
def test_the_job_timeout_covers_the_whole_poll_plus_the_work(hazard):
    """
    A job that can now SLEEP for over two hours needs a timeout sized from
    the schedule, or it either kills a legitimate wait or -- if left off
    -- lets a wedged poll sit on GitHub's 360-minute default. Deriving it
    means retuning the deadline moves the timeout with it.
    """
    spec = _workflow(WORKFLOWS[hazard])
    declared = spec["jobs"]["generate"]["timeout-minutes"]
    assert declared == job_timeout_minutes(hazard), (
        f"{WORKFLOWS[hazard]} declares timeout-minutes: {declared}, but the schedule implies "
        f"{job_timeout_minutes(hazard)}"
    )
    longest_poll = POLL_DEADLINE_OFFSET_MINUTES - poll_start_offset_minutes(hazard)
    assert declared > longest_poll, "the timeout would fire before the deadline does"
    assert declared - longest_poll == GENERATION_ALLOWANCE_MINUTES[hazard]


@pytest.mark.parametrize("hazard", sorted(WORKFLOWS))
def test_the_publish_decision_happens_before_the_dependency_install(hazard):
    """
    THE change that makes polling affordable. Every non-publishing run used
    to fetch NBM and generate polygons before finding out it had nothing
    to publish -- 15-20 minutes of runner time for IFR, tens of minutes
    for MTN OBSC, three times out of four. If `pip install` ever moves
    back above the guard, that cost quietly returns.
    """
    steps = _workflow(WORKFLOWS[hazard])["jobs"]["generate"]["steps"]
    names = [step.get("name", "") for step in steps]
    poll = next(i for i, n in enumerate(names) if n.startswith("Wait for the target NBM"))
    preflight = next(i for i, n in enumerate(names) if n.startswith("Decide whether"))
    install = next(i for i, n in enumerate(names) if n.startswith("Install dependencies"))
    generate = next(i for i, n in enumerate(names) if n.startswith("Generate latest"))
    assert poll < preflight < install < generate, names

    for index in (install, generate):
        assert "preflight" in str(steps[index].get("if", "")), (
            f"{names[index]!r} is not gated on the pre-flight guard, so a run with nothing "
            f"to publish still pays for it"
        )


@pytest.mark.parametrize("hazard", sorted(WORKFLOWS))
def test_the_generate_step_is_told_which_cycle_the_preflight_approved(hazard):
    """
    Otherwise it re-derives one, and a cycle that posts in between makes
    the generated package a different one from the package the guard
    approved.
    """
    steps = _workflow(WORKFLOWS[hazard])["jobs"]["generate"]["steps"]
    generate = next(s for s in steps if s.get("name", "").startswith("Generate latest"))
    assert "NBM_SOURCE_CYCLE" in generate["env"]
    assert "await_nbm" in generate["env"]["NBM_SOURCE_CYCLE"]


def test_the_old_retry_crons_are_gone():
    stale = {
        "20 3,9,15,21 * * *", "35 3,9,15,21 * * *",   # the pre-NBM originals
        "15 4,10,16,22 * * *", "45 4,10,16,22 * * *",  # the IFR retry ladder
        "15 5,11,17,23 * * *", "45 5,11,17,23 * * *",
        "30 4,10,16,22 * * *", "0 5,11,17,23 * * *",   # the MTN OBSC one
        "30 5,11,17,23 * * *", "0 0,6,12,18 * * *",
    }
    for workflow in WORKFLOWS.values():
        assert not (set(_crons(workflow)) & stale), f"{workflow} still carries a retry cron"


# --- cron round trip -------------------------------------------------------
#     The forward-only check above agrees with itself. These fire the
#     generated cron at its own minute and read the schedule back out of
#     it, which is what caught an hour-rollover error last time the
#     offsets moved.

@pytest.mark.parametrize("hazard", sorted(WORKFLOWS))
def test_every_generated_cron_lands_inside_the_window_it_encodes(hazard):
    """
    Round trip, adapted from the four-attempt version: each cron line,
    fired at its own minute, must read back as a run for the cycle that
    generated it, at the offset that generated it, inside its own poll
    window.
    """
    for cron in cron_entries(hazard):
        minute, hours = cron.split()[0], cron.split()[1]
        for hour in (int(h) for h in hours.split(",")):
            # Sunday 2026-09-06 is arbitrary; any date works, but an
            # hour-0 cron must be read as the PREVIOUS day's cycle.
            when = datetime(2026, 9, 6, hour, int(minute))
            target = target_nbm_cycle(when)
            offset = (when - target).total_seconds() / 60
            assert offset == poll_start_offset_minutes(hazard), (
                f"{hazard} cron {cron!r} at {when} is +{offset:.0f} into the "
                f"{target:%H}Z cycle, not +{poll_start_offset_minutes(hazard)}"
            )
            assert scheduled_start(when, hazard) == when
            assert when < poll_deadline(when)


@pytest.mark.parametrize("offset,expected", [
    (43, ["43 3,9,15,21 * * *"]),      # today's IFR offset: no rollover
    (58, ["58 3,9,15,21 * * *"]),      # today's MTN OBSC offset
    (75, ["15 4,10,16,22 * * *"]),     # the old +1:15 -- rolls into the next hour
    (180, ["0 0,6,12,18 * * *"]),      # +3:00 after 21Z rolls into hour 0 of the NEXT day
    (210, ["30 0,6,12,18 * * *"]),     # the deadline itself, if it were ever a start
])
def test_cron_generation_still_handles_the_hour_and_day_rollover(monkeypatch, offset, expected):
    """
    Today's offsets happen not to roll over, so the round trip above
    cannot exercise the arithmetic that does. The offsets are meant to be
    retuned from the NBM-ARRIVAL measurements, and pushing one past the
    hour -- or, after 21Z, past midnight -- is exactly the edit that goes
    silently wrong by hand. Checked directly rather than left uncovered
    until the day someone makes it.
    """
    import pipeline.publish_schedule as schedule

    monkeypatch.setattr(schedule, "POLL_START_OFFSET_MINUTES", offset)
    assert schedule.cron_entries("ifr") == expected


def test_a_cron_that_rolls_past_midnight_still_reads_back_to_its_own_cycle(monkeypatch):
    """The other half of the rollover: 00:00 must mean YESTERDAY's 21Z."""
    import pipeline.publish_schedule as schedule

    monkeypatch.setattr(schedule, "POLL_START_OFFSET_MINUTES", 180)
    when = datetime(2026, 9, 5, 0, 0)
    target = schedule.target_nbm_cycle(when)
    assert (target.year, target.month, target.day, target.hour) == (2026, 9, 4, 21)
    assert schedule.scheduled_start(when, "ifr") == when


# --- which cycle is a run working on --------------------------------------

@pytest.mark.parametrize("now,target_hour,target_day", [
    ("2026-09-04T03:43", 3, 4),    # IFR's scheduled start
    ("2026-09-04T03:58", 3, 4),    # MTN OBSC's
    ("2026-09-04T05:18", 3, 4),    # started 95 minutes late -- still the 03Z cycle
    ("2026-09-04T06:30", 3, 4),    # the deadline itself
    ("2026-09-04T21:43", 21, 4),
    ("2026-09-05T00:30", 21, 4),   # the 21Z cycle's deadline, after midnight
])
def test_the_target_cycle_survives_a_late_start_and_midnight(now, target_hour, target_day):
    when = datetime.fromisoformat(now)
    target = target_nbm_cycle(when)
    assert (target.hour, target.day) == (target_hour, target_day)


def test_a_run_started_past_its_own_deadline_still_knows_how_late_it_is():
    """
    Not hypothetical: a ~95 minute queue delay against a +3:30 deadline
    leaves plenty of room for this. The number the log needs is the queue
    delay, and it stays correct as long as the run has not crossed into
    the next synoptic hour.
    """
    when = datetime(2026, 9, 4, 6, 45)  # +3:45 into the 03Z cycle
    assert target_nbm_cycle(when).hour == 3
    assert when > poll_deadline(when)
    assert (when - scheduled_start(when, "ifr")) == timedelta(minutes=182)


# --- the arrival instrumentation ------------------------------------------

def _arrival_fields(capsys):
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    return dict(pair.split("=", 1) for pair in line.split()[1:])


def test_the_queue_delay_and_the_nbm_arrival_are_separate_numbers(capsys):
    """
    THE reason this instrumentation changed. On 2026-09-07 the platform
    was ~95 minutes late and NBM was on time, and the log reported a
    single conflated "delta_min" that could have meant either. They need
    different fixes -- one is unfixable platform behaviour to absorb, the
    other is a schedule constant to retune -- so they are reported apart.
    """
    job_start = datetime(2026, 9, 4, 5, 18)   # +1:35: 95 minutes after IFR's +0:43
    detected = datetime(2026, 9, 4, 5, 19)    # found on the first probe
    log_nbm_arrival(datetime(2026, 9, 4, 3, 0), hazard="ifr", now=detected,
                    job_start=job_start, polled=False)
    fields = _arrival_fields(capsys)

    assert fields["queue_delay_min"] == "95", "the platform's lateness"
    assert fields["poll_wait_min"] == "1"
    assert fields["arrival_min"] == "139", "when the cycle was seen, from its synoptic hour"
    assert fields["on_target"] == "yes"
    # And says the arrival number is only an upper bound: the data was
    # already there when we first looked, so all we know is "by then".
    assert fields["arrival_measured"] == "no"


def test_an_arrival_the_poll_actually_watched_land_is_marked_as_measured(capsys):
    """
    The case the whole exercise is collecting: at least one probe found
    nothing, then one found the cycle, so arrival_min is a real
    measurement rather than an upper bound. A week of these is what lets
    the start offset and the deadline be set from data.
    """
    job_start = datetime(2026, 9, 4, 3, 43)
    detected = datetime(2026, 9, 4, 4, 23)
    log_nbm_arrival(datetime(2026, 9, 4, 3, 0), hazard="ifr", now=detected,
                    job_start=job_start, polled=True)
    fields = _arrival_fields(capsys)

    assert fields["arrival_measured"] == "yes"
    assert fields["arrival_min"] == "83"
    assert fields["poll_wait_min"] == "40"
    assert fields["queue_delay_min"] == "0", "started exactly on time"


def test_a_deadline_miss_records_no_cycle_rather_than_inventing_one(capsys):
    job_start = datetime(2026, 9, 4, 3, 43)
    log_nbm_arrival(None, hazard="ifr", now=datetime(2026, 9, 4, 6, 30),
                    job_start=job_start, polled=True)
    fields = _arrival_fields(capsys)
    assert fields["found"] == "none"
    assert fields["on_target"] == "no"
    assert fields["arrival_measured"] == "no"


def test_falling_back_to_an_older_cycle_is_not_on_target(capsys):
    job_start = datetime(2026, 9, 4, 3, 43)
    log_nbm_arrival(datetime(2026, 9, 3, 21, 0), hazard="ifr",
                    now=datetime(2026, 9, 4, 6, 30), job_start=job_start, polled=True)
    fields = _arrival_fields(capsys)
    assert fields["on_target"] == "no"
    assert fields["behind_min"] == "360"


def test_the_line_stays_greppable_and_flat(capsys):
    """
    What actually reads these is `grep NBM-ARRIVAL` over a downloaded job
    log, not a log pipeline this project does not have. One prefix, flat
    key=value pairs, no spaces inside a value.
    """
    log_nbm_arrival(datetime(2026, 9, 4, 3, 0), hazard="ifr",
                    now=datetime(2026, 9, 4, 4, 0), job_start=datetime(2026, 9, 4, 3, 43))
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    for pair in line.split()[1:]:
        assert pair.count("=") >= 1 and not pair.startswith("=")


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
    assert int(window.group(1)) == PUBLISH_WINDOW_CLOSE_MINUTES == 210, (
        "the viewer is still anchored to the old +3:00 attempt window; a cycle would be "
        "called stale during the last half hour a run is legitimately still polling for it"
    )

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


# --- the staleness expectation, on a frozen clock -------------------------
#     expected_gairmet_cycle() is the Python mirror of map.js's
#     expectedGairmetCycle(). The cases below are the ones that move when
#     the deadline moves, which is exactly what just happened: at 06:15Z
#     the 03Z cycle's window is still OPEN under +3:30 and had already
#     closed under +3:00, and getting that wrong shows a forecaster a
#     STALE DATA banner over a run that is still legitimately polling.

@pytest.mark.parametrize("now,expected", [
    # Just before the 03Z cycle's +3:30 deadline: the newest package that
    # SHOULD be out is still the one from 21Z the previous day (21Z + 6h).
    ("2026-09-04T06:29", "2026-09-04T03:00"),
    # The moment the deadline passes, the 03Z package becomes expected.
    ("2026-09-04T06:30", "2026-09-04T09:00"),
    # The old +3:00 anchor would have flipped half an hour earlier. This
    # is the case the deadline change is about.
    ("2026-09-04T06:15", "2026-09-04T03:00"),
    # Midnight rollover: at 00:15Z the 21Z cycle's window is still open,
    # so the expectation is still the package built from 15Z.
    ("2026-09-05T00:15", "2026-09-04T21:00"),
    ("2026-09-05T00:30", "2026-09-05T03:00"),
    # Deep inside a quiet stretch -- nothing due, the last closed window
    # still governs.
    ("2026-09-04T14:00", "2026-09-04T15:00"),
])
def test_the_expected_package_flips_at_the_deadline_not_before(now, expected):
    assert expected_gairmet_cycle(datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


def test_the_expectation_is_the_package_from_the_last_closed_window():
    """
    The whole definition, restated as an invariant rather than as cases:
    whatever `now` is, the expected package is exactly the lead offset
    ahead of an NBM synoptic hour whose poll deadline has already passed,
    and it is the NEWEST such.
    """
    when = datetime(2026, 9, 4, 0, 0)
    for _ in range(24 * 4):  # a full day, every 15 minutes
        expected = expected_gairmet_cycle(when)
        synoptic = expected - timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
        assert synoptic.hour in GAIRMET_CYCLE_HOURS
        assert synoptic + timedelta(minutes=POLL_DEADLINE_OFFSET_MINUTES) <= when, (
            "the viewer expects a package whose publish window has not closed yet -- it would "
            "show STALE DATA over a run that is still legitimately polling"
        )
        later = synoptic + timedelta(hours=6)
        assert later + timedelta(minutes=POLL_DEADLINE_OFFSET_MINUTES) > when, (
            "the expectation is a whole cycle stale itself"
        )
        when += timedelta(minutes=15)


def test_the_expectation_never_lags_the_clock_by_a_whole_cycle():
    """
    Why that matters: the indicator only fires at a FULL cycle behind (a
    deliberate tolerance for browser clock error). If the expectation
    itself lagged by a cycle, it would take TWO missed publishes to say
    anything, which is not a staleness check.

    The bounds are not symmetric, and that is correct rather than
    surprising. With a +6h lead and a +3:30 deadline, the expected
    package sits anywhere from 3h30m behind the wall clock (just before
    the next window closes) to 2h30m ahead of it (just after one does).
    """
    when = datetime(2026, 9, 4, 0, 0)
    lead = timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
    deadline = timedelta(minutes=POLL_DEADLINE_OFFSET_MINUTES)
    for _ in range(24 * 4):
        offset = expected_gairmet_cycle(when) - when
        assert -deadline < offset <= lead - deadline
        assert abs(offset) < timedelta(hours=6), "a full cycle of slack"
        when += timedelta(minutes=15)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
