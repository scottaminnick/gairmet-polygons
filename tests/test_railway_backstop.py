"""
tests/test_railway_backstop.py
---------------------------------
The +2:30 Railway backstop: a second Railway cron service running the same
dispatcher with --trigger railway-backstop. It is the FIRST backstop; the
GitHub `schedule:` at +3:37 stays behind it as the last resort.

Why it exists, measured over 2026-10-28..11-11: GitHub's `schedule:`
backstop fired 2-356 minutes late (median 83) and skipped about 22 of 57
slots, and 16 of the runs that did fire landed after the NEXT synoptic hour
and so backed up the wrong cycle. A Railway cron fires on time.

What is pinned here:
  - the timing: after the primary poll closes (+2:20), before the viewer's
    staleness cutoff (+3:05), before the GitHub backstop (+3:37), and
    inside its own six-hour slot;
  - the config file Railway reads, and that it is separate from the
    primary's (one cron service, one schedule);
  - the trigger: classified, timed against +2:30, sent by the dispatcher's
    --trigger flag, accepted by the pre-flight scripts;
  - the concurrency groups: per hazard and shared by every trigger, so no
    backstop can race the run it backs up.
"""

import importlib.util
import json
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
    POLL_WINDOW_MINUTES,
    PUBLISH_WINDOW_CLOSE_MINUTES,
    RAILWAY_BACKSTOP_OFFSET_MINUTES,
    TRIGGERS,
    classify_trigger,
    dispatch_cron_entry,
    log_nbm_arrival,
    nominal_start_offset_minutes,
    package_for,
    scheduled_start,
    target_nbm_cycle,
    warn_nbm_not_posted,
)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = _load(REPO_ROOT / "scripts" / "dispatch_workflows.py", "dispatch_workflows")
resolver = _load(REPO_ROOT / ".github" / "scripts" / "resolve_target_cycle.py", "resolve_target_cycle")
should_publish = _load(REPO_ROOT / ".github" / "scripts" / "should_publish_cycle.py", "should_publish_cycle")

BACKSTOP_CONFIG = REPO_ROOT / "railway.backstop.json"
PRIMARY_CONFIG = REPO_ROOT / "railway.dispatch.json"


# --- timing -----------------------------------------------------------------

def test_the_railway_backstop_is_at_plus_2_30():
    assert RAILWAY_BACKSTOP_OFFSET_MINUTES == 150
    assert dispatch_cron_entry("railway-backstop") == "30 5,11,17,23 * * *"


def test_it_fires_after_the_primary_poll_and_before_the_viewer_calls_the_package_late():
    """
    After +2:20, when the primary's poll window has closed, so it never
    backs up a run that could still be waiting for NBM; before +3:05, the
    viewer's staleness cutoff, so a package it saves can still land on time.
    """
    primary_poll_closes = DISPATCH_OFFSET_MINUTES + POLL_WINDOW_MINUTES
    assert primary_poll_closes == 140
    assert primary_poll_closes < RAILWAY_BACKSTOP_OFFSET_MINUTES < PUBLISH_WINDOW_CLOSE_MINUTES == 185


def test_it_comes_before_the_github_backstop_and_inside_its_own_slot():
    assert RAILWAY_BACKSTOP_OFFSET_MINUTES < BACKSTOP_OFFSET_MINUTES
    # target_nbm_cycle() takes the most recent synoptic hour: unambiguous
    # only while the backstop's own poll window ends inside its slot.
    assert RAILWAY_BACKSTOP_OFFSET_MINUTES + POLL_WINDOW_MINUTES < 6 * 60


@pytest.mark.parametrize("hour", GAIRMET_CYCLE_HOURS)
def test_every_cron_firing_targets_the_cycle_it_backs_up(hour):
    """Round trip: each firing of the generated line reads back as +2:30 after its own cycle."""
    minute, hours = dispatch_cron_entry("railway-backstop").split()[:2]
    fire_hour = (hour + RAILWAY_BACKSTOP_OFFSET_MINUTES // 60) % 24
    assert str(fire_hour) in hours.split(",")
    when = datetime(2026, 11, 11, fire_hour, int(minute))
    assert target_nbm_cycle(when).hour == hour
    assert scheduled_start(when, "railway-backstop") == when


def test_dispatch_cron_entry_refuses_a_non_railway_trigger():
    with pytest.raises(ValueError):
        dispatch_cron_entry("schedule-backstop")


# --- the Railway config -------------------------------------------------------

def test_the_backstop_config_matches_the_schedule():
    config = json.loads(BACKSTOP_CONFIG.read_text())
    assert config["deploy"]["cronSchedule"] == dispatch_cron_entry("railway-backstop")
    assert config["deploy"]["restartPolicyType"] == "NEVER", "a restarting cron service re-POSTs in a loop"


def test_the_backstop_config_runs_the_dispatcher_as_the_backstop():
    command = json.loads(BACKSTOP_CONFIG.read_text())["deploy"]["startCommand"]
    assert "scripts/dispatch_workflows.py" in command
    assert "--trigger railway-backstop" in command


def test_the_primary_config_is_unchanged_and_separate():
    """Two services, two files: one Railway cron service has one schedule."""
    primary = json.loads(PRIMARY_CONFIG.read_text())["deploy"]
    backstop = json.loads(BACKSTOP_CONFIG.read_text())["deploy"]
    assert primary["cronSchedule"] == dispatch_cron_entry() == "20 4,10,16,22 * * *"
    assert "--trigger" not in primary["startCommand"], "the primary sends the default, railway-cron"
    assert primary["cronSchedule"] != backstop["cronSchedule"]


# --- the trigger ------------------------------------------------------------------

def test_the_trigger_is_recognised_and_timed_against_plus_2_30():
    assert "railway-backstop" in TRIGGERS
    assert classify_trigger("workflow_dispatch", "railway-backstop") == "railway-backstop"
    assert classify_trigger("workflow_dispatch", "  Railway-Backstop ") == "railway-backstop"
    # A schedule event carries no inputs; claiming one is a contradiction.
    assert classify_trigger("schedule", "railway-backstop") == "schedule-backstop"
    assert nominal_start_offset_minutes("railway-backstop") == RAILWAY_BACKSTOP_OFFSET_MINUTES


def test_an_on_time_backstop_logs_a_near_zero_start_delay(capsys):
    target = datetime(2026, 11, 11, 3, 0)
    log_nbm_arrival(target, "ifr", "railway-backstop", datetime(2026, 11, 11, 5, 31),
                    now=datetime(2026, 11, 11, 5, 32))
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("NBM-ARRIVAL ")][-1]
    fields = dict(pair.split("=", 1) for pair in line.split()[1:])
    assert fields["trigger"] == "railway-backstop"
    assert fields["start_delay_min"] == "1", "measured against +2:30, not the primary's +1:20"


