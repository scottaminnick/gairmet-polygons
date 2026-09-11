"""
tests/test_publish_schedule.py
---------------------------------
The publish schedule, and the four places that now have to agree about
it: pipeline/publish_schedule.py, the workflow crons, railway.dispatch.json,
and the viewer's staleness check.

WHAT CHANGED, AND WHY THESE TESTS DID. The trigger left `schedule:`.
GitHub delays scheduled runs by 95 minutes (measured 2026-09-07) to 4.5
hours (2026-09-10), and the delay is growing; scheduled events are the
lowest-priority trigger on the platform. `workflow_dispatch` is not
queued that way, so a Railway cron service POSTs one at +0:45 and the
`schedule:` entry survives only as a single +3:37 backstop for the case
where that dispatch did not happen.

That makes this module the source of truth for one more consumer than
before -- the Railway config carries a copy of the dispatch cron, and a
dispatcher that fires at a different time from the schedule the rest of
the system assumes is a failure nobody would see until a package went
missing.
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.publish_schedule import (  # noqa: E402
    BACKSTOP_OFFSET_MINUTES,
    DISPATCH_OFFSET_MINUTES,
    GAIRMET_CYCLE_HOURS,
    HAZARD_WORKFLOWS,
    HAZARDS,
    NBM_LEAD_TIME_OFFSET_HOURS,
    POLL_INTERVAL_MINUTES,
    POLL_WINDOW_MINUTES,
    PROBE_FORECAST_HOUR,
    PUBLISH_WINDOW_CLOSE_MINUTES,
    TRIGGERS,
    WORK_ALLOWANCE_MINUTES,
    classify_trigger,
    cron_entries,
    dispatch_cron_entry,
    expected_gairmet_cycle,
    job_timeout_minutes,
    log_nbm_arrival,
    nominal_start_offset_minutes,
    package_for,
    poll_deadline,
    scheduled_start,
    target_nbm_cycle,
)


def _workflow(hazard):
    return yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / HAZARD_WORKFLOWS[hazard]).read_text()
    )


def _crons(hazard):
    spec = _workflow(hazard)
    on = spec[next(k for k in spec if str(k) in ("on", "True"))]
    return [entry["cron"] for entry in on["schedule"]]


# --- the trigger moved off `schedule:` ------------------------------------

def test_there_is_exactly_one_scheduled_entry_per_hazard():
    """
    The headline change. Adding scheduled runs to work around
    scheduled-run delay is what produced the four-attempt ladder this
    replaced -- and it quadrupled the load we were putting into the very
    queue that was making us late.
    """
    for hazard in HAZARDS:
        assert len(cron_entries(hazard)) == 1, f"{hazard} has more than one backstop"
        assert len(_crons(hazard)) == 1, f"{HAZARD_WORKFLOWS[hazard]} declares more than one"


def test_the_backstop_is_late_enough_to_only_ever_be_a_backstop():
    """
    It exists for one failure: the Railway dispatch did not happen. By
    then NBM has certainly posted and the normal path has certainly
    either worked or failed, so the backstop either finds the package
    already published or publishes it late.
    """
    assert BACKSTOP_OFFSET_MINUTES == 217, "the backstop is meant to be +3:37"
    normal_path_ends = DISPATCH_OFFSET_MINUTES + POLL_WINDOW_MINUTES + max(
        WORK_ALLOWANCE_MINUTES.values()
    )
    assert BACKSTOP_OFFSET_MINUTES > normal_path_ends, (
        "the backstop fires while the dispatched run could still be working, so it would "
        "race its own normal path rather than back it up"
    )


def test_the_backstop_avoids_the_platforms_busiest_scheduled_minutes():
    """
    :00/:15/:30/:45 are the most oversubscribed slots. This is the one
    trigger still exposed to GitHub's scheduled-run queue, so it is the
    one that can least afford to sit behind everyone else's cron. Free;
    not a fix.
    """
    minute = BACKSTOP_OFFSET_MINUTES % 60
    assert minute % 15 != 0, f"the backstop fires on a quarter hour ({minute})"
    assert minute % 5 != 0, f"the backstop fires on a multiple of five ({minute})"


def test_the_whole_backstop_window_stays_inside_its_own_synoptic_slot():
    """
    target_nbm_cycle() takes the most recent synoptic hour, which is only
    unambiguous while even the latest run's poll window lands inside its
    own six-hour slot.
    """
    assert BACKSTOP_OFFSET_MINUTES + POLL_WINDOW_MINUTES < 6 * 60


# --- the Railway dispatch -------------------------------------------------

def test_the_dispatch_fires_before_nbm_is_due_with_room_to_wait():
    """
    The forecaster reports NBM 21Z lands 2215-2230Z (+1:15 to +1:30).
    Dispatching at +0:45 puts the job in place ~45 minutes early, and the
    60-minute window carries it to +1:45 -- past the late end of the
    observed range, with margin.
    """
    assert DISPATCH_OFFSET_MINUTES == 45
    assert POLL_WINDOW_MINUTES == 60
    latest_observed_nbm = 90  # +1:30
    assert DISPATCH_OFFSET_MINUTES < latest_observed_nbm < (
        DISPATCH_OFFSET_MINUTES + POLL_WINDOW_MINUTES
    ), "the poll window no longer straddles NBM's observed arrival range"


def test_the_railway_config_matches_the_schedule():
    """
    The dispatch cron lives in TWO places -- this module and
    railway.dispatch.json, which is what Railway actually reads. A
    dispatcher firing at a different time from the schedule the rest of
    the system assumes is invisible until a package goes missing.
    """
    config = json.loads((REPO_ROOT / "railway.dispatch.json").read_text())
    assert config["deploy"]["cronSchedule"] == dispatch_cron_entry()
    assert dispatch_cron_entry() == "45 3,9,15,21 * * *"


def test_the_railway_service_runs_the_dispatcher_and_does_not_restart_it():
    """
    A cron service that restarts on exit would re-POST the dispatch in a
    loop. Railway's own guidance is restartPolicyType NEVER for cron.
    """
    config = json.loads((REPO_ROOT / "railway.dispatch.json").read_text())
    assert "scripts/dispatch_workflows.py" in config["deploy"]["startCommand"]
    assert config["deploy"]["restartPolicyType"] == "NEVER"


def test_the_railway_config_is_separate_from_the_web_apps():
    """
    Railway reads railway.json by default. If the cron service used that,
    one config file would have to describe both this service and the web
    app -- and the web app's build is not this one. The separate filename
    is what the forecaster points the service at.
    """
    assert (REPO_ROOT / "railway.dispatch.json").exists()
    assert not (REPO_ROOT / "railway.json").exists(), (
        "a default railway.json now exists and would be picked up by BOTH services"
    )


def test_the_poll_interval_is_fine_grained_enough_to_be_the_point():
    """The old retry ladder's resolution was 30 minutes: a cycle landing
    at +1:16 waited until +1:45."""
    assert POLL_INTERVAL_MINUTES == 5
    assert POLL_INTERVAL_MINUTES < 30


# --- trigger classification ----------------------------------------------

@pytest.mark.parametrize("event_name,trigger_input,expected", [
    ("workflow_dispatch", "railway-cron", "railway-cron"),
    ("workflow_dispatch", "manual", "manual"),
    ("workflow_dispatch", "", "manual"),
    ("workflow_dispatch", None, "manual"),
    ("schedule", None, "schedule-backstop"),
    # A `schedule` event carries no inputs at all, so a scheduled run
    # claiming to be railway-cron is a contradiction, not a data point.
    ("schedule", "railway-cron", "schedule-backstop"),
    # Free text from anyone with the Run workflow button. A typo must not
    # be able to land verbatim in the instrumentation and look like a
    # measurement.
    ("workflow_dispatch", "railway_cron", "manual"),
    ("workflow_dispatch", "  RAILWAY-CRON  ", "railway-cron"),
    ("repository_dispatch", "railway-cron", "railway-cron"),
])
def test_the_trigger_is_classified_not_trusted(event_name, trigger_input, expected):
    assert classify_trigger(event_name, trigger_input) == expected


def test_every_classification_is_one_of_the_three_known_triggers():
    for event in ("schedule", "workflow_dispatch", "push", ""):
        for value in (None, "", "railway-cron", "nonsense", "12345"):
            assert classify_trigger(event, value) in TRIGGERS


def test_only_the_scheduled_triggers_have_a_nominal_start_to_be_late_against():
    assert nominal_start_offset_minutes("railway-cron") == DISPATCH_OFFSET_MINUTES
    assert nominal_start_offset_minutes("schedule-backstop") == BACKSTOP_OFFSET_MINUTES
    assert nominal_start_offset_minutes("manual") is None, (
        "a manual run has no schedule, and a zero delay would read as 'started on time'"
    )
    assert scheduled_start(datetime(2026, 9, 4, 4, 0), "manual") is None


# --- the workflows have to agree ------------------------------------------

@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_workflow_crons_match_the_schedule(hazard):
    assert _crons(hazard) == cron_entries(hazard)


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_workflow_accepts_the_trigger_input(hazard):
    """
    Without it the Railway dispatch's `inputs` are rejected outright by
    the API (422), and every dispatched run would fail to start.
    """
    spec = _workflow(hazard)
    on = spec[next(k for k in spec if str(k) in ("on", "True"))]
    inputs = on["workflow_dispatch"]["inputs"]
    assert "trigger" in inputs, f"{HAZARD_WORKFLOWS[hazard]} would reject the dispatch payload"
    assert inputs["trigger"].get("default") == "manual", (
        "the default has to be the SAFE label: a button press that inherited 'railway-cron' "
        "would make a human run look like the automated one in the logs"
    )
    assert inputs["trigger"].get("required") is False


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_job_timeout_covers_the_whole_poll_plus_the_work(hazard):
    """
    A job that can sleep for an hour needs a timeout sized from the
    schedule, or it either kills a legitimate wait or -- left off -- lets
    a wedged poll sit on GitHub's 360-minute default.
    """
    declared = _workflow(hazard)["jobs"]["generate"]["timeout-minutes"]
    assert declared == job_timeout_minutes(hazard), (
        f"{HAZARD_WORKFLOWS[hazard]} declares timeout-minutes: {declared}, but the schedule "
        f"implies {job_timeout_minutes(hazard)}"
    )
    assert declared > POLL_WINDOW_MINUTES, "the timeout would fire during a legitimate poll"
    assert declared - POLL_WINDOW_MINUTES == WORK_ALLOWANCE_MINUTES[hazard]


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_publish_decision_happens_before_the_poll_and_the_install(hazard):
    """
    THE ordering that makes everything else affordable. Checking
    publishability BEFORE polling means an already-published cycle -- the
    routine case for the backstop -- costs seconds rather than up to an
    hour of waiting for data nobody is going to use. Checking it before
    `pip install` means it costs seconds rather than minutes.
    """
    steps = _workflow(hazard)["jobs"]["generate"]["steps"]
    names = [step.get("name", "") for step in steps]

    def index_of(prefix):
        return next(i for i, n in enumerate(names) if n.startswith(prefix))

    resolve = index_of("Resolve the target NBM cycle")
    read = index_of("Read the cycle currently on")
    preflight = index_of("Decide whether")
    poll = index_of("Wait for the target NBM cycle")
    install = index_of("Install dependencies")
    generate = index_of("Generate latest")
    assert resolve < read < preflight < poll < install < generate, names

    assert "preflight" in str(steps[poll].get("if", "")), (
        "the poll is not gated on the publish decision, so an already-published cycle "
        "still waits up to an hour for data it will not use"
    )
    for index in (install, generate):
        assert "await_nbm" in str(steps[index].get("if", "")), (
            f"{names[index]!r} is not gated on the poll, so it would run against a cycle "
            f"that never posted"
        )


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_publish_step_only_runs_when_a_package_was_actually_built(hazard):
    steps = _workflow(hazard)["jobs"]["generate"]["steps"]
    publish = next(s for s in steps if s.get("name", "").startswith("Publish artifacts"))
    assert "await_nbm" in str(publish.get("if", ""))


@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_generate_step_is_told_which_cycle_was_approved(hazard):
    """
    Otherwise it re-derives one, and a cycle that posts in between makes
    the generated package a different one from the package the pre-flight
    guard approved.
    """
    steps = _workflow(hazard)["jobs"]["generate"]["steps"]
    generate = next(s for s in steps if s.get("name", "").startswith("Generate latest"))
    assert "NBM_SOURCE_CYCLE" in generate["env"]
    assert "resolve" in generate["env"]["NBM_SOURCE_CYCLE"]


def test_the_old_schedules_are_all_gone():
    stale = {
        "20 3,9,15,21 * * *", "35 3,9,15,21 * * *",     # the pre-NBM originals
        "15 4,10,16,22 * * *", "45 4,10,16,22 * * *",   # the IFR retry ladder
        "15 5,11,17,23 * * *", "45 5,11,17,23 * * *",
        "30 4,10,16,22 * * *", "0 5,11,17,23 * * *",    # the MTN OBSC one
        "30 5,11,17,23 * * *", "0 0,6,12,18 * * *",
        "43 3,9,15,21 * * *", "58 3,9,15,21 * * *",     # the single-poll-job pair
    }
    for hazard in HAZARDS:
        assert not (set(_crons(hazard)) & stale), (
            f"{HAZARD_WORKFLOWS[hazard]} still carries a superseded cron"
        )


# --- cron round trip -------------------------------------------------------

@pytest.mark.parametrize("hazard", sorted(HAZARDS))
def test_the_backstop_cron_reads_back_to_the_offset_it_encodes(hazard):
    """
    Round trip, adapted from the four-attempt version to the single
    backstop: the generated line, fired at its own minute, must be
    recognised as +3:37 into the cycle that generated it.

    This is the check that caught an hour-rollover error last time the
    offsets moved, and the rollover is LIVE here rather than theoretical:
    +3:37 after 21Z is 00:37 the following day, so the hour-0 entry has
    to read back as YESTERDAY's 21Z cycle.
    """
    for cron in cron_entries(hazard):
        minute, hours = cron.split()[0], cron.split()[1]
        assert "0" in hours.split(","), "the +3:37 offset should roll past midnight"
        for hour in (int(h) for h in hours.split(",")):
            # Sunday 2026-09-06 is arbitrary; any date works, but the
            # hour-0 entry must be read as the PREVIOUS day's cycle.
            when = datetime(2026, 9, 6, hour, int(minute))
            target = target_nbm_cycle(when)
            offset = (when - target).total_seconds() / 60
            assert offset == BACKSTOP_OFFSET_MINUTES, (
                f"{hazard} cron {cron!r} at {when} is +{offset:.0f} into the "
                f"{target:%H}Z cycle, not +{BACKSTOP_OFFSET_MINUTES}"
            )
            assert scheduled_start(when, "schedule-backstop") == when


def test_the_midnight_backstop_targets_yesterdays_cycle():
    """A date bug here would make the 21Z package look like a 03Z miss."""
    when = datetime(2026, 9, 5, 0, 37)
    target = target_nbm_cycle(when)
    assert (target.year, target.month, target.day, target.hour) == (2026, 9, 4, 21)
    assert package_for(target) == datetime(2026, 9, 5, 3, 0)


def test_the_dispatch_cron_round_trips_too():
    """The Railway line is generated by the same arithmetic and is the one
    the forecaster types by hand into a UI field."""
    minute, hours = dispatch_cron_entry().split()[0], dispatch_cron_entry().split()[1]
    for hour in (int(h) for h in hours.split(",")):
        when = datetime(2026, 9, 6, hour, int(minute))
        assert (when - target_nbm_cycle(when)).total_seconds() / 60 == DISPATCH_OFFSET_MINUTES
        assert scheduled_start(when, "railway-cron") == when


def test_the_probe_hour_matches_the_smallest_hour_any_hazard_needs():
    """
    The pre-install scripts cannot import gairmet_cycle (it reaches
    requests at import time), which is why publish_schedule carries the
    derived probe hour. FORECAST_HOURS is read out of the source rather
    than imported for the same reason -- importing it here would make
    this test file itself need requests, which CI deliberately does not
    install (see tests/test_workflow_dependencies.py).
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


