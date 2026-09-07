#!/usr/bin/env python3
"""
Wait for a G-AIRMET-aligned NBM cycle to post, then say which cycle this
run would build -- WITHOUT fetching any of it.

WHY THIS EXISTS
---------------
The generate workflows used to fire four cron attempts per cycle and
decide whether to publish only after fetching NBM and generating
polygons. Three of every four attempts did 15-20 minutes of real work
and threw it away, and GitHub's scheduled-run queue delay (~95 minutes,
consistently, on 2026-09-07) made the whole retry ladder start late
anyway. See pipeline/publish_schedule.py's module docstring for the full
story.

This script replaces the ladder with one job that waits. Two properties
make that affordable:

  1. It is STDLIB-ONLY, so it runs BEFORE `pip install` in the workflow.
     Together with the pre-flight publish guard immediately after it, a
     run with nothing to publish exits in seconds, having installed
     nothing and downloaded nothing.
  2. It probes with HTTP HEAD on the .idx URL. The index's EXISTENCE is
     the signal; its contents are irrelevant here, so nothing is
     downloaded -- not the index, and certainly not the GRIB2.

WHAT IT WRITES
--------------
A small JSON file, shaped like the front of a real hazard manifest:

    {"model_cycle": ..., "nbm_source_cycle": ..., "target_nbm_cycle": ...,
     "found": true, "deadline_reached": false, ...}

model_cycle is the G-AIRMET package this run WOULD produce (the NBM cycle
plus the +6h lead offset), which is exactly the field
should_publish_cycle.py compares against the data branch. So the same
guard, unchanged, answers "would this run publish anything?" before any
work happens -- and its existing [already-published] / [nbm-not-yet]
classification keeps working, just earlier.

EXIT CODES
    0  the file was written; the caller decides what to do with it. This
       includes hitting the deadline without the cycle posting, which is
       a clean no-op, not a failure.
    1  no G-AIRMET-aligned cycle is reachable at all, or the network is
       down hard enough that even the fallback probes fail.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# publish_schedule and nbm_urls are BOTH stdlib-only, deliberately -- see
# this module's docstring. Importing anything that pulls in requests here
# would defeat the point, and tests/test_workflow_dependencies.py fails
# if it ever does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from pipeline.nbm_urls import candidate_idx_urls  # noqa: E402
from pipeline.publish_schedule import (  # noqa: E402
    GAIRMET_CYCLE_HOURS,
    NBM_LEAD_TIME_OFFSET_HOURS,
    POLL_INTERVAL_MINUTES,
    PROBE_FORECAST_HOUR,
    log_nbm_arrival,
    poll_deadline,
    target_nbm_cycle,
    warn_deadline_missed,
)

# How many older G-AIRMET-aligned cycles to fall back through once the
# deadline passes without the target posting. Matches
# pipeline.gairmet_cycle.MAX_CYCLES_TO_TRY: the fallback exists so the
# run can still report WHICH cycle is live rather than dying, and the
# publish guard then skips it.
MAX_CYCLES_TO_TRY = 8

HTTP_TIMEOUT_SECONDS = 30


def _probe(url: str, timeout: int = HTTP_TIMEOUT_SECONDS) -> bool:
    """
    True if `url` exists. HEAD first; a one-byte ranged GET if the server
    refuses HEAD.

    nomads is Apache and answers HEAD fine, and S3 does too, but a 405 or
    501 from either would otherwise read as "not posted yet" and make the
    job poll to its deadline against data that is sitting right there.
    The fallback costs one byte.
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


def aligned_cycles_before(now: datetime, count: int) -> list[datetime]:
    """The `count` most recent G-AIRMET-aligned cycles at or before `now`, newest first."""
    cycles = []
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    while len(cycles) < count + len(GAIRMET_CYCLE_HOURS):
        for hour in sorted(GAIRMET_CYCLE_HOURS, reverse=True):
            candidate = day.replace(hour=hour)
            if candidate <= now:
                cycles.append(candidate)
        day -= timedelta(days=1)
    cycles.sort(reverse=True)
    return cycles[:count]


