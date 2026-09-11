#!/usr/bin/env python3
"""
Wait for one specific NBM cycle to post. Nothing else.

WHERE IT SITS
-------------
Fourth step of each generate workflow, and only reached if the first
three decided the run has something to publish:

    resolve_target_cycle.py   which cycle, which package   (no network)
    read the data branch      what is already published
    should_publish_cycle.py   would this publish?  no -> done, seconds
    THIS SCRIPT               wait for the cycle to post
    pip install / generate / publish

Polling after the publish check rather than before it is deliberate: an
already-published cycle must not cost an hour of waiting for data nobody
is going to use.

HOW IT WAITS
------------
HTTP HEAD on the `.idx` URL every POLL_INTERVAL_MINUTES, for
POLL_WINDOW_MINUTES from job start. The index's EXISTENCE is the signal;
its contents are irrelevant here, so nothing is downloaded -- not the
index, and certainly not the GRIB2.

The window is measured from JOB START, not from the synoptic hour,
because job start is now dispatch time: `workflow_dispatch` runs start
promptly, which is the entire reason the trigger moved to Railway. The
`schedule:` backstop gets the same 60 minutes from its own later start.

IT DOES NOT FALL BACK. If the cycle has not posted when the window
closes, this exits 10 and the job stops. The old behaviour -- rebuild
from the previous NBM run -- produced the package that was already live,
so the publish guard skipped it after a full fetch and generation had
already been paid for. Building nothing is the honest outcome, and the
+3:37 backstop is what tries again.

STDLIB-ONLY: it runs before `pip install`, so the probe is urllib and the
URL templates come from pipeline/nbm_urls.py.

EXIT CODES
    0   the cycle is posted; the job proceeds
    10  it did not post within the window. A clean outcome, not a
        failure: the caller turns it into a green job that built nothing,
        having logged the [nbm-not-yet] WARNING.
    1   the poll could not be run at all (a malformed target).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from pipeline.nbm_urls import candidate_idx_urls  # noqa: E402
from pipeline.publish_schedule import (  # noqa: E402
    POLL_INTERVAL_MINUTES,
    PROBE_FORECAST_HOUR,
    log_nbm_arrival,
    poll_deadline,
    warn_nbm_not_posted,
)

NOT_POSTED = 10
HTTP_TIMEOUT_SECONDS = 30


def _probe(url: str, timeout: int = HTTP_TIMEOUT_SECONDS) -> bool:
    """
    True if `url` exists. HEAD first; a one-byte ranged GET if the server
    refuses HEAD.

    nomads is Apache and answers HEAD fine, and S3 does too, but a 405 or
    501 from either would otherwise read as "not posted yet" and make the
    job poll out its whole window against data sitting right there. The
    fallback costs one byte.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code not in (405, 501):
            return False
    except (urllib.error.URLError, TimeoutError, OSError):
        return False

    ranged = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(ranged, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def cycle_is_posted(cycle: datetime, probe_fxx: int = PROBE_FORECAST_HOUR, probe=None) -> bool:
    """
    Whether `cycle` has its smallest needed forecast hour posted, on
    EITHER source.

    Both are tried because they are separate mirrors that can and do lag
    each other; the fetch path (pipeline/fetch_nbm.py) already falls
    through them in the same order, so finding it on one here is a
    truthful answer to "can this run proceed".

    `probe` defaults to _probe but is resolved at CALL time, not bound as
    a default at import time -- otherwise a test that replaces _probe is
    silently ignored and the suite quietly starts hitting NOAA.
    """
    probe = probe or _probe
    return any(probe(url) for url in candidate_idx_urls(cycle, probe_fxx))


def await_cycle(
    target: datetime,
    hazard: str,
    trigger: str,
    job_start: datetime,
    interval_seconds: int = POLL_INTERVAL_MINUTES * 60,
    probe=None,
    clock=None,
    sleep=time.sleep,
) -> bool:
    """
    Poll until `target` posts or the window closes. True if it posted.

    THE FIRST PROBE ALWAYS HAPPENS, before the deadline is consulted. The
    window is measured from job start so a job cannot normally begin
    after its own deadline, but a manual run resumed from the Actions tab
    can, and never looking would turn that into a guaranteed miss.
    """
    probe = probe or _probe
    clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    deadline = poll_deadline(job_start)
    print(
        f"Waiting for NBM {target:%Y-%m-%d %H}Z F{PROBE_FORECAST_HOUR:03d} "
        f"(hazard={hazard}, trigger={trigger}). Job started {job_start:%H:%M}Z; "
        f"giving up at {deadline:%H:%M}Z; probing every {interval_seconds // 60} min."
    )

    polled_at_least_once = False
    while True:
        if cycle_is_posted(target, probe=probe):
            now = clock()
            print(f"NBM {target:%Y-%m-%d %H}Z is posted.")
            log_nbm_arrival(target, hazard, trigger, job_start, now=now,
                            posted=True, polled=polled_at_least_once)
            return True

        now = clock()
        if now >= deadline:
            print(f"Poll window closed at {deadline:%H:%M}Z.")
            log_nbm_arrival(target, hazard, trigger, job_start, now=now, posted=False)
            warn_nbm_not_posted(target, hazard, trigger)
            return False

        remaining = (deadline - now).total_seconds()
        print(f"  not yet ({now:%H:%M}Z); {remaining / 60:.0f} min left in the window.")
        polled_at_least_once = True
        sleep(min(interval_seconds, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight",
        required=True,
        help="the JSON resolve_target_cycle.py wrote -- the target cycle and the "
             "classified trigger come from there, so the poll cannot chase a different "
             "cycle from the one the publish guard approved",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=POLL_INTERVAL_MINUTES * 60,
        help="seconds between probes (testing; the real value comes from publish_schedule)",
    )
    parser.add_argument("--now", help="ISO timestamp to treat as job start (testing)")
    args = parser.parse_args(argv)

    try:
        preflight = json.loads(Path(args.preflight).read_text())
        target = datetime.fromisoformat(
            preflight["target_nbm_cycle"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        print(f"ERROR: could not read a target cycle from {args.preflight}: {exc}",
              file=sys.stderr)
        return 1

    job_start = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).replace(tzinfo=None)
        if args.now
        else datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    )

    posted = await_cycle(
        target,
        preflight.get("hazard", "-"),
        preflight.get("trigger", "manual"),
        job_start,
        interval_seconds=args.interval_seconds,
    )
    return 0 if posted else NOT_POSTED


if __name__ == "__main__":
    sys.exit(main())