# --- which cycle is a run working on --------------------------------------

@pytest.mark.parametrize("now,target_hour,target_day", [
    ("2026-09-04T03:45", 3, 4),    # the Railway dispatch
    ("2026-09-04T04:45", 3, 4),    # its poll window closing
    ("2026-09-04T06:37", 3, 4),    # the backstop
    ("2026-09-04T07:37", 3, 4),    # the backstop's own window closing
    ("2026-09-04T21:45", 21, 4),
    ("2026-09-05T00:37", 21, 4),   # the 21Z backstop, after midnight
    ("2026-09-05T01:37", 21, 4),
])
def test_the_target_cycle_is_unambiguous_across_every_start_and_midnight(
    now, target_hour, target_day
):
    when = datetime.fromisoformat(now)
    target = target_nbm_cycle(when)
    assert (target.hour, target.day) == (target_hour, target_day)


def test_the_poll_window_is_measured_from_job_start_not_the_synoptic_hour():
    """
    Job start IS dispatch time now -- that is the whole point of moving
    off `schedule:` -- so the window is a statement about how long we
    wait after ASKING, and the backstop gets the same 60 minutes from its
    own later start.
    """
    dispatch = datetime(2026, 9, 4, 3, 45)
    backstop = datetime(2026, 9, 4, 6, 37)
    assert poll_deadline(dispatch) == datetime(2026, 9, 4, 4, 45)
    assert poll_deadline(backstop) == datetime(2026, 9, 4, 7, 37)


