"""
pipeline/gairmet_cycle.py
----------------------------
Shared, hazard-agnostic G-AIRMET cycle-scheduling logic: which NBM
cycle today's G-AIRMET product should be built from, and the real
G-AIRMET issuance schedule itself (03/09/15/21Z, valid-time offsets
0/3/6/9/12h). Used identically by every hazard's production driver
(pipeline/generate_latest_ifr.py, pipeline/generate_latest_mtn_obsc.py)
-- extracted here once a second hazard needed the exact same logic,
rather than duplicated. Same pattern already used for
pipeline.grid_spec.GridSpec and pipeline.polygons's rasterization
helpers: nothing about cycle-scheduling is IFR-specific, so it doesn't
belong bundled inside generate_latest_ifr.py.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from pipeline.fetch_nbm import fetch_idx
from pipeline.publish_schedule import (
    GAIRMET_CYCLE_HOURS,
    NBM_LEAD_TIME_OFFSET_HOURS,
    attempt_number,
    is_final_attempt,
    target_nbm_cycle,
)

# Real G-AIRMET issuance hours (UTC) and real G-AIRMET valid-time
# offsets -- see NWSI 10-811 section 7.2 ("G-AIRMETs will be issued on
# a scheduled basis every six (6) hours around 0245, 0845, 1445, and
# 2045 UTC" for the text product; the graphical product's discrete
# valid-time snapshots are 0/3/6/9/12h per section 7).
# GAIRMET_CYCLE_HOURS is imported from pipeline.publish_schedule, which
# owns it -- that module is stdlib-only so the publish guard can share
# these numbers without pulling in the fetch stack. Re-exported here
# because every existing caller imports it from this module.
FORECAST_HOURS = [0, 3, 6, 9, 12]  # hours INTO the upcoming G-AIRMET cycle -- used for labeling/filenames/UI

# The NBM cycle find_latest_gairmet_cycle() finds is always the PREVIOUS
# G-AIRMET-aligned hour (e.g. 09Z), and G-AIRMET's own 6-hour cadence
# means the cycle we actually want to PRODUCE is one interval ahead of
# that (15Z) -- see find_latest_gairmet_cycle()'s docstring for the full
# reasoning.
# Imported from pipeline.publish_schedule, which owns it now -- that
# module is stdlib-only, so the value is reachable without pulling in
# fetch_nbm and requests. Unchanged at 6: the forecaster has accepted
# ~4.5 hours of lead in exchange for fresher guidance. Re-exported here
# because every existing caller imports it from this module.

from pipeline.publish_schedule import ATTEMPTS_PER_CYCLE  # noqa: E402  (re-exported for callers)

MAX_CYCLES_TO_TRY = 8  # how many recent G-AIRMET-aligned cycles to try before giving up
# Probe using the SMALLEST NBM forecast hour any hazard will actually need
# (F00 -> NBM hour 6) -- if that's not posted yet, none of the longer lead
# times any hazard needs would be either.
PROBE_FORECAST_HOUR = FORECAST_HOURS[0] + NBM_LEAD_TIME_OFFSET_HOURS


def log_nbm_arrival(found_cycle: datetime, hazard: str | None = None, now: datetime | None = None) -> dict:
    """
    One machine-readable line per run recording what this run WANTED, what
    it GOT, and how long after the cycle it was looking.

    THE POINT. The schedule is currently set from an estimate: NBM 03Z's
    arrival could only be bracketed between +20 minutes and +3h30m from
    existing logs, which is far too wide to schedule against. Every run
    emits one of these, so after a week the crons can be tightened or
    loosened against a real arrival distribution instead.

    A single greppable prefix and flat key=value pairs, because the thing
    that will actually read these is `grep NBM-ARRIVAL` over downloaded
    job logs, not a log pipeline this project does not have.

    on_target=no is NOT a failure. It means the run fell back to an older
    NBM and will rebuild an already-published cycle, which the publish
    guard then skips. It only matters if it is still happening on the
    final attempt -- see should_publish_cycle.py.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    target = target_nbm_cycle(now)
    on_target = found_cycle == target
    attempt = attempt_number(now, hazard) if hazard else None
    record = {
        "hazard": hazard or "-",
        "attempt": f"{attempt}/{ATTEMPTS_PER_CYCLE}" if attempt else "manual",
        "target": f"{target:%Y-%m-%dT%H:%M:%S}Z",
        "found": f"{found_cycle:%Y-%m-%dT%H:%M:%S}Z",
        "on_target": "yes" if on_target else "no",
        # Minutes from the TARGET cycle's nominal time to this run. On an
        # on_target=yes line this is an upper bound on that cycle's arrival
        # latency; across attempts the yes/no boundary brackets it.
        "delta_min": f"{(now - target).total_seconds() / 60:.0f}",
        "behind_min": f"{(target - found_cycle).total_seconds() / 60:.0f}",
        "run": f"{now:%Y-%m-%dT%H:%M:%S}Z",
    }
    print("NBM-ARRIVAL " + " ".join(f"{k}={v}" for k, v in record.items()))
    if not on_target and hazard and is_final_attempt(now, hazard):
        print(
            f"WARNING: final attempt ({attempt}/{ATTEMPTS_PER_CYCLE}) for NBM "
            f"{target:%Y-%m-%d %H}Z and it is still not posted -- falling back to "
            f"{found_cycle:%Y-%m-%d %H}Z. The G-AIRMET cycle this run should have "
            f"seeded is being missed, not merely delayed.",
            file=sys.stderr,
        )
    return record


def find_latest_gairmet_cycle(probe_fxx: int = PROBE_FORECAST_HOUR,
                              hazard: str | None = None) -> datetime:
    """
    Tries the most recent NBM cycles aligned to G-AIRMET's real 03/09/15/21Z
    issuance schedule, newest first, until one actually has data posted.

    NOTE: this returns the NBM cycle date itself, NOT the G-AIRMET cycle
    being produced from it -- callers apply NBM_LEAD_TIME_OFFSET_HOURS to
    turn this into the upcoming G-AIRMET cycle's label (see
    pipeline/generate_latest_ifr.py's or
    pipeline/generate_latest_mtn_obsc.py's main() for the +6h shift).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)

    candidates = []
    day_start = now.replace(hour=0)
    while len(candidates) < MAX_CYCLES_TO_TRY + len(GAIRMET_CYCLE_HOURS):
        for h in sorted(GAIRMET_CYCLE_HOURS, reverse=True):
            candidate = day_start.replace(hour=h)
            if candidate <= now:
                candidates.append(candidate)
        day_start -= timedelta(days=1)
    candidates.sort(reverse=True)

    for candidate in candidates[:MAX_CYCLES_TO_TRY]:
        try:
            fetch_idx(candidate, probe_fxx)
            print(f"Found available G-AIRMET-aligned NBM cycle: {candidate:%Y-%m-%d %H}Z")
            log_nbm_arrival(candidate, hazard=hazard)
            return candidate
        except RuntimeError:
            print(f"  not yet available: {candidate:%Y-%m-%d %H}Z")
            continue
    raise RuntimeError(
        f"No G-AIRMET-aligned NBM cycle (03/09/15/21Z) in the last {MAX_CYCLES_TO_TRY} tries "
        f"has F{probe_fxx:03d} posted yet"
    )
