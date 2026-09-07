"""
tests/test_await_nbm_cycle.py
--------------------------------
The poller that replaced the four-attempt retry ladder
(.github/scripts/await_nbm_cycle.py).

WHY IT IS WORTH TESTING RATHER THAN TRUSTING. It runs 8x/day, it decides
whether the rest of the job happens at all, and its two most important
behaviours are both ones a real run would only exercise on a bad day: a
job GitHub starts after its own deadline, and a deadline that passes
without NBM ever posting. Neither is reachable by waiting around for a
production run to demonstrate it.

Nothing here touches the network. The probe function is injected, so the
tests describe what the runner would see -- a cycle already posted, a
cycle that appears on the third look, one that never does -- and assert
what the job does about it.
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
    NBM_LEAD_TIME_OFFSET_HOURS,
    POLL_DEADLINE_OFFSET_MINUTES,
    poll_start_offset_minutes,
)


def _load_poller():
    """Imported by path: .github/scripts/ is not (and shouldn't be) an importable package."""
    path = REPO_ROOT / ".github" / "scripts" / "await_nbm_cycle.py"
    spec = importlib.util.spec_from_file_location("await_nbm_cycle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poller = _load_poller()

CYCLE_03Z = datetime(2026, 9, 4, 3, 0)
CYCLE_21Z = datetime(2026, 9, 3, 21, 0)
# IFR's scheduled start for the 03Z cycle: +0:43.
ON_TIME = CYCLE_03Z + timedelta(minutes=poll_start_offset_minutes("ifr"))
DEADLINE = CYCLE_03Z + timedelta(minutes=POLL_DEADLINE_OFFSET_MINUTES)


class FakeClock:
    """
    A clock that only moves when the code under test sleeps.

    Deliberately not freezegun or a monkeypatched datetime: the thing
    being tested IS the relationship between sleeping and the deadline,
    so the sleep has to be what advances time. A test that let real time
    pass would take three hours.
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
    """A probe that reports exactly `cycles` as available, by URL substring."""
    stamps = {f"blend.{c:%Y%m%d}/{c:%H}/" for c in cycles}
    return lambda url: any(stamp in url for stamp in stamps)


def appears_after(cycle, probes, clock):
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


# --- the happy paths -------------------------------------------------------

def test_a_cycle_already_posted_is_taken_on_the_first_look(capsys):
    """
    The case a 95-minute queue delay produces, and the reason the design
    is robust to that delay: started late, the job finds the data already
    there and proceeds immediately. No worse than the old first attempt.
    """
    clock = FakeClock(ON_TIME + timedelta(minutes=95))
    result = poller.await_cycle("ifr", clock.now, probe=posted(CYCLE_03Z),
                                clock=clock, sleep=clock.sleep)

    assert result["found"] is True
    assert result["nbm_source_cycle"] == CYCLE_03Z.isoformat() + "Z"
    assert result["deadline_reached"] is False
    assert clock.slept == [], "it slept despite the data being there"

    fields = _arrival(capsys)
    assert fields["queue_delay_min"] == "95", "the platform's lateness, on its own"
    assert fields["arrival_measured"] == "no", (
        "the data was already there on the first probe, so its arrival time is an upper "
        "bound -- reporting it as measured is how the two latencies got conflated"
    )


def test_a_cycle_that_lands_mid_poll_is_picked_up_within_one_interval(capsys):
    """
    The gain over the old ladder, whose resolution was 30 minutes: a cycle
    landing just after an attempt waited half an hour for the next one.
    """
    clock = FakeClock(ON_TIME)
    result = poller.await_cycle(
        "ifr", clock.now, interval_seconds=300,
        probe=appears_after(CYCLE_03Z, probes=4, clock=clock),
        clock=clock, sleep=clock.sleep,
    )

    assert result["found"] is True
    assert clock.slept == [300, 300, 300, 300]
    assert result["poll_wait_minutes"] == 20.0
    fields = _arrival(capsys)
    assert fields["arrival_measured"] == "yes", (
        "the poll watched this one appear, so it is a real measurement"
    )
    assert fields["poll_wait_min"] == "20"


def test_the_package_it_reports_is_the_cycle_plus_the_lead_offset():
    """
    model_cycle is what should_publish_cycle.py compares against the data
    branch, so it has to be the G-AIRMET package, not the NBM cycle.
    """
    clock = FakeClock(ON_TIME)
    result = poller.await_cycle("ifr", clock.now, probe=posted(CYCLE_03Z),
                                clock=clock, sleep=clock.sleep)
    assert result["model_cycle"] == (
        CYCLE_03Z + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
    ).isoformat() + "Z"


# --- the deadline ----------------------------------------------------------

def test_the_poll_stops_at_the_deadline_and_falls_back(capsys):
    clock = FakeClock(ON_TIME)
    result = poller.await_cycle("ifr", clock.now, interval_seconds=300,
                                probe=posted(CYCLE_21Z), clock=clock, sleep=clock.sleep)

    assert result["found"] is False
    assert result["deadline_reached"] is True
    assert result["nbm_source_cycle"] == CYCLE_21Z.isoformat() + "Z"
    assert clock.now <= DEADLINE + timedelta(seconds=1), "it kept polling past the deadline"
    assert sum(clock.slept) == (DEADLINE - ON_TIME).total_seconds()


def test_hitting_the_deadline_warns_rather_than_exiting_quietly(capsys):
    """
    The [nbm-not-yet] case, and the whole reason it is a WARNING now: with
    one job per cycle there is no later attempt behind it, so the package
    is being missed rather than delayed.
    """
    clock = FakeClock(ON_TIME)
    poller.await_cycle("ifr", clock.now, interval_seconds=300, probe=posted(CYCLE_21Z),
                       clock=clock, sleep=clock.sleep)
    err = capsys.readouterr().err
    assert "nbm-not-yet" in err and "MISSED" in err
    assert "+3:30" in err


def test_the_deadline_is_anchored_to_the_synoptic_hour_not_to_job_start():
    """
    Otherwise a job GitHub starts 95 minutes late would get 95 extra
    minutes of runway and could drift toward the next cycle's window.
    """
    late_start = ON_TIME + timedelta(minutes=95)
    clock = FakeClock(late_start)
    poller.await_cycle("ifr", late_start, interval_seconds=300, probe=posted(CYCLE_21Z),
                       clock=clock, sleep=clock.sleep)
    assert clock.now <= DEADLINE + timedelta(seconds=1)
    assert sum(clock.slept) == (DEADLINE - late_start).total_seconds()


def test_a_job_started_after_its_own_deadline_still_looks_once():
    """
    NOT hypothetical: a ~95 minute queue delay against a +3:30 deadline
    leaves room for this to become routine. Checking the deadline before
    the first probe would turn a late start into a guaranteed miss --
    exactly the failure this whole design exists to remove.
    """
    late = DEADLINE + timedelta(minutes=20)
    clock = FakeClock(late)
    result = poller.await_cycle("ifr", late, probe=posted(CYCLE_03Z),
                                clock=clock, sleep=clock.sleep)
    assert result["found"] is True, "it gave up without looking"
    assert clock.slept == []


def test_a_job_started_after_the_deadline_does_not_then_sleep():
    late = DEADLINE + timedelta(minutes=20)
    clock = FakeClock(late)
    result = poller.await_cycle("ifr", late, interval_seconds=300, probe=posted(CYCLE_21Z),
                                clock=clock, sleep=clock.sleep)
    assert clock.slept == []
    assert result["deadline_reached"] is True


# --- manual runs -----------------------------------------------------------

def test_a_manual_run_probes_once_and_does_not_hold_a_runner():
    """
    A person pressing the button wants output now. Waiting up to three
    hours for data that may be an hour out is not what they asked for --
    and the guard classifies the resulting fallback accordingly, without
    the missed-cycle alarm.
    """
    clock = FakeClock(CYCLE_03Z + timedelta(minutes=10))
    result = poller.await_cycle("ifr", clock.now, poll=False, probe=posted(CYCLE_21Z),
                                clock=clock, sleep=clock.sleep)
    assert clock.slept == []
    assert result["found"] is False
    assert result["deadline_reached"] is False, (
        "a manual run never reached a deadline; recording that it did would make the guard "
        "report a missed cycle that is not missed"
    )
    assert result["nbm_source_cycle"] == CYCLE_21Z.isoformat() + "Z"


# --- fallback behaviour ----------------------------------------------------

def test_the_fallback_walks_back_through_aligned_cycles_only():
    """
    G-AIRMET's schedule is 03/09/15/21Z. An hourly NBM cycle would produce
    a package on no issuance boundary at all.
    """
    older = datetime(2026, 9, 3, 15, 0)
    clock = FakeClock(ON_TIME)
    result = poller.await_cycle("ifr", clock.now, interval_seconds=300, probe=posted(older),
                                clock=clock, sleep=clock.sleep)
    assert result["nbm_source_cycle"] == older.isoformat() + "Z"


def test_nothing_reachable_at_all_reports_no_cycle_rather_than_guessing():
    """
    A NOAA outage or a dead network, not a late cycle. main() turns this
    into a non-zero exit; inventing a cycle here would put a fabricated
    package label in front of the publish guard.
    """
    clock = FakeClock(ON_TIME)
    result = poller.await_cycle("ifr", clock.now, interval_seconds=300,
                                probe=lambda url: False, clock=clock, sleep=clock.sleep)
    assert result["nbm_source_cycle"] is None
    assert result["model_cycle"] is None


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


# --- the file it writes ----------------------------------------------------

def test_main_writes_a_file_the_publish_guard_can_read(tmp_path, monkeypatch):
    """
    The pre-flight step hands this straight to should_publish_cycle.py as
    --new. If the shape drifts, the guard silently classifies every skip
    as "unknown" instead of telling the two apart.
    """
    monkeypatch.setattr(poller, "_probe", lambda url, timeout=30: True)
    out = tmp_path / "nbm_preflight.json"
    status = poller.main(["--hazard", "ifr", "--out", str(out),
                          "--now", ON_TIME.isoformat() + "Z"])
    assert status == 0

    written = json.loads(out.read_text())
    for field in ("model_cycle", "nbm_source_cycle", "deadline_reached"):
        assert field in written, f"the guard reads {field} and it is not there"
    assert written["model_cycle"].endswith("Z")


def test_main_fails_when_no_cycle_is_reachable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(poller, "_probe", lambda url, timeout=30: False)
    out = tmp_path / "nbm_preflight.json"
    status = poller.main(["--hazard", "ifr", "--out", str(out), "--no-poll",
                          "--now", ON_TIME.isoformat() + "Z"])
    assert status == 1
    assert "outage" in capsys.readouterr().err


def _arrival(capsys):
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    return dict(pair.split("=", 1) for pair in line.split()[1:])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