def test_a_late_dispatch_still_measures_its_own_lateness():
    """
    The number that will tell us whether the move worked. If Railway or
    the dispatch itself is ever late, this has to stay correct rather
    than silently reading as on-time.
    """
    when = datetime(2026, 9, 4, 4, 20)  # 35 minutes after the +0:45 dispatch
    assert target_nbm_cycle(when).hour == 3
    assert when - scheduled_start(when, "railway-cron") == timedelta(minutes=35)


# --- the arrival instrumentation ------------------------------------------

def _arrival(capsys):
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    return dict(pair.split("=", 1) for pair in line.split()[1:])


def test_the_line_records_which_trigger_started_the_run(capsys):
    """
    Replaces the old attempt=N/4, which described a retry ladder that no
    longer exists. If schedule-backstop lines start appearing regularly,
    the Railway service is broken and the pipeline is running on its
    safety net.
    """
    target = datetime(2026, 9, 4, 3, 0)
    log_nbm_arrival(target, "ifr", "railway-cron", datetime(2026, 9, 4, 3, 45),
                    now=datetime(2026, 9, 4, 4, 20), polled=True)
    fields = _arrival(capsys)
    assert fields["trigger"] == "railway-cron"
    assert "attempt" not in fields


def test_a_prompt_dispatch_shows_a_near_zero_start_delay(capsys):
    """
    THE measurement this whole change exists to produce. `schedule:` runs
    were ~95 minutes late on 2026-09-07 and up to 4.5 hours by
    2026-09-10; a workflow_dispatch should be ~0. A week of these is how
    we find out whether the move worked.
    """
    target = datetime(2026, 9, 4, 3, 0)
    log_nbm_arrival(target, "ifr", "railway-cron", datetime(2026, 9, 4, 3, 46),
                    now=datetime(2026, 9, 4, 4, 18), polled=True)
    fields = _arrival(capsys)
    assert fields["start_delay_min"] == "1"
    assert fields["poll_wait_min"] == "32", "how long we then waited for NBM"
    assert fields["arrival_min"] == "78", "when the cycle appeared, from its synoptic hour"
    assert fields["arrival_measured"] == "yes", "the poll watched this one land"


