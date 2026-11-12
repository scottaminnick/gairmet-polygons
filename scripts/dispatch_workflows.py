#!/usr/bin/env python3
"""
scripts/dispatch_workflows.py
--------------------------------
POST a `workflow_dispatch` for every hazard workflow (IFR, MTN OBSC, TANGO --
publish_schedule.HAZARD_WORKFLOWS). Run by a Railway
cron service; this is the NORMAL trigger for the pipeline now.

WHY THIS EXISTS
---------------
GitHub delays `schedule:` runs, and the delay is growing. Measured on
this repo:

    2026-09-07  the 03Z package's four IFR attempts were scheduled at
                22:15/22:45/23:15/23:45Z and started at
                23:54/00:22/01:01/01:18Z -- ~95 minutes late, every one.
    2026-09-10  up to 4.5 hours late.

Scheduled events are the lowest-priority trigger on the platform and are
documented as best-effort. `workflow_dispatch` is not queued that way and
starts promptly, so the trigger moved off GitHub's scheduler onto an
external caller that can be relied on to fire on time.

A 4.5-hour delay does not make the package late; it makes it useless. The
03Z package exists to seed the 0245Z issuance, and it cannot do that
after the issuance has gone out.

----------------------------------------------------------------------
WHAT THE FORECASTER SETS UP IN THE RAILWAY UI
----------------------------------------------------------------------
Two cron services in the same Railway project as the web app, both from
THIS repository, configured by their SERVICE SETTINGS. Railway stops
reading Config as Code (railway.json, and custom paths like
railway.dispatch.json) on 2026-12-01 and has deprecated Nixpacks, so no
repo file configures them. RAILWAY_DISPATCH.md has the full table and the
steps to move the primary off its old config file;
tests/test_railway_settings_doc.py keeps that table in line with this
script and pipeline.publish_schedule.

                    PRIMARY (+1:20)                 BACKSTOP (+2:30)
  Builder           Railpack                        Railpack
  Start Command     python3 scripts/dispatch_workflows.py
                                                    python3 scripts/dispatch_workflows.py --trigger railway-backstop
  Cron Schedule     %(cron)s             %(backstop_cron)s
  Restart Policy    Never                           Never
  Config File       (empty)                         (empty)
  GITHUB_TOKEN      its own fine-grained PAT        its own fine-grained PAT

The start command MUST be set: without one, Railpack falls back to the
repo's Procfile, whose `web` entry is the web app, and the service would
serve HTTP instead of dispatching. Restart Policy MUST be Never: a cron
job that restarts on exit re-POSTs the dispatches in a loop.

Each token: fine-grained, this repository only, the single permission
`Actions: Read and write` (what the dispatches endpoint requires). Give
the two different expiry dates so one lapsing cannot stop both.
Optional, only if the repo is ever renamed or forked:
    GITHUB_REPOSITORY = owner/repo     (default: %(default_repo)s)
    DISPATCH_REF       = branch        (default: %(default_ref)s)

Both cron lines come from pipeline.publish_schedule.dispatch_cron_entry();
`--print-schedule [--trigger railway-backstop]` prints them. After a
run, the Railway log should show one DISPATCH line per hazard with
`status=204`.

Railway's cron granularity is one minute and it does not guarantee the
container starts on the exact second. That is fine: the job it starts
polls for its NBM cycle, so a minute either way changes nothing.

----------------------------------------------------------------------
STDLIB ONLY
----------------------------------------------------------------------
urllib, not requests. This runs in a minimal Railway container whose
whole job is two HTTP POSTs, and the repo's `requirements.txt` exists to
describe the WEB app -- making a cron service install fastapi, numpy,
scipy, shapely and scikit-image to send two POSTs would be absurd, and
would give the thing that must not fail a large surface to fail on.
tests/test_workflow_dependencies.py fails if a third-party import appears
here.

EXIT CODE
    0  every dispatch returned 204
    1  any dispatch did not -- so Railway marks the job failed and the
       forecaster sees it, rather than the pipeline silently going quiet.
       The +2:30 Railway backstop (and, behind it, the +3:37 GitHub one)
       is what actually saves the cycle; this exit code is what tells
       somebody to fix the dispatcher.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# publish_schedule is stdlib-only, deliberately -- see this module's
# docstring. It owns the hazard -> workflow-file mapping and the cron
# line, so neither is retyped here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.publish_schedule import (  # noqa: E402
    HAZARD_WORKFLOWS,
    RAILWAY_TRIGGER_OFFSETS,
    dispatch_cron_entry,
)

DEFAULT_REPOSITORY = "scottaminnick/gairmet-polygons"
DEFAULT_REF = "main"

# The value the workflows see as the `trigger` input, and the value
# pipeline.publish_schedule.classify_trigger() recognises. A run that
# arrives with anything else is classified "manual", so this string is
# what makes a Railway-dispatched run identifiable in the logs. The
# backstop service overrides it with --trigger railway-backstop.
TRIGGER_NAME = "railway-cron"

API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
HTTP_TIMEOUT_SECONDS = 30

# GitHub returns 204 No Content on a successful dispatch. There is no
# body and no run id -- the API is fire-and-forget, which is why the
# workflows have to identify themselves via the `trigger` input rather
# than this script recording what it started.
DISPATCH_SUCCESS_STATUS = 204

# The hazards are NOT staggered. Checked rather than assumed (for IFR and
# MTN OBSC; TANGO joined on the same terms, with its own data-tango branch
# and concurrency group): they
# force-push separate orphan branches (data-ifr, data-mtnobsc) with
# disjoint file globs, their `concurrency:` groups are separate, each job
# runs on its own GitHub-hosted runner with its own IP so there is no
# shared rate-limit bucket at NOAA, and only MTN OBSC reads the terrain
# grid. The decisive point: MTN OBSC runs 30+ minutes against IFR's
# 15-20, so under the old 15-minute stagger the two were fetching from
# NOAA simultaneously on most cycles anyway -- it was protecting nothing.
#
# Kept as a knob at 0 rather than deleted, so that if NOAA ever does
# start objecting the fix is a variable and not a redesign.
DEFAULT_STAGGER_SECONDS = 0


def dispatch_url(repository: str, workflow_file: str) -> str:
    """
    The REST endpoint for one workflow's dispatches.

    `repository` is "owner/repo" and is interpolated as-is: it comes from
    a Railway variable the forecaster sets, not from anything a request
    could influence.
    """
    if repository.count("/") != 1 or not all(repository.split("/")):
        raise ValueError(
            f"GITHUB_REPOSITORY must look like 'owner/repo', got {repository!r}"
        )
    return f"{API_ROOT}/repos/{repository}/actions/workflows/{workflow_file}/dispatches"


def dispatch_payload(ref: str = DEFAULT_REF, trigger: str = TRIGGER_NAME) -> dict:
    """
    The POST body. `ref` is the branch the workflow file is read FROM --
    a workflow_dispatch always runs the definition on that ref, so this
    has to be main even though the run publishes to the data branches.
    """
    return {"ref": ref, "inputs": {"trigger": trigger}}


def _request(url: str, payload: dict, token: str) -> urllib.request.Request:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
    )
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
    request.add_header("Content-Type", "application/json")
    # GitHub rejects API requests without one, and a named agent is what
    # a support ticket would be traced by.
    request.add_header("User-Agent", "gairmet-polygons-dispatcher")
    return request


def _log(hazard: str, workflow_file: str, status, detail: str = "") -> None:
    """
    One flat, greppable line per dispatch, with the timestamp Railway's
    log viewer does not always make obvious.

    Never includes the token or the Authorization header -- everything
    printed here comes from the response or from arguments the forecaster
    set.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"DISPATCH at={stamp} hazard={hazard} workflow={workflow_file} status={status}"
    if detail:
        line += f" detail={detail!r}"
    print(line, flush=True)


