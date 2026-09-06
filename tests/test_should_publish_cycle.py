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


def _manifest(tmp_path, name, cycle, nbm_source_cycle=None):
    path = tmp_path / name
    body = {"snapshots": []}
    if cycle is not None:
        body["model_cycle"] = cycle
    if nbm_source_cycle is not None:
        body["nbm_source_cycle"] = nbm_source_cycle
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
# With retry crons, "the branch already holds this cycle" has two very
# different causes, and they used to log identically:
#
#   already-published  the target NBM had arrived, we built the right
#                      cycle, an earlier attempt got there first. Routine.
#   nbm-not-yet        the target NBM had NOT arrived, so the run fell
#                      back and rebuilt the already-live cycle. Fine on
#                      attempt one; on the last attempt that cycle is
#                      being missed, and a missed cycle that reads like a
#                      routine no-op is a missed cycle nobody notices.
# ---------------------------------------------------------------------------

SKIP_CASE = dict(cycle="2026-09-04T15:00:00Z")


def _skip_pair(tmp_path, nbm_source_cycle):
    existing = _manifest(tmp_path, "existing.json", SKIP_CASE["cycle"])
    new = _manifest(tmp_path, "new.json", SKIP_CASE["cycle"], nbm_source_cycle)
    return new, existing


def test_skip_is_labelled_already_published_when_the_target_nbm_was_used(tmp_path, capsys):
    new, existing = _skip_pair(tmp_path, "2026-09-04T09:00:00Z")
    status = guard.main(["--new", new, "--existing", existing,
                         "--hazard", "ifr", "--now", "2026-09-04T10:45:00Z"])
    assert status == SKIP
    out = capsys.readouterr()
    assert "already-published" in out.out
    assert "WARNING" not in out.out and not out.err.strip()


def test_skip_is_labelled_nbm_not_yet_when_the_run_fell_back(tmp_path, capsys):
    new, existing = _skip_pair(tmp_path, "2026-09-04T03:00:00Z")
    status = guard.main(["--new", new, "--existing", existing,
                         "--hazard", "ifr", "--now", "2026-09-04T10:15:00Z"])
    assert status == SKIP
    out = capsys.readouterr()
    assert "nbm-not-yet" in out.out
    assert "attempt 1/4" in out.out
    assert "WARNING" not in out.out, "attempt one falling back is routine, not a warning"


def test_the_final_attempt_falling_back_warns_on_stderr(tmp_path, capsys):
    """
    The one case that needs to look different from every other skip: no
    further attempt is scheduled, so this cycle is missed rather than
    delayed.
    """
    new, existing = _skip_pair(tmp_path, "2026-09-04T03:00:00Z")
    status = guard.main(["--new", new, "--existing", existing,
                         "--hazard", "ifr", "--now", "2026-09-04T11:45:00Z"])
    assert status == SKIP, "a missed cycle is still a skip, not a build failure"
    out = capsys.readouterr()
    assert "WARNING" in out.err and "nbm-not-yet" in out.err
    assert "MISSED" in out.err
    assert "attempt 4/4" in out.err
    assert not out.out.strip(), "the warning should not also go to stdout"


def test_mtn_obsc_final_attempt_after_midnight_is_recognised(tmp_path, capsys):
    """
    MTN OBSC's fourth attempt at the 21Z cycle runs at 00:00 the next day.
    A date bug would report it as attempt-less and stay quiet.
    """
    existing = _manifest(tmp_path, "existing.json", "2026-09-05T03:00:00Z")
    new = _manifest(tmp_path, "new.json", "2026-09-05T03:00:00Z", "2026-09-04T15:00:00Z")
    assert guard.main(["--new", new, "--existing", existing,
                       "--hazard", "mtn_obsc", "--now", "2026-09-05T00:00:00Z"]) == SKIP
    assert "attempt 4/4" in capsys.readouterr().err


def test_a_skip_without_a_hazard_says_unknown_rather_than_guessing(tmp_path, capsys):
    """
    Without --hazard there is no attempt schedule to compare against.
    Reporting "routine" would be a guess, and the wrong one half the time.
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
    assert guard.main(["--new", newer, "--existing", existing,
                       "--hazard", "ifr", "--now", "2026-09-04T11:45:00Z"]) == PUBLISH