def test_the_backstops_start_delay_is_measured_against_its_own_cron(capsys):
    """
    Not against the dispatch time -- a backstop starting at +3:40 is
    three minutes late, not nearly three hours late, and reporting the
    latter would make GitHub's queue look worse than it is.
    """
    target = datetime(2026, 9, 4, 3, 0)
    log_nbm_arrival(target, "ifr", "schedule-backstop", datetime(2026, 9, 4, 6, 40),
                    now=datetime(2026, 9, 4, 6, 41))
    assert _arrival(capsys)["start_delay_min"] == "3"


def test_a_manual_run_has_no_start_delay_to_report(capsys):
    """A zero would read as 'started on time' for something with no
    schedule to be on time for."""
    target = datetime(2026, 9, 4, 3, 0)
    log_nbm_arrival(target, "ifr", "manual", datetime(2026, 9, 4, 5, 12),
                    now=datetime(2026, 9, 4, 5, 13))
    assert _arrival(capsys)["start_delay_min"] == "-"


def test_a_cycle_already_there_on_the_first_probe_is_an_upper_bound_only(capsys):
    """
    All we know is that it arrived at or before then. Reporting that as a
    measurement is precisely how a platform delay came to look like an
    NBM delay.
    """
    target = datetime(2026, 9, 4, 3, 0)
    log_nbm_arrival(target, "ifr", "schedule-backstop", datetime(2026, 9, 4, 6, 37),
                    now=datetime(2026, 9, 4, 6, 38), polled=False)
    assert _arrival(capsys)["arrival_measured"] == "no"