def await_cycle(
    hazard: str,
    job_start: datetime,
    poll: bool = True,
    interval_seconds: int = POLL_INTERVAL_MINUTES * 60,
    probe=None,
    clock=None,
    sleep=time.sleep,
) -> dict:
    """
    Poll until the target NBM cycle posts, the deadline passes, or -- with
    poll=False -- exactly once.

    THE FIRST PROBE ALWAYS HAPPENS. The deadline is checked after it, not
    before, because a job GitHub starts after its own deadline (a 95
    minute queue delay against a +3:30 deadline leaves plenty of room for
    that to become routine) must still look. Never looking would turn a
    late start into a guaranteed miss, which is precisely the failure
    this design exists to remove.

    poll=False is for workflow_dispatch: a person pressing the button
    wants the run to do something now, not to hold a runner for three
    hours waiting for data that may be an hour out.
    """
    probe = probe or _probe
    clock = clock or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    target = target_nbm_cycle(job_start)
    deadline = poll_deadline(job_start)
    print(
        f"Waiting for NBM {target:%Y-%m-%d %H}Z F{PROBE_FORECAST_HOUR:03d} (hazard={hazard}). "
        f"Job started {job_start:%H:%M}Z; deadline {deadline:%Y-%m-%d %H:%M}Z; "
        f"probing every {interval_seconds // 60} min."
    )

    polled_at_least_once = False
    while True:
        if cycle_is_posted(target, probe=probe):
            now = clock()
            print(f"NBM {target:%Y-%m-%d %H}Z is posted.")
            log_nbm_arrival(target, hazard=hazard, now=now, job_start=job_start,
                            polled=polled_at_least_once)
            return _result(target, target, job_start, now, found=True, deadline_reached=False)

        now = clock()
        if not poll:
            print(f"NBM {target:%Y-%m-%d %H}Z is not posted, and this run is not polling.")
            break
        if now >= deadline:
            print(f"Deadline {deadline:%Y-%m-%d %H:%M}Z reached.")
            break

        remaining = (deadline - now).total_seconds()
        print(f"  not yet ({now:%H:%M}Z); {remaining / 60:.0f} min left before the deadline.")
        polled_at_least_once = True
        sleep(min(interval_seconds, remaining))

    # The target never posted. Fall back to the newest cycle that HAS, so
    # the run can report which package is actually current -- the publish
    # guard turns that into a clean skip. This is the [nbm-not-yet] case.
    now = clock()
    fallback = None
    for candidate in aligned_cycles_before(now, MAX_CYCLES_TO_TRY):
        if candidate == target:
            continue
        if cycle_is_posted(candidate, probe=probe):
            fallback = candidate
            break

    log_nbm_arrival(fallback, hazard=hazard, now=now, job_start=job_start, polled=False)
    if poll:
        warn_deadline_missed(target, fallback, hazard)
    return _result(target, fallback, job_start, now, found=False,
                   deadline_reached=bool(poll))


def _result(target, found_cycle, job_start, now, found, deadline_reached) -> dict:
    result = {
        "target_nbm_cycle": target.isoformat() + "Z",
        "nbm_source_cycle": found_cycle.isoformat() + "Z" if found_cycle else None,
        # The G-AIRMET package this run would build. Named model_cycle so
        # should_publish_cycle.py can read this file as if it were a
        # manifest -- see this module's docstring.
        "model_cycle": (
            (found_cycle + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)).isoformat() + "Z"
            if found_cycle else None
        ),
        "found": found,
        "deadline_reached": deadline_reached,
        "job_start": job_start.isoformat() + "Z",
        "detected_at": now.isoformat() + "Z",
        "poll_wait_minutes": round((now - job_start).total_seconds() / 60, 1),
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hazard", required=True, choices=["ifr", "mtn_obsc"])
    parser.add_argument("--out", required=True, help="where to write the result JSON")
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="probe once and return rather than waiting -- used for workflow_dispatch",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=POLL_INTERVAL_MINUTES * 60,
        help="seconds between probes (testing; the real value comes from publish_schedule)",
    )
    parser.add_argument("--now", help="ISO timestamp to treat as job start (testing)")
    args = parser.parse_args(argv)

    job_start = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).replace(tzinfo=None)
        if args.now
        else datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    )

    result = await_cycle(
        args.hazard,
        job_start,
        poll=not args.no_poll,
        interval_seconds=args.interval_seconds,
    )
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"Wrote {args.out}: {json.dumps(result)}")

    if result["nbm_source_cycle"] is None:
        print(
            "ERROR: no G-AIRMET-aligned NBM cycle at all is reachable -- not the target and "
            "not any of the last few. That is a NOAA outage or a network failure, not a late "
            "cycle, and there is nothing for this run to build from.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
