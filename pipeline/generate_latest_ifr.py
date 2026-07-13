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
  2. Generate real IFR hazard polygons for each of G-AIRMET's real
     valid-time offsets from that cycle: 00, 03, 06, 09, and 12 hours.
  3. Write each as its own file (output/ifr_f00.geojson, ifr_f03.geojson,
     etc.) plus a small manifest (output/ifr_manifest.json) describing
     what's available, so the web app can offer a forecast-hour
     selector instead of only ever showing one snapshot.

     NBM doesn't provide a true F000 (0-hour/analysis) file -- it's a
     statistically post-processed blend, not a raw model analysis, so
     hour 0 doesn't quite apply the way it does for a raw NWP model.
     When F000 isn't available we fall back to F001 as the closest
     available stand-in, and record that substitution honestly in the
     manifest rather than silently mislabeling it.

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
FORECAST_HOURS = [0, 3, 6, 9, 12]

MAX_CYCLES_TO_TRY = 8  # how many recent G-AIRMET-aligned cycles to try before giving up
PROBE_FORECAST_HOUR = 3  # used only to check whether a CYCLE exists yet; F003 is always requested anyway

THRESHOLD_PCT = float(os.environ.get("IFR_THRESHOLD_PCT", "50.0"))
NEIGHBORHOOD_RADIUS_NM = float(os.environ.get("IFR_NEIGHBORHOOD_RADIUS_NM", "50.0"))
MIN_AREA_SQ_MI = float(os.environ.get("IFR_MIN_AREA_SQ_MI", "3000.0"))

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def find_latest_gairmet_cycle(probe_fxx: int = PROBE_FORECAST_HOUR) -> datetime:
    """
    Tries the most recent NBM cycles aligned to G-AIRMET's real 03/09/15/21Z
    issuance schedule, newest first, until one actually has data posted.
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
            print(f"Found available G-AIRMET-aligned cycle: {candidate:%Y-%m-%d %H}Z")
            return candidate
        except RuntimeError:
            print(f"  not yet available: {candidate:%Y-%m-%d %H}Z")
            continue
    raise RuntimeError(
        f"No G-AIRMET-aligned cycle (03/09/15/21Z) in the last {MAX_CYCLES_TO_TRY} tries "
        f"has F{probe_fxx:03d} posted yet"
    )


def generate_one_snapshot(cycle_date: datetime, fxx: int):
    """
    Generates one forecast-hour snapshot, falling back from F000 to
    F001 if NBM doesn't have a true 0-hour file. Returns
    (actual_fxx_used, feature_collection, combined_grid, grid_spec) --
    the grid/grid_spec are returned too so main() can cache them for
    the web app's live parameter-adjustment endpoint (see
    pipeline.polygons.save_grid_cache).
    """
    try:
        combined, grid_spec = prepare_ifr_grid(cycle_date, fxx)
        actual_fxx = fxx
    except RuntimeError:
        if fxx == 0:
            print("  F000 not available (expected -- NBM has no true 0-hour file), trying F001 instead")
            combined, grid_spec = prepare_ifr_grid(cycle_date, 1)
            actual_fxx = 1
        else:
            raise

    fc = polygonize_ifr_grid(
        combined, grid_spec, cycle_date, actual_fxx,
        threshold_pct=THRESHOLD_PCT,
        neighborhood_radius_nm=NEIGHBORHOOD_RADIUS_NM,
        min_area_sq_mi=MIN_AREA_SQ_MI,
    )
    return actual_fxx, fc, combined, grid_spec


def main():
    print(
        f"Generating IFR snapshot set: F{FORECAST_HOURS}, threshold={THRESHOLD_PCT}%, "
        f"neighborhood_radius={NEIGHBORHOOD_RADIUS_NM}nm, min_area={MIN_AREA_SQ_MI}sq mi\n"
    )

    try:
        cycle_date = find_latest_gairmet_cycle()
    except Exception:
        print("FAILED to find any available cycle. Full traceback:\n")
        traceback.print_exc()
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "model_cycle": cycle_date.isoformat() + "Z",
        "threshold_pct": THRESHOLD_PCT,
        "neighborhood_radius_nm": NEIGHBORHOOD_RADIUS_NM,
        "min_area_sq_mi": MIN_AREA_SQ_MI,
        "snapshots": [],
    }

    any_succeeded = False
    for requested_fxx in FORECAST_HOURS:
        print(f"\n--- F{requested_fxx:02d} ---")
        try:
            actual_fxx, fc, combined, grid_spec = generate_one_snapshot(cycle_date, requested_fxx)
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

        valid_time = cycle_date + timedelta(hours=actual_fxx)
        manifest["snapshots"].append({
            "requested_forecast_hour": requested_fxx,
            "actual_forecast_hour": actual_fxx,
            "substituted": actual_fxx != requested_fxx,
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