def test_a_cycle_that_never_posted_reports_no_arrival_rather_than_a_number(capsys):
    target = datetime(2026, 9, 4, 3, 0)
    log_nbm_arrival(target, "ifr", "railway-cron", datetime(2026, 9, 4, 3, 45),
                    now=datetime(2026, 9, 4, 4, 45), posted=False, polled=True)
    fields = _arrival(capsys)
    assert fields["posted"] == "no"
    assert fields["arrival_min"] == "-"
    assert fields["arrival_measured"] == "no"


def test_the_line_stays_greppable_and_flat(capsys):
    """
    What actually reads these is `grep NBM-ARRIVAL` over a downloaded job
    log, not a log pipeline this project does not have.
    """
    log_nbm_arrival(datetime(2026, 9, 4, 3, 0), "ifr", "railway-cron",
                    datetime(2026, 9, 4, 3, 45), now=datetime(2026, 9, 4, 4, 0))
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    for pair in line.split()[1:]:
        assert pair.count("=") >= 1 and not pair.startswith("=")


# --- the viewer has to agree ----------------------------------------------

def test_the_viewer_uses_the_same_numbers_as_the_scheduler():
    js = (REPO_ROOT / "webapp" / "static" / "map.js").read_text()

    hours = re.search(r"const CYCLE_HOURS = \[([^\]]+)\]", js)
    assert hours, "map.js no longer declares CYCLE_HOURS"
    assert [int(h) for h in hours.group(1).split(",")] == GAIRMET_CYCLE_HOURS

    window = re.search(r"const PUBLISH_WINDOW_CLOSE_MINUTES = (\d+)", js)
    assert window, "map.js still uses the old publish-grace constant"
    assert int(window.group(1)) == PUBLISH_WINDOW_CLOSE_MINUTES == 150, (
        "the viewer is anchored to a publish window that is not the one the pipeline "
        "actually follows"
    )

    lead = re.search(r"const NBM_LEAD_OFFSET_HOURS = (\d+)", js)
    assert lead, "map.js does not account for the NBM lead offset"
    assert int(lead.group(1)) == NBM_LEAD_TIME_OFFSET_HOURS == 6


