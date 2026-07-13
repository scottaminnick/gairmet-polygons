"""
pipeline/generate_latest_ifr.py
---------------------------------
THE production driver. Run on a schedule (see
.github/workflows/generate_ifr.yml) to:

  1. Find the most recent NBM cycle aligned to G-AIRMET's REAL issuance
     schedule -- 03Z, 09Z, 15Z, and 21Z -- rather than just any hourly
     NBM cycle. We probe with a cheap .idx fetch (not a full download)
     and step backward through recent aligned cycles until one is
     actually posted, since we don't know the exact posting lag in
     advance.
  2. SHIFT FORWARD one G-AIRMET interval (+6h) to produce the UPCOMING
     cycle's product from data that already exists, rather than the
     cycle that just occurred. This matches how forecasting actually
     works: once 09Z's NBM run is available, a forecaster uses it to
     prepare the 15Z G-AIRMET (valid times 15/18/21/00/03Z), not another
     09Z one -- the 09Z-labeled product was already finished using the
     PREVIOUS (03Z) cycle's data. Concretely: G-AIRMET valid-time offset
     0h (labeled F00 in filenames/UI) is actually NBM forecast hour 6
     from the cycle we found; F03 is NBM hour 9; and so on. This also
     conveniently eliminates the old F000-doesn't-exist-in-NBM problem
     entirely, since the smallest NBM hour we ever request is now 6.
  3. Generate real IFR hazard polygons for each of G-AIRMET's real
     valid-time offsets: 00, 03, 06, 09, and 12 hours into that
     UPCOMING cycle.
  4. Write each as its own file (output/ifr_f00.geojson, ifr_f03.geojson,
     etc.) plus a small manifest (output/ifr_manifest.json) describing
     what's available, so the web app can offer a forecast-hour
     selector instead of only ever showing one snapshot.

All three forecaster-adjustable parameters (threshold, neighborhood
radius, min area) apply uniformly to all 5 snapshots in a given run --
see the workflow's workflow_dispatch inputs.
"""

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.fetch_nbm import fetch_idx
from pipeline.hazards.ifr import polygonize_ifr_grid, prepare_ifr_grid
from pipeline.polygons import save_grid_cache

# Real G-AIRMET issuance hours (UTC) and real G-AIRMET valid-time
# offsets -- see NWSI 10-811 section 7.2 ("G-AIRMETs will be issued on
# a scheduled basis every six (6) hours around 0245, 0845, 1445, and
# 2045 UTC" for the text product; the graphical product's discrete
# valid-time snapshots are 0/3/6/9/12h per section 7, matching what
# we're reproducing here).
GAIRMET_CYCLE_HOURS = [3, 9, 15, 21]
FORECAST_HOURS = [0, 3, 6, 9, 12]  # hours INTO the upcoming G-AIRMET cycle -- used for labeling/filenames/UI

# The NBM cycle find_latest_gairmet_cycle() finds is always the PREVIOUS
# G-AIRMET-aligned hour (e.g. 09Z), and G-AIRMET's own 6-hour cadence
# means the cycle we actually want to PRODUCE is one interval ahead of
# that (15Z) -- see this module's docstring for the full reasoning.
NBM_LEAD_TIME_OFFSET_HOURS = 6

MAX_CYCLES_TO_TRY = 8  # how many recent G-AIRMET-aligned cycles to try before giving up
# Probe using the SMALLEST NBM forecast hour we'll actually need (F00 -> NBM hour 6) --
# if that's not posted yet, none of the longer lead times we need would be either.
PROBE_FORECAST_HOUR = FORECAST_HOURS[0] + NBM_LEAD_TIME_OFFSET_HOURS

