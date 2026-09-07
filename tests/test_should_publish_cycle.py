"""
Tests for .github/scripts/should_publish_cycle.py -- the guard that keeps
a data branch's model_cycle moving forward only.

Worth testing rather than trusting: it sits in a workflow step that runs
8x/day, its failure mode is silent (the live site quietly serves an older
cycle), and the case it exists for -- someone re-running an old workflow
run -- is rare enough that a bug here could sit unnoticed for months.

PUBLISH/SKIP/ERROR below are the script's exit codes, which the workflow
step branches on: SKIP has to be a clean exit-0 outcome for the job, not
a failure.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PUBLISH, SKIP, ERROR = 0, 10, 1


def _load_guard():
    """Imported by path: .github/scripts/ is not (and shouldn't be) an importable package."""
    path = REPO_ROOT / ".github" / "scripts" / "should_publish_cycle.py"
    spec = importlib.util.spec_from_file_location("should_publish_cycle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _manifest(tmp_path, name, cycle, nbm_source_cycle=None, **extra):
    path = tmp_path / name
    body = {"snapshots": []}
    if cycle is not None:
        body["model_cycle"] = cycle
    if nbm_source_cycle is not None:
        body["nbm_source_cycle"] = nbm_source_cycle
    body.update(extra)
    path.write_text(json.dumps(body))
    return str(path)


def test_publishes_when_the_branch_has_nothing_yet(tmp_path):
    new = _manifest(tmp_path, "new.json", "2026-08-19T09:00:00Z")
    assert guard.main(["--new", new]) == PUBLISH


def test_publishes_a_newer_cycle(tmp_path):
    new = _manifest(tmp_path, "new.json", "2026-08-19T15:00:00Z")
    existing = _manifest(tmp_path, "existing.json", "2026-08-19T09:00:00Z")
    assert guard.main(["--new", new, "--existing", existing]) == PUBLISH


def test_skips_an_older_cycle(tmp_path):
    """The case concurrency: can't catch -- re-running an old run from the Actions tab."""
    new = _manifest(tmp_path, "new.json", "2026-08-19T09:00:00Z")
    existing = _manifest(tmp_path, "existing.json", "2026-08-19T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing]) == SKIP


def test_skips_an_identical_cycle(tmp_path):
    """Republishing the same cycle is pointless churn on the branch, not an update."""
    new = _manifest(tmp_path, "new.json", "2026-08-19T15:00:00Z")
    existing = _manifest(tmp_path, "existing.json", "2026-08-19T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing]) == SKIP


@pytest.mark.parametrize(
    "existing_body",
    [
        None,  # file never created -- branch has no manifest
        "",  # empty: what the workflow leaves when `git show` finds no such file
        "not json at all",
        json.dumps({"snapshots": []}),  # valid JSON, no model_cycle
        json.dumps({"model_cycle": "whenever"}),  # unparseable timestamp
    ],
    ids=["missing", "empty", "garbage", "no-cycle-field", "unparseable-cycle"],
)
def test_unusable_existing_manifest_never_blocks_publishing(tmp_path, existing_body):
    """
    "No usable comparison" must always mean PUBLISH. A guard that can
    wedge the pipeline shut on a malformed or partially-written file
    would be worse than the problem it prevents.
    """
    new = _manifest(tmp_path, "new.json", "2026-08-19T09:00:00Z")
    existing = str(tmp_path / "existing.json")
    if existing_body is not None:
        Path(existing).write_text(existing_body)
    assert guard.main(["--new", new, "--existing", existing]) == PUBLISH


@pytest.mark.parametrize("new_body", [None, "not json", json.dumps({"snapshots": []})],
                         ids=["missing", "garbage", "no-cycle-field"])
def test_unusable_new_manifest_is_an_error(tmp_path, new_body):
    """Opposite direction: if what we're about to publish has no cycle, the generate step produced junk."""
    new = str(tmp_path / "new.json")
    if new_body is not None:
        Path(new).write_text(new_body)
    assert guard.main(["--new", new]) == ERROR


def test_compares_instants_not_strings(tmp_path):
    """
    These two sort one way as text and the other way as time. Plain
    string comparison happens to work for the exact format the pipeline
    emits today, and would break silently if an offset ever appeared.
    """
    assert "2026-08-19T10:00:00-06:00" < "2026-08-19T15:00:00Z"  # ...as text
    new = _manifest(tmp_path, "new.json", "2026-08-19T10:00:00-06:00")  # 16:00Z, genuinely newer
    existing = _manifest(tmp_path, "existing.json", "2026-08-19T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing]) == PUBLISH


# ---------------------------------------------------------------------------
# Telling the two skips apart.
#
# "The branch already holds this cycle" has two very different causes,
# and they used to log identically:
#
#   already-published  the target NBM had arrived, we built the right
#                      cycle, something got there first. Routine.
#   nbm-not-yet        the target NBM had NOT arrived, so the run fell
#                      back and would rebuild the already-live cycle.
#
# The second one got MORE serious with the move to a polling job, not
# less. There used to be four attempts, so falling back on attempt one
# was expected and only the last one meant anything. There is now one job
# per hazard per cycle: if it fell back, it already polled to its +3:30
# deadline and gave up, and nothing else is coming. The single exception
# is workflow_dispatch, which does not poll at all -- a person pressing
# the button before NBM has posted is not a missed cycle.
# ---------------------------------------------------------------------------

SKIP_CASE = dict(cycle="2026-09-04T15:00:00Z")


def _skip_pair(tmp_path, nbm_source_cycle, **extra):
    existing = _manifest(tmp_path, "existing.json", SKIP_CASE["cycle"])
    new = _manifest(tmp_path, "new.json", SKIP_CASE["cycle"], nbm_source_cycle, **extra)
    return new, existing


def test_skip_is_labelled_already_published_when_the_target_nbm_was_used(tmp_path, capsys):
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    status = guard.main(["--new", new, "--existing", existing,
                         "--hazard", "ifr", "--now", "2026-09-04T10:45:00Z"])
    assert status == SKIP
    out = capsys.readouterr()
    assert "already-published" in out.out
    assert "WARNING" not in out.out and not out.err.strip()


def test_a_scheduled_run_that_fell_back_has_missed_the_cycle_and_says_so(tmp_path, capsys):
    """
    The case that changed. A scheduled run only reaches the publish step
    having already polled to its deadline, so falling back is never
    "attempt one, a later one will get it" any more -- there is no later
    one. It has to read as an alarm, not as routine.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    status = guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                         "--polled", "yes", "--now", "2026-09-04T18:30:00Z"])
    assert status == SKIP, "a missed cycle is still a skip, not a build failure"
    out = capsys.readouterr()
    assert "WARNING" in out.err and "nbm-not-yet" in out.err
    assert "MISSED" in out.err
    assert "+3:30" in out.err, "the message should name the deadline it actually hit"
    assert not out.out.strip(), "the warning should not also go to stdout"


def test_a_manual_run_that_fell_back_is_reported_without_the_alarm(tmp_path, capsys):
    """
    workflow_dispatch probes once and proceeds rather than holding a
    runner for three hours. Falling back is then just what the button
    does before NBM has posted; the scheduled job for that cycle is still
    coming, so calling it a missed cycle would be wrong.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    status = guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                         "--polled", "no", "--now", "2026-09-04T15:30:00Z"])
    assert status == SKIP
    out = capsys.readouterr()
    assert "nbm-not-yet" in out.out
    assert "did not poll" in out.out
    assert "WARNING" not in out.out and not out.err.strip()


def test_the_pollers_own_result_file_answers_it_when_no_flag_is_passed(tmp_path, capsys):
    """
    The pre-flight call reads await_nbm_cycle.py's output, which records
    deadline_reached itself. The flag is what the workflow passes; the
    field is the cross-check, so a hand invocation against a poller result
    still classifies correctly.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z", deadline_reached=False)
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                       "--now", "2026-09-04T15:30:00Z"]) == SKIP
    assert "did not poll" in capsys.readouterr().out


def test_an_ordinary_manifest_with_no_poll_information_alarms_rather_than_staying_quiet(
    tmp_path, capsys
):
    """
    The real hazard manifests carry no deadline_reached field, and the
    conservative reading of a fallback is that a cycle is being missed.
    Silence would be the failure mode this classification exists to
    remove.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                       "--now", "2026-09-04T18:30:00Z"]) == SKIP
    assert "WARNING" in capsys.readouterr().err


def test_a_fallback_after_midnight_is_recognised(tmp_path, capsys):
    """
    MTN OBSC's 21Z poll runs to 00:30 the next day. A date bug would read
    it against the wrong synoptic hour and report a fallback as routine.
    """
    existing = _manifest(tmp_path, "existing.json", "2026-09-05T03:00:00Z")
    new = _manifest(tmp_path, "new.json", "2026-09-05T03:00:00Z", "2026-09-04T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "mtn_obsc",
                       "--polled", "yes", "--now", "2026-09-05T00:30:00Z"]) == SKIP
    err = capsys.readouterr().err
    assert "MISSED" in err
    assert "2026-09-04 21Z" in err, "the target should be yesterday's 21Z, not today's 03Z"