def test_the_viewer_no_longer_compares_the_cycle_to_the_wall_clock():
    js = (REPO_ROOT / "webapp" / "static" / "map.js").read_text()
    assert "PUBLISH_GRACE_MINUTES" not in js, "the old 90-minute grace is still there"
    assert "expectedGairmetCycle" in js


def test_the_expected_window_follows_the_normal_path_not_the_backstop():
    """
    Deliberate: +0:45 dispatch, +60 poll, +45 work. Waiting for the
    +3:37 backstop's own completion (~+4:22) before saying anything would
    hide a real failure for nearly two hours to avoid one honest warning.
    Past +2:30 the package IS late; the backstop is recovery, not runway.
    """
    assert PUBLISH_WINDOW_CLOSE_MINUTES == (
        DISPATCH_OFFSET_MINUTES + POLL_WINDOW_MINUTES + max(WORK_ALLOWANCE_MINUTES.values())
    )
    assert PUBLISH_WINDOW_CLOSE_MINUTES < BACKSTOP_OFFSET_MINUTES


# --- the staleness expectation, on a frozen clock -------------------------
#     expected_gairmet_cycle() is the Python mirror of map.js's
#     expectedGairmetCycle(). These are the cases that move when the
#     publish window moves, which is what just happened: the window closed
#     at +3:30 under the old poll-job design and closes at +2:30 now.

