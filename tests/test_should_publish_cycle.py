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


def _manifest(tmp_path, name, cycle):
    path = tmp_path / name
    body = {"snapshots": []}
    if cycle is not None:
        body["model_cycle"] = cycle
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