def dispatch_one(hazard: str, workflow_file: str, token: str, repository: str,
                 ref: str, opener=urllib.request.urlopen, trigger: str = TRIGGER_NAME) -> bool:
    """
    POST one dispatch. True on 204, False on anything else -- including a
    network failure, which is indistinguishable from a rejection as far
    as "did the run start" is concerned.

    Deliberately does NOT raise: one hazard failing must not stop the
    other from being dispatched. Half a package is better than none, and
    the caller collects both outcomes before deciding the exit code.
    """
    url = dispatch_url(repository, workflow_file)
    request = _request(url, dispatch_payload(ref, trigger), token)
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        # GitHub puts the actual reason here ("Workflow does not have
        # 'workflow_dispatch' trigger", a bad ref, an expired token), and
        # without it a 422 is unactionable.
        body = exc.read(2048).decode("utf-8", "replace").strip()
        _log(hazard, workflow_file, exc.code, body)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log(hazard, workflow_file, "no-response", str(exc))
        return False

    _log(hazard, workflow_file, status)
    return status == DISPATCH_SUCCESS_STATUS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__ % {
        "cron": dispatch_cron_entry(),
        "backstop_cron": dispatch_cron_entry("railway-backstop"),
        "default_repo": DEFAULT_REPOSITORY,
        "default_ref": DEFAULT_REF,
    }, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--print-schedule",
        action="store_true",
        help="print the cron line to set on the Railway service and exit",
    )
    parser.add_argument(
        "--trigger",
        choices=sorted(RAILWAY_TRIGGER_OFFSETS),
        default=TRIGGER_NAME,
        help="the `trigger` input to send: railway-cron for the +1:20 primary "
             "service (default), railway-backstop for the +2:30 backstop service",
    )
    parser.add_argument(
        "--stagger-seconds",
        type=float,
        default=DEFAULT_STAGGER_SECONDS,
        help="sleep between dispatches; 0 because the hazards share nothing "
             "(see DEFAULT_STAGGER_SECONDS)",
    )
    parser.add_argument("--ref", default=os.environ.get("DISPATCH_REF", DEFAULT_REF))
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be POSTed, send nothing, and require no token",
    )
    args = parser.parse_args(argv)

    if args.print_schedule:
        print(dispatch_cron_entry(args.trigger))
        return 0

    if args.dry_run:
        for hazard, workflow_file in sorted(HAZARD_WORKFLOWS.items()):
            print(f"POST {dispatch_url(args.repository, workflow_file)}")
            print(f"     {json.dumps(dispatch_payload(args.ref, args.trigger))}")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print(
            "ERROR: GITHUB_TOKEN is not set. Set it on the Railway service "
            "(Variables -> New Variable) as a fine-grained PAT scoped to this "
            "repository with 'Actions: Read and write'.",
            file=sys.stderr,
        )
        return 1

    results = {}
    for index, (hazard, workflow_file) in enumerate(sorted(HAZARD_WORKFLOWS.items())):
        if index and args.stagger_seconds > 0:
            time.sleep(args.stagger_seconds)
        results[hazard] = dispatch_one(
            hazard, workflow_file, token, args.repository, args.ref, trigger=args.trigger
        )

    failed = sorted(h for h, ok in results.items() if not ok)
    if failed:
        print(
            f"ERROR: {len(failed)} of {len(results)} dispatch(es) failed: "
            f"{', '.join(failed)}. The status line above says why. Railway will mark this "
            f"job failed; the +3:37 GitHub backstop still covers the cycle itself.",
            file=sys.stderr,
        )
        return 1

    print(f"Dispatched {len(results)} workflow(s) successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