@pytest.mark.parametrize("now,expected", [
    # Just before the 03Z cycle's window closes (+2:30 = 05:30Z): the
    # newest package that SHOULD be out is still the one from 21Z the
    # previous day.
    ("2026-09-04T05:29", "2026-09-04T03:00"),
    # The moment it closes, the 03Z package becomes expected.
    ("2026-09-04T05:30", "2026-09-04T09:00"),
    # The old +3:30 anchor would still have been waiting here. Under the
    # dispatch path the package is genuinely late by now.
    ("2026-09-04T06:00", "2026-09-04T09:00"),
    # Midnight rollover: at 23:29Z the 21Z cycle's window is still open,
    # so the expectation is still the package built from 15Z.
    ("2026-09-04T23:29", "2026-09-04T21:00"),
    ("2026-09-04T23:30", "2026-09-05T03:00"),
    # And just after midnight, still the 21Z cycle's package.
    ("2026-09-05T00:15", "2026-09-05T03:00"),
    # Deep inside a quiet stretch -- nothing due, the last closed window
    # still governs.
    ("2026-09-04T14:00", "2026-09-04T15:00"),
])
def test_the_expected_package_flips_when_the_normal_path_would_have_finished(now, expected):
    assert expected_gairmet_cycle(datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


def test_the_expectation_is_the_package_from_the_last_closed_window():
    """
    The whole definition, restated as an invariant rather than as cases:
    whatever `now` is, the expected package is exactly the lead offset
    ahead of an NBM synoptic hour whose publish window has already
    closed, and it is the NEWEST such.
    """
    when = datetime(2026, 9, 4, 0, 0)
    for _ in range(24 * 4):  # a full day, every 15 minutes
        expected = expected_gairmet_cycle(when)
        synoptic = expected - timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
        assert synoptic.hour in GAIRMET_CYCLE_HOURS
        assert synoptic + timedelta(minutes=PUBLISH_WINDOW_CLOSE_MINUTES) <= when, (
            "the viewer expects a package whose publish window has not closed yet -- it "
            "would show STALE DATA over a run that is still legitimately working"
        )
        later = synoptic + timedelta(hours=6)
        assert later + timedelta(minutes=PUBLISH_WINDOW_CLOSE_MINUTES) > when, (
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
    surprising. With a +6h lead and a +2:30 window, the expected package
    sits anywhere from 2h30m behind the wall clock to 3h30m ahead of it.
    """
    when = datetime(2026, 9, 4, 0, 0)
    lead = timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
    window = timedelta(minutes=PUBLISH_WINDOW_CLOSE_MINUTES)
    for _ in range(24 * 4):
        offset = expected_gairmet_cycle(when) - when
        assert -window < offset <= lead - window
        assert abs(offset) < timedelta(hours=6), "a full cycle of slack"
        when += timedelta(minutes=15)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
