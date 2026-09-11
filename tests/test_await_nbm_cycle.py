"""
tests/test_await_nbm_cycle.py
--------------------------------
The two pre-install steps that decide what a run does before it does
anything: .github/scripts/resolve_target_cycle.py (which cycle, which
package, which trigger -- no network at all) and
.github/scripts/await_nbm_cycle.py (wait for that cycle to post).

WHY THEY ARE WORTH TESTING RATHER THAN TRUSTING. Between them they gate
every scheduled run of both hazards, and their most important behaviours
are ones a real run only exercises on a bad day: a cycle that never
posts, a backstop finding the work already done, a manual run with no
schedule to be late against. None of that is reachable by waiting around
for a production run to demonstrate it.

Nothing here touches the network. The probe is injected, so the tests
describe what the runner would see and assert what the job does about it.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.publish_schedule import (  # noqa: E402
    BACKSTOP_OFFSET_MINUTES,
    DISPATCH_OFFSET_MINUTES,
    POLL_WINDOW_MINUTES,
)


def _load(name):
    """Imported by path: .github/scripts/ is not (and shouldn't be) a package."""
    path = REPO_ROOT / ".github" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poller = _load("await_nbm_cycle")
resolver = _load("resolve_target_cycle")

CYCLE_03Z = datetime(2026, 9, 4, 3, 0)
CYCLE_21Z = datetime(2026, 9, 3, 21, 0)
DISPATCHED = CYCLE_03Z + timedelta(minutes=DISPATCH_OFFSET_MINUTES)   # +0:45
BACKSTOP = CYCLE_03Z + timedelta(minutes=BACKSTOP_OFFSET_MINUTES)     # +3:37


class FakeClock:
    """
    A clock that only moves when the code under test sleeps.

    Deliberately not freezegun or a monkeypatched datetime: the thing
    being tested IS the relationship between sleeping and the window
    closing, so the sleep has to be what advances time. A test that let
    real time pass would take an hour.
    """

    def __init__(self, start):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def posted(*cycles):
    """A probe reporting exactly `cycles` as available, by URL substring."""
    stamps = {f"blend.{c:%Y%m%d}/{c:%H}/" for c in cycles}
    return lambda url: any(stamp in url for stamp in stamps)


def appears_after(cycle, probes):
    """A probe that starts finding `cycle` only after `probes` failed looks."""
    stamp = f"blend.{cycle:%Y%m%d}/{cycle:%H}/"
    state = {"seen": 0}

    def probe(url):
        if stamp not in url:
            return False
        state["seen"] += 1
        # Both mirrors are probed per attempt, so count attempts, not URLs.
        return state["seen"] > probes * 2

    return probe


def _arrival(capsys):
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    return dict(pair.split("=", 1) for pair in line.split()[1:])


# ===========================================================================
# resolve_target_cycle.py -- the check-before-work half
# ===========================================================================

def test_resolving_the_run_needs_no_network_at_all():
    """
    The synoptic hours are fixed and the package is the cycle plus the
    +6h lead, so "which package would this run produce" is answerable
    without asking NOAA anything. That is what lets the publish decision
    happen first, and an already-published cycle end the run in seconds.
    """
    result = resolver.resolve("ifr", "workflow_dispatch", "railway-cron", DISPATCHED)
    assert result["target_nbm_cycle"] == "2026-09-04T03:00:00Z"
    assert result["model_cycle"] == "2026-09-04T09:00:00Z", "the package is target + 6h"


def test_the_source_cycle_is_always_the_target_because_there_is_no_fallback():
    """
    The old design, when the target had not posted, rebuilt the package
    from the PREVIOUS NBM run -- which is the package already live, so
    the guard skipped it after a full fetch and generation had been paid
    for. A run now builds the cycle it came for or builds nothing.
    """
    result = resolver.resolve("ifr", "workflow_dispatch", "railway-cron", DISPATCHED)
    assert result["nbm_source_cycle"] == result["target_nbm_cycle"]


@pytest.mark.parametrize("event,raw,expected", [
    ("workflow_dispatch", "railway-cron", "railway-cron"),
    ("schedule", "", "schedule-backstop"),
    ("workflow_dispatch", "manual", "manual"),
    ("workflow_dispatch", "typo-here", "manual"),
])
def test_the_trigger_is_recorded_on_the_run(event, raw, expected):
    assert resolver.resolve("ifr", event, raw, DISPATCHED)["trigger"] == expected


def test_a_prompt_dispatch_records_a_near_zero_start_delay():
    """
    The measurement the whole change exists to produce: `schedule:` runs
    were ~95 minutes late on 2026-09-07 and up to 4.5 hours by
    2026-09-10, and a workflow_dispatch should be ~0.
    """
    result = resolver.resolve("ifr", "workflow_dispatch", "railway-cron",
                              DISPATCHED + timedelta(minutes=1))
    assert result["start_delay_minutes"] == 1.0


def test_the_backstops_delay_is_measured_against_its_own_cron():
    result = resolver.resolve("ifr", "schedule", "", BACKSTOP + timedelta(minutes=12))
    assert result["trigger"] == "schedule-backstop"
    assert result["start_delay_minutes"] == 12.0, (
        "measured against +3:37, not against the dispatch time -- otherwise GitHub's queue "
        "looks nearly three hours worse than it is"
    )


def test_a_manual_run_has_no_start_delay():
    result = resolver.resolve("ifr", "workflow_dispatch", "manual", DISPATCHED)
    assert result["start_delay_minutes"] is None


def test_the_resolver_writes_a_file_the_publish_guard_can_read(tmp_path):
    """
    The pre-flight step hands this straight to should_publish_cycle.py as
    --new. If the shape drifts, the guard silently classifies every skip
    as "unknown" instead of telling the two apart.
    """
    out = tmp_path / "nbm_preflight.json"
    assert resolver.main(["--hazard", "ifr", "--out", str(out), "--event-name",
                          "workflow_dispatch", "--trigger-input", "railway-cron",
                          "--now", DISPATCHED.isoformat() + "Z"]) == 0
    written = json.loads(out.read_text())
    for field in ("model_cycle", "nbm_source_cycle", "target_nbm_cycle", "trigger", "hazard"):
        assert field in written, f"the next step reads {field} and it is not there"
    assert written["model_cycle"].endswith("Z")


def test_the_resolver_publishes_the_two_values_later_steps_need(tmp_path, monkeypatch):
    """
    The poll needs the target cycle and the generate step needs the
    source cycle, and both read them as step outputs. Written by the
    script rather than by a shell snippet in each workflow, so there is
    one implementation and it can be tested.
    """
    output = tmp_path / "gh_output"
    output.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    resolver.main(["--hazard", "ifr", "--out", str(tmp_path / "p.json"),
                   "--event-name", "workflow_dispatch", "--trigger-input", "railway-cron",
                   "--now", DISPATCHED.isoformat() + "Z"])
    written = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert written["trigger"] == "railway-cron"
    assert written["nbm_source_cycle"] == "2026-09-04T03:00:00Z"


def test_the_resolver_runs_fine_outside_actions(tmp_path, monkeypatch):
    """A developer running it by hand has no $GITHUB_OUTPUT, and that must
    not be an error."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert resolver.main(["--hazard", "ifr", "--out", str(tmp_path / "p.json"),
                          "--now", DISPATCHED.isoformat() + "Z"]) == 0


def test_a_run_that_starts_after_midnight_is_still_working_on_yesterdays_cycle():
    """The 21Z backstop fires at 00:37. A date bug would make it chase 03Z."""
    result = resolver.resolve("mtn_obsc", "schedule", "", datetime(2026, 9, 4, 0, 37))
    assert result["target_nbm_cycle"] == "2026-09-03T21:00:00Z"
    assert result["model_cycle"] == "2026-09-04T03:00:00Z"
    assert result["start_delay_minutes"] == 0.0


# ===========================================================================
# await_nbm_cycle.py -- the poll
# ===========================================================================

def test_a_cycle_already_posted_is_taken_on_the_first_look(capsys):
    """
    The routine case for the backstop, and for any dispatch that lands
    after NBM: no sleeping at all.
    """
    clock = FakeClock(BACKSTOP)
    assert poller.await_cycle(CYCLE_03Z, "ifr", "schedule-backstop", clock.now,
                              probe=posted(CYCLE_03Z), clock=clock, sleep=clock.sleep)
    assert clock.slept == [], "it slept despite the data being there"
    fields = _arrival(capsys)
    assert fields["posted"] == "yes"
    assert fields["arrival_measured"] == "no", (
        "the data was already there on the first probe, so its arrival time is an upper "
        "bound -- reporting it as measured is how a platform delay looked like an NBM delay"
    )


def test_a_cycle_that_lands_mid_poll_is_picked_up_within_one_interval(capsys):
    """
    The gain over the old retry ladder, whose resolution was 30 minutes:
    a cycle landing just after an attempt waited half an hour.
    """
    clock = FakeClock(DISPATCHED)
    assert poller.await_cycle(CYCLE_03Z, "ifr", "railway-cron", clock.now,
                              interval_seconds=300, probe=appears_after(CYCLE_03Z, probes=6),
                              clock=clock, sleep=clock.sleep)
    assert clock.slept == [300] * 6
    fields = _arrival(capsys)
    assert fields["poll_wait_min"] == "30"
    assert fields["arrival_measured"] == "yes", "the poll watched this one appear"
    assert fields["arrival_min"] == "75", "NBM's own latency, from its synoptic hour"


def test_the_poll_gives_up_after_the_window_and_builds_nothing(capsys):
    """
    NOT a fallback to an older cycle. Rebuilding the package that is
    already live costs a full fetch and generation to produce something
    the publish guard then skips.
    """
    clock = FakeClock(DISPATCHED)
    assert not poller.await_cycle(CYCLE_03Z, "ifr", "railway-cron", clock.now,
                                  interval_seconds=300, probe=posted(CYCLE_21Z),
                                  clock=clock, sleep=clock.sleep)
    assert sum(clock.slept) == POLL_WINDOW_MINUTES * 60
    assert clock.now == DISPATCHED + timedelta(minutes=POLL_WINDOW_MINUTES)
    assert _arrival(capsys)["posted"] == "no"


def test_giving_up_warns_and_says_the_backstop_will_retry(capsys):
    """
    The [nbm-not-yet] case. It is a real warning -- the package does not
    exist yet -- but it is recoverable, and an alarm that overstates
    itself gets ignored.
    """
    clock = FakeClock(DISPATCHED)
    poller.await_cycle(CYCLE_03Z, "ifr", "railway-cron", clock.now, interval_seconds=300,
                       probe=posted(CYCLE_21Z), clock=clock, sleep=clock.sleep)
    err = capsys.readouterr().err
    assert "nbm-not-yet" in err
    assert "+3:37" in err and "backstop" in err
    assert "does NOT fall back" in err


def test_the_backstop_giving_up_says_nothing_else_is_coming(capsys):
    """The one case where there is no next attempt, and the log has to
    stop promising one."""
    clock = FakeClock(BACKSTOP)
    poller.await_cycle(CYCLE_03Z, "ifr", "schedule-backstop", clock.now,
                       interval_seconds=300, probe=posted(CYCLE_21Z),
                       clock=clock, sleep=clock.sleep)
    err = capsys.readouterr().err
    assert "nothing else is scheduled" in err.lower()


def test_the_window_is_measured_from_job_start_so_the_backstop_gets_its_own():
    """
    Job start IS dispatch time now, so the window says how long we wait
    after ASKING. The backstop asks nearly three hours later and gets the
    same 60 minutes from then.
    """
    for start in (DISPATCHED, BACKSTOP):
        clock = FakeClock(start)
        poller.await_cycle(CYCLE_03Z, "ifr", "railway-cron", start, interval_seconds=300,
                           probe=posted(CYCLE_21Z), clock=clock, sleep=clock.sleep)
        assert clock.now == start + timedelta(minutes=POLL_WINDOW_MINUTES)


def test_the_first_probe_always_happens_before_the_deadline_is_consulted():
    """
    The window runs from job start, so a job cannot normally begin after
    its own deadline -- but a run resumed from the Actions tab can, and
    never looking would turn that into a guaranteed miss.
    """
    clock = FakeClock(DISPATCHED)
    clock.now = DISPATCHED + timedelta(minutes=POLL_WINDOW_MINUTES + 10)
    assert poller.await_cycle(CYCLE_03Z, "ifr", "manual", DISPATCHED,
                              probe=posted(CYCLE_03Z), clock=clock, sleep=clock.sleep)
    assert clock.slept == []


def test_it_polls_only_for_its_own_target_and_ignores_an_older_cycle():
    """
    An older cycle being available is not a reason to proceed; it is the
    package that is already live.
    """
    clock = FakeClock(DISPATCHED)
    assert not poller.await_cycle(CYCLE_03Z, "ifr", "railway-cron", clock.now,
                                  interval_seconds=300,
                                  probe=posted(CYCLE_21Z, datetime(2026, 9, 3, 15, 0)),
                                  clock=clock, sleep=clock.sleep)


def test_both_mirrors_are_tried_before_a_cycle_is_called_missing():
    """
    nomads and AWS are separate mirrors that lag each other. The fetch
    path already falls through them in this order, so finding it on
    either is a truthful "this run can proceed".
    """
    seen = []

    def only_aws(url):
        seen.append(url)
        return "amazonaws.com" in url

    assert poller.cycle_is_posted(CYCLE_03Z, probe=only_aws) is True
    assert len(seen) == 2 and "nomads" in seen[0]


def test_the_probe_asks_for_the_smallest_forecast_hour_any_hazard_needs():
    """
    F00 of the package maps to NBM hour 6. If that is not posted, no
    longer lead time is either, so it is the cheapest sufficient probe.
    """
    urls = []
    poller.cycle_is_posted(CYCLE_03Z, probe=lambda url: urls.append(url) or False)
    assert all(url.endswith(".f006.co.grib2.idx") for url in urls), urls


# --- exit codes the workflow branches on ----------------------------------

def _preflight_file(tmp_path, trigger="railway-cron"):
    path = tmp_path / "nbm_preflight.json"
    path.write_text(json.dumps({
        "hazard": "ifr",
        "trigger": trigger,
        "target_nbm_cycle": CYCLE_03Z.isoformat() + "Z",
        "nbm_source_cycle": CYCLE_03Z.isoformat() + "Z",
        "model_cycle": "2026-09-04T09:00:00Z",
    }))
    return str(path)


def test_main_exits_zero_when_the_cycle_is_posted(tmp_path, monkeypatch):
    monkeypatch.setattr(poller, "_probe", lambda url, timeout=30: True)
    assert poller.main(["--preflight", _preflight_file(tmp_path),
                        "--now", DISPATCHED.isoformat() + "Z"]) == 0


def test_main_exits_ten_when_it_never_posts(tmp_path, monkeypatch):
    """
    Ten, not one: the workflow turns it into a green job that built
    nothing. A cycle NBM has not published is not a build failure.
    """
    monkeypatch.setattr(poller, "_probe", lambda url, timeout=30: False)
    monkeypatch.setattr(poller.time, "sleep", lambda seconds: None)
    # An already-expired window, so the loop probes once and stops.
    started = DISPATCHED - timedelta(minutes=POLL_WINDOW_MINUTES + 1)
    assert poller.main(["--preflight", _preflight_file(tmp_path),
                        "--now", started.isoformat() + "Z"]) == poller.NOT_POSTED


def test_main_takes_the_target_from_the_preflight_rather_than_the_clock(tmp_path, monkeypatch):
    """
    Up to an hour passes between resolving the cycle and finishing the
    poll. Re-deriving the target from `now` would let a long poll quietly
    change which cycle the run is chasing, away from the one the publish
    guard approved.
    """
    probed = []
    monkeypatch.setattr(poller, "_probe", lambda url, timeout=30: probed.append(url) or True)
    poller.main(["--preflight", _preflight_file(tmp_path),
                 "--now", "2026-09-04T08:59:00Z"])   # nearly the NEXT synoptic hour
    assert all(f"blend.{CYCLE_03Z:%Y%m%d}/{CYCLE_03Z:%H}/" in u for u in probed), probed


def test_an_unreadable_preflight_is_an_error_not_a_silent_skip(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("not json")
    assert poller.main(["--preflight", str(broken)]) == 1
    assert "target cycle" in capsys.readouterr().err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