THRESHOLD_PCT = float(os.environ.get("IFR_THRESHOLD_PCT", "50.0"))
NEIGHBORHOOD_RADIUS_NM = float(os.environ.get("IFR_NEIGHBORHOOD_RADIUS_NM", "50.0"))
MIN_AREA_SQ_MI = float(os.environ.get("IFR_MIN_AREA_SQ_MI", "3000.0"))

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def find_latest_gairmet_cycle(probe_fxx: int = PROBE_FORECAST_HOUR) -> datetime:
    """
    Tries the most recent NBM cycles aligned to G-AIRMET's real 03/09/15/21Z
    issuance schedule, newest first, until one actually has data posted.

    NOTE: this returns the NBM cycle date itself, NOT the G-AIRMET cycle
    being produced from it -- see main() for the +6h shift that turns
    this into the upcoming G-AIRMET cycle's label.
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


def generate_one_snapshot(nbm_cycle_date: datetime, gairmet_cycle_date: datetime, requested_fxx: int):
    """
    Generates one forecast-hour snapshot. requested_fxx is hours INTO
    the G-AIRMET cycle being produced (0/3/6/9/12, matching filenames
    and the UI) -- the actual NBM forecast hour fetched is
    requested_fxx + NBM_LEAD_TIME_OFFSET_HOURS, from nbm_cycle_date
    (the previous G-AIRMET-aligned NBM cycle, which already exists).

    Returns (feature_collection, combined_grid, grid_spec) -- the grid
    is returned too so main() can cache it for the web app's live
    parameter-adjustment endpoint (see pipeline.polygons.save_grid_cache).
    """
    actual_nbm_fxx = requested_fxx + NBM_LEAD_TIME_OFFSET_HOURS
    combined, grid_spec = prepare_ifr_grid(nbm_cycle_date, actual_nbm_fxx)

    # gairmet_cycle_date + requested_fxx correctly gives the real valid
    # time (e.g. 15Z cycle's F00 = 15Z) -- and equals nbm_cycle_date +
    # actual_nbm_fxx by construction, so this is just the more
    # meaningful of two equal ways to express the same instant.
    fc = polygonize_ifr_grid(
        combined, grid_spec, gairmet_cycle_date, requested_fxx,
        threshold_pct=THRESHOLD_PCT,
        neighborhood_radius_nm=NEIGHBORHOOD_RADIUS_NM,
        min_area_sq_mi=MIN_AREA_SQ_MI,
    )
    return fc, combined, grid_spec


def main():
    print(
        f"Generating IFR snapshot set: F{FORECAST_HOURS}, threshold={THRESHOLD_PCT}%, "
        f"neighborhood_radius={NEIGHBORHOOD_RADIUS_NM}nm, min_area={MIN_AREA_SQ_MI}sq mi\n"
    )

    try:
        nbm_cycle_date = find_latest_gairmet_cycle()
    except Exception:
        print("FAILED to find any available cycle. Full traceback:\n")
        traceback.print_exc()
        sys.exit(1)

    gairmet_cycle_date = nbm_cycle_date + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
    print(f"Producing G-AIRMET cycle: {gairmet_cycle_date:%Y-%m-%d %H}Z "
          f"(from NBM's {nbm_cycle_date:%H}Z run, hours {PROBE_FORECAST_HOUR}-"
          f"{FORECAST_HOURS[-1] + NBM_LEAD_TIME_OFFSET_HOURS})\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "model_cycle": gairmet_cycle_date.isoformat() + "Z",
        "nbm_source_cycle": nbm_cycle_date.isoformat() + "Z",
        "threshold_pct": THRESHOLD_PCT,
        "neighborhood_radius_nm": NEIGHBORHOOD_RADIUS_NM,
        "min_area_sq_mi": MIN_AREA_SQ_MI,
        "snapshots": [],
    }

    any_succeeded = False
    for requested_fxx in FORECAST_HOURS:
        actual_nbm_fxx = requested_fxx + NBM_LEAD_TIME_OFFSET_HOURS
        print(f"\n--- F{requested_fxx:02d} (NBM {nbm_cycle_date:%H}Z F{actual_nbm_fxx:03d}) ---")
        try:
            fc, combined, grid_spec = generate_one_snapshot(nbm_cycle_date, gairmet_cycle_date, requested_fxx)
        except Exception:
            print(f"  FAILED for F{requested_fxx:02d}, skipping this snapshot. Traceback:")
            traceback.print_exc()
            continue

        filename = f"ifr_f{requested_fxx:02d}.geojson"
        with open(OUTPUT_DIR / filename, "w") as f:
            json.dump(fc, f, indent=2)

        # Cache the prepared grid too -- lets the web app re-run just the
        # threshold/merge/area-filter/smoothing steps with different
        # forecaster-chosen parameters, without re-fetching from NBM.
        cache_filename = f"ifr_f{requested_fxx:02d}_grid.npz"
        save_grid_cache(OUTPUT_DIR / cache_filename, combined, grid_spec)

        valid_time = gairmet_cycle_date + timedelta(hours=requested_fxx)
        manifest["snapshots"].append({
            "requested_forecast_hour": requested_fxx,
            "actual_forecast_hour": requested_fxx,
            "substituted": False,
            "valid_time": valid_time.isoformat() + "Z",
            "filename": filename,
            "cache_filename": cache_filename,
            "feature_count": len(fc["features"]),
        })
        print(f"  wrote {len(fc['features'])} polygon(s) to {filename} (valid {valid_time:%Y-%m-%d %HZ})")
        print(f"  cached prepared grid to {cache_filename}")
        any_succeeded = True

    if not any_succeeded:
        print("\nFAILED: every forecast hour failed for this cycle.")
        sys.exit(1)

    with open(OUTPUT_DIR / "ifr_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nSUCCESS: wrote manifest with {len(manifest['snapshots'])} snapshot(s) to ifr_manifest.json")


if __name__ == "__main__":
    main()