@pytest.mark.parametrize("hazard", sorted(HAZARD_WORKFLOWS))
def test_the_preflight_resolves_the_cycle_it_backs_up(hazard):
    cycle = datetime(2026, 11, 10, 21, 0)
    now = cycle + timedelta(minutes=RAILWAY_BACKSTOP_OFFSET_MINUTES)
    result = resolver.resolve(hazard, "workflow_dispatch", "railway-backstop", now)
    assert result["trigger"] == "railway-backstop"
    assert result["nbm_source_cycle"] == "2026-11-10T21:00:00Z"
    assert result["model_cycle"] == package_for(cycle).isoformat() + "Z"
    assert result["start_delay_minutes"] == 0


def test_the_publish_guard_accepts_the_trigger(tmp_path):
    """
    The already-published guard is what makes the backstop free on a
    normal day; it must accept the new trigger rather than error on it.
    """
    manifest = {"model_cycle": "2026-11-11T03:00:00Z", "nbm_source_cycle": "2026-11-10T21:00:00Z"}
    new = tmp_path / "new.json"
    existing = tmp_path / "existing.json"
    new.write_text(json.dumps(manifest))
    existing.write_text(json.dumps(manifest))
    code = should_publish.main(["--new", str(new), "--existing", str(existing), "--hazard", "ifr",
                                "--trigger", "railway-backstop"])
    assert code == should_publish.main(["--new", str(new), "--existing", str(existing), "--hazard", "ifr"])


def test_a_failed_primary_poll_points_at_both_backstops(capsys):
    warn_nbm_not_posted(datetime(2026, 11, 11, 3), "ifr", "railway-cron")
    err = capsys.readouterr().err
    assert "+2:30 Railway backstop" in err and "+3:37 scheduled backstop" in err

    warn_nbm_not_posted(datetime(2026, 11, 11, 3), "ifr", "railway-backstop")
    err = capsys.readouterr().err
    assert "+2:30" not in err and "+3:37 scheduled backstop" in err


# --- the dispatcher ---------------------------------------------------------------

class _Ok:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_dispatcher_sends_the_trigger_it_is_given(monkeypatch):
    sent = []

    def opener(request, timeout=None):
        sent.append(json.loads(request.data))
        return _Ok()

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    real = dispatch.dispatch_one
    monkeypatch.setattr(dispatch, "dispatch_one",
                        lambda *a, **k: real(*a, opener=opener, **k))
    assert dispatch.main(["--trigger", "railway-backstop"]) == 0
    assert len(sent) == len(HAZARD_WORKFLOWS)
    assert all(body["inputs"]["trigger"] == "railway-backstop" for body in sent)


def test_the_dispatcher_defaults_to_the_primary_trigger(capsys):
    assert dispatch.main(["--dry-run"]) == 0
    assert '"trigger": "railway-cron"' in capsys.readouterr().out


def test_the_dispatcher_refuses_any_other_trigger(capsys):
    """A typo here would dispatch runs that log themselves as 'manual'."""
    with pytest.raises(SystemExit):
        dispatch.main(["--trigger", "railway_backstop", "--dry-run"])


def test_print_schedule_gives_each_services_cron_line(capsys):
    dispatch.main(["--print-schedule", "--trigger", "railway-backstop"])
    assert capsys.readouterr().out.strip() == json.loads(BACKSTOP_CONFIG.read_text())["deploy"]["cronSchedule"]


# --- no backstop can race the run it backs up ----------------------------------------

@pytest.mark.parametrize("hazard, workflow", sorted(HAZARD_WORKFLOWS.items()))
def test_every_hazard_serialises_all_its_runs(hazard, workflow):
    """
    One concurrency group per hazard, the same whatever triggered the run,
    and queued rather than cancelled. A group keyed on the event, the run
    id, or the trigger input would let the +2:30 Railway backstop or a late
    +3:37 schedule run start alongside a Railway run still publishing the
    same cycle; cancel-in-progress would let a backstop kill it.
    """
    spec = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow).read_text())
    concurrency = spec.get("concurrency")
    assert concurrency, f"{workflow} has no concurrency group"
    group = concurrency["group"]
    assert "${{" not in group, f"{workflow}: the group varies per run ({group!r}), so runs could overlap"
    assert concurrency.get("cancel-in-progress") is False


def test_the_hazards_groups_are_distinct():
    """Separate branches, separate groups: MTN OBSC's long runs must not block IFR."""
    groups = [
        yaml.safe_load((REPO_ROOT / ".github" / "workflows" / w).read_text())["concurrency"]["group"]
        for w in HAZARD_WORKFLOWS.values()
    ]
    assert len(set(groups)) == len(groups)
