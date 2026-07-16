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

from datetime import datetime, timedelta, timezone

from pipeline.fetch_nbm import fetch_idx

# Real G-AIRMET issuance hours (UTC) and real G-AIRMET valid-time
# offsets -- see NWSI 10-811 section 7.2 ("G-AIRMETs will be issued on
# a scheduled basis every six (6) hours around 0245, 0845, 1445, and
# 2045 UTC" for the text product; the graphical product's discrete
# valid-time snapshots are 0/3/6/9/12h per section 7).
GAIRMET_CYCLE_HOURS = [3, 9, 15, 21]
FORECAST_HOURS = [0, 3, 6, 9, 12]  # hours INTO the upcoming G-AIRMET cycle -- used for labeling/filenames/UI

# The NBM cycle find_latest_gairmet_cycle() finds is always the PREVIOUS
# G-AIRMET-aligned hour (e.g. 09Z), and G-AIRMET's own 6-hour cadence
# means the cycle we actually want to PRODUCE is one interval ahead of
# that (15Z) -- see find_latest_gairmet_cycle()'s docstring for the full
# reasoning.
NBM_LEAD_TIME_OFFSET_HOURS = 6

MAX_CYCLES_TO_TRY = 8  # how many recent G-AIRMET-aligned cycles to try before giving up
# Probe using the SMALLEST NBM forecast hour any hazard will actually need
# (F00 -> NBM hour 6) -- if that's not posted yet, none of the longer lead
# times any hazard needs would be either.
PROBE_FORECAST_HOUR = FORECAST_HOURS[0] + NBM_LEAD_TIME_OFFSET_HOURS


def find_latest_gairmet_cycle(probe_fxx: int = PROBE_FORECAST_HOUR) -> datetime:
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
            return candidate
        except RuntimeError:
            print(f"  not yet available: {candidate:%Y-%m-%d %H}Z")
            continue
    raise RuntimeError(
        f"No G-AIRMET-aligned NBM cycle (03/09/15/21Z) in the last {MAX_CYCLES_TO_TRY} tries "
        f"has F{probe_fxx:03d} posted yet"
    )
