"""
tests/test_dispatch_workflows.py
-----------------------------------
The Railway cron dispatcher (scripts/dispatch_workflows.py), which is the
NORMAL trigger for the pipeline now.

WHY IT IS WORTH TESTING RATHER THAN TRUSTING. It is two HTTP POSTs, but
it is the thing that has to work for any package to be produced at all,
and it runs somewhere this repo's CI cannot see. Its failure modes are
quiet ones: a URL that 404s, a payload GitHub rejects with a 422 because
the workflow has no `trigger` input, a non-204 response treated as
success so Railway shows the job green while nothing was dispatched.

NOTHING HERE TOUCHES THE NETWORK. The opener is injected, so the tests
describe what GitHub would answer and assert what the script does about
it.
"""
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.publish_schedule import HAZARD_WORKFLOWS, classify_trigger  # noqa: E402


def _load_dispatcher():
    path = REPO_ROOT / "scripts" / "dispatch_workflows.py"
    spec = importlib.util.spec_from_file_location("dispatch_workflows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dispatch = _load_dispatcher()

REPOSITORY = "scottaminnick/gairmet-polygons"


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def recording_opener(status=204):
    """An opener that captures the Request it was handed and returns `status`."""
    seen = []

    def opener(request, timeout=None):
        seen.append(request)
        return FakeResponse(status)

    return opener, seen


# --- URL construction ------------------------------------------------------

def test_the_url_is_the_workflow_dispatches_endpoint():
    url = dispatch.dispatch_url(REPOSITORY, "generate_ifr.yml")
    assert url == (
        "https://api.github.com/repos/scottaminnick/gairmet-polygons"
        "/actions/workflows/generate_ifr.yml/dispatches"
    )


def test_the_workflow_filenames_come_from_the_schedule_module():
    """
    Retyping them here would let the dispatcher POST to a workflow that
    does not exist -- a 404 per cycle, and no packages, until somebody
    reads the Railway logs.
    """
    assert set(HAZARD_WORKFLOWS.values()) == {"generate_ifr.yml", "generate_mtn_obsc.yml"}
    for workflow_file in HAZARD_WORKFLOWS.values():
        assert (REPO_ROOT / ".github" / "workflows" / workflow_file).exists(), (
            f"{workflow_file} is dispatched but does not exist"
        )


@pytest.mark.parametrize("repository", ["noslash", "too/many/slashes", "/repo", "owner/", ""])
def test_a_malformed_repository_is_refused_rather_than_posted_to(repository):
    """
    It comes from a Railway variable a person typed. A silent malformed
    URL is a 404 every six hours that looks like nothing at all.
    """
    with pytest.raises(ValueError):
        dispatch.dispatch_url(repository, "generate_ifr.yml")


# --- payload construction --------------------------------------------------

def test_the_payload_carries_the_ref_and_the_trigger_input():
    payload = dispatch.dispatch_payload()
    assert payload == {"ref": "main", "inputs": {"trigger": "railway-cron"}}


def test_the_ref_is_where_the_workflow_definition_is_read_from():
    """
    workflow_dispatch always runs the definition on the given ref, so
    this has to be a branch that HAS the workflow -- not one of the
    orphan data branches the run publishes to.
    """
    assert dispatch.DEFAULT_REF == "main"
    assert dispatch.dispatch_payload(ref="some-feature-branch")["ref"] == "some-feature-branch"


def test_the_trigger_value_is_the_one_the_workflows_actually_recognise():
    """
    The single string tying the two halves together. If it drifts, every
    dispatched run classifies itself "manual" and the instrumentation
    silently stops being able to tell the normal path from a button
    press.
    """
    sent = dispatch.dispatch_payload()["inputs"]["trigger"]
    assert classify_trigger("workflow_dispatch", sent) == "railway-cron"


def test_the_request_carries_the_token_and_the_api_version():
    opener, seen = recording_opener()
    dispatch.dispatch_one("ifr", "generate_ifr.yml", "tok_abc", REPOSITORY, "main", opener)
    request = seen[0]

    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer tok_abc"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("X-github-api-version") == dispatch.GITHUB_API_VERSION
    assert request.get_header("User-agent"), "GitHub rejects API requests with no User-Agent"
    assert json.loads(request.data) == {"ref": "main", "inputs": {"trigger": "railway-cron"}}


# --- what counts as success ------------------------------------------------

def test_only_204_counts_as_dispatched():
    """
    GitHub answers a successful dispatch with 204 and no body -- there is
    no run id to check. Anything else means the run did not start, and
    treating it as success would show Railway green while the pipeline
    goes quiet.
    """
    opener, _ = recording_opener(204)
    assert dispatch.dispatch_one("ifr", "generate_ifr.yml", "t", REPOSITORY, "main", opener)

    for status in (200, 201, 202, 302):
        opener, _ = recording_opener(status)
        assert not dispatch.dispatch_one("ifr", "generate_ifr.yml", "t", REPOSITORY, "main", opener)


def test_an_http_error_is_logged_with_githubs_own_reason(capsys):
    """
    A bare 422 is unactionable; GitHub's body says whether it was a bad
    ref, an unknown input, or a workflow with no workflow_dispatch
    trigger.
    """
    import io

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 422, "Unprocessable Entity", {},
            io.BytesIO(b'{"message":"Unexpected inputs provided: [\\"trigger\\"]"}'),
        )

    assert not dispatch.dispatch_one("ifr", "generate_ifr.yml", "t", REPOSITORY, "main", opener)
    out = capsys.readouterr().out
    assert "status=422" in out
    assert "Unexpected inputs" in out


