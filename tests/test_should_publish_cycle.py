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
# "The branch already holds this cycle" has two very different causes:
#
#   already-published  the run's target NBM cycle is one whose package is
#                      already on the branch. Under the dispatch design
#                      this is the ROUTINE outcome for the +3:37
#                      backstop -- the Railway-dispatched run did the
#                      work nearly three hours earlier and the backstop
#                      correctly does nothing.
#   nbm-not-yet        the run would build from an OLDER NBM cycle than
#                      the one it should be chasing. The workflows cannot
#                      produce this any more: they poll for their target
#                      and build nothing if it does not post. So it means
#                      a hand-run pipeline that fell back through
#                      find_latest_gairmet_cycle().
# ---------------------------------------------------------------------------

SKIP_CASE = dict(cycle="2026-09-04T15:00:00Z")


def _skip_pair(tmp_path, nbm_source_cycle, **extra):
    existing = _manifest(tmp_path, "existing.json", SKIP_CASE["cycle"])
    new = _manifest(tmp_path, "new.json", SKIP_CASE["cycle"], nbm_source_cycle, **extra)
    return new, existing


def test_skip_is_labelled_already_published_when_the_target_is_the_live_one(tmp_path, capsys):
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    status = guard.main(["--new", new, "--existing", existing,
                         "--hazard", "ifr", "--now", "2026-09-04T10:45:00Z"])
    assert status == SKIP
    out = capsys.readouterr()
    assert "already-published" in out.out
    assert "WARNING" not in out.out and not out.err.strip()


def test_the_backstop_finding_the_work_done_reads_as_the_backstop_working(tmp_path, capsys):
    """
    The single most common outcome of the whole pipeline now: the Railway
    dispatch published this cycle at ~+1:30 and the +3:37 backstop finds
    nothing to do. It must not read like a problem, or the one log line
    that DOES mean a problem gets lost among them.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    status = guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                         "--trigger", "schedule-backstop", "--now", "2026-09-04T12:37:00Z"])
    assert status == SKIP
    out = capsys.readouterr()
    assert "already-published" in out.out
    assert "backstop" in out.out and "cost seconds" in out.out
    assert not out.err.strip(), "the routine case must not go to stderr"


def test_a_run_building_from_an_older_cycle_is_warned_about(tmp_path, capsys):
    """
    The workflows cannot do this -- they poll for their target and build
    nothing if it does not post -- so it means a hand-run pipeline fell
    back. Nothing publishes either way; the warning is because a rebuild
    of the live package is never what anyone wanted.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T03:00:00Z")
    status = guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                         "--trigger", "manual", "--now", "2026-09-04T10:15:00Z"])
    assert status == SKIP, "a fallback rebuild is still a skip, not a build failure"
    out = capsys.readouterr()
    assert "WARNING" in out.err and "nbm-not-yet" in out.err
    assert "find_latest_gairmet_cycle" in out.err, "it should say where the fallback came from"
    assert not out.out.strip(), "the warning should not also go to stdout"


def test_the_trigger_is_optional_and_its_absence_changes_nothing_material(tmp_path, capsys):
    """
    It only phrases the message. A guard invoked by hand, with no trigger
    to report, must still classify correctly.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                       "--now", "2026-09-04T10:45:00Z"]) == SKIP
    assert "already-published" in capsys.readouterr().out


def test_an_unknown_trigger_is_refused_rather_than_echoed(tmp_path):
    """
    The value reaches the guard from a workflow expression. Only the
    three classifications pipeline.publish_schedule knows about are
    accepted, so a raw user-typed string can never reach the log here.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    with pytest.raises(SystemExit):
        guard.main(["--new", new, "--existing", existing, "--hazard", "ifr",
                    "--trigger", "whatever-i-typed"])


def test_a_fallback_after_midnight_is_recognised(tmp_path, capsys):
    """
    The 21Z backstop runs at 00:37 the next day. A date bug would read it
    against the wrong synoptic hour and report a fallback as routine.
    """
    existing = _manifest(tmp_path, "existing.json", "2026-09-05T03:00:00Z")
    new = _manifest(tmp_path, "new.json", "2026-09-05T03:00:00Z", "2026-09-04T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing, "--hazard", "mtn_obsc",
                       "--trigger", "schedule-backstop", "--now", "2026-09-05T00:37:00Z"]) == SKIP
    err = capsys.readouterr().err
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
                       "--trigger", "railway-cron", "--now", "2026-09-04T11:45:00Z"]) == PUBLISH


# ---------------------------------------------------------------------------
# The pre-flight call.
#
# The guard now runs TWICE per workflow: once against the poller's result
# file before `pip install`, and again against the real manifest at
# publish time. The first call is what makes a non-publishing run cost
# seconds instead of 15-20 minutes of fetch and generation, so the shape
# the poller writes has to keep satisfying it.
# ---------------------------------------------------------------------------

def _preflight(tmp_path, nbm_source_cycle, trigger="railway-cron"):
    """The file .github/scripts/resolve_target_cycle.py writes, as it writes it."""
    from datetime import datetime, timedelta

    nbm = datetime.fromisoformat(nbm_source_cycle.replace("Z", "+00:00"))
    path = tmp_path / "nbm_preflight.json"
    path.write_text(json.dumps({
        "hazard": "ifr",
        "trigger": trigger,
        "target_nbm_cycle": nbm_source_cycle,
        "nbm_source_cycle": nbm_source_cycle,
        "model_cycle": (nbm + timedelta(hours=6)).isoformat().replace("+00:00", "") + "Z",
        "job_start": "2026-09-04T09:45:00Z",
        "start_delay_minutes": 0.0,
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
    The single highest-value case, and the routine one for the backstop:
    the run ends here, green, in seconds, having installed nothing,
    fetched no NBM data, and -- because this check comes BEFORE the poll
    -- waited zero minutes for a cycle nobody was going to use.
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
    An unusable model_cycle means the step that wrote it produced
    something broken. That is an error, not something to publish -- the
    guard must never be the thing that lets a malformed package through.
    """
    path = tmp_path / "nbm_preflight.json"
    path.write_text(json.dumps({"model_cycle": None, "nbm_source_cycle": None}))
    assert guard.main(["--new", str(path), "--hazard", "ifr"]) == ERROR