def test_a_skip_without_a_hazard_says_unknown_rather_than_guessing(tmp_path, capsys):
    """
    Without --hazard there is no schedule to compare against. Reporting
    "routine" would be a guess, and the wrong one half the time.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T03:00:00Z")
    assert guard.main(["--new", new, "--existing", existing]) == SKIP
    assert "unknown" in capsys.readouterr().out


def test_classification_never_changes_the_publish_decision(tmp_path):
    """
    The labels are for the log. A genuinely newer cycle publishes whatever
    NBM it came from, and an older one is skipped either way.
    """
    existing = _manifest(tmp_path, "existing.json", "2026-09-04T09:00:00Z")
    newer = _manifest(tmp_path, "new.json", "2026-09-04T15:00:00Z", "2026-09-04T03:00:00Z")
    assert guard.main(["--new", newer, "--existing", existing, "--hazard", "ifr",
                       "--polled", "yes", "--now", "2026-09-04T11:45:00Z"]) == PUBLISH


# ---------------------------------------------------------------------------
# The pre-flight call.
#
# The guard now runs TWICE per workflow: once against the poller's result
# file before `pip install`, and again against the real manifest at
# publish time. The first call is what makes a non-publishing run cost
# seconds instead of 15-20 minutes of fetch and generation, so the shape
# the poller writes has to keep satisfying it.
# ---------------------------------------------------------------------------

def _preflight(tmp_path, nbm_source_cycle, deadline_reached=False):
    """The file .github/scripts/await_nbm_cycle.py writes, as it writes it."""
    from datetime import datetime, timedelta

    nbm = datetime.fromisoformat(nbm_source_cycle.replace("Z", "+00:00"))
    path = tmp_path / "nbm_preflight.json"
    path.write_text(json.dumps({
        "target_nbm_cycle": "2026-09-04T09:00:00Z",
        "nbm_source_cycle": nbm_source_cycle,
        "model_cycle": (nbm + timedelta(hours=6)).isoformat().replace("+00:00", "") + "Z",
        "found": not deadline_reached,
        "deadline_reached": deadline_reached,
    }))
    return str(path)


def test_the_preflight_file_is_a_manifest_as_far_as_the_guard_is_concerned(tmp_path):
    """
    No special case in the guard: the poller writes model_cycle and
    nbm_source_cycle, which is all the comparison and the classification
    read. That is why the pre-flight needed no new code here.
    """
    new = _preflight(tmp_path, "2026-09-04T09:00:00Z")
    existing = _manifest(tmp_path, "existing.json", "2026-09-04T09:00:00Z")
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                       "--now", "2026-09-04T10:45:00Z"]) == PUBLISH


def test_the_preflight_skips_a_cycle_that_is_already_on_the_branch(tmp_path, capsys):
    """
    The single highest-value case: the run ends here, green, in seconds,
    having installed nothing and fetched no NBM data.
    """
    new = _preflight(tmp_path, "2026-09-04T09:00:00Z")
    existing = _manifest(tmp_path, "existing.json", "2026-09-04T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                       "--now", "2026-09-04T10:45:00Z"]) == SKIP
    assert "already-published" in capsys.readouterr().out


def test_the_preflight_publishes_the_first_time_a_branch_is_seen(tmp_path):
    """An absent branch must never be able to wedge the pipeline shut."""
    new = _preflight(tmp_path, "2026-09-04T09:00:00Z")
    assert guard.main(["--new", new, "--hazard", "ifr"]) == PUBLISH


def test_a_preflight_that_never_found_its_cycle_is_an_error_not_a_silent_skip(tmp_path, capsys):
    """
    nbm_source_cycle=None means the poller could not reach ANY aligned
    cycle -- a NOAA outage, not a late run. await_nbm_cycle.py exits 1 on
    that before the guard is reached, but if it ever were, an unusable
    manifest is an error rather than something to publish.
    """
    path = tmp_path / "nbm_preflight.json"
    path.write_text(json.dumps({"model_cycle": None, "nbm_source_cycle": None}))
    assert guard.main(["--new", str(path), "--hazard", "ifr"]) == ERROR