def test_a_network_failure_is_a_failed_dispatch_not_a_crash(capsys):
    def opener(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    assert not dispatch.dispatch_one("ifr", "generate_ifr.yml", "t", REPOSITORY, "main", opener)
    assert "status=no-response" in capsys.readouterr().out


def test_each_dispatch_logs_a_status_and_a_timestamp(capsys):
    """
    Railway's log viewer does not always make the time obvious, and the
    first thing anyone will want to know is whether the cron fired when
    it was supposed to.
    """
    opener, _ = recording_opener()
    dispatch.dispatch_one("ifr", "generate_ifr.yml", "t", REPOSITORY, "main", opener)
    line = capsys.readouterr().out.strip()
    assert line.startswith("DISPATCH ")
    fields = dict(pair.split("=", 1) for pair in line.split()[1:])
    assert fields["hazard"] == "ifr"
    assert fields["workflow"] == "generate_ifr.yml"
    assert fields["status"] == "204"
    assert fields["at"].endswith("Z")


def test_the_token_is_never_logged(capsys):
    opener, _ = recording_opener()
    dispatch.dispatch_one("ifr", "generate_ifr.yml", "ghp_supersecret", REPOSITORY,
                          "main", opener)
    assert "ghp_supersecret" not in capsys.readouterr().out


# --- main() ---------------------------------------------------------------

def test_main_dispatches_every_hazard(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(dispatch, "dispatch_one",
                        lambda h, w, t, r, ref, *a, **k: calls.append(h) or True)
    assert dispatch.main([]) == 0
    assert sorted(calls) == sorted(HAZARD_WORKFLOWS)


def test_main_exits_non_zero_when_any_dispatch_fails(monkeypatch, capsys):
    """
    So Railway marks the job failed and somebody sees it, rather than the
    pipeline silently going quiet.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(dispatch, "dispatch_one",
                        lambda h, w, t, r, ref, *a, **k: h != "mtn_obsc")
    assert dispatch.main([]) == 1
    assert "mtn_obsc" in capsys.readouterr().err


def test_one_hazard_failing_does_not_stop_the_other_being_dispatched(monkeypatch):
    """Half a package beats none; the exit code still reports the failure."""
    attempted = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    def one(hazard, *args, **kwargs):
        attempted.append(hazard)
        return hazard != "ifr"          # IFR is sorted first, and fails

    monkeypatch.setattr(dispatch, "dispatch_one", one)
    assert dispatch.main([]) == 1
    assert sorted(attempted) == sorted(HAZARD_WORKFLOWS)


def test_a_missing_token_fails_loudly_before_any_request(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(dispatch, "dispatch_one",
                        lambda *a, **k: pytest.fail("posted without a token"))
    assert dispatch.main([]) == 1
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err
    assert "Actions: Read and write" in err, "the message should say how to fix it"


def test_the_dry_run_sends_nothing_and_needs_no_token(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(dispatch, "dispatch_one",
                        lambda *a, **k: pytest.fail("--dry-run posted"))
    assert dispatch.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("POST https://api.github.com") == len(HAZARD_WORKFLOWS)
    assert '"trigger": "railway-cron"' in out


def test_print_schedule_matches_the_railway_config(monkeypatch, capsys):
    """What the forecaster types into the Railway UI, printable without
    reading the source."""
    assert dispatch.main(["--print-schedule"]) == 0
    printed = capsys.readouterr().out.strip()
    config = json.loads((REPO_ROOT / "railway.dispatch.json").read_text())
    assert printed == config["deploy"]["cronSchedule"]


def test_the_repository_and_ref_can_be_overridden_by_environment(monkeypatch):
    """A fork or a rename should not need a code change."""
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/fork")
    monkeypatch.setenv("DISPATCH_REF", "staging")
    monkeypatch.setattr(dispatch, "dispatch_one",
                        lambda h, w, t, r, ref, *a, **k: seen.update(repo=r, ref=ref) or True)
    assert dispatch.main([]) == 0
    assert seen == {"repo": "someone/fork", "ref": "staging"}


# --- the stagger ----------------------------------------------------------

def test_the_hazards_are_dispatched_together_by_default():
    """
    Checked rather than assumed, per the finding recorded on
    DEFAULT_STAGGER_SECONDS: the two hazards force-push separate orphan
    branches with disjoint globs, have separate concurrency groups, and
    run on separate runners with separate IPs. And the stagger never
    prevented overlap anyway -- MTN OBSC runs 30+ minutes against IFR's
    15-20, so the old 15-minute gap had them fetching from NOAA
    simultaneously on most cycles.
    """
    assert dispatch.DEFAULT_STAGGER_SECONDS == 0


def test_the_stagger_knob_still_works_if_that_ever_changes(monkeypatch):
    slept = []
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(dispatch.time, "sleep", slept.append)
    monkeypatch.setattr(dispatch, "dispatch_one", lambda *a, **k: True)
    dispatch.main(["--stagger-seconds", "30"])
    assert slept == [30.0], "one sleep BETWEEN the two dispatches, not before the first"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
